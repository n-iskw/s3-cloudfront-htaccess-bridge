import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import shlex
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


KVS_PUBLISH_MAX_ATTEMPTS = 5
KVS_PUBLISH_BASE_DELAY_SECONDS = 0.2


SUPPORTED_DIRECTIVES = {
    "authtype",
    "authname",
    "require",
    "redirect",
    "redirectpermanent",
    "redirecttemp",
    "rewriteengine",
    "rewriterule",
    "directoryindex",
}

DIRECTORY_INDEX_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,255}$")

REDIRECT_STATUSES = {301, 302, 307, 308}
IPV4_CIDR_RE = re.compile(r"^(\d{1,3})(?:\.(\d{1,3})){3}(?:/(\d|[12]\d|3[0-2]))?$")


class HtaccessError(ValueError):
    pass


@dataclass
class ParsedLine:
    line_no: int
    directive: str
    args: List[str]
    raw: str


def parse_htaccess(
    text: str,
    *,
    allowed_external_hosts: Optional[Iterable[str]] = None,
    source_key: str = ".htaccess",
) -> Dict[str, Any]:
    allowed_hosts = {h.strip().lower() for h in (allowed_external_hosts or []) if h.strip()}
    parsed_lines = list(_read_lines(text))

    maintenance = {
        "enabled": False,
        "realm": "Maintenance",
        "allowIps": [],
    }
    redirects: List[Dict[str, Any]] = []

    auth_type_basic = False
    require_valid_user = False
    rewrite_engine_on = False
    directory_index: List[str] = []
    directory_index_disabled = False

    for line in parsed_lines:
        name = line.directive
        args = line.args

        if name not in SUPPORTED_DIRECTIVES:
            raise HtaccessError(f"line {line.line_no}: unsupported directive: {line.raw}")

        if name == "authtype":
            _expect_arg_count(line, 1)
            if args[0].lower() != "basic":
                raise HtaccessError(f"line {line.line_no}: only AuthType Basic is supported")
            auth_type_basic = True

        elif name == "authname":
            _expect_arg_count(line, 1)
            maintenance["realm"] = args[0]

        elif name == "require":
            if len(args) == 1 and args[0].lower() == "valid-user":
                require_valid_user = True
            elif len(args) >= 2 and args[0].lower() == "ip":
                maintenance["allowIps"].extend(_parse_require_ip_args(args[1:], line.line_no))
            else:
                raise HtaccessError(f"line {line.line_no}: only Require valid-user and Require ip are supported")

        elif name == "redirect":
            redirects.append(_parse_redirect(line, allowed_hosts))

        elif name == "redirectpermanent":
            redirects.append(_parse_named_redirect(line, 301, allowed_hosts))

        elif name == "redirecttemp":
            redirects.append(_parse_named_redirect(line, 302, allowed_hosts))

        elif name == "rewriteengine":
            _expect_arg_count(line, 1)
            value = args[0].lower()
            if value not in {"on", "off"}:
                raise HtaccessError(f"line {line.line_no}: RewriteEngine must be On or Off")
            rewrite_engine_on = value == "on"

        elif name == "rewriterule":
            if not rewrite_engine_on:
                raise HtaccessError(f"line {line.line_no}: RewriteRule requires RewriteEngine On")
            redirects.append(_parse_rewrite_rule(line, allowed_hosts))

        elif name == "directoryindex":
            directory_index, directory_index_disabled = _apply_directory_index(
                line, directory_index, directory_index_disabled
            )

    maintenance["enabled"] = auth_type_basic and require_valid_user

    config = {
        "schemaVersion": 1,
        "sourceKey": source_key,
        "maintenance": maintenance,
        "redirects": redirects,
        "directoryIndex": [] if directory_index_disabled else directory_index,
    }
    validate_config(config)
    return config


def enrich_config(config: Dict[str, Any], *, basic_auth_value: Optional[str] = None) -> Dict[str, Any]:
    enriched = json.loads(json.dumps(config, separators=(",", ":")))
    if basic_auth_value:
        enriched["maintenance"]["authorization"] = basic_auth_value
        for scope in enriched.get("authScopes", []):
            if scope.get("enabled"):
                scope["authorization"] = basic_auth_value
    return enriched


def build_site_config(
    files: List[Tuple[str, str]],
    *,
    allowed_external_hosts: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    parsed_files = []
    for key, text in files:
        parsed = parse_htaccess(text, allowed_external_hosts=allowed_external_hosts, source_key=key)
        parsed_files.append((key, _base_path_for_htaccess(key), parsed))

    redirects = []
    auth_scopes = []
    directory_index_scopes = []
    root_maintenance = {"enabled": False, "realm": "Maintenance"}

    for key, base_path, parsed in sorted(parsed_files, key=lambda item: (item[1].count("/"), item[1])):
        maintenance = parsed["maintenance"]
        if maintenance["enabled"]:
            scope = {
                "pathPrefix": base_path,
                "enabled": True,
                "realm": maintenance.get("realm", "Maintenance"),
                "allowIps": maintenance.get("allowIps", []),
                "sourceKey": key,
            }
            auth_scopes.append(scope)
            if base_path == "/":
                root_maintenance = {
                    "enabled": True,
                    "realm": maintenance.get("realm", "Maintenance"),
                    "allowIps": maintenance.get("allowIps", []),
                }

        if parsed.get("directoryIndex"):
            directory_index_scopes.append(
                {
                    "pathPrefix": base_path,
                    "names": parsed["directoryIndex"],
                    "sourceKey": key,
                }
            )

        for rule in parsed["redirects"]:
            normalized = dict(rule)
            normalized["sourceKey"] = key
            normalized["basePath"] = base_path
            redirects.append(normalized)

    redirects.sort(key=lambda rule: _rule_specificity(rule), reverse=True)
    auth_scopes.sort(key=lambda scope: len(scope["pathPrefix"]), reverse=True)
    # Most specific (longest) pathPrefix first, so CloudFront Functions can
    # pick the first matching scope for a given URI.
    directory_index_scopes.sort(key=lambda scope: len(scope["pathPrefix"]), reverse=True)

    config = {
        "schemaVersion": 1,
        "maintenance": root_maintenance,
        "authScopes": auth_scopes,
        "redirects": redirects,
        "directoryIndexScopes": directory_index_scopes,
    }
    validate_config(config)
    return config


def validate_config(config: Dict[str, Any]) -> None:
    seen = set()
    for rule in config.get("redirects", []):
        rule_type = rule.get("type")
        status = int(rule.get("status", 0))
        if status not in REDIRECT_STATUSES:
            raise HtaccessError(f"line {rule.get('line')}: unsupported redirect status: {status}")

        if rule_type == "redirect":
            source = rule["from"]
            target = rule["to"]
            key = ("redirect", source)
            if key in seen:
                raise HtaccessError(f"line {rule.get('line')}: duplicate redirect source: {source}")
            seen.add(key)
            if _is_internal_path(target) and _would_loop(source, target):
                raise HtaccessError(
                    f"line {rule.get('line')}: redirect target can loop: {source} -> {target}"
                )

        elif rule_type == "rewrite":
            key = ("rewrite", rule["pattern"])
            if key in seen:
                raise HtaccessError(f"line {rule.get('line')}: duplicate rewrite pattern: {rule['pattern']}")
            seen.add(key)
        else:
            raise HtaccessError(f"line {rule.get('line')}: unknown rule type: {rule_type}")

    _validate_redirect_chains(config.get("redirects", []))


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    s3 = _boto3_client("s3")
    rules_key = os.environ.get("RULES_KEY")
    rules_suffix = os.environ.get("RULES_SUFFIX", ".htaccess")
    history_prefix = os.environ.get("HISTORY_PREFIX", "_control-history").strip("/")
    allowed_hosts = _split_csv(os.environ.get("ALLOWED_EXTERNAL_HOSTS", ""))
    basic_secret_id = os.environ.get("BASIC_AUTH_SECRET_ID")

    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not _is_rules_key(key, rules_key, rules_suffix):
            _log_event("skipped", bucket=bucket, key=key)
            results.append({"bucket": bucket, "key": key, "status": "skipped"})
            continue

        files = _load_all_htaccess_files(s3, bucket, rules_key, rules_suffix)
        source_hash = hashlib.sha256(
            "\n".join(f"{file_key}:{hashlib.sha256(body.encode('utf-8')).hexdigest()}" for file_key, body in files).encode(
                "utf-8"
            )
        ).hexdigest()
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        try:
            config = build_site_config(files, allowed_external_hosts=allowed_hosts)
            basic_auth_value = _load_basic_auth_value(basic_secret_id)
            if _has_enabled_auth(config) and not basic_auth_value:
                raise HtaccessError("maintenance is enabled but BASIC_AUTH_SECRET_ID is not configured")
            config = enrich_config(config, basic_auth_value=basic_auth_value)
            puts = split_config_for_kvs(config)
            payload = _json_dumps(config)

            _publish_to_kvs(puts)
            history_key = f"{history_prefix}/published/{timestamp}-{source_hash[:12]}.json"
            s3.put_object(
                Bucket=bucket,
                Key=history_key,
                Body=payload.encode("utf-8"),
                ContentType="application/json",
            )
            _log_event(
                "published",
                bucket=bucket,
                key=key,
                sourceHash=source_hash,
                historyKey=history_key,
                htaccessFileCount=len(files),
                redirectCount=len(config.get("redirects", [])),
                authScopeCount=len(config.get("authScopes", [])),
                directoryIndexScopeCount=len(config.get("directoryIndexScopes", [])),
                kvsValueSizes={k: len(v.encode("utf-8")) for k, v in puts.items()},
            )
            results.append({"bucket": bucket, "key": key, "status": "published", "historyKey": history_key})

        except Exception as exc:
            error_payload = _json_dumps(
                {
                    "status": "rejected",
                    "sourceKey": key,
                    "sourceHash": source_hash,
                    "error": str(exc),
                }
            )
            rejected_key = f"{history_prefix}/rejected/{timestamp}-{source_hash[:12]}.json"
            s3.put_object(
                Bucket=bucket,
                Key=rejected_key,
                Body=error_payload.encode("utf-8"),
                ContentType="application/json",
            )
            _log_event(
                "rejected",
                bucket=bucket,
                key=key,
                sourceHash=source_hash,
                historyKey=rejected_key,
                htaccessFileCount=len(files),
                error=str(exc),
                errorType=type(exc).__name__,
            )
            results.append({"bucket": bucket, "key": key, "status": "rejected", "historyKey": rejected_key})

    return {"results": results}


def _read_lines(text: str) -> Iterable[ParsedLine]:
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=False, posix=True)
        except ValueError as exc:
            raise HtaccessError(f"line {line_no}: cannot parse line: {exc}") from exc
        if not tokens:
            continue
        yield ParsedLine(line_no=line_no, directive=tokens[0].lower(), args=tokens[1:], raw=stripped)


def _parse_redirect(line: ParsedLine, allowed_hosts: set) -> Dict[str, Any]:
    if len(line.args) == 2:
        status = 302
        source, target = line.args
    elif len(line.args) == 3:
        status = _parse_redirect_status(line.args[0], line.line_no)
        source, target = line.args[1], line.args[2]
    else:
        raise HtaccessError(f"line {line.line_no}: Redirect expects [status] from to")
    return _redirect_rule(line, status, source, target, allowed_hosts)


def _parse_named_redirect(line: ParsedLine, status: int, allowed_hosts: set) -> Dict[str, Any]:
    _expect_arg_count(line, 2)
    return _redirect_rule(line, status, line.args[0], line.args[1], allowed_hosts)


def _redirect_rule(line: ParsedLine, status: int, source: str, target: str, allowed_hosts: set) -> Dict[str, Any]:
    _validate_source_path(source, line.line_no)
    _validate_target(target, allowed_hosts, line.line_no)
    return {
        "type": "redirect",
        "from": source,
        "to": target,
        "status": status,
        "match": "prefix",
        "line": line.line_no,
    }


def _parse_rewrite_rule(line: ParsedLine, allowed_hosts: set) -> Dict[str, Any]:
    if len(line.args) not in {2, 3}:
        raise HtaccessError(f"line {line.line_no}: RewriteRule expects pattern substitution [flags]")
    pattern = line.args[0]
    target = line.args[1]
    flags = _parse_flags(line.args[2] if len(line.args) == 3 else "")
    unsupported_flags = sorted(set(flags) - {"r", "l"})
    if unsupported_flags:
        raise HtaccessError(
            f"line {line.line_no}: unsupported RewriteRule flag(s): {', '.join(unsupported_flags)}"
        )
    status = flags.get("r")
    if not status:
        raise HtaccessError(f"line {line.line_no}: only RewriteRule redirects with [R=...] are supported")
    if "l" not in flags:
        raise HtaccessError(f"line {line.line_no}: RewriteRule must include [L]")
    status_code = _parse_redirect_status(status, line.line_no)
    _validate_rewrite_pattern(pattern, line.line_no)
    _validate_target(target, allowed_hosts, line.line_no)
    return {
        "type": "rewrite",
        "pattern": pattern,
        "to": target,
        "status": status_code,
        "line": line.line_no,
    }


def _parse_flags(raw: str) -> Dict[str, str]:
    if not raw:
        return {}
    if not (raw.startswith("[") and raw.endswith("]")):
        raise HtaccessError(f"invalid RewriteRule flags: {raw}")
    flags = {}
    for item in raw[1:-1].split(","):
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            flags[key.strip().lower()] = value.strip()
        else:
            flags[item.strip().lower()] = "true"
    return flags


def _parse_redirect_status(value: str, line_no: int) -> int:
    aliases = {
        "permanent": 301,
        "temp": 302,
        "temporary": 302,
        "seeother": 303,
    }
    lowered = value.lower()
    if lowered in aliases:
        status = aliases[lowered]
    else:
        try:
            status = int(value)
        except ValueError as exc:
            raise HtaccessError(f"line {line_no}: invalid redirect status: {value}") from exc
    if status not in REDIRECT_STATUSES:
        raise HtaccessError(f"line {line_no}: supported redirect statuses are 301, 302, 307, 308")
    return status


def _validate_source_path(path: str, line_no: int) -> None:
    if not path.startswith("/"):
        raise HtaccessError(f"line {line_no}: redirect source must start with /")
    if "://" in path:
        raise HtaccessError(f"line {line_no}: redirect source must be a path, not a URL")


def _validate_target(target: str, allowed_hosts: set, line_no: int) -> None:
    if _is_internal_path(target):
        return
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HtaccessError(f"line {line_no}: redirect target must be an internal path or http(s) URL")
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in allowed_hosts:
        raise HtaccessError(f"line {line_no}: external redirect host is not allowed: {host}")


def _validate_rewrite_pattern(pattern: str, line_no: int) -> None:
    if len(pattern) > 200:
        raise HtaccessError(f"line {line_no}: rewrite pattern is too long")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HtaccessError(f"line {line_no}: invalid rewrite regex: {exc}") from exc


def _parse_require_ip_args(values: List[str], line_no: int) -> List[str]:
    if not values:
        raise HtaccessError(f"line {line_no}: Require ip expects at least one IPv4 address or CIDR")
    return [_normalize_ipv4_cidr(value, line_no) for value in values]


def _normalize_ipv4_cidr(value: str, line_no: int) -> str:
    if not IPV4_CIDR_RE.match(value):
        raise HtaccessError(f"line {line_no}: only IPv4 address/CIDR is supported for Require ip: {value}")
    address, _, prefix = value.partition("/")
    octets = address.split(".")
    if any(int(octet) > 255 for octet in octets):
        raise HtaccessError(f"line {line_no}: invalid IPv4 address: {value}")
    if not prefix:
        return address + "/32"
    return address + "/" + prefix


def _apply_directory_index(
    line: ParsedLine, current: List[str], disabled: bool
) -> Tuple[List[str], bool]:
    args = line.args
    if not args:
        raise HtaccessError(f"line {line.line_no}: DirectoryIndex expects at least one argument")

    if len(args) == 1 and args[0].lower() == "disabled":
        return [], True

    if "disabled" in (a.lower() for a in args):
        raise HtaccessError(
            f"line {line.line_no}: DirectoryIndex 'disabled' must be the only argument on the line"
        )

    for name in args:
        if not DIRECTORY_INDEX_NAME_RE.match(name):
            raise HtaccessError(
                f"line {line.line_no}: DirectoryIndex resource name must be a plain filename "
                f"(alphanumeric, dot, underscore, hyphen), not a path: {name}"
            )
        if ".." in name:
            raise HtaccessError(
                f"line {line.line_no}: DirectoryIndex resource name must not contain '..': {name}"
            )

    # Apache semantics: multiple DirectoryIndex directives within the same
    # context add to the list rather than replace it, unless a prior
    # "disabled" reset the list to empty.
    updated = (current if not disabled else []) + list(args)
    return updated, False


KVS_KEY_REDIRECTS_META = "htaccess-redirects-meta"
KVS_KEY_REDIRECTS_CHUNK_PREFIX = "htaccess-redirects-"
KVS_KEY_AUTH_SCOPES_META = "htaccess-auth-scopes-meta"
KVS_KEY_AUTH_SCOPES_CHUNK_PREFIX = "htaccess-auth-scopes-"
KVS_KEY_DIRECTORY_INDEX_META = "htaccess-directory-index-meta"
KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX = "htaccess-directory-index-"
KVS_KEY_MAINTENANCE = "htaccess-maintenance"

# CloudFront KeyValueStore quota: maximum size of the value in a key-value
# pair is 1 KB. See https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html
# This quota is not adjustable (no "Request a higher quota" link on that page),
# so oversized directive sets must be handled by splitting values, not by
# requesting a larger limit.
KVS_VALUE_MAX_BYTES = 1024

# Leave headroom under the 1 KB hard limit for JSON array brackets/commas
# introduced when chunks are reassembled, and for future field additions
# without having to re-tune this constant to the byte.
CHUNK_TARGET_BYTES = 900

# CloudFront KeyValueStore quota: a single UpdateKeys call accepts at most
# 50 key-value pairs. Three directive types (redirects, auth scopes,
# directory index) each reserve one key for their meta key, leaving at most
# 47 chunk keys total to share across them in a single publish alongside
# the single-key maintenance value.
MAX_TOTAL_CHUNKS = 47


def _bin_pack_rules_for_kvs(rules: List[Dict[str, Any]]) -> List[str]:
    """Bin-pack a list of rule dicts into JSON-array chunks, each under CHUNK_TARGET_BYTES.

    Each chunk is a JSON array of one or more rules (not one rule per key)
    so the number of KVS keys grows roughly with the total payload size,
    not with the rule count alone. A single rule that itself exceeds the
    byte budget still becomes its own chunk (and is size-checked by the
    caller against the hard 1 KB limit). Used for all three directive types
    that can grow without bound as .htaccess files accumulate over time:
    redirects, Basic auth scopes, and DirectoryIndex scopes.
    """
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_size = 2  # "[" + "]"
    for rule in rules:
        rule_json = _json_dumps(rule)
        added_size = len(rule_json.encode("utf-8")) + (1 if current else 0)  # +1 for comma separator
        if current and current_size + added_size > CHUNK_TARGET_BYTES:
            chunks.append(current)
            current = []
            current_size = 2
            added_size = len(rule_json.encode("utf-8"))
        current.append(rule)
        current_size += added_size
    if current:
        chunks.append(current)
    return [_json_dumps(chunk) for chunk in chunks]


def split_config_for_kvs(config: Dict[str, Any]) -> Dict[str, str]:
    """Split a site config into KVS key-value pairs.

    Redirects, Basic auth scopes, and DirectoryIndex scopes are each
    bin-packed independently across a variable number of chunk keys plus a
    per-type meta key recording the chunk count. All three are expected to
    grow without bound as a site accumulates .htaccess files and rules over
    time (confirmed in practice: 10 DirectoryIndex-only .htaccess files
    alone exceeded the 1 KB single-key limit), so a fixed single-key layout
    is not safe for any of them. Maintenance mode config (at most one
    enabled/realm/allowIps/authorization object site-wide) remains a single
    key since it cannot grow with the number of .htaccess files.
    """
    chunk_groups = {
        (KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX): config.get("redirects", []),
        (KVS_KEY_AUTH_SCOPES_META, KVS_KEY_AUTH_SCOPES_CHUNK_PREFIX): config.get("authScopes", []),
        (KVS_KEY_DIRECTORY_INDEX_META, KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX): config.get("directoryIndexScopes", []),
    }

    parts = {
        KVS_KEY_MAINTENANCE: _json_dumps(config.get("maintenance", {"enabled": False, "realm": "Maintenance"})),
    }
    total_chunks = 0
    for (meta_key, chunk_prefix), rules in chunk_groups.items():
        chunk_values = _bin_pack_rules_for_kvs(rules)
        total_chunks += len(chunk_values)
        for index, chunk_value in enumerate(chunk_values):
            parts[f"{chunk_prefix}{index}"] = chunk_value
        parts[meta_key] = _json_dumps({"chunkCount": len(chunk_values)})

    if total_chunks > MAX_TOTAL_CHUNKS:
        raise HtaccessError(
            f"too many rules across redirects/auth-scopes/directory-index: {total_chunks} KVS chunks "
            f"required, exceeding the {MAX_TOTAL_CHUNKS}-chunk limit per publish"
        )

    oversized = {key: len(value.encode("utf-8")) for key, value in parts.items() if len(value.encode("utf-8")) > KVS_VALUE_MAX_BYTES}
    if oversized:
        details = ", ".join(f"{key} ({size} bytes)" for key, size in oversized.items())
        raise HtaccessError(
            f"config too large for KVS: exceeds {KVS_VALUE_MAX_BYTES}-byte per-value limit for: {details}"
        )
    return parts


def _expect_arg_count(line: ParsedLine, count: int) -> None:
    if len(line.args) != count:
        raise HtaccessError(f"line {line.line_no}: {line.raw} expects {count} argument(s)")


def _is_internal_path(target: str) -> bool:
    return target.startswith("/") and not target.startswith("//")


def _would_loop(source: str, target: str) -> bool:
    return target == source or target.startswith(source)


def _validate_redirect_chains(rules: List[Dict[str, Any]]) -> None:
    sorted_rules = sorted(rules, key=_rule_specificity, reverse=True)
    for rule in sorted_rules:
        seeds = _loop_probe_uris(rule)
        for seed in seeds:
            _assert_no_redirect_loop(seed, sorted_rules, rule)


def _loop_probe_uris(rule: Dict[str, Any]) -> List[str]:
    if rule.get("type") == "redirect":
        source = rule["from"]
        if not _is_internal_path(rule["to"]):
            return []
        probes = [source]
        if source.endswith("/"):
            probes.append(source + "__loop_probe__")
        return probes

    if rule.get("type") == "rewrite":
        if not _is_internal_path(rule["to"]):
            return []
        sample = _sample_uri_for_rewrite(rule)
        return [sample] if sample else []

    return []


def _assert_no_redirect_loop(seed: str, rules: List[Dict[str, Any]], source_rule: Dict[str, Any]) -> None:
    seen = {}
    uri = seed
    for step in range(len(rules) + 2):
        if uri in seen:
            raise HtaccessError(
                f"line {source_rule.get('line')}: redirect chain can loop at {uri}"
            )
        seen[uri] = step

        redirected = _apply_redirect_once(uri, rules)
        if not redirected or not _is_internal_path(redirected):
            return
        uri = redirected

    raise HtaccessError(
        f"line {source_rule.get('line')}: redirect chain exceeds validation limit"
    )


def _apply_redirect_once(uri: str, rules: List[Dict[str, Any]]) -> Optional[str]:
    for rule in rules:
        if rule.get("type") == "redirect" and uri.startswith(rule["from"]):
            return _append_remainder(uri, rule["from"], rule["to"])

        if rule.get("type") == "rewrite":
            base_path = rule.get("basePath", "/")
            if not uri.startswith(base_path):
                continue
            relative_path = uri[1:] if base_path == "/" else uri[len(base_path) :]
            if re.search(rule["pattern"], relative_path):
                target = _apache_substitution_to_python(rule["to"])
                return re.sub(rule["pattern"], target, relative_path, count=1)

    return None


def _append_remainder(uri: str, source: str, target: str) -> str:
    remainder = uri[len(source) :]
    if not remainder:
        return target
    if target.endswith("/") or remainder.startswith("/"):
        return target + remainder
    return target + "/" + remainder


def _sample_uri_for_rewrite(rule: Dict[str, Any]) -> Optional[str]:
    pattern = rule.get("pattern", "")
    base_path = rule.get("basePath", "/")
    # Keep loop detection conservative for the supported common forms.
    sample = pattern
    sample = sample.removeprefix("^").removesuffix("$")
    sample = re.sub(r"\(\.\*\)", "__loop_probe__", sample)
    sample = re.sub(r"\([^)]*\)", "__loop_probe__", sample)
    sample = sample.replace("\\/", "/")
    if any(ch in sample for ch in "[]+?{}|"):
        return None
    if base_path == "/":
        return "/" + sample.lstrip("/")
    return base_path + sample.lstrip("/")


def _apache_substitution_to_python(target: str) -> str:
    return re.sub(r"\$(\d+)", r"\\\1", target)


def _base_path_for_htaccess(key: str) -> str:
    if key == ".htaccess":
        return "/"
    if key.endswith("/.htaccess"):
        prefix = key[: -len("/.htaccess")]
        return "/" + prefix.strip("/") + "/"
    raise HtaccessError(f"not an htaccess key: {key}")


def _rule_specificity(rule: Dict[str, Any]) -> int:
    if rule.get("type") == "redirect":
        return len(rule.get("from", ""))
    return len(rule.get("basePath", "")) + len(rule.get("pattern", ""))


def _has_enabled_auth(config: Dict[str, Any]) -> bool:
    if config.get("maintenance", {}).get("enabled"):
        return True
    return any(scope.get("enabled") for scope in config.get("authScopes", []))


def _is_rules_key(key: str, exact_key: Optional[str], suffix: str) -> bool:
    if exact_key:
        return key == exact_key
    return key == suffix or key.endswith("/" + suffix)


def _load_all_htaccess_files(s3: Any, bucket: str, exact_key: Optional[str], suffix: str) -> List[Tuple[str, str]]:
    if exact_key:
        body = s3.get_object(Bucket=bucket, Key=exact_key)["Body"].read().decode("utf-8")
        return [(exact_key, body)]

    files = []
    token = None
    while True:
        kwargs = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        response = s3.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item["Key"]
            if _is_rules_key(key, None, suffix):
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
                files.append((key, body))
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")

    files.sort(key=lambda item: item[0])
    return files


def _load_basic_auth_value(secret_id: Optional[str]) -> Optional[str]:
    if not secret_id:
        return None
    sm = _boto3_client("secretsmanager")
    raw_secret = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    secret = json.loads(raw_secret)
    if "authorization" in secret:
        return secret["authorization"]
    username = secret.get("username")
    password = secret.get("password")
    if not username or not password:
        raise HtaccessError("Basic auth secret must contain authorization or username/password")
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _publish_to_kvs(puts: Dict[str, str]) -> None:
    """Publish multiple key-value pairs to the KVS in a single all-or-nothing UpdateKeys call.

    puts: mapping of KVS key name -> JSON string value. Each value must stay
    under the CloudFront KeyValueStore per-entry size limit (1 KB as of this
    writing; see https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html).
    Splitting the config into multiple keys by directive type (redirects,
    auth scopes, directory index, maintenance) keeps each individual value
    well under that limit for typical single-.htaccess-at-root sites; very
    large rule sets within a single directive type can still exceed it.
    """
    kvs_arn = os.environ.get("KVS_ARN")
    if not kvs_arn:
        return
    client = _boto3_client("cloudfront-keyvaluestore")

    put_items = [{"Key": key, "Value": value} for key, value in puts.items()]

    last_error: Optional[Exception] = None
    for attempt in range(KVS_PUBLISH_MAX_ATTEMPTS):
        describe = client.describe_key_value_store(KvsARN=kvs_arn)
        etag = describe["ETag"]
        try:
            client.update_keys(
                KvsARN=kvs_arn,
                IfMatch=etag,
                Puts=put_items,
                Deletes=[],
            )
            return
        except Exception as exc:  # noqa: BLE001 - boto3 raises dynamically generated exception classes
            if not _is_conflict_exception(exc):
                raise
            last_error = exc
            if attempt == KVS_PUBLISH_MAX_ATTEMPTS - 1:
                break
            _sleep_with_jitter(attempt)

    raise HtaccessError(
        f"KVS update conflicted after {KVS_PUBLISH_MAX_ATTEMPTS} attempts (ETag mismatch): {last_error}"
    )


def _is_conflict_exception(exc: Exception) -> bool:
    # boto3 raises a dynamically generated exception class per service error code.
    # ConflictException (HTTP 409) means another writer updated the KVS ETag concurrently.
    response = getattr(exc, "response", None)
    error_code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return error_code == "ConflictException" or type(exc).__name__ == "ConflictException"


def _sleep_with_jitter(attempt: int) -> None:
    delay = KVS_PUBLISH_BASE_DELAY_SECONDS * (2**attempt) + random.uniform(0, KVS_PUBLISH_BASE_DELAY_SECONDS)
    time.sleep(delay)


def _boto3_client(name: str) -> Any:
    import boto3

    return boto3.client(name)


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _log_event(status: str, **fields: Any) -> None:
    """Emit a single-line structured JSON log to stdout, which Lambda ships to
    CloudWatch Logs. This is intentionally separate from the S3
    _control-history/ audit trail: CloudWatch Logs is for operational
    visibility (CloudWatch Logs Insights queries, metric filters, alarms on
    "rejected" events), while _control-history/ is the durable audit record
    of what was actually published. Neither replaces the other.
    """
    entry = {"status": status, **fields}
    print(_json_dumps(entry))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    import sys

    allowed = _split_csv(os.environ.get("ALLOWED_EXTERNAL_HOSTS", ""))
    config = parse_htaccess(sys.stdin.read(), allowed_external_hosts=allowed)
    print(_json_dumps(config))

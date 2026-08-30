"""Integration-style tests for handler() and _publish_to_kvs() using mocked boto3 clients.

These tests do not call AWS. They patch htaccess_bridge._boto3_client so that
handler() exercises its full S3 -> parse/validate -> KVS publish -> history write
control flow against in-memory fake clients.
"""

import contextlib
import datetime as dt
import io
import json
import unittest
from unittest.mock import patch

import htaccess_bridge as hb


def _load_redirects_from_kvs(kvs_stored):
    """Reassemble the full redirects list from the meta key + chunk keys, mirroring loadRedirects() in handler.js."""
    meta = json.loads(kvs_stored[hb.KVS_KEY_REDIRECTS_META])
    redirects = []
    for i in range(meta["chunkCount"]):
        redirects.extend(json.loads(kvs_stored[f"{hb.KVS_KEY_REDIRECTS_CHUNK_PREFIX}{i}"]))
    return redirects


def _load_auth_scopes_from_kvs(kvs_stored):
    meta = json.loads(kvs_stored[hb.KVS_KEY_AUTH_SCOPES_META])
    scopes = []
    for i in range(meta["chunkCount"]):
        scopes.extend(json.loads(kvs_stored[f"{hb.KVS_KEY_AUTH_SCOPES_CHUNK_PREFIX}{i}"]))
    return scopes


class FakeConflictOnce(Exception):
    """Mimics a boto3 dynamically generated ConflictException."""


FakeConflictOnce.__name__ = "ConflictException"


class FakeS3:
    def __init__(
        self,
        objects=None,
        page_size=None,
        *,
        object_heads=None,
        head_error=None,
        copy_error=None,
    ):
        # objects: dict[key] -> str body
        # page_size: if set, list_objects_v2 simulates S3's real pagination
        # behavior (max 1000 keys per call in production) by only returning
        # up to this many keys per call and setting IsTruncated/
        # NextContinuationToken accordingly. None means "return everything
        # in one call" (the common case for small test fixtures).
        self.objects = dict(objects or {})
        self.put_calls = []
        self.page_size = page_size
        self.list_calls = []
        self.head_calls = []
        self.copy_calls = []
        self.head_error = head_error
        self.copy_error = copy_error
        self.object_heads = {}
        for key, body in self.objects.items():
            body_bytes = body.encode("utf-8") if isinstance(body, str) else body
            self.object_heads[key] = {
                "ETag": '"etag-current"',
                "ContentLength": len(body_bytes),
                "ContentType": "application/octet-stream",
                "Metadata": {},
            }
        for key, response in (object_heads or {}).items():
            self.object_heads.setdefault(key, {}).update(response)

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        return {"Body": _FakeBody(body)}

    def put_object(self, Bucket, Key, Body, ContentType):
        text = Body.decode("utf-8") if isinstance(Body, bytes) else Body
        self.objects[Key] = text
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": text, "ContentType": ContentType})

    def head_object(self, Bucket, Key):
        self.head_calls.append({"Bucket": Bucket, "Key": Key})
        if self.head_error:
            raise self.head_error
        return dict(self.object_heads[Key])

    def copy_object(self, **kwargs):
        self.copy_calls.append(kwargs)
        if self.copy_error:
            raise self.copy_error
        head = self.object_heads[kwargs["Key"]]
        head["CacheControl"] = kwargs["CacheControl"]
        head["Metadata"] = dict(kwargs["Metadata"])
        return {"CopyObjectResult": {"ETag": head["ETag"]}}

    def list_objects_v2(self, Bucket, ContinuationToken=None):
        self.list_calls.append(ContinuationToken)
        keys = sorted(self.objects.keys())
        if self.page_size is None:
            return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        end = start + len(page)
        is_truncated = end < len(keys)
        response = {"Contents": [{"Key": k} for k in page], "IsTruncated": is_truncated}
        if is_truncated:
            response["NextContinuationToken"] = str(end)
        return response


class _FakeBody:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


class FakeKVS:
    """Fake CloudFront KeyValueStore client.

    conflict_countdown: number of update_keys calls that should raise
    ConflictException before a call is allowed to succeed. Each successful
    describe/update pair advances the ETag, mimicking real KVS behavior.
    """

    def __init__(self, conflict_countdown: int = 0):
        self.conflict_countdown = conflict_countdown
        self.etag_counter = 0
        self.describe_calls = 0
        self.update_calls = []
        self.stored = {}

    def describe_key_value_store(self, KvsARN):
        self.describe_calls += 1
        return {"ETag": f"etag-{self.etag_counter}"}

    def update_keys(self, KvsARN, IfMatch, Puts, Deletes):
        if self.conflict_countdown > 0:
            self.conflict_countdown -= 1
            raise FakeConflictOnce("Resource is not in expected state.")
        for item in Puts:
            self.stored[item["Key"]] = item["Value"]
        for item in Deletes:
            self.stored.pop(item["Key"], None)
        self.etag_counter += 1
        return {"ItemCount": len(self.stored), "TotalSizeInBytes": sum(len(v) for v in self.stored.values())}


def _make_boto3_client_stub(fakes):
    def _stub(name):
        return fakes[name]

    return _stub


class FakeS3ServiceError(Exception):
    def __init__(self, code, status_code):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


def _s3_created_event(bucket, key, *, event_name=None, etag=None, version_id=None):
    object_info = {"key": key}
    if etag is not None:
        object_info["eTag"] = etag
    if version_id is not None:
        object_info["versionId"] = version_id
    record = {"s3": {"bucket": {"name": bucket}, "object": object_info}}
    if event_name is not None:
        record["eventName"] = event_name
    return {"Records": [record]}


class PublishToKvsRetryTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {"KVS_ARN": "arn:aws:cloudfront::123456789012:key-value-store/test"},
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_publishes_on_first_attempt_when_no_conflict(self):
        kvs = FakeKVS(conflict_countdown=0)
        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"cloudfront-keyvaluestore": kvs})):
            hb._publish_to_kvs({"htaccess-redirects": "[]", "htaccess-maintenance": '{"enabled":false}'})

        self.assertEqual(kvs.stored["htaccess-redirects"], "[]")
        self.assertEqual(kvs.stored["htaccess-maintenance"], '{"enabled":false}')
        self.assertEqual(kvs.describe_calls, 1)

    def test_retries_and_recovers_from_conflict_exception(self):
        kvs = FakeKVS(conflict_countdown=2)
        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"cloudfront-keyvaluestore": kvs})):
            with patch.object(hb, "_sleep_with_jitter", lambda attempt: None):
                hb._publish_to_kvs({"htaccess-redirects": "[]"})

        self.assertEqual(kvs.stored["htaccess-redirects"], "[]")
        # 2 conflicts + 1 success = 3 describe calls (one fresh ETag fetch per attempt)
        self.assertEqual(kvs.describe_calls, 3)

    def test_raises_htaccess_error_after_exhausting_retries(self):
        kvs = FakeKVS(conflict_countdown=hb.KVS_PUBLISH_MAX_ATTEMPTS + 5)
        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"cloudfront-keyvaluestore": kvs})):
            with patch.object(hb, "_sleep_with_jitter", lambda attempt: None):
                with self.assertRaisesRegex(hb.HtaccessError, "KVS update conflicted"):
                    hb._publish_to_kvs({"htaccess-redirects": "[]"})

        self.assertEqual(kvs.describe_calls, hb.KVS_PUBLISH_MAX_ATTEMPTS)

    def test_non_conflict_exception_is_not_retried(self):
        class Boom(Exception):
            pass

        class RaisingKVS(FakeKVS):
            def update_keys(self, **kwargs):
                raise Boom("unexpected failure")

        kvs = RaisingKVS()
        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"cloudfront-keyvaluestore": kvs})):
            with self.assertRaises(Boom):
                hb._publish_to_kvs({"htaccess-redirects": "[]"})

        # Only one describe call: no retry loop for non-conflict errors.
        self.assertEqual(kvs.describe_calls, 1)


class HandlerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "KVS_ARN": "arn:aws:cloudfront::123456789012:key-value-store/test",
                "HISTORY_PREFIX": "_control-history",
            },
            clear=False,
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_handler_publishes_valid_htaccess_end_to_end(self):
        s3 = FakeS3({".htaccess": "Redirect 301 /old/ /new/"})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        redirects = _load_redirects_from_kvs(kvs.stored)
        self.assertEqual(redirects[0]["from"], "/old/")
        # The other 2 directive-type meta keys must also be published (as empty defaults),
        # plus the single-key maintenance value.
        self.assertIn(hb.KVS_KEY_AUTH_SCOPES_META, kvs.stored)
        self.assertIn(hb.KVS_KEY_DIRECTORY_INDEX_META, kvs.stored)
        self.assertIn(hb.KVS_KEY_MAINTENANCE, kvs.stored)

        history_key = result["results"][0]["historyKey"]
        self.assertTrue(history_key.startswith("_control-history/published/"))
        self.assertIn(history_key, s3.objects)
        # History still stores the full unsplit config for audit purposes.
        full_history_config = json.loads(s3.objects[history_key])
        self.assertEqual(full_history_config["redirects"][0]["from"], "/old/")

    def test_handler_logs_structured_json_to_stdout_on_publish(self):
        s3 = FakeS3({".htaccess": "Redirect 301 /old/ /new/"})
        kvs = FakeKVS()
        captured = io.StringIO()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            with contextlib.redirect_stdout(captured):
                hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        log_lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(log_lines), 1)
        log_entry = json.loads(log_lines[0])
        self.assertEqual(log_entry["status"], "published")
        self.assertEqual(log_entry["redirectCount"], 1)
        self.assertIn("kvsValueSizes", log_entry)
        self.assertIn(hb.KVS_KEY_REDIRECTS_META, log_entry["kvsValueSizes"])

    def test_handler_logs_structured_json_to_stdout_on_reject(self):
        s3 = FakeS3({".htaccess": "Header set X-Frame-Options DENY"})
        kvs = FakeKVS()
        captured = io.StringIO()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            with contextlib.redirect_stdout(captured):
                hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        log_lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(log_lines), 1)
        log_entry = json.loads(log_lines[0])
        self.assertEqual(log_entry["status"], "rejected")
        self.assertIn("unsupported directive", log_entry["error"])
        self.assertEqual(log_entry["errorType"], "HtaccessError")

    def test_handler_logs_skipped_status_for_non_rules_keys(self):
        s3 = FakeS3({"images/logo.png": "binary-ish-placeholder"})
        kvs = FakeKVS()
        captured = io.StringIO()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            with contextlib.redirect_stdout(captured):
                hb.handler(_s3_created_event("my-bucket", "images/logo.png"), None)

        log_lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(log_lines), 1)
        log_entry = json.loads(log_lines[0])
        self.assertEqual(log_entry["status"], "skipped")

    def test_handler_rejects_invalid_htaccess_and_writes_rejected_history(self):
        s3 = FakeS3({".htaccess": "Header set X-Frame-Options DENY"})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertNotIn(hb.KVS_KEY_REDIRECTS_META, kvs.stored)

        history_key = result["results"][0]["historyKey"]
        self.assertTrue(history_key.startswith("_control-history/rejected/"))
        rejected_payload = json.loads(s3.objects[history_key])
        self.assertEqual(rejected_payload["status"], "rejected")
        self.assertIn("unsupported directive", rejected_payload["error"])

    def test_handler_rejects_maintenance_without_htpasswd(self):
        s3 = FakeS3(
            {
                ".htaccess": """
                AuthType Basic
                AuthName "Members"
                Require valid-user
                """
            }
        )
        kvs = FakeKVS()
        # A matching .htpasswd is required when maintenance mode is enabled.
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertNotIn(hb.KVS_KEY_MAINTENANCE, kvs.stored)

    def test_handler_publishes_when_htpasswd_is_uploaded(self):
        s3 = FakeS3(
            {
                ".htaccess": "AuthType Basic\nAuthName Members\nAuthUserFile .htpasswd\nRequire valid-user",
                ".htpasswd": "user:{SHA}nU4eI71bcnBGqeO0t9tXvY1u5oQ=",
            }
        )
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htpasswd"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        scopes = _load_auth_scopes_from_kvs(kvs.stored)
        self.assertEqual(scopes[0]["credentials"][0]["username"], "user")
        history = json.loads(s3.objects[result["results"][0]["historyKey"]])
        self.assertNotIn("credentials", history["authScopes"][0])

    def test_handler_publishes_empty_config_when_last_htaccess_deleted(self):
        # No .htaccess files remain in the bucket at all.
        s3 = FakeS3({})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        redirects = _load_redirects_from_kvs(kvs.stored)
        maintenance = json.loads(kvs.stored[hb.KVS_KEY_MAINTENANCE])
        self.assertEqual(redirects, [])
        self.assertFalse(maintenance["enabled"])

    def test_handler_skips_non_rules_keys(self):
        s3 = FakeS3({"images/logo.png": "binary-ish-placeholder"})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", "images/logo.png"), None)

        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertEqual(kvs.stored, {})

    def test_handler_retries_kvs_conflict_before_publishing(self):
        s3 = FakeS3({".htaccess": "Redirect 301 /old/ /new/"})
        kvs = FakeKVS(conflict_countdown=1)
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            with patch.object(hb, "_sleep_with_jitter", lambda attempt: None):
                result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        self.assertEqual(kvs.describe_calls, 2)

    def test_handler_bin_packs_many_redirects_across_chunk_keys_instead_of_rejecting(self):
        # Before bin-packing, 60 redirects on a single .htaccess would exceed
        # the "redirects" KVS value's 1 KB per-value limit and get rejected.
        # With bin-packing, they must now be split across multiple chunk
        # keys and published successfully.
        many_redirects = "\n".join(f"Redirect 301 /old-{i}/ /new-{i}/" for i in range(60))
        s3 = FakeS3({".htaccess": many_redirects})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        redirects = _load_redirects_from_kvs(kvs.stored)
        self.assertEqual(len(redirects), 60)
        # build_site_config() sorts redirects by specificity, not upload
        # order, so verify the full set is present rather than assuming a
        # particular index corresponds to a particular source rule.
        self.assertEqual({r["from"] for r in redirects}, {f"/old-{i}/" for i in range(60)})
        # More than one chunk key must have been used to stay under the
        # 1 KB per-value limit on each individual chunk.
        chunk_keys = [key for key in kvs.stored if key.startswith(hb.KVS_KEY_REDIRECTS_CHUNK_PREFIX)]
        self.assertGreater(len(chunk_keys), 1)
        for key in chunk_keys:
            self.assertLessEqual(len(kvs.stored[key].encode("utf-8")), hb.KVS_VALUE_MAX_BYTES)

    def test_handler_bin_packs_many_directory_index_scopes_across_chunk_keys(self):
        # Reproduces the real-world case discovered during load testing: many
        # .htaccess files that each set nothing but DirectoryIndex (no
        # redirects) still accumulate enough directoryIndexScopes entries to
        # exceed the single-key 1 KB limit. Confirms these are bin-packed
        # across multiple chunk keys rather than published to a single
        # oversized key or silently dropped.
        htaccess_files = {".htaccess": "DirectoryIndex index.html"}
        for i in range(30):
            htaccess_files[f"section-{i}/.htaccess"] = f"DirectoryIndex section{i}-index.html"
        s3 = FakeS3(htaccess_files)
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        meta = json.loads(kvs.stored[hb.KVS_KEY_DIRECTORY_INDEX_META])
        chunk_keys = [
            key
            for key in kvs.stored
            if key.startswith(hb.KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX) and key != hb.KVS_KEY_DIRECTORY_INDEX_META
        ]
        self.assertEqual(meta["chunkCount"], len(chunk_keys))
        self.assertGreater(len(chunk_keys), 1)
        for key in chunk_keys:
            self.assertLessEqual(len(kvs.stored[key].encode("utf-8")), hb.KVS_VALUE_MAX_BYTES)

        # All 31 scopes (root + 30 sections) must be present across chunks.
        all_scopes = []
        for i in range(meta["chunkCount"]):
            all_scopes.extend(json.loads(kvs.stored[f"{hb.KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX}{i}"]))
        self.assertEqual(len(all_scopes), 31)
        path_prefixes = {scope["pathPrefix"] for scope in all_scopes}
        self.assertIn("/", path_prefixes)
        self.assertIn("/section-0/", path_prefixes)
        self.assertIn("/section-29/", path_prefixes)

    def test_handler_rejects_when_total_chunks_exceed_the_max_chunk_count(self):
        # Enough redirects to require more chunks than MAX_REDIRECT_CHUNKS
        # allows (a single UpdateKeys call accepts at most 50 key-value
        # pairs, one of which is reserved for the meta key) must be rejected
        # cleanly rather than silently dropping rules or raising an
        # unhandled boto3 error.
        redirect_count = hb.MAX_TOTAL_CHUNKS * 8 + 50
        many_redirects = "\n".join(f"Redirect 301 /old-{i}/ /new-{i}/" for i in range(redirect_count))
        s3 = FakeS3({".htaccess": many_redirects})
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertNotIn(hb.KVS_KEY_REDIRECTS_META, kvs.stored)
        history_key = result["results"][0]["historyKey"]
        rejected_payload = json.loads(s3.objects[history_key])
        self.assertIn("too many rules across redirects/auth-scopes/directory-index", rejected_payload["error"])
        self.assertIn("47-chunk limit", rejected_payload["error"])

    def test_handler_finds_htaccess_files_across_multiple_s3_list_pages(self):
        # Real S3's ListObjectsV2 caps each call at 1000 keys and requires
        # the caller to follow IsTruncated/NextContinuationToken to see the
        # rest (confirmed against the real bucket with 1152 objects during
        # manual verification: 1000 in page 1, 152 in page 2). This test
        # exercises the same multi-page path against FakeS3 configured with
        # a small page_size, so a future regression in the while-loop in
        # _load_all_htaccess_files (e.g. someone "simplifying" it to a
        # single list_objects_v2 call) is caught by the test suite instead
        # of only being caught by a one-off manual AWS verification.
        objects = {".htaccess": "Redirect 301 /old/ /new/"}
        # 25 unrelated content files, well above the page_size below, so
        # the .htaccess key is guaranteed to land on a different page than
        # at least some content files regardless of sort order.
        for i in range(25):
            objects[f"content/file-{i}.html"] = f"<html>{i}</html>"
        s3 = FakeS3(objects, page_size=5)
        kvs = FakeKVS()
        with patch.object(
            hb,
            "_boto3_client",
            _make_boto3_client_stub({"s3": s3, "cloudfront-keyvaluestore": kvs}),
        ):
            result = hb.handler(_s3_created_event("my-bucket", ".htaccess"), None)

        self.assertEqual(result["results"][0]["status"], "published")
        # Both .htaccess and .htpasswd discovery paginate through the bucket.
        # Each pass over 26 objects at page_size=5 requires 6 calls.
        # (5+5+5+5+5+1) to see every key, proving the loop actually paged
        # through instead of stopping after the first call.
        self.assertEqual(len(s3.list_calls), 12)
        redirects = _load_redirects_from_kvs(kvs.stored)
        self.assertEqual(len(redirects), 1)
        self.assertEqual(redirects[0]["from"], "/old/")


class CacheControlHandlerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "DEFAULT_CACHE_CONTROL": "public, max-age=60",
                "HISTORY_PREFIX": "_control-history",
            },
            clear=False,
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_handler_adds_default_cache_control_and_preserves_object_metadata(self):
        expires = dt.datetime(2027, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
        s3 = FakeS3(
            {"assets/app.js": "console.log('ok')"},
            object_heads={
                "assets/app.js": {
                    "ETag": '"etag-current"',
                    "VersionId": "version-3",
                    "ContentLength": 17,
                    "Metadata": {"uploaded-by": "cms", "build": "42"},
                    "ContentType": "text/javascript; charset=utf-8",
                    "ContentEncoding": "gzip",
                    "ContentDisposition": 'inline; filename="app.js"',
                    "ContentLanguage": "ja",
                    "Expires": expires,
                    "WebsiteRedirectLocation": "/assets/current.js",
                    "StorageClass": "INTELLIGENT_TIERING",
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": "arn:aws:kms:ap-northeast-1:123456789012:key/test",
                    "BucketKeyEnabled": True,
                }
            },
        )
        event = _s3_created_event(
            "my-bucket",
            "assets/app.js",
            event_name="ObjectCreated:Put",
            etag="etag-current",
            version_id="version-3",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(
            result["results"][0],
            {
                "bucket": "my-bucket",
                "key": "assets/app.js",
                "status": "cache-control-updated",
                "cacheControl": "public, max-age=60",
            },
        )
        # The current-version check must not include VersionId; otherwise it
        # would only prove that an old event version still exists.
        self.assertEqual(s3.head_calls, [{"Bucket": "my-bucket", "Key": "assets/app.js"}])
        self.assertEqual(len(s3.copy_calls), 1)
        copy_call = s3.copy_calls[0]
        self.assertEqual(
            copy_call["CopySource"],
            {"Bucket": "my-bucket", "Key": "assets/app.js", "VersionId": "version-3"},
        )
        self.assertEqual(copy_call["CopySourceIfMatch"], '"etag-current"')
        self.assertEqual(copy_call["IfMatch"], '"etag-current"')
        self.assertEqual(copy_call["MetadataDirective"], "REPLACE")
        self.assertEqual(copy_call["TaggingDirective"], "COPY")
        self.assertEqual(copy_call["AnnotationDirective"], "EXCLUDE")
        self.assertEqual(copy_call["CacheControl"], "public, max-age=60")
        self.assertEqual(copy_call["Metadata"], {"uploaded-by": "cms", "build": "42"})
        for field in (
            "ContentType",
            "ContentEncoding",
            "ContentDisposition",
            "ContentLanguage",
            "Expires",
            "WebsiteRedirectLocation",
            "StorageClass",
            "ServerSideEncryption",
            "SSEKMSKeyId",
            "BucketKeyEnabled",
        ):
            self.assertEqual(copy_call[field], s3.object_heads["assets/app.js"][field])

    def test_handler_accepts_each_non_copy_content_upload_event(self):
        for event_name in (
            "ObjectCreated:Put",
            "ObjectCreated:Post",
            "ObjectCreated:CompleteMultipartUpload",
        ):
            with self.subTest(event_name=event_name):
                s3 = FakeS3({"index.html": "<html></html>"})
                event = _s3_created_event(
                    "my-bucket",
                    "index.html",
                    event_name=event_name,
                    etag="etag-current",
                )
                with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
                    result = hb.handler(event, None)

                self.assertEqual(result["results"][0]["status"], "cache-control-updated")
                self.assertEqual(len(s3.copy_calls), 1)

    def test_handler_targets_current_null_version_when_versioning_is_suspended(self):
        s3 = FakeS3(
            {"index.html": "<html></html>"},
            object_heads={"index.html": {"VersionId": "null"}},
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-updated")
        self.assertEqual(s3.copy_calls[0]["CopySource"]["VersionId"], "null")

    def test_handler_uses_configured_cache_control_value(self):
        s3 = FakeS3({"index.html": "<html></html>"})
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.dict("os.environ", {"DEFAULT_CACHE_CONTROL": "public, max-age=120"}):
            with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
                result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["cacheControl"], "public, max-age=120")
        self.assertEqual(s3.copy_calls[0]["CacheControl"], "public, max-age=120")

    def test_handler_preserves_explicit_cache_control_without_copying(self):
        s3 = FakeS3(
            {"assets/app.js": "console.log('ok')"},
            object_heads={
                "assets/app.js": {"CacheControl": "public, max-age=31536000, immutable"}
            },
        )
        event = _s3_created_event(
            "my-bucket",
            "assets/app.js",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-preserved")
        self.assertEqual(
            result["results"][0]["cacheControl"], "public, max-age=31536000, immutable"
        )
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_out_of_order_event_when_etag_is_not_current(self):
        s3 = FakeS3(
            {"index.html": "new"},
            object_heads={"index.html": {"ETag": '"etag-new"'}},
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-old",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-stale")
        self.assertEqual(result["results"][0]["reason"], "etag-mismatch")
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_out_of_order_version_even_when_content_etag_matches(self):
        s3 = FakeS3(
            {"index.html": "same-content"},
            object_heads={"index.html": {"VersionId": "version-new"}},
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
            version_id="version-old",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-stale")
        self.assertEqual(result["results"][0]["reason"], "version-mismatch")
        self.assertEqual(s3.copy_calls, [])

    def test_handler_treats_failed_copy_precondition_as_stale(self):
        s3 = FakeS3(
            {"index.html": "<html></html>"},
            copy_error=FakeS3ServiceError("PreconditionFailed", 412),
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )
        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-stale")
        self.assertEqual(result["results"][0]["reason"], "PreconditionFailed")

    def test_handler_propagates_conditional_request_conflict_for_async_retry(self):
        conflict = FakeS3ServiceError("ConditionalRequestConflict", 409)
        s3 = FakeS3(
            {"index.html": "<html></html>"},
            copy_error=conflict,
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            with self.assertRaises(FakeS3ServiceError) as raised:
                hb.handler(event, None)

        self.assertIs(raised.exception, conflict)

    def test_handler_treats_deleted_object_as_stale(self):
        s3 = FakeS3(head_error=FakeS3ServiceError("NoSuchKey", 404))
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-deleted",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-stale")
        self.assertEqual(result["results"][0]["reason"], "NoSuchKey")
        self.assertEqual(s3.copy_calls, [])

    def test_handler_propagates_non_conflict_s3_errors_for_async_retry(self):
        s3 = FakeS3(
            {"index.html": "<html></html>"},
            copy_error=FakeS3ServiceError("InternalError", 500),
        )
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            with self.assertRaises(FakeS3ServiceError):
                hb.handler(event, None)

    def test_handler_rejects_empty_default_cache_control(self):
        s3 = FakeS3({"index.html": "<html></html>"})
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.dict("os.environ", {"DEFAULT_CACHE_CONTROL": "   "}):
            with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
                with self.assertRaisesRegex(ValueError, "DEFAULT_CACHE_CONTROL must not be empty"):
                    hb.handler(event, None)

        self.assertEqual(s3.head_calls, [])
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_malformed_content_event_without_etag(self):
        s3 = FakeS3({"index.html": "<html></html>"})
        event = _s3_created_event(
            "my-bucket",
            "index.html",
            event_name="ObjectCreated:Put",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-skipped")
        self.assertEqual(result["results"][0]["reason"], "event-etag-missing")
        self.assertEqual(s3.head_calls, [])
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_object_larger_than_single_copy_limit(self):
        s3 = FakeS3(
            {"video/large.mp4": "placeholder"},
            object_heads={
                "video/large.mp4": {"ContentLength": hb.S3_COPY_OBJECT_MAX_BYTES + 1}
            },
        )
        event = _s3_created_event(
            "my-bucket",
            "video/large.mp4",
            event_name="ObjectCreated:CompleteMultipartUpload",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-skipped")
        self.assertEqual(result["results"][0]["reason"], "object-exceeds-copy-object-limit")
        self.assertEqual(result["results"][0]["objectSize"], hb.S3_COPY_OBJECT_MAX_BYTES + 1)
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_object_when_head_exposes_object_lock_settings(self):
        s3 = FakeS3(
            {"records/report.pdf": "locked"},
            object_heads={
                "records/report.pdf": {
                    "ObjectLockMode": "COMPLIANCE",
                    "ObjectLockRetainUntilDate": dt.datetime(
                        2030, 1, 1, tzinfo=dt.timezone.utc
                    ),
                }
            },
        )
        event = _s3_created_event(
            "my-bucket",
            "records/report.pdf",
            event_name="ObjectCreated:Put",
            etag="etag-current",
        )

        with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
            result = hb.handler(event, None)

        self.assertEqual(result["results"][0]["status"], "cache-control-skipped")
        self.assertEqual(result["results"][0]["reason"], "object-lock-unsupported")
        self.assertEqual(
            result["results"][0]["objectLockFields"],
            ["ObjectLockMode", "ObjectLockRetainUntilDate"],
        )
        self.assertEqual(s3.copy_calls, [])

    def test_handler_skips_copy_delete_history_and_literal_control_file_events(self):
        cases = (
            ("assets/app.js", "ObjectCreated:Copy", {}),
            ("assets/app.js", "ObjectRemoved:Delete", {}),
            ("_control-history/published/config.json", "ObjectCreated:Put", {}),
            # With a custom RULES_KEY, literal .htaccess is no longer the
            # configured rules key but must still never be treated as content.
            ("nested/.htaccess", "ObjectCreated:Put", {"RULES_KEY": "config/rules.conf"}),
        )
        for key, event_name, extra_env in cases:
            with self.subTest(key=key, event_name=event_name):
                s3 = FakeS3({key: "placeholder"})
                event = _s3_created_event(
                    "my-bucket",
                    key,
                    event_name=event_name,
                    etag="etag-current",
                )
                with patch.dict("os.environ", extra_env):
                    with patch.object(hb, "_boto3_client", _make_boto3_client_stub({"s3": s3})):
                        result = hb.handler(event, None)

                self.assertEqual(result["results"][0]["status"], "skipped")
                self.assertEqual(s3.head_calls, [])
                self.assertEqual(s3.copy_calls, [])


if __name__ == "__main__":
    unittest.main()

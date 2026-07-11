import cf from 'cloudfront';

var kvs = cf.kvs();
var KVS_KEY_REDIRECTS_META = 'htaccess-redirects-meta';
var KVS_KEY_REDIRECTS_CHUNK_PREFIX = 'htaccess-redirects-';
var KVS_KEY_AUTH_SCOPES_META = 'htaccess-auth-scopes-meta';
var KVS_KEY_AUTH_SCOPES_CHUNK_PREFIX = 'htaccess-auth-scopes-';
var KVS_KEY_DIRECTORY_INDEX_META = 'htaccess-directory-index-meta';
var KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX = 'htaccess-directory-index-';
var KVS_KEY_MAINTENANCE = 'htaccess-maintenance';

async function handler(event) {
  var request = event.request;
  var uri = request.uri || '/';

  if (isBlockedPath(uri)) {
    return {
      statusCode: 403,
      statusDescription: 'Forbidden'
    };
  }

  var config = await loadConfig();
  var authScope = findAuthScope(uri, config);

  if (authScope && !isAuthorized(event, request, authScope)) {
    return unauthorized(authScope);
  }

  var redirect = findRedirect(uri, config.redirects || []);
  if (redirect) {
    return {
      statusCode: redirect.status,
      statusDescription: redirect.status === 301 ? 'Moved Permanently' : 'Found',
      headers: {
        location: { value: redirect.location }
      }
    };
  }

  request.uri = resolveIndexDocument(uri, config.directoryIndexScopes || []);
  return request;
}

async function loadConfig() {
  var results = await Promise.all([
    loadBinPackedRules(KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX),
    loadBinPackedRules(KVS_KEY_AUTH_SCOPES_META, KVS_KEY_AUTH_SCOPES_CHUNK_PREFIX),
    loadBinPackedRules(KVS_KEY_DIRECTORY_INDEX_META, KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX),
    loadKvsJson(KVS_KEY_MAINTENANCE, { enabled: false, realm: 'Maintenance' }),
  ]);
  return {
    schemaVersion: 1,
    redirects: results[0],
    authScopes: results[1],
    directoryIndexScopes: results[2],
    maintenance: results[3],
  };
}

// Redirects, Basic auth scopes, and DirectoryIndex scopes are each
// bin-packed across a variable number of chunk keys (see
// split_config_for_kvs in the Lambda) because all three are expected to
// grow past the 1 KB per-value limit as a site accumulates .htaccess files
// and rules over time (confirmed in practice: 10 DirectoryIndex-only
// .htaccess files alone exceeded the single-key limit). Each type's meta
// key records how many chunks exist so they can all be fetched in
// parallel in one extra round trip, rather than probing key names one at
// a time.
async function loadBinPackedRules(metaKey, chunkPrefix) {
  var meta = await loadKvsJson(metaKey, { chunkCount: 0 });
  var chunkCount = meta.chunkCount || 0;
  if (chunkCount === 0) {
    return [];
  }
  var chunkPromises = [];
  for (var i = 0; i < chunkCount; i++) {
    chunkPromises.push(loadKvsJson(chunkPrefix + i, []));
  }
  var chunks = await Promise.all(chunkPromises);
  var rules = [];
  for (var j = 0; j < chunks.length; j++) {
    rules = rules.concat(chunks[j]);
  }
  return rules;
}

async function loadKvsJson(key, fallback) {
  try {
    var raw = await kvs.get(key);
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

function isBlockedPath(uri) {
  return uri === '/.htaccess' ||
    uri === '/.htpasswd' ||
    uri.indexOf('/_control-history/') === 0 ||
    endsWith(uri, '/.htaccess') ||
    endsWith(uri, '/.htpasswd');
}

function findAuthScope(uri, config) {
  var scopes = config.authScopes || [];
  for (var i = 0; i < scopes.length; i++) {
    if (scopes[i].enabled && startsWith(uri, scopes[i].pathPrefix)) {
      return scopes[i];
    }
  }
  if (config.maintenance && config.maintenance.enabled) {
    return config.maintenance;
  }
  return null;
}

function isAuthorized(event, request, maintenance) {
  if (isAllowedViewerIp(event, maintenance.allowIps || [])) {
    return true;
  }
  if (!maintenance.authorization) {
    return false;
  }
  var headers = request.headers || {};
  var auth = headers.authorization && headers.authorization.value;
  return auth === maintenance.authorization;
}

function isAllowedViewerIp(event, allowIps) {
  var viewerIp = event.viewer && event.viewer.ip;
  if (!viewerIp || viewerIp.indexOf(':') !== -1) {
    return false;
  }
  var viewerInt = ipv4ToInt(viewerIp);
  if (viewerInt === null) {
    return false;
  }
  for (var i = 0; i < allowIps.length; i++) {
    if (ipv4InCidr(viewerInt, allowIps[i])) {
      return true;
    }
  }
  return false;
}

function ipv4InCidr(viewerInt, cidr) {
  var parts = cidr.split('/');
  var networkInt = ipv4ToInt(parts[0]);
  if (networkInt === null) {
    return false;
  }
  var prefix = parts.length > 1 ? parseInt(parts[1], 10) : 32;
  if (prefix < 0 || prefix > 32) {
    return false;
  }
  var mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (viewerInt & mask) === (networkInt & mask);
}

function ipv4ToInt(ip) {
  var parts = ip.split('.');
  if (parts.length !== 4) {
    return null;
  }
  var value = 0;
  for (var i = 0; i < 4; i++) {
    var part = parseInt(parts[i], 10);
    if (isNaN(part) || part < 0 || part > 255 || String(part) !== parts[i]) {
      return null;
    }
    value = ((value << 8) + part) >>> 0;
  }
  return value >>> 0;
}

function unauthorized(maintenance) {
  var realm = maintenance.realm || 'Maintenance';
  return {
    statusCode: 401,
    statusDescription: 'Unauthorized',
    headers: {
      'www-authenticate': { value: 'Basic realm="' + escapeRealm(realm) + '"' },
      'cache-control': { value: 'no-store' }
    }
  };
}

function findRedirect(uri, rules) {
  for (var i = 0; i < rules.length; i++) {
    var rule = rules[i];
    if (rule.type === 'redirect' && startsWith(uri, rule.from)) {
      return {
        status: rule.status,
        location: appendRemainder(uri, rule.from, rule.to)
      };
    }
    if (rule.type === 'rewrite') {
      var basePath = rule.basePath || '/';
      if (!startsWith(uri, basePath)) {
        continue;
      }
      var relativePath = basePath === '/' ? uri.substring(1) : uri.substring(basePath.length);
      var re = new RegExp(rule.pattern);
      if (re.test(relativePath)) {
        return {
          status: rule.status,
          location: relativePath.replace(re, rule.to)
        };
      }
    }
  }
  return null;
}

function appendRemainder(uri, from, to) {
  var remainder = uri.substring(from.length);
  if (!remainder) {
    return to;
  }
  if (to.charAt(to.length - 1) === '/' || remainder.charAt(0) === '/') {
    return to + remainder;
  }
  return to + '/' + remainder;
}

function resolveIndexDocument(uri, directoryIndexScopes) {
  if (uri !== '/' && uri.charAt(uri.length - 1) !== '/' && hasFileExtension(uri)) {
    return uri;
  }

  var directoryPath = uri.charAt(uri.length - 1) === '/' ? uri : uri + '/';
  var indexName = firstDirectoryIndexName(directoryPath, directoryIndexScopes) || 'index.html';

  if (uri === '/') {
    return '/' + indexName;
  }
  if (uri.charAt(uri.length - 1) === '/') {
    return uri + indexName;
  }
  return uri + '/' + indexName;
}

// Apache's DirectoryIndex lets a .htaccess declare a priority list of
// candidate filenames (e.g. "DirectoryIndex index.php index.html") and the
// server serves whichever one actually exists first. CloudFront Functions
// cannot pre-fetch the origin to check existence (see the "About SPA
// fallback" note in README.md for the same limitation), so this is a
// simplified reproduction: it always uses the FIRST name in the most
// specific matching scope's list, without checking whether it exists.
function firstDirectoryIndexName(uri, directoryIndexScopes) {
  for (var i = 0; i < directoryIndexScopes.length; i++) {
    var scope = directoryIndexScopes[i];
    if (startsWith(uri, scope.pathPrefix) && scope.names && scope.names.length > 0) {
      return scope.names[0];
    }
  }
  return null;
}

function hasFileExtension(uri) {
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  var lastDotIndex = lastSegment.lastIndexOf('.');

  // No dot in the last segment: definitely not a file extension.
  if (lastDotIndex === -1) {
    return false;
  }
  // Dot at position 0 means the segment is a dotfile (e.g. ".well-known"),
  // not an extension.
  if (lastDotIndex === 0) {
    return false;
  }
  // Dot at the very end (e.g. "foo.") has no extension characters after it.
  if (lastDotIndex === lastSegment.length - 1) {
    return false;
  }

  var extension = lastSegment.substring(lastDotIndex + 1);
  // A real file extension is a short run of alphanumeric characters
  // (e.g. html, css, js, json, png, woff2). Reject anything that doesn't
  // look like one, so segments such as "v1.2" or "file.name.with.dots"
  // (final segment ending in a dictionary word, not a known extension)
  // are treated as extensionless when the trailing token isn't a plausible
  // extension pattern.
  return /^[A-Za-z0-9]{1,10}$/.test(extension);
}

function startsWith(value, prefix) {
  return value.substring(0, prefix.length) === prefix;
}

function endsWith(value, suffix) {
  return value.substring(value.length - suffix.length) === suffix;
}

function escapeRealm(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

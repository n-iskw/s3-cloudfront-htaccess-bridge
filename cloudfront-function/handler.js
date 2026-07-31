import cf from 'cloudfront';
import crypto from 'crypto';

var kvs = cf.kvs();

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
    if (authScope.mode === 'ip') {
      return forbidden();
    }
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
    loadBinPackedRules('redirects'),
    loadBinPackedRules('auth-scopes'),
    loadBinPackedRules('directory-index'),
    loadKvsJson('htaccess-maintenance', { enabled: false, realm: 'Maintenance' }),
  ]);
  return {
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
async function loadBinPackedRules(name) {
  var keyPrefix = 'htaccess-' + name;
  var chunkCount = (await loadKvsJson(keyPrefix + '-meta', { chunkCount: 0 })).chunkCount || 0;
  if (!chunkCount) {
    return [];
  }
  var chunkPromises = [];
  for (var i = 0; i < chunkCount; i++) {
    chunkPromises.push(loadKvsJson(keyPrefix + '-' + i, []));
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
    return JSON.parse(await kvs.get(key));
  } catch (e) {
    return fallback;
  }
}

function isBlockedPath(uri) {
  return startsWith(uri, '/_control-history/') ||
    endsWith(uri, '/.htaccess') ||
    endsWith(uri, '/.htpasswd');
}

function findAuthScope(uri, config) {
  var scope = findScope(uri, config.authScopes || [], true);
  return scope || (config.maintenance && config.maintenance.enabled ? config.maintenance : null);
}

function findScope(uri, scopes, requireEnabled) {
  for (var i = 0; i < scopes.length; i++) {
    var scope = scopes[i];
    if ((!requireEnabled || scope.enabled) && startsWith(uri, scope.pathPrefix)) {
      return scope;
    }
  }
  return null;
}

function isAuthorized(event, request, maintenance) {
  if (maintenance.mode === 'ip') {
    return isAllowedViewerIp(event, maintenance.allowIps || []);
  }
  if (isAllowedViewerIp(event, maintenance.allowIps || [])) {
    return true;
  }
  var headers = request.headers || {};
  var auth = headers.authorization && headers.authorization.value;
  if (!auth || auth.substring(0, 6).toLowerCase() !== 'basic ') {
    return false;
  }
  var decoded;
  try {
    decoded = Buffer.from(auth.substring(6), 'base64').toString('utf8');
  } catch (e) {
    return false;
  }
  var separator = decoded.indexOf(':');
  if (separator < 1) {
    return false;
  }
  var username = decoded.substring(0, separator);
  var password = decoded.substring(separator + 1);
  var digest = crypto.createHash('sha1').update(password).digest('base64');
  var credentials = maintenance.credentials || [];
  for (var i = 0; i < credentials.length; i++) {
    if (credentials[i].username === username && credentials[i].sha1 === digest) {
      return true;
    }
  }
  return false;
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
  return value;
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

function forbidden() {
  return {
    statusCode: 403,
    statusDescription: 'Forbidden',
    headers: {
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
  var separator = remainder && !endsWith(to, '/') && !startsWith(remainder, '/') ? '/' : '';
  return to + separator + remainder;
}

function resolveIndexDocument(uri, directoryIndexScopes) {
  var hasTrailingSlash = endsWith(uri, '/');
  if (!hasTrailingSlash && hasFileExtension(uri)) {
    return uri;
  }

  // DirectoryIndex is opt-in. A scope with an empty names list represents
  // an explicit "DirectoryIndex disabled" and blocks inherited scopes.
  var directoryPath = hasTrailingSlash ? uri : uri + '/';
  var directoryIndexScope = findScope(directoryPath, directoryIndexScopes, false);
  if (!directoryIndexScope || !directoryIndexScope.names || directoryIndexScope.names.length === 0) {
    return uri;
  }
  return uri + (hasTrailingSlash ? '' : '/') + directoryIndexScope.names[0];
}

// Apache's DirectoryIndex lets a .htaccess declare a priority list of
// candidate filenames (e.g. "DirectoryIndex index.php index.html") and the
// server serves whichever one actually exists first. CloudFront Functions
// cannot pre-fetch the origin to check existence (see the "About SPA
// fallback" note in README.md for the same limitation), so this is a
// simplified reproduction: it always uses the FIRST name in the most
// specific matching scope's list, without checking whether it exists.
function hasFileExtension(uri) {
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  var lastDotIndex = lastSegment.lastIndexOf('.');

  // Dotfiles are not extensions. The pattern also rejects a trailing dot
  // and limits extensions to short alphanumeric names.
  return lastDotIndex > 0 && /^[A-Za-z0-9]{1,10}$/.test(lastSegment.substring(lastDotIndex + 1));
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

// Minimal Node-based tests for pure logic in handler.js. CloudFront Functions
// runtime is not Node, so the pure function bodies are extracted for testing.

var crypto = require('crypto');
var fs = require('fs');
var handlerSource = fs.readFileSync(__dirname + '/handler.js', 'utf8');
var minifiedSource = fs.readFileSync(__dirname + '/handler.min.js', 'utf8');
var templateSource = fs.readFileSync(__dirname + '/../infra/bridge-resources.yaml', 'utf8');
var maxFunctionBytes = 10 * 1024;
var minifiedBytes = Buffer.byteLength(minifiedSource, 'utf8');
if (minifiedBytes >= maxFunctionBytes) {
  throw new Error('minified CloudFront Function exceeds 10 KB: ' + minifiedBytes + ' bytes');
}
if (!/\b(?:async )?function handler\b/.test(minifiedSource)) {
  throw new Error('minified CloudFront Function must preserve the global handler entry point');
}
var indentedMinified = minifiedSource.trim().split('\n').map(function (line) {
  return '        ' + line;
}).join('\n');
if (templateSource.indexOf(indentedMinified) === -1) {
  throw new Error('CloudFormation template does not embed the current minified FunctionCode');
}

function isAuthorizedWithCredentials(auth, credentials) {
  if (!auth || auth.substring(0, 6).toLowerCase() !== 'basic ') return false;
  var decoded;
  try {
    decoded = Buffer.from(auth.substring(6), 'base64').toString('utf8');
  } catch (e) {
    return false;
  }
  var separator = decoded.indexOf(':');
  if (separator < 1) return false;
  var username = decoded.substring(0, separator);
  var password = decoded.substring(separator + 1);
  var digest = crypto.createHash('sha1').update(password).digest('base64');
  for (var i = 0; i < credentials.length; i++) {
    if (credentials[i].username === username && credentials[i].sha1 === digest) return true;
  }
  return false;
}

var credential = { username: 'preview', sha1: 'nU4eI71bcnBGqeO0t9tXvY1u5oQ=' };
if (!isAuthorizedWithCredentials('Basic ' + Buffer.from('preview:pass').toString('base64'), [credential])) {
  throw new Error('expected SHA-1 htpasswd credential to authorize');
}
if (isAuthorizedWithCredentials('Basic ' + Buffer.from('preview:wrong').toString('base64'), [credential])) {
  throw new Error('expected wrong password to be rejected');
}

if (handlerSource.indexOf("if (authScope.mode === 'ip')") === -1 ||
    handlerSource.indexOf('return forbidden();') === -1) {
  throw new Error('IP-only denial should use the forbidden response');
}

var blockedPathMatch = handlerSource.match(/function isBlockedPath\(uri\) \{([\s\S]*?)\n\}/);
if (!blockedPathMatch) {
  throw new Error('blocked-path helper is missing');
}
var startsWith = function (value, prefix) {
  return value.substring(0, prefix.length) === prefix;
};
var endsWith = function (value, suffix) {
  return value.substring(value.length - suffix.length) === suffix;
};

function extractFunction(name, parameters) {
  var match = handlerSource.match(new RegExp('function ' + name + '\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}'));
  if (!match) {
    throw new Error(name + ' helper is missing');
  }
  return eval('(function ' + name + '(' + parameters + ') {' + match[1] + '\n})');
}

var isBlockedPath = eval('(function isBlockedPath(uri) {' + blockedPathMatch[1] + '\n})');
var blockedPathCases = [
  ['/.htaccess', true],
  ['/.htpasswd', true],
  ['/_control-history/published/config.json', true],
  ['/members/.htaccess', true],
  ['/members/.htpasswd', true],
  ['/members/.htaccess.bak', false],
  ['/public/index.html', false],
];
blockedPathCases.forEach(function (testCase) {
  if (isBlockedPath(testCase[0]) !== testCase[1]) {
    throw new Error('unexpected blocked-path result for ' + testCase[0]);
  }
});

var findScope = extractFunction('findScope', 'uri, scopes, requireEnabled');
var scopeCases = [
  ['/members/page', [
    { pathPrefix: '/members/', enabled: false },
    { pathPrefix: '/', enabled: true },
  ], true, '/'],
  ['/members/page', [
    { pathPrefix: '/members/', enabled: false },
    { pathPrefix: '/', enabled: true },
  ], false, '/members/'],
];
scopeCases.forEach(function (testCase) {
  var scope = findScope(testCase[0], testCase[1], testCase[2]);
  if (!scope || scope.pathPrefix !== testCase[3]) {
    throw new Error('unexpected scope result for ' + testCase[0]);
  }
});

var forbiddenMatch = handlerSource.match(/function forbidden\(\) \{([\s\S]*?)\n\}/);
if (!forbiddenMatch) {
  throw new Error('forbidden response helper is missing');
}
var forbidden = eval('(function forbidden() {' + forbiddenMatch[1] + '\n})');
var forbiddenResponse = forbidden();
if (forbiddenResponse.statusCode !== 403 || forbiddenResponse.statusDescription !== 'Forbidden') {
  throw new Error('IP-only denial should return 403 Forbidden');
}

var unauthorizedMatch = handlerSource.match(/function unauthorized\(maintenance\) \{([\s\S]*?)\n\}/);
if (!unauthorizedMatch) {
  throw new Error('unauthorized response helper is missing');
}
var escapeRealm = function (value) { return value; };
var unauthorized = eval('(function unauthorized(maintenance) {' + unauthorizedMatch[1] + '\n})');
if (unauthorized({ realm: 'Maintenance' }).statusCode !== 401) {
  throw new Error('Basic auth denial should remain 401 Unauthorized');
}

var hasFileExtension = extractFunction('hasFileExtension', 'uri');
var resolveIndexDocument = extractFunction('resolveIndexDocument', 'uri, directoryIndexScopes');
var appendRemainder = extractFunction('appendRemainder', 'uri, from, to');

var appendRemainderCases = [
  ['/old', '/old', '/new', '/new'],
  ['/old/path', '/old/', '/new/', '/new/path'],
  ['/old/path', '/old', '/new', '/new/path'],
  ['/old/path', '/old/', '/new', '/new/path'],
];
appendRemainderCases.forEach(function (testCase) {
  var actual = appendRemainder(testCase[0], testCase[1], testCase[2]);
  if (actual !== testCase[3]) {
    throw new Error('unexpected redirect remainder result: ' + actual);
  }
});

var cases = [
  // [uri, directoryIndexScopes, expectedResolved]
  ['/style.css', [], '/style.css'],
  ['/assets/app.js', [], '/assets/app.js'],
  ['/about', [], '/about'],
  ['/deep/nested', [], '/deep/nested'],
  ['/nonexistent.png', [], '/nonexistent.png'],
  ['/.well-known/foo', [], '/.well-known/foo'],
  ['/foo.', [], '/foo.'],
  ['/v1.2', [], '/v1.2'],
  ['/file.name.with.dots', [], '/file.name.with.dots'],
  ['/report.v2', [], '/report.v2'],
  ['/', [], '/'],
  // DirectoryIndex custom filename cases
  ['/', [{ pathPrefix: '/', names: ['index.php', 'index.html'] }], '/index.php'],
  ['/about/', [{ pathPrefix: '/', names: ['index.php'] }], '/about/index.php'],
  ['/about', [{ pathPrefix: '/', names: ['index.php'] }], '/about/index.php'],
  ['/about/', [{ pathPrefix: '/', names: [] }], '/about/'],
  [
    '/members/',
    [
      { pathPrefix: '/members/', names: [] },
      { pathPrefix: '/', names: ['index.html'] },
    ],
    '/members/',
  ],
  // Most specific scope (longest pathPrefix) wins when scopes overlap.
  [
    '/members/profile',
    [
      { pathPrefix: '/members/', names: ['portal.html'] },
      { pathPrefix: '/', names: ['index.html'] },
    ],
    '/members/profile/portal.html',
  ],
  [
    '/other',
    [
      { pathPrefix: '/members/', names: ['portal.html'] },
      { pathPrefix: '/', names: ['index.html'] },
    ],
    '/other/index.html',
  ],
  // Regression: requesting the scope's own prefix WITHOUT a trailing slash
  // must still match that scope (the directory path must be completed with
  // a trailing slash before comparing against pathPrefix).
  [
    '/members',
    [
      { pathPrefix: '/members/', names: ['portal.html', 'index.html'] },
      { pathPrefix: '/', names: ['index.html'] },
    ],
    '/members/portal.html',
  ],
];

var failures = 0;

var hasFileExtensionCases = [
  ['/style.css', true],
  ['/assets/app.js', true],
  ['/about', false],
  ['/deep/nested', false],
  ['/nonexistent.png', true],
  ['/.well-known/foo', false],
  ['/foo.', false],
  ['/v1.2', true],
  ['/file.name.with.dots', true],
  ['/report.v2', true],
];
for (var h = 0; h < hasFileExtensionCases.length; h++) {
  var huri = hasFileExtensionCases[h][0];
  var expectedHasExt = hasFileExtensionCases[h][1];
  var actualHasExt = hasFileExtension(huri);
  if (actualHasExt !== expectedHasExt) {
    console.log('FAIL hasFileExtension(' + huri + '): expected ' + expectedHasExt + ', got ' + actualHasExt);
    failures++;
  }
}

for (var i = 0; i < cases.length; i++) {
  var uri = cases[i][0];
  var directoryIndexScopes = cases[i][1];
  var expectedResolved = cases[i][2];

  var actualResolved = resolveIndexDocument(uri, directoryIndexScopes);
  if (actualResolved !== expectedResolved) {
    console.log('FAIL resolveIndexDocument(' + uri + '): expected ' + expectedResolved + ', got ' + actualResolved);
    failures++;
  }
}

// (Synchronous hasFileExtension/resolveIndexDocument cases counted into the
// final tally below; intermediate pass/fail is only logged on failure.)

// loadRedirects() logic: reads a meta key for the chunk count, then fetches
// that many chunk keys in parallel and concatenates them. Duplicated here
// (rather than imported) for the same dependency-free reasons as above;
// kvs.get() is replaced with a mock so this can run outside the CloudFront
// Functions runtime.
function makeMockKvs(store) {
  return {
    get: function (key) {
      return new Promise(function (resolve, reject) {
        if (Object.prototype.hasOwnProperty.call(store, key)) {
          resolve(store[key]);
        } else {
          reject(new Error('key not found: ' + key));
        }
      });
    }
  };
}

async function loadKvsJsonWith(kvs, key, fallback) {
  try {
    var raw = await kvs.get(key);
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

async function loadBinPackedRulesWith(kvs, name) {
  var keyPrefix = 'htaccess-' + name;
  var chunkCount = (await loadKvsJsonWith(kvs, keyPrefix + '-meta', { chunkCount: 0 })).chunkCount || 0;
  if (!chunkCount) {
    return [];
  }
  var chunkPromises = [];
  for (var i = 0; i < chunkCount; i++) {
    chunkPromises.push(loadKvsJsonWith(kvs, keyPrefix + '-' + i, []));
  }
  var chunks = await Promise.all(chunkPromises);
  var rules = [];
  for (var j = 0; j < chunks.length; j++) {
    rules = rules.concat(chunks[j]);
  }
  return rules;
}

async function runLoadRedirectsTests() {
  // Case 1: no meta key at all (e.g. a brand new site with zero redirects
  // ever published) falls back to chunkCount 0, yielding an empty list
  // without attempting to fetch any chunk key.
  var noMetaKvs = makeMockKvs({});
  var noMetaResult = await loadBinPackedRulesWith(noMetaKvs, 'redirects');
  if (JSON.stringify(noMetaResult) !== '[]') {
    console.log('FAIL loadRedirects (no meta key): expected [], got ' + JSON.stringify(noMetaResult));
    failures++;
  }

  // Case 2: a single chunk.
  var singleChunkKvs = makeMockKvs({
    'htaccess-redirects-meta': JSON.stringify({ chunkCount: 1 }),
    'htaccess-redirects-0': JSON.stringify([{ from: '/old/', to: '/new/', status: 301 }])
  });
  var singleChunkResult = await loadBinPackedRulesWith(singleChunkKvs, 'redirects');
  if (singleChunkResult.length !== 1 || singleChunkResult[0].from !== '/old/') {
    console.log('FAIL loadRedirects (single chunk): got ' + JSON.stringify(singleChunkResult));
    failures++;
  }

  // Case 3: multiple chunks must be concatenated in chunk-index order.
  var multiChunkKvs = makeMockKvs({
    'htaccess-redirects-meta': JSON.stringify({ chunkCount: 3 }),
    'htaccess-redirects-0': JSON.stringify([{ from: '/a/', to: '/a2/', status: 301 }]),
    'htaccess-redirects-1': JSON.stringify([{ from: '/b/', to: '/b2/', status: 301 }]),
    'htaccess-redirects-2': JSON.stringify([{ from: '/c/', to: '/c2/', status: 301 }])
  });
  var multiChunkResult = await loadBinPackedRulesWith(multiChunkKvs, 'redirects');
  var multiChunkFroms = multiChunkResult.map(function (r) { return r.from; }).join(',');
  if (multiChunkFroms !== '/a/,/b/,/c/') {
    console.log('FAIL loadRedirects (multi chunk order): expected /a/,/b/,/c/, got ' + multiChunkFroms);
    failures++;
  }

  // Case 4: a missing chunk key (should not happen in practice, since the
  // Lambda writes the meta key and all its chunks atomically in a single
  // UpdateKeys call, but the per-chunk fallback must not throw) falls back
  // to an empty array for that chunk rather than aborting the whole load.
  var missingChunkKvs = makeMockKvs({
    'htaccess-redirects-meta': JSON.stringify({ chunkCount: 2 }),
    'htaccess-redirects-0': JSON.stringify([{ from: '/a/', to: '/a2/', status: 301 }])
    // htaccess-redirects-1 intentionally absent
  });
  var missingChunkResult = await loadBinPackedRulesWith(missingChunkKvs, 'redirects');
  if (missingChunkResult.length !== 1 || missingChunkResult[0].from !== '/a/') {
    console.log('FAIL loadRedirects (missing chunk falls back to empty): got ' + JSON.stringify(missingChunkResult));
    failures++;
  }

  // Case 5: the same bin-packed loader used for a different directive type
  // (directory-index) with its own meta/chunk-prefix key names, confirming
  // loadBinPackedRulesWith() is genuinely generic and not accidentally
  // coupled to the "redirects" key names.
  var dirIndexKvs = makeMockKvs({
    'htaccess-directory-index-meta': JSON.stringify({ chunkCount: 2 }),
    'htaccess-directory-index-0': JSON.stringify([{ pathPrefix: '/', names: ['index.html'] }]),
    'htaccess-directory-index-1': JSON.stringify([{ pathPrefix: '/section-0/', names: ['section0-index.html'] }])
  });
  var dirIndexResult = await loadBinPackedRulesWith(dirIndexKvs, 'directory-index');
  var dirIndexPrefixes = dirIndexResult.map(function (r) { return r.pathPrefix; }).join(',');
  if (dirIndexPrefixes !== '/,/section-0/') {
    console.log('FAIL loadBinPackedRulesWith (directory-index key names): expected /,/section-0/, got ' + dirIndexPrefixes);
    failures++;
  }

  var totalCases = hasFileExtensionCases.length + cases.length + blockedPathCases.length +
    scopeCases.length + appendRemainderCases.length + 5;
  if (failures === 0) {
    console.log('All ' + totalCases + ' cases passed.');
    process.exit(0);
  } else {
    console.log(failures + ' failure(s).');
    process.exit(1);
  }
}

runLoadRedirectsTests();

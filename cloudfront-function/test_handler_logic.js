// Minimal Node-based tests for the hasFileExtension()/resolveIndexDocument()
// logic in handler.js. CloudFront Functions runtime is not Node, but this
// logic is plain ES5-ish JS with no CloudFront-specific APIs, so it can be
// unit tested by extracting the function bodies via regex and eval, or by
// duplicating the pure logic here. We duplicate here to keep this test
// dependency-free and avoid parsing the ES module import.

function hasFileExtension(uri) {
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  var lastDotIndex = lastSegment.lastIndexOf('.');

  if (lastDotIndex === -1) {
    return false;
  }
  if (lastDotIndex === 0) {
    return false;
  }
  if (lastDotIndex === lastSegment.length - 1) {
    return false;
  }

  var extension = lastSegment.substring(lastDotIndex + 1);
  return /^[A-Za-z0-9]{1,10}$/.test(extension);
}

function resolveIndexDocument(uri, directoryIndexScopes) {
  if (uri !== '/' && uri.charAt(uri.length - 1) !== '/' && hasFileExtension(uri)) {
    return uri;
  }
  var directoryPath = uri.charAt(uri.length - 1) === '/' ? uri : uri + '/';
  var indexName = firstDirectoryIndexName(directoryPath, directoryIndexScopes || []) || 'index.html';
  if (uri === '/') {
    return '/' + indexName;
  }
  if (uri.charAt(uri.length - 1) === '/') {
    return uri + indexName;
  }
  return uri + '/' + indexName;
}

function firstDirectoryIndexName(uri, directoryIndexScopes) {
  for (var i = 0; i < directoryIndexScopes.length; i++) {
    var scope = directoryIndexScopes[i];
    if (uri.substring(0, scope.pathPrefix.length) === scope.pathPrefix && scope.names && scope.names.length > 0) {
      return scope.names[0];
    }
  }
  return null;
}

var cases = [
  // [uri, directoryIndexScopes, expectedResolved]
  ['/style.css', [], '/style.css'],
  ['/assets/app.js', [], '/assets/app.js'],
  ['/about', [], '/about/index.html'],
  ['/deep/nested', [], '/deep/nested/index.html'],
  ['/nonexistent.png', [], '/nonexistent.png'],
  ['/.well-known/foo', [], '/.well-known/foo/index.html'],
  ['/foo.', [], '/foo./index.html'],
  ['/v1.2', [], '/v1.2'],
  ['/file.name.with.dots', [], '/file.name.with.dots'],
  ['/report.v2', [], '/report.v2'],
  ['/', [], '/index.html'],
  // DirectoryIndex custom filename cases
  ['/', [{ pathPrefix: '/', names: ['index.php', 'index.html'] }], '/index.php'],
  ['/about/', [{ pathPrefix: '/', names: ['index.php'] }], '/about/index.php'],
  ['/about', [{ pathPrefix: '/', names: ['index.php'] }], '/about/index.php'],
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
var KVS_KEY_REDIRECTS_META = 'htaccess-redirects-meta';
var KVS_KEY_REDIRECTS_CHUNK_PREFIX = 'htaccess-redirects-';
var KVS_KEY_DIRECTORY_INDEX_META = 'htaccess-directory-index-meta';
var KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX = 'htaccess-directory-index-';

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

async function loadBinPackedRulesWith(kvs, metaKey, chunkPrefix) {
  var meta = await loadKvsJsonWith(kvs, metaKey, { chunkCount: 0 });
  var chunkCount = meta.chunkCount || 0;
  if (chunkCount === 0) {
    return [];
  }
  var chunkPromises = [];
  for (var i = 0; i < chunkCount; i++) {
    chunkPromises.push(loadKvsJsonWith(kvs, chunkPrefix + i, []));
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
  var noMetaResult = await loadBinPackedRulesWith(noMetaKvs, KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX);
  if (JSON.stringify(noMetaResult) !== '[]') {
    console.log('FAIL loadRedirects (no meta key): expected [], got ' + JSON.stringify(noMetaResult));
    failures++;
  }

  // Case 2: a single chunk.
  var singleChunkKvs = makeMockKvs({
    'htaccess-redirects-meta': JSON.stringify({ chunkCount: 1 }),
    'htaccess-redirects-0': JSON.stringify([{ from: '/old/', to: '/new/', status: 301 }])
  });
  var singleChunkResult = await loadBinPackedRulesWith(singleChunkKvs, KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX);
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
  var multiChunkResult = await loadBinPackedRulesWith(multiChunkKvs, KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX);
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
  var missingChunkResult = await loadBinPackedRulesWith(missingChunkKvs, KVS_KEY_REDIRECTS_META, KVS_KEY_REDIRECTS_CHUNK_PREFIX);
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
  var dirIndexResult = await loadBinPackedRulesWith(
    dirIndexKvs, KVS_KEY_DIRECTORY_INDEX_META, KVS_KEY_DIRECTORY_INDEX_CHUNK_PREFIX
  );
  var dirIndexPrefixes = dirIndexResult.map(function (r) { return r.pathPrefix; }).join(',');
  if (dirIndexPrefixes !== '/,/section-0/') {
    console.log('FAIL loadBinPackedRulesWith (directory-index key names): expected /,/section-0/, got ' + dirIndexPrefixes);
    failures++;
  }

  var totalCases = hasFileExtensionCases.length + cases.length + 5;
  if (failures === 0) {
    console.log('All ' + totalCases + ' cases passed.');
    process.exit(0);
  } else {
    console.log(failures + ' failure(s).');
    process.exit(1);
  }
}

runLoadRedirectsTests();

// Exercise real Python parsing/KVS packing and both shipped Function sources.
var assert = require('assert');
var fs = require('fs');
var vm = require('vm');
var crypto = require('crypto');
var execFileSync = require('child_process').execFileSync;

module.exports = async function () {
  var rootRules = String.raw`DirectoryIndex index.html
Redirect 301 /legacy/ /new/
RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$
RewriteRule ^secret\.html$ /access-error.html [R=302,L]
RewriteCond %{HTTP_REFERER} ^$
RewriteRule ^direct\.html$ /direct-error.html [R=302,L]
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html$
RewriteCond %{HTTP_REFERER} !^https://example\.com/other\.html$
RewriteRule ^multi\.html$ /multi-error.html [R=302,L]
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html$ [NC]
RewriteRule ^case\.html$ /case-error.html [R=302,L]
RewriteCond %{HTTP_REFERER} ^https://example\.com/entrance\.html$
RewriteRule ^positive\.html$ /positive-target.html [R=302,L]
RewriteCond %{HTTP_REFERER} ^$
RewriteRule ^choice\.html$ /missing-error.html [R=302,L]
RewriteRule ^choice\.html$ /fallback.html [R=302,L]`;
  var nestedRules = String.raw`RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$
RewriteRule ^secret\.html$ /nested-error.html [R=302,L]`;
  var files = [['.htaccess', rootRules], ['nested/.htaccess', nestedRules],
    ['members/.htaccess', 'AuthType Basic\nRequire valid-user\n' + nestedRules]];
  var store = JSON.parse(execFileSync('python3', ['-B', '-c',
    'import sys,json; sys.path.insert(0,"lambda"); ' +
    'from htaccess_bridge import build_site_config,split_config_for_kvs; ' +
    'print(json.dumps(split_config_for_kvs(build_site_config(json.load(sys.stdin),' +
    'htpasswd_files={"members/.htpasswd":"preview:{SHA}nU4eI71bcnBGqeO0t9tXvY1u5oQ="}))))'
  ], { cwd: __dirname + '/..', input: JSON.stringify(files), encoding: 'utf8' }));

  var cases = [
    ['/secret.html', null, 302, '/access-error.html'],
    ['/secret.html', '', 302, '/access-error.html'],
    ['/secret.html', 'https://example.com/entrance.html', 0],
    ['/secret.html', 'https://example.com/entrance.html?from=top', 0],
    ['/secret.html', 'https://example.com/other.html', 302, '/access-error.html'],
    ['/secret.html', 'https://exampleXcom/entrance.html', 302, '/access-error.html'],
    ['/secret.html', 'https://example.com/entranceXhtml', 302, '/access-error.html'],
    ['/secret.html', 'https://example.com.evil/entrance.html', 302, '/access-error.html'],
    ['/secret.html', 'https://example.com/entrance.html/evil', 302, '/access-error.html'],
    ['/secretXhtml', null, 0, null, false, '/secretXhtml/index.html'],
    ['/public.html', null, 0],
    ['/access-error.html', null, 0],
    ['/direct.html', null, 302, '/direct-error.html'],
    ['/direct.html', 'https://external.example/', 0],
    ['/multi.html', null, 302, '/multi-error.html'],
    ['/multi.html', 'https://example.com/entrance.html', 0],
    ['/multi.html', 'https://example.com/other.html', 0],
    ['/multi.html', 'https://example.com/denied.html', 302, '/multi-error.html'],
    ['/case.html', 'HTTPS://EXAMPLE.COM/ENTRANCE.HTML', 0],
    ['/case.html', null, 302, '/case-error.html'],
    ['/positive.html', 'https://example.com/entrance.html', 302, '/positive-target.html'],
    ['/positive.html', null, 0],
    ['/choice.html', null, 302, '/missing-error.html'],
    ['/choice.html', 'https://example.com/', 302, '/fallback.html'],
    ['/nested/secret.html', null, 302, '/nested-error.html'],
    ['/nested/secret.html', 'https://example.com/entrance.html', 0],
    ['/nested-error.html', null, 0],
    ['/members/secret.html', null, 401],
    ['/members/secret.html', null, 302, '/nested-error.html', true],
    ['/members/secret.html', 'https://example.com/entrance.html', 0, null, true],
    ['/.htaccess', null, 403],
    ['/legacy/path', null, 301, '/new/path'],
    ['/about/', null, 0, null, false, '/about/index.html'],
  ];
  for (var sourceName of ['handler.js', 'handler.min.js']) {
    var source = fs.readFileSync(__dirname + '/' + sourceName, 'utf8')
      .replace(/import\s+(\w+)\s+from\s*['"]cloudfront['"];?/g, 'var $1 = __cloudfront;')
      .replace(/import\s+(\w+)\s+from\s*['"]crypto['"];?/g, 'var $1 = __crypto;');
    var context = {
      __cloudfront: { kvs: function () { return { get: async function (key) {
        if (!(key in store)) throw new Error('missing key');
        return store[key];
      } }; } },
      __crypto: crypto, Buffer: Buffer,
    };
    vm.runInNewContext(source + '\nthis.__handler = handler;', context);
    for (var testCase of cases) {
      var headers = {};
      if (testCase[1] !== null) headers.referer = { value: testCase[1] };
      if (testCase[4]) headers.authorization = {
        value: 'Basic ' + Buffer.from('preview:pass').toString('base64')
      };
      var response = await context.__handler({ request: { uri: testCase[0], headers: headers },
        viewer: { ip: '198.51.100.20' } });
      var label = sourceName + ': ' + JSON.stringify(testCase);
      assert.strictEqual(response.statusCode || 0, testCase[2], label);
      if (testCase[3]) assert.strictEqual(response.headers.location.value, testCase[3], label);
      if (testCase[2] === 302) {
        assert.strictEqual(response.headers['cache-control'].value, 'no-store', label);
      }
      if (!testCase[2]) assert.strictEqual(response.uri, testCase[5] || testCase[0], label);
    }
  }
  console.log('Referer parser -> KVS -> source/minified Function: ' + cases.length * 2 + ' cases passed.');
};

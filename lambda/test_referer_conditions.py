import json
import unittest

from htaccess_bridge import HtaccessError, build_site_config, parse_htaccess, split_config_for_kvs


class RefererConditionTests(unittest.TestCase):
    def test_preserves_regex_escapes_and_attaches_only_to_next_rule(self):
        config = parse_htaccess(r'''
RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$ [NC]
RewriteRule ^secret\.html$ /access-error.html [R=302,L]
RewriteRule ^old\.html$ /new.html [R=301,L]
''')
        rule, other = config['redirects']
        self.assertEqual(rule['pattern'], r'^secret\.html$')
        self.assertEqual(rule['conditions'], [{
            'pattern': r'^https://example\.com/entrance\.html(\?.*)?$',
            'negate': True, 'ignoreCase': True,
        }])
        self.assertNotIn('conditions', other)

    def test_empty_referer_and_multiple_conditions(self):
        config = parse_htaccess('''RewriteEngine On
RewriteCond %{HTTP_REFERER} ^$
RewriteCond %{HTTP_REFERER} !^https://example.com/ [nc]
RewriteRule ^secret.html$ /error.html [R=302,L]''')
        conditions = config['redirects'][0]['conditions']
        self.assertEqual(len(conditions), 2)
        self.assertFalse(conditions[0]['negate'])
        self.assertTrue(conditions[1]['ignoreCase'])

    def test_nested_scopes_and_conditions_survive_kvs_serialization(self):
        text = r'''RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/ [NC]
RewriteRule ^secret\.html$ /error.html [R=302,L]'''
        config = build_site_config([('.htaccess', text), ('members/.htaccess', text)])
        self.assertEqual([r['basePath'] for r in config['redirects']], ['/members/', '/'])
        puts = split_config_for_kvs(config)
        stored = []
        for i in range(json.loads(puts['htaccess-redirects-meta'])['chunkCount']):
            stored.extend(json.loads(puts[f'htaccess-redirects-{i}']))
        self.assertEqual(stored, config['redirects'])

    def test_same_path_can_have_different_conditions_and_an_unconditional_fallback(self):
        config = parse_htaccess('''RewriteEngine On
RewriteCond %{HTTP_REFERER} ^$
RewriteRule ^secret.html$ /missing.html [R=302,L]
RewriteCond %{HTTP_REFERER} !^https://example.com/
RewriteRule ^secret.html$ /external.html [R=302,L]
RewriteRule ^secret.html$ /fallback.html [R=302,L]''')
        self.assertEqual(len(config['redirects']), 3)

    def test_rejects_unsupported_or_dangling_conditions(self):
        cases = [
            'RewriteCond %{HTTP_HOST} ^example$',
            'RewriteCond %{HTTP_REFERER} ^x [OR]',
            'RewriteCond %{HTTP_REFERER} ^x [NC=true]',
            'RewriteCond %{HTTP_REFERER} !',
            'RewriteCond %{HTTP_REFERER} [',
            'RewriteCond %{HTTP_REFERER} (?i)example',
            'RewriteCond %{HTTP_REFERER} a++',
            r'RewriteCond %{HTTP_REFERER} \Aexample',
            'RewriteCond %{HTTP_REFERER} ' + 'x' * 201,
            'RewriteCond %{HTTP_REFERER} ^$\nDirectoryIndex index.html',
            'RewriteCond %{HTTP_REFERER} ^$\nRewriteEngine Off',
        ]
        for condition in cases:
            with self.subTest(condition=condition), self.assertRaises(HtaccessError):
                parse_htaccess('RewriteEngine On\n' + condition +
                              '\nRewriteRule ^secret.html$ /error.html [R=302,L]')
        with self.assertRaisesRegex(HtaccessError, 'followed by RewriteRule'):
            parse_htaccess('RewriteEngine On\nRewriteCond %{HTTP_REFERER} ^$')
        with self.assertRaisesRegex(HtaccessError, 'RewriteEngine On'):
            parse_htaccess('RewriteCond %{HTTP_REFERER} ^$')

    def test_keeps_redirect_target_and_loop_validation_for_conditioned_rules(self):
        for target in ['/secret.html', '//evil.example', '/\\evil.example',
                       'https://evil.example/error.html']:
            with self.subTest(target=target), self.assertRaises(HtaccessError):
                parse_htaccess('RewriteEngine On\nRewriteCond %{HTTP_REFERER} ^$\n' +
                              r'RewriteRule ^secret\.html$ ' + target + ' [R=302,L]')

    def test_rejects_duplicate_conditioned_rule(self):
        text = '''RewriteCond %{HTTP_REFERER} ^$
RewriteRule ^secret.html$ /error.html [R=302,L]\n'''
        with self.assertRaisesRegex(HtaccessError, 'duplicate'):
            parse_htaccess('RewriteEngine On\n' + text + text)

    def test_quoted_patterns_keep_backslashes(self):
        config = parse_htaccess(r'''RewriteEngine On
RewriteCond "%{HTTP_REFERER}" "!^https://example\.com/" [NC]
RewriteRule "^secret\.html$" "/error.html" [R=302,L]''')
        self.assertEqual(config['redirects'][0]['conditions'][0]['pattern'],
                         r'^https://example\.com/')

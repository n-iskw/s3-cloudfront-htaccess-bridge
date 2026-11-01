import json
import unittest

from htaccess_bridge import HtaccessError, build_site_config, parse_htaccess, split_config_for_kvs


RULES = r'''RewriteEngine On
RewriteCond %{HTTP_REFERER} !^https://example\.com/entrance\.html(\?.*)?$
RewriteRule ^secret\.html$ - [F,L]
'''


class ForbiddenRuleTests(unittest.TestCase):
    def test_forbidden_rule_survives_nested_scope_and_kvs_packing(self):
        config = build_site_config([('.htaccess', RULES), ('nested/.htaccess', RULES)])
        self.assertEqual([r['basePath'] for r in config['redirects']], ['/nested/', '/'])
        rule = config['redirects'][0]
        self.assertEqual(rule['type'], 'forbidden')
        self.assertEqual(rule['status'], 403)
        self.assertEqual(rule['pattern'], r'^secret\.html$')
        self.assertNotIn('to', rule)
        self.assertTrue(rule['conditions'][0]['negate'])
        puts = split_config_for_kvs(config)
        stored = []
        for i in range(json.loads(puts['htaccess-redirects-meta'])['chunkCount']):
            stored.extend(json.loads(puts[f'htaccess-redirects-{i}']))
        self.assertEqual(stored, config['redirects'])

    def test_f_is_terminal_without_explicit_l(self):
        rule = parse_htaccess('RewriteEngine On\nRewriteRule ^private/ - [F]')['redirects'][0]
        self.assertEqual(rule['type'], 'forbidden')

    def test_conditions_reset_for_the_next_rule(self):
        rules = parse_htaccess(RULES + 'RewriteRule ^old$ /new [R=301,L]')['redirects']
        self.assertNotIn('conditions', rules[1])

    def test_rejects_ambiguous_flags_and_substitutions(self):
        for target, flags in [('/404.html', '[F,L]'), ('-', '[F,R=302,L]'),
                              ('-', '[F=403,L]'), ('-', '[F,L=false]'),
                              ('-', '[F,NC]'), ('-', '[F,OR]')]:
            with self.subTest(target=target, flags=flags), self.assertRaises(HtaccessError):
                parse_htaccess(f'RewriteEngine On\nRewriteRule ^secret$ {target} {flags}')

    def test_forbidden_and_redirect_with_identical_conditions_are_duplicates(self):
        with self.assertRaisesRegex(HtaccessError, 'duplicate'):
            parse_htaccess(RULES + RULES.split('\n', 1)[1].replace('- [F,L]', '/404.html [R=302,L]'))

    def test_positive_nc_and_and_conditions(self):
        rule = parse_htaccess('''RewriteEngine On
RewriteCond %{HTTP_REFERER} ^https://example.com/ [NC]
RewriteCond %{HTTP_REFERER} !/allowed$
RewriteRule ^secret$ - [f,l]''')['redirects'][0]
        self.assertEqual(len(rule['conditions']), 2)
        self.assertTrue(rule['conditions'][0]['ignoreCase'])

    def test_error_document_is_left_to_cloudfront(self):
        with self.assertRaisesRegex(HtaccessError, 'unsupported directive'):
            parse_htaccess('ErrorDocument 403 /error.html')


if __name__ == '__main__':
    unittest.main()

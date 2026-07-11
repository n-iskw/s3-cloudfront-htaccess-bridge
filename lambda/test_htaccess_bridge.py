import json
import unittest

from htaccess_bridge import HtaccessError, build_site_config, parse_htaccess, parse_htpasswd


class HtaccessBridgeTests(unittest.TestCase):
    def test_parses_sha1_htpasswd(self):
        self.assertEqual(
            parse_htpasswd("user:{SHA}nU4eI71bcnBGqeO0t9tXvY1u5oQ="),
            [{"username": "user", "sha1": "nU4eI71bcnBGqeO0t9tXvY1u5oQ="}],
        )

    def test_rejects_unsupported_htpasswd_hash(self):
        with self.assertRaisesRegex(HtaccessError, "only Apache SHA-1"):
            parse_htpasswd("user:$2y$05$abcdefghijklmnopqrstuuuuuuuuuuuuuuuuuuuuuuuuuuu")

    def test_auth_user_file_must_be_same_directory_htpasswd(self):
        config = parse_htaccess(
            "AuthType Basic\nAuthName Maintenance\nAuthUserFile .htpasswd\nRequire valid-user"
        )
        self.assertEqual(config["maintenance"]["authUserFile"], ".htpasswd")
        with self.assertRaisesRegex(HtaccessError, "must be .htpasswd"):
            parse_htaccess("AuthUserFile /etc/apache2/.htpasswd")

    def test_parses_basic_auth_and_redirects(self):
        config = parse_htaccess(
            """
            # Maintenance
            AuthType Basic
            AuthName "Members Only"
            Require valid-user
            Require ip 203.0.113.10 198.51.100.0/24

            Redirect 301 /old/ /new/
            RedirectTemp /tmp/ /maintenance/
            """
        )

        self.assertTrue(config["maintenance"]["enabled"])
        self.assertEqual(config["maintenance"]["realm"], "Members Only")
        self.assertEqual(config["maintenance"]["allowIps"], ["203.0.113.10/32", "198.51.100.0/24"])
        self.assertEqual(config["redirects"][0]["status"], 301)
        self.assertEqual(config["redirects"][0]["from"], "/old/")
        self.assertEqual(config["redirects"][1]["status"], 302)

    def test_maintenance_off_without_require_valid_user(self):
        config = parse_htaccess(
            """
            AuthType Basic
            AuthName "Maintenance"
            Redirect 302 /old/ /new/
            """
        )

        self.assertFalse(config["maintenance"]["enabled"])

    def test_rejects_unsupported_directive(self):
        with self.assertRaisesRegex(HtaccessError, "unsupported directive"):
            parse_htaccess("Header set X-Frame-Options DENY")

    def test_rejects_unsupported_auth_and_access_control_directives(self):
        cases = [
            "AuthType Digest",
            "AuthUserFile /path/to/.htpasswd",
            "AuthDigestProvider file",
            "AuthDigestDomain /",
            "AuthDigestNonceLifetime 300",
            "AuthGroupFile /path/to/groups",
            "Require user alice",
            "Require group admin",
            "Require ip 2001:db8::1",
            "Require ip 999.0.0.1",
            "Order allow,deny",
            "Allow from all",
            "Deny from all",
            "Satisfy any",
        ]

        for htaccess in cases:
            with self.subTest(htaccess=htaccess):
                with self.assertRaises(HtaccessError):
                    parse_htaccess(htaccess)

    def test_rejects_unsupported_rewrite_features(self):
        cases = [
            "RewriteCond %{REQUEST_FILENAME} !-f",
            "RewriteBase /app/",
            """
            RewriteEngine On
            RewriteRule ^old/(.*)$ /new/$1 [L]
            """,
            """
            RewriteEngine On
            RewriteRule ^old/(.*)$ /new/$1 [R=301,QSA,L]
            """,
            """
            RewriteEngine On
            RewriteRule ^old/(.*)$ /new/$1 [R=301,NC,L]
            """,
        ]

        for htaccess in cases:
            with self.subTest(htaccess=htaccess):
                with self.assertRaises(HtaccessError):
                    parse_htaccess(htaccess)

    def test_rejects_unsupported_headers_metadata_and_context_directives(self):
        cases = [
            "Header set X-Frame-Options DENY",
            "ExpiresActive On",
            'ExpiresByType text/html "access plus 1 hour"',
            "AddType text/html .html",
            "AddEncoding gzip .gz",
            "Options -Indexes",
            "ErrorDocument 404 /404.html",
            "<Files .htpasswd>",
            "<FilesMatch \"\\.php$\">",
            "<Directory /var/www>",
            "<IfModule mod_rewrite.c>",
            "SetEnv FOO bar",
            "SetEnvIf Request_URI ^/foo FOO=bar",
        ]

        for htaccess in cases:
            with self.subTest(htaccess=htaccess):
                with self.assertRaises(HtaccessError):
                    parse_htaccess(htaccess)

    def test_rejects_external_redirect_without_allowlist(self):
        with self.assertRaisesRegex(HtaccessError, "not allowed"):
            parse_htaccess("Redirect 301 /old/ https://example.com/new/")

    def test_allows_external_redirect_with_allowlist(self):
        config = parse_htaccess(
            "Redirect 301 /old/ https://example.com/new/",
            allowed_external_hosts=["example.com"],
        )

        self.assertEqual(config["redirects"][0]["to"], "https://example.com/new/")

    def test_rejects_obvious_loop(self):
        with self.assertRaisesRegex(HtaccessError, "loop"):
            parse_htaccess("Redirect 301 /old/ /old/new/")

    def test_rejects_redirect_chain_loop(self):
        with self.assertRaisesRegex(HtaccessError, "loop"):
            parse_htaccess(
                """
                Redirect 301 /a/ /b/
                Redirect 301 /b/ /a/
                """
            )

    def test_rejects_redirect_chain_loop_across_nested_htaccess_files(self):
        with self.assertRaisesRegex(HtaccessError, "loop"):
            build_site_config(
                [
                    (".htaccess", "Redirect 301 /a/ /members/"),
                    ("members/.htaccess", "Redirect 301 /members/ /a/"),
                ]
            )

    def test_parses_simple_rewrite_redirect(self):
        config = parse_htaccess(
            """
            RewriteEngine On
            RewriteRule ^docs/(.*)$ /manual/$1 [R=301,L]
            """
        )

        self.assertEqual(config["redirects"][0]["type"], "rewrite")
        self.assertEqual(config["redirects"][0]["pattern"], "^docs/(.*)$")
        self.assertEqual(config["redirects"][0]["status"], 301)

    def test_rejects_rewrite_without_l_flag(self):
        with self.assertRaisesRegex(HtaccessError, r"\[L\]"):
            parse_htaccess(
                """
                RewriteEngine On
                RewriteRule ^docs/(.*)$ /manual/$1 [R=301]
                """
            )

    def test_rejects_rewrite_self_loop(self):
        with self.assertRaisesRegex(HtaccessError, "loop"):
            parse_htaccess(
                """
                RewriteEngine On
                RewriteRule ^old/(.*)$ /old/$1 [R=301,L]
                """
            )

    def test_output_is_json_serializable(self):
        config = parse_htaccess("Redirect 302 /campaign-old/ /campaign/")
        json.dumps(config)

    def test_builds_site_config_from_multiple_htaccess_files(self):
        config = build_site_config(
            [
                (
                    ".htaccess",
                    """
                    AuthType Basic
                    AuthName "Global"
                    Require valid-user
                    Redirect 301 /old/ /new/
                    """,
                ),
                (
                    "members/.htaccess",
                    """
                    AuthType Basic
                    AuthName "Members"
                    Require valid-user
                    RewriteEngine On
                    RewriteRule ^docs/(.*)$ /members/manual/$1 [R=301,L]
                    """,
                ),
            ]
        )

        self.assertEqual(config["authScopes"][0]["pathPrefix"], "/members/")
        self.assertEqual(config["authScopes"][0]["realm"], "Members")
        self.assertEqual(config["authScopes"][1]["pathPrefix"], "/")
        self.assertEqual(config["redirects"][0]["basePath"], "/members/")
        self.assertEqual(config["redirects"][1]["from"], "/old/")

    def test_builds_auth_scope_with_same_directory_htpasswd(self):
        config = build_site_config(
            [("members/.htaccess", "AuthType Basic\nRequire valid-user")],
            htpasswd_files={
                "members/.htpasswd": "user:{SHA}nU4eI71bcnBGqeO0t9tXvY1u5oQ="
            },
        )
        self.assertEqual(config["authScopes"][0]["credentials"][0]["username"], "user")

    def test_ignores_unreferenced_htpasswd(self):
        config = build_site_config(
            [(".htaccess", "Redirect 302 /old/ /new/")],
            htpasswd_files={"unused/.htpasswd": "not-a-valid-record"},
        )
        self.assertEqual(config["authScopes"], [])

    def test_builds_empty_site_config_when_all_htaccess_files_are_deleted(self):
        config = build_site_config([])

        self.assertFalse(config["maintenance"]["enabled"])
        self.assertEqual(config["authScopes"], [])
        self.assertEqual(config["redirects"], [])
        self.assertEqual(config["directoryIndexScopes"], [])

    def test_parses_directory_index_single_name(self):
        config = parse_htaccess("DirectoryIndex index.php")
        self.assertEqual(config["directoryIndex"], ["index.php"])

    def test_parses_directory_index_multiple_names_priority_order(self):
        config = parse_htaccess("DirectoryIndex index.html index.htm default.html")
        self.assertEqual(config["directoryIndex"], ["index.html", "index.htm", "default.html"])

    def test_directory_index_disabled(self):
        config = parse_htaccess("DirectoryIndex disabled")
        self.assertEqual(config["directoryIndex"], [])

    def test_multiple_directory_index_directives_add_to_list(self):
        config = parse_htaccess(
            """
            DirectoryIndex index.html
            DirectoryIndex index.php
            """
        )
        self.assertEqual(config["directoryIndex"], ["index.html", "index.php"])

    def test_directory_index_disabled_then_reenabled_resets_list(self):
        config = parse_htaccess(
            """
            DirectoryIndex index.html
            DirectoryIndex disabled
            DirectoryIndex index.php
            """
        )
        self.assertEqual(config["directoryIndex"], ["index.php"])

    def test_rejects_directory_index_disabled_with_other_arguments(self):
        with self.assertRaisesRegex(HtaccessError, "disabled"):
            parse_htaccess("DirectoryIndex disabled index.html")

    def test_rejects_directory_index_with_path_traversal(self):
        with self.assertRaisesRegex(HtaccessError, "not a path"):
            parse_htaccess("DirectoryIndex ../secret.html")

    def test_rejects_directory_index_with_dot_dot_no_slash(self):
        with self.assertRaisesRegex(HtaccessError, "must not contain"):
            parse_htaccess("DirectoryIndex a..html")

    def test_rejects_directory_index_with_slash(self):
        with self.assertRaisesRegex(HtaccessError, "not a path"):
            parse_htaccess("DirectoryIndex /cgi-bin/index.pl")

    def test_rejects_directory_index_with_no_arguments(self):
        with self.assertRaisesRegex(HtaccessError, "expects at least one argument"):
            parse_htaccess("DirectoryIndex")

    def test_builds_site_config_with_directory_index_scope(self):
        config = build_site_config(
            [
                (".htaccess", "DirectoryIndex index.html"),
                ("members/.htaccess", "DirectoryIndex portal.html index.html"),
            ]
        )
        self.assertEqual(len(config["directoryIndexScopes"]), 2)
        # Most specific (longest) pathPrefix must come first.
        self.assertEqual(config["directoryIndexScopes"][0]["pathPrefix"], "/members/")
        self.assertEqual(config["directoryIndexScopes"][0]["names"], ["portal.html", "index.html"])
        self.assertEqual(config["directoryIndexScopes"][1]["pathPrefix"], "/")
        self.assertEqual(config["directoryIndexScopes"][1]["names"], ["index.html"])


if __name__ == "__main__":
    unittest.main()

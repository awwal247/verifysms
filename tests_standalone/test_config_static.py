"""Security-relevant configuration checked by reading source text (no Django import)."""
import re
import unittest

from tests_standalone._common import read

SETTINGS = read("verify_sms/settings.py")
MIDDLEWARE = read("core/middleware.py")


class SettingsDefaults(unittest.TestCase):
    def test_secure_cookie_and_header_settings_present(self):
        for line in ("SESSION_COOKIE_HTTPONLY = True", 'SESSION_COOKIE_SAMESITE = "Lax"',
                     "SESSION_COOKIE_SECURE = HTTPS_ENFORCED", "CSRF_COOKIE_SECURE = HTTPS_ENFORCED",
                     "SECURE_CONTENT_TYPE_NOSNIFF = True", 'X_FRAME_OPTIONS = "DENY"',
                     'SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"'):
            self.assertIn(line, SETTINGS)

    def test_app_refuses_to_start_without_secret_key_in_production(self):
        self.assertIn("raise ImproperlyConfigured", SETTINGS)
        self.assertRegex(SETTINGS, r"if not SECRET_KEY:\s+if DEBUG:")

    def test_wildcard_host_stripped_outside_debug(self):
        self.assertIn('h != "*"', SETTINGS)

    def test_security_middleware_order(self):
        order = re.findall(r'^\s+"([\w.]+)",', SETTINGS.split("MIDDLEWARE = [")[1].split("]")[0], flags=re.M)
        self.assertEqual(order[0], "django.middleware.security.SecurityMiddleware")
        for a, b in (("SessionMiddleware", "AuthenticationMiddleware"),
                     ("CsrfViewMiddleware", "AuthenticationMiddleware"),
                     ("AuthenticationMiddleware", "ActiveUserMiddleware")):
            ia = next(i for i, m in enumerate(order) if m.endswith(a))
            ib = next(i for i, m in enumerate(order) if m.endswith(b))
            self.assertLess(ia, ib, f"{a} must precede {b}")

    def test_password_validators_configured(self):
        for v in ("MinimumLengthValidator", "CommonPasswordValidator", "NumericPasswordValidator",
                  "UserAttributeSimilarityValidator"):
            self.assertIn(v, SETTINGS)

    def test_database_falls_back_to_sqlite_and_postgres_requires_ssl_by_default(self):
        self.assertIn("django.db.backends.sqlite3", SETTINGS)
        self.assertIn('os.getenv("DB_SSL_REQUIRE", "1")', SETTINGS)

    def test_request_size_limits(self):
        self.assertRegex(SETTINGS, r"DATA_UPLOAD_MAX_MEMORY_SIZE\s*=\s*1024\s*\*\s*1024")

    def test_hsts_starts_conservatively(self):
        self.assertIn('os.getenv("SECURE_HSTS_SECONDS", "86400")', SETTINGS)
        self.assertIn('_flag("SECURE_HSTS_PRELOAD", "0")', SETTINGS)


class MiddlewareSource(unittest.TestCase):
    def test_csp_has_no_unsafe_eval_and_no_wildcards(self):
        directives = MIDDLEWARE.split("directives = [")[1].split("]")[0]
        self.assertNotIn("unsafe-eval", directives)
        self.assertNotRegex(directives, r"\s\*[\s\"';]")

    def test_csp_locks_down_framing_plugins_base_and_forms(self):
        for d in ("object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'", "default-src 'self'"):
            self.assertIn(d, MIDDLEWARE)

    def test_only_known_third_party_hosts(self):
        hosts = set(re.findall(r'"(https://[^"]+)"', MIDDLEWARE))
        self.assertEqual(hosts, {"https://cdnjs.cloudflare.com", "https://js.paystack.co",
                                 "https://checkout.paystack.com", "https://api.paystack.co"})

    def test_headers_are_never_overwritten_and_private_paths_listed(self):
        self.assertIn("def _set_once", MIDDLEWARE)
        for prefix in ("/user/", "/admin/", "/api/", "/login.html"):
            self.assertIn(f'"{prefix}"', MIDDLEWARE)

    def test_permissions_policy_disables_sensors(self):
        for feature in ("camera=()", "microphone=()", "geolocation=()"):
            self.assertIn(feature, MIDDLEWARE)


class ViewsSecurityStatic(unittest.TestCase):
    VIEWS = read("core/views.py")

    def test_no_raw_sql_string_formatting(self):
        self.assertNotRegex(self.VIEWS, r"\.(raw|execute)\(\s*f[\"']")
        self.assertNotRegex(self.VIEWS, r"\.extra\(")

    def test_no_mark_safe_on_request_data(self):
        self.assertNotIn("mark_safe(request", self.VIEWS)

    def test_login_redirect_target_is_validated(self):
        self.assertIn("url_has_allowed_host_and_scheme", self.VIEWS)

    def test_throttle_constant_defined(self):
        self.assertRegex(self.VIEWS, r"LOGIN_MAX_FAILURES\s*=\s*\d+")

    def test_deploy_setup_uses_constant_time_compare(self):
        self.assertIn("compare_digest", self.VIEWS)

    def test_wallet_mutations_are_atomic(self):
        services = read("core/services.py")
        self.assertIn("select_for_update", services)
        self.assertIn("transaction.atomic", services)


if __name__ == "__main__":
    unittest.main()

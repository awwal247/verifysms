"""Tests that REQUIRE the installed dependencies (Django, dj-database-url, psycopg).

Run:  DJANGO_DEBUG=1 python manage.py test core.tests.test_dependencies -v 2
      (or  python scripts/run_tests.py --full)

Complements test_security.py and test_verification.py with: settings under different
environments (run in a clean subprocess so the test process settings stay untouched),
database configuration, migrations on a blank database, ``check --deploy``, models,
middleware, service helpers, admin actions and maintenance mode. No network.
"""
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings

from core import services
from core.middleware import ActiveUserMiddleware, PERMISSIONS_POLICY, build_csp
from core.models import Coupon, Setting, Topup, Transaction, User

ROOT = Path(__file__).resolve().parent.parent.parent
PW = "Str0ng-Pass-Phrase!"


def run_python(code, env_extra=None, cwd=ROOT):
    """Run ``code`` in a fresh interpreter with a controlled environment."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DJANGO_", "DATABASE_", "SITE_", "ALLOWED_", "VERCEL", "REDIS_", "SECURE_", "CSP_", "TRUST_"))}
    env["DJANGO_SETTINGS_MODULE"] = "verify_sms.settings"
    env.update(env_extra or {})
    code = "import django; django.setup(); " + code
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(cwd), timeout=120)


def settings_as_json(env_extra, *names):
    code = ("import json, django; from django.conf import settings; "
            f"print(json.dumps({{n: getattr(settings, n) for n in {list(names)!r}}}, default=str))")
    result = run_python(code, env_extra)
    return result, (json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else None)


import secrets
KEY = secrets.token_urlsafe(64)


class SettingsEnvironments(TestCase):
    """verify_sms.settings evaluated under different environments."""

    def test_production_without_secret_key_refuses_to_start(self):
        r = run_python("from django.conf import settings; settings.SECRET_KEY", {"DJANGO_DEBUG": "0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is not set", r.stderr)

    def test_debug_without_secret_key_starts_with_dev_key(self):
        r, data = settings_as_json({"DJANGO_DEBUG": "1"}, "DEBUG", "SECRET_KEY")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(data["DEBUG"])
        self.assertEqual(data["SECRET_KEY"], "dev-only-change-me")

    def test_debug_defaults_to_off(self):
        r, data = settings_as_json({"DJANGO_SECRET_KEY": KEY}, "DEBUG")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(data["DEBUG"])

    def test_https_site_url_turns_on_secure_flags(self):
        r, d = settings_as_json({"DJANGO_SECRET_KEY": KEY, "SITE_URL": "https://verifysms.example"},
                                "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT",
                                "SECURE_HSTS_SECONDS", "CSRF_TRUSTED_ORIGINS")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(d["SESSION_COOKIE_SECURE"] and d["CSRF_COOKIE_SECURE"] and d["SECURE_SSL_REDIRECT"])
        self.assertEqual(d["SECURE_HSTS_SECONDS"], 86400)
        self.assertIn("https://verifysms.example", d["CSRF_TRUSTED_ORIGINS"])

    def test_http_site_url_keeps_cookies_non_secure_and_no_hsts(self):
        r, d = settings_as_json({"DJANGO_SECRET_KEY": KEY, "SITE_URL": "http://127.0.0.1:8000", "ALLOWED_HOSTS": "127.0.0.1"},
                                "SESSION_COOKIE_SECURE", "SECURE_SSL_REDIRECT", "SECURE_HSTS_SECONDS")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(d["SESSION_COOKIE_SECURE"] or d["SECURE_SSL_REDIRECT"])
        self.assertEqual(d["SECURE_HSTS_SECONDS"], 0)

    def test_wildcard_host_removed_and_hosts_derived_from_site_url(self):
        r, d = settings_as_json({"DJANGO_SECRET_KEY": KEY, "ALLOWED_HOSTS": "*", "SITE_URL": "https://verifysms.example"}, "ALLOWED_HOSTS")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("*", d["ALLOWED_HOSTS"])
        self.assertIn("verifysms.example", d["ALLOWED_HOSTS"])
        self.assertIn("www.verifysms.example", d["ALLOWED_HOSTS"])

    def test_vercel_hostnames_are_allowed_and_proxy_header_trusted(self):
        r, d = settings_as_json({"DJANGO_SECRET_KEY": KEY, "SITE_URL": "https://verifysms.example", "VERCEL": "1",
                                 "VERCEL_URL": "verify-abc.vercel.app"}, "ALLOWED_HOSTS", "SECURE_PROXY_SSL_HEADER")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verify-abc.vercel.app", d["ALLOWED_HOSTS"])
        self.assertEqual(d["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])

    def test_proxy_header_not_trusted_by_default_off_vercel(self):
        r, d = settings_as_json({"DJANGO_SECRET_KEY": KEY, "SITE_URL": "https://verifysms.example"}, "SECURE_PROXY_SSL_HEADER")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(d["SECURE_PROXY_SSL_HEADER"])

    def test_sqlite_is_the_fallback_database(self):
        r, d = settings_as_json({"DJANGO_DEBUG": "1"}, "DATABASES")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(d["DATABASES"]["default"]["ENGINE"].endswith("sqlite3"))

    def test_database_url_selects_postgres_with_ssl_required(self):
        url = "postgresql://postgres.abc:pw@aws-0-eu.pooler.supabase.com:6543/postgres"
        r, d = settings_as_json({"DJANGO_DEBUG": "1", "DATABASE_URL": url}, "DATABASES")
        self.assertEqual(r.returncode, 0, r.stderr)
        db = d["DATABASES"]["default"]
        self.assertIn("postgresql", db["ENGINE"])
        self.assertEqual((db["HOST"], str(db["PORT"]), db["NAME"]), ("aws-0-eu.pooler.supabase.com", "6543", "postgres"))
        self.assertEqual(db["OPTIONS"]["sslmode"], "require")
        self.assertEqual(db["CONN_MAX_AGE"], 60)

    def test_ssl_can_be_disabled_explicitly_for_local_postgres(self):
        r, d = settings_as_json({"DJANGO_DEBUG": "1", "DATABASE_URL": "postgresql://u:p@localhost:5432/db", "DB_SSL_REQUIRE": "0"}, "DATABASES")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("sslmode", d["DATABASES"]["default"].get("OPTIONS", {}))

    def test_psycopg_driver_is_importable(self):
        import psycopg  # noqa: F401  (requirements.txt: psycopg[binary])

    def test_redis_cache_only_when_url_set(self):
        r, d = settings_as_json({"DJANGO_DEBUG": "1", "REDIS_URL": "redis://localhost:6379/0"}, "CACHES")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RedisCache", d["CACHES"]["default"]["BACKEND"])
        r, d = settings_as_json({"DJANGO_DEBUG": "1"}, "CACHES")
        self.assertNotIn("RedisCache", d["CACHES"]["default"]["BACKEND"])


class DeploymentChecks(TestCase):
    def test_check_deploy_reports_only_the_two_optional_hsts_hardening_warnings(self):
        r = run_python("from django.core.management import call_command; call_command('check', deploy=True)",
                       {"DJANGO_SECRET_KEY": KEY, "SITE_URL": "https://verifysms.example"})
        self.assertEqual(r.returncode, 0, r.stderr)
        warnings = {line.split(")")[0].split("(")[-1] for line in r.stderr.splitlines() if "security.W" in line}
        self.assertLessEqual(warnings, {"security.W005", "security.W021"}, r.stderr)

    def test_migrations_apply_to_a_blank_database_and_are_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("from django.core.management import call_command; import django; "
                    "from django.conf import settings; "
                    "settings.DATABASES['default']['NAME'] = r'%s'; "
                    "call_command('migrate', verbosity=0); call_command('makemigrations', check=True, dry_run=True, verbosity=0); "
                    "from core.models import User; print('users', User.objects.count())") % (Path(tmp) / "blank.sqlite3")
            r = run_python(code, {"DJANGO_DEBUG": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("users 0", r.stdout)

    def test_manage_py_check_passes(self):
        r = subprocess.run([sys.executable, "manage.py", "check"], capture_output=True, text=True, cwd=str(ROOT),
                           env={**os.environ, "DJANGO_DEBUG": "1"}, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_wsgi_entrypoint_exposes_application(self):
        r = run_python("from verify_sms.wsgi import application; print(callable(application))", {"DJANGO_DEBUG": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("True", r.stdout)


class ModelTests(TestCase):
    def test_create_user_requires_email_and_hashes_password(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password=PW)
        u = User.objects.create_user(email="Mixed@EXAMPLE.com", password=PW)
        self.assertEqual(u.email, "Mixed@example.com")          # domain lower-cased by Django
        self.assertNotEqual(u.password, PW)
        self.assertTrue(u.check_password(PW))

    def test_email_is_unique(self):
        from django.db import IntegrityError, transaction
        User.objects.create_user(email="a@example.com", password=PW)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="a@example.com", password=PW)

    def test_defaults_and_staff_flag_follow_role(self):
        u = User.objects.create_user(email="u@example.com", password=PW)
        self.assertEqual((u.role, u.status, u.balance, u.is_staff), ("user", "active", Decimal("0.00"), False))
        u.role = "admin"; u.save()
        self.assertTrue(u.is_staff)

    def test_create_superuser(self):
        s = User.objects.create_superuser(email="root@example.com", password=PW)
        self.assertEqual((s.role, s.is_staff, s.is_superuser), ("admin", True, True))

    def test_setting_roundtrip_default_and_update(self):
        self.assertEqual(Setting.value("nope", "dflt"), "dflt")
        Setting.set_value("k", 5, "desc")
        Setting.set_value("k", 6)
        self.assertEqual(Setting.value("k"), "6")
        self.assertEqual(Setting.objects.filter(setting_key="k").count(), 1)

    def test_topup_fee_rate_display(self):
        u = User.objects.create_user(email="t@example.com", password=PW)
        t = Topup.objects.create(user=u, amount_paid=Decimal("2000"), fee_amount=Decimal("120"), net_credited=Decimal("1880"), reference="R")
        self.assertEqual(t.fee_rate, "6.0")
        t.amount_paid = Decimal("0")
        self.assertEqual(t.fee_rate, "")

    def test_ledger_row_is_removed_with_user(self):
        u = User.objects.create_user(email="d@example.com", password=PW, balance=Decimal("10"))
        services.credit_balance(u.id, "5", "adjustment", "x")
        self.assertEqual(Transaction.objects.count(), 1)
        u.delete()
        self.assertEqual(Transaction.objects.count(), 0)


class MiddlewareAndHeadersTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_build_csp_options(self):
        self.assertNotIn("upgrade-insecure-requests", build_csp())
        self.assertIn("upgrade-insecure-requests", build_csp(upgrade_insecure_requests=True))
        self.assertTrue(build_csp(report_uri="/csp").endswith("report-uri /csp"))

    def test_permissions_policy_payment_limited_to_paystack(self):
        self.assertIn('payment=(self "https://checkout.paystack.com")', PERMISSIONS_POLICY)

    def test_view_set_header_is_not_overwritten(self):
        from django.http import HttpResponse
        from core.middleware import SecurityHeadersMiddleware
        mw = SecurityHeadersMiddleware(lambda req: HttpResponse("x", headers={"Cache-Control": "public, max-age=60"}))
        r = mw(RequestFactory().get("/user/dashboard.html"))
        self.assertEqual(r["Cache-Control"], "public, max-age=60")
        self.assertIn("Content-Security-Policy", r)

    def test_public_pages_are_not_marked_no_store(self):
        r = Client().get("/")
        self.assertNotIn("no-store", r.get("Cache-Control", ""))

    def test_suspended_user_is_logged_out_with_json_403_on_api(self):
        u = User.objects.create_user(email="s@example.com", password=PW, status="suspended")
        c = Client(); c.force_login(u)
        r = c.get("/api/coupon/?action=validate&code=X")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()["success"])
        self.assertNotIn("_auth_user_id", c.session)

    def test_banned_user_on_html_page_redirected_to_login(self):
        u = User.objects.create_user(email="b@example.com", password=PW, status="banned")
        c = Client(); c.force_login(u)
        r = c.get("/user/dashboard.html")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login.html", r["Location"])

    def test_active_user_middleware_ignores_anonymous(self):
        from django.http import HttpResponse
        from django.contrib.auth.models import AnonymousUser
        req = RequestFactory().get("/api/x"); req.user = AnonymousUser()
        self.assertEqual(ActiveUserMiddleware(lambda r: HttpResponse("ok"))(req).content, b"ok")

    @override_settings(CSP_REPORT_URI="https://example.test/report")
    def test_csp_report_uri_wired_from_settings(self):
        from core.middleware import SecurityHeadersMiddleware
        from django.http import HttpResponse
        r = SecurityHeadersMiddleware(lambda q: HttpResponse("x"))(RequestFactory().get("/"))
        self.assertIn("report-uri https://example.test/report", r["Content-Security-Policy"])


class ServiceHelperTests(TestCase):
    def test_format_ngn_uses_currency_setting_and_grouping(self):
        self.assertEqual(services.format_ngn("1234567.891"), "₦1,234,567.89")
        Setting.set_value("currency_symbol", "$")
        self.assertEqual(services.format_ngn(5), "$5.00")

    def test_setting_number_falls_back_on_garbage(self):
        Setting.set_value("n", "x")
        self.assertEqual(services.setting_number("n", 7), Decimal("7"))
        Setting.set_value("n", "2.5")
        self.assertEqual(services.setting_number("n", 7), Decimal("2.5"))

    def test_server_provider_mapping(self):
        sp = services.server_provider
        self.assertEqual((sp("1"), sp("2"), sp("smsbus"), sp(" 5SIM ")), ("smsbus", "5sim", "smsbus", "5sim"))
        for nothing in ("", "any", "ALL", "auto", None):
            self.assertIsNone(sp(nothing))
        self.assertIsNone(sp("99"))

    def test_demo_mode_follows_keys(self):
        with override_settings(FIVESIM_API_KEY="", SMSBUS_API_KEY=""):
            self.assertTrue(services.demo_mode())
        with override_settings(SMSBUS_API_KEY="abc"):
            self.assertFalse(services.demo_mode())
            self.assertTrue(services.provider_configured()["smsbus"])

    def test_credit_then_debit_leaves_consistent_ledger(self):
        u = User.objects.create_user(email="l@example.com", password=PW)
        services.credit_balance(u.id, "100", "topup", "t", "REF")
        services.debit_balance(u.id, "40", "buy")
        rows = list(Transaction.objects.filter(user=u).order_by("id"))
        self.assertEqual([r.balance_after for r in rows], [Decimal("100.00"), Decimal("60.00")])
        self.assertEqual(rows[1].balance_before, rows[0].balance_after)

    def test_friendly_error_default_message(self):
        self.assertEqual(services.friendly_provider_error(None), "An error occurred. Please try again.")
        self.assertIn("Connection to provider failed", services.friendly_provider_error("HTTP_404"))


class AdminActionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(email="adm@example.com", password=PW, role="admin")
        self.user = User.objects.create_user(email="cust@example.com", password=PW, balance=Decimal("100"))
        self.c = Client(); self.c.force_login(self.admin)

    def post(self, **data):
        return self.c.post("/admin/action/", data)

    def test_credit_and_debit_adjustments_write_ledger(self):
        self.post(action="adjust_balance", user_id=self.user.id, amount="50", adjust_type="credit")
        self.post(action="adjust_balance", user_id=self.user.id, amount="30", adjust_type="debit")
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("120.00"))
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 2)

    def test_debit_beyond_balance_refused(self):
        self.post(action="adjust_balance", user_id=self.user.id, amount="500", adjust_type="debit")
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("100.00"))

    def test_non_positive_amount_refused(self):
        for amt in ("0", "-5", "abc"):
            self.post(action="adjust_balance", user_id=self.user.id, amount=amt)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("100.00"))

    def test_ban_activate_delete_user(self):
        self.post(action="ban", user_id=self.user.id)
        self.user.refresh_from_db(); self.assertEqual(self.user.status, "banned")
        self.post(action="activate", user_id=self.user.id)
        self.user.refresh_from_db(); self.assertEqual(self.user.status, "active")
        self.post(action="delete", user_id=self.user.id)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_admin_cannot_adjust_another_admin_via_user_id(self):
        other = User.objects.create_user(email="adm2@example.com", password=PW, role="admin")
        self.assertEqual(self.post(action="adjust_balance", user_id=other.id, amount="5").status_code, 404)

    def test_save_settings_only_writes_whitelisted_keys(self):
        self.post(action="save_settings", site_name="Acme", is_superuser_flag="1", **{"evil_key": "x"})
        self.assertEqual(Setting.value("site_name"), "Acme")
        self.assertFalse(Setting.objects.filter(setting_key="evil_key").exists())

    def test_blank_provider_key_does_not_erase_saved_key(self):
        Setting.set_value("fivesim_api_key", "saved")
        self.post(action="save_provider", fivesim_api_key="", provider_api_key="")
        self.assertEqual(Setting.value("fivesim_api_key"), "saved")
        self.assertEqual(Setting.value("active_provider"), "auto")

    def test_blank_paystack_secret_does_not_erase_saved_secret(self):
        Setting.set_value("paystack_secret_key", "sk_saved")
        self.post(action="save_payment", paystack_public_key="pk", paystack_secret_key="")
        self.assertEqual(Setting.value("paystack_secret_key"), "sk_saved")

    def test_coupon_create_duplicate_toggle_delete(self):
        self.post(action="create_coupon", code=" save10 ", discount_type="percent", discount_value="10")
        self.assertTrue(Coupon.objects.filter(code="SAVE10").exists())
        self.post(action="create_coupon", code="SAVE10", discount_type="percent", discount_value="20")
        self.assertEqual(Coupon.objects.filter(code="SAVE10").count(), 1)
        self.post(action="create_coupon", code="", discount_type="percent", discount_value="10")
        self.assertEqual(Coupon.objects.count(), 1)

    def test_secrets_never_rendered_back_on_admin_pages(self):
        Setting.set_value("paystack_secret_key", "SECRET-MARKER-PAYSTACK-9981")
        Setting.set_value("fivesim_api_key", "FIVESIM_SECRET_TOKEN_123")
        for page in ("payment", "providers", "settings"):
            body = self.c.get(f"/admin/{page}.html").content.decode()
            self.assertNotIn("SECRET-MARKER-PAYSTACK-9981", body, page)
            self.assertNotIn("FIVESIM_SECRET_TOKEN_123", body, page)


class MaintenanceAndErrorPages(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="m@example.com", password=PW)
        self.admin = User.objects.create_user(email="ma@example.com", password=PW, role="admin")

    def test_maintenance_blocks_customers_but_not_admins(self):
        Setting.set_value("maintenance_mode", "1")
        c = Client(); c.force_login(self.user)
        self.assertTemplateUsed(c.get("/user/dashboard.html"), "maintenance.html")
        a = Client(); a.force_login(self.admin)
        self.assertTemplateUsed(a.get("/user/dashboard.html"), "user/dashboard.html")

    def test_health_endpoint_works_in_maintenance(self):
        Setting.set_value("maintenance_mode", "1")
        self.assertEqual(Client().get("/healthz/").status_code, 200)

    def test_unknown_user_page_uses_404_template(self):
        c = Client(); c.force_login(self.user)
        self.assertTemplateUsed(c.get("/user/nothing.html"), "404.html")

    def test_404_page_links_assets_from_the_site_root(self):
        c = Client(); c.force_login(self.user)
        body = c.get("/user/nothing.html").content.decode()
        self.assertIn('href="/assets/css/style.css"', body)
        self.assertNotIn('href="assets/', body)

    def test_unknown_admin_page_is_404_not_500(self):
        """Regression: admin_page used to raise TemplateDoesNotExist (HTTP 500) for unknown pages."""
        a = Client(); a.force_login(self.admin)
        self.assertEqual(a.get("/admin/nothing.html").status_code, 404)


class StaticAssetsServed(TestCase):
    def test_static_url_prefix_and_dirs(self):
        from django.conf import settings
        self.assertEqual(settings.STATIC_URL, "/assets/")
        self.assertTrue(any(Path(p) == ROOT / "assets" for p in settings.STATICFILES_DIRS))

    def test_check_templates_all_compile(self):
        from django.template.loader import get_template
        for tpl in (ROOT / "templates").rglob("*.html"):
            get_template(str(tpl.relative_to(ROOT / "templates")))

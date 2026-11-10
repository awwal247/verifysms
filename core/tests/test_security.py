"""Security regression tests.   Run:  DJANGO_DEBUG=1 python manage.py test core

(DJANGO_DEBUG=1 or a DJANGO_SECRET_KEY must be set, otherwise settings refuse to load.)
"""
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core import views
from core.middleware import PERMISSIONS_POLICY, build_csp
from core.models import Setting, Transaction, User
from core.services import Paystack
from core.well_known import build_security_txt

ROOT = Path(__file__).resolve().parent.parent.parent


def make_user(email="u@example.com", password="Str0ng-Pass-Phrase!", **kw):
    return User.objects.create_user(email=email, password=password, first_name="U", **kw)


class HeaderTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_baseline_headers_on_public_page(self):
        r = Client().get("/login.html")
        csp = r["Content-Security-Policy"]
        self.assertNotIn("unsafe-eval", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(r["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")
        for feature in ("camera=()", "microphone=()", "geolocation=()"):
            self.assertIn(feature, r["Permissions-Policy"])

    def test_csp_only_allows_known_third_parties(self):
        csp = build_csp()
        hosts = set(re.findall(r"https://[^\s;]+", csp))
        self.assertEqual(hosts, {"https://cdnjs.cloudflare.com", "https://js.paystack.co",
                                 "https://checkout.paystack.com", "https://api.paystack.co"})
        self.assertNotIn("*", csp.replace("'self'", ""))

    def test_csp_report_only_switch(self):
        with override_settings(CSP_REPORT_ONLY=True):
            r = Client().get("/login.html")
        self.assertIn("Content-Security-Policy-Report-Only", r)
        self.assertNotIn("Content-Security-Policy", r)

    def test_hsts_and_https_redirect_when_enforced(self):
        with override_settings(SECURE_HSTS_SECONDS=86400, SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=["testserver"]):
            c = Client()
            plain = c.get("/login.html")
            self.assertEqual(plain.status_code, 301)
            self.assertTrue(plain["Location"].startswith("https://"))
            secure = c.get("/login.html", secure=True)
            self.assertEqual(secure.status_code, 200)
            self.assertIn("max-age=86400", secure["Strict-Transport-Security"])

    def test_private_pages_not_cacheable_and_noindex(self):
        user = make_user()
        c = Client(); c.force_login(user)
        r = c.get("/user/dashboard.html")
        self.assertIn("no-store", r["Cache-Control"])
        self.assertIn("noindex", r["X-Robots-Tag"])

    def test_cookie_flags(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.SESSION_COOKIE_SECURE, settings.HTTPS_ENFORCED)
        self.assertEqual(settings.CSRF_COOKIE_SECURE, settings.HTTPS_ENFORCED)

    def test_no_wildcard_allowed_hosts_outside_debug(self):
        from django.conf import settings
        if not settings.DEBUG:
            self.assertNotIn("*", settings.ALLOWED_HOSTS)


class SecurityFilesTests(TestCase):
    def test_security_txt_served_directly(self):
        for path in ("/.well-known/security.txt", "/security.txt"):
            r = Client().get(path)
            self.assertEqual(r.status_code, 200, path)          # no redirect
            self.assertTrue(r["Content-Type"].startswith("text/plain"))
            body = r.content.decode()
            self.assertRegex(body, r"(?m)^Contact: (mailto:|https://)\S+$")
            self.assertRegex(body, r"(?m)^Expires: \d{4}-\d\d-\d\dT")

    def test_security_txt_expiry_is_in_future_and_rfc_bounded(self):
        body = build_security_txt()
        expires = re.search(r"Expires: (\S+)", body).group(1)
        from datetime import datetime
        dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
        self.assertGreater(dt, timezone.now())
        self.assertLess(dt, timezone.now() + timedelta(days=366))   # RFC 9116 recommends < 1 year

    @override_settings(SECURITY_CONTACT="mailto:security@example.com")
    def test_security_contact_env_wins(self):
        self.assertIn("Contact: mailto:security@example.com", build_security_txt())

    @override_settings(SECURITY_CONTACT="javascript:alert(1)")
    def test_invalid_contact_rejected(self):
        self.assertNotIn("javascript:", build_security_txt())

    def test_robots_txt_no_redirect(self):
        r = Client().get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/plain"))
        self.assertIn("User-agent: *", r.content.decode())
        self.assertEqual(Client().post("/robots.txt").status_code, 405)

    def test_robots_works_during_maintenance(self):
        Setting.set_value("maintenance_mode", "1")
        self.assertEqual(Client().get("/robots.txt").status_code, 200)


class AuthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()

    def login(self, qs="", password="Str0ng-Pass-Phrase!"):
        return Client().post("/login.html" + qs, {"email": "u@example.com", "password": password})

    def test_open_redirect_blocked(self):
        for evil in ("https://evil.example/x", "//evil.example", "javascript:alert(1)", "/\\evil.example"):
            r = self.login("?redirect=" + evil)
            self.assertEqual(r.status_code, 302, evil)
            self.assertEqual(r["Location"], "/user/dashboard.html", evil)

    def test_same_site_redirect_allowed(self):
        self.assertEqual(self.login("?redirect=/user/cart.html")["Location"], "/user/cart.html")

    def test_login_throttled_after_repeated_failures(self):
        c = Client()
        for _ in range(views.LOGIN_MAX_FAILURES):
            self.assertEqual(c.post("/login.html", {"email": "u@example.com", "password": "bad"}).status_code, 200)
        locked = c.post("/login.html", {"email": "u@example.com", "password": "Str0ng-Pass-Phrase!"})
        self.assertEqual(locked.status_code, 429)

    def test_weak_password_rejected_on_register(self):
        r = Client().post("/register.html", {"name": "N", "email": "n@example.com", "password": "123456", "confirm": "123456"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(email="n@example.com").exists())

    def test_password_change_requires_current_password(self):
        c = Client(); c.force_login(self.user)
        c.post("/user/settings.html", {"action": "password", "current_password": "wrong",
                                       "new_password": "An0ther-Strong-One!", "confirm_password": "An0ther-Strong-One!"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Str0ng-Pass-Phrase!"))
        c.post("/user/settings.html", {"action": "password", "current_password": "Str0ng-Pass-Phrase!",
                                       "new_password": "An0ther-Strong-One!", "confirm_password": "An0ther-Strong-One!"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("An0ther-Strong-One!"))
        self.assertEqual(c.get("/user/dashboard.html").status_code, 200)   # session survived

    def test_banned_user_blocked_on_json_api(self):
        c = Client(); c.force_login(self.user)
        self.assertEqual(c.get("/api/coupon/?action=validate&code=X").status_code, 200)
        User.objects.filter(pk=self.user.pk).update(status="banned")
        self.assertEqual(c.get("/api/coupon/?action=validate&code=X").status_code, 403)

    def test_admin_pages_forbidden_for_normal_user(self):
        c = Client(); c.force_login(self.user)
        self.assertEqual(c.get("/admin/dashboard.html")["Location"], "/user/dashboard.html")
        self.assertEqual(c.post("/admin/action/", {"action": "save_settings"}).status_code, 403)

    def test_orders_are_scoped_to_owner(self):
        other = make_user("o@example.com")
        c = Client(); c.force_login(other)
        self.assertEqual(c.get("/api/orders/?order_id=1").status_code, 404)

    def test_csrf_enforced_on_post(self):
        c = Client(enforce_csrf_checks=True); c.force_login(self.user)
        self.assertEqual(c.post("/api/orders/", {"action": "buy"}).status_code, 403)


class DeploySetupTests(TestCase):
    @override_settings(DEPLOY_SETUP_TOKEN="")
    def test_inert_when_token_unset(self):
        self.assertEqual(Client().get("/deploy-setup/?token=").status_code, 404)

    @override_settings(DEPLOY_SETUP_TOKEN="s3cret-token")
    def test_wrong_token_404(self):
        self.assertEqual(Client().get("/deploy-setup/?token=nope").status_code, 404)

    @override_settings(DEPLOY_SETUP_TOKEN="s3cret-token", DEBUG=False)
    def test_never_seeds_default_accounts_in_production(self):
        with mock.patch("django.core.management.call_command") as cc:
            r = Client().get("/deploy-setup/?token=s3cret-token")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c.args[0] for c in cc.call_args_list], ["migrate"])


class InjectionTests(TestCase):
    def test_promo_slides_reject_script_urls(self):
        Setting.set_value("promo_slides",
                          "javascript:alert(1) | x | t\n"
                          "../assets/img/a.jpg | javascript:alert(1) | ok\n"
                          "../assets/img/b.jpg');}</style> | https://example.com | bad")
        slides = views.promo_slides()
        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0]["href"], "services.html")

    def test_support_api_only_exposes_https_links(self):
        Setting.set_value("support_channel", "javascript:alert(1)")
        Setting.set_value("logs_bot_url", "https://t.me/ok")
        Setting.set_value("support_whatsapp", "+234 808-621 8152<script>")
        data = Client().get("/api/support/").json()
        self.assertEqual(data["channel"], "")
        self.assertEqual(data["logs"], "https://t.me/ok")
        self.assertEqual(data["whatsapp"], "2348086218152")

    def test_paystack_callback_encodes_reference(self):
        c = Client(); c.force_login(make_user("p@example.com"))
        r = c.get("/api/paystack/callback/", {"reference": "a&next=//evil"})
        self.assertEqual(r["Location"], "/user/topup.html?reference=a%26next%3D%2F%2Fevil")

    def test_webhook_rejects_bad_signature_and_bad_json(self):
        self.assertEqual(Client().post("/api/paystack/webhook/", "{}", content_type="application/json").status_code, 401)
        with mock.patch.object(Paystack, "validate_webhook", return_value=True):
            self.assertEqual(Client().post("/api/paystack/webhook/", "not json", content_type="application/json").status_code, 400)

    def test_frontend_has_no_unescaped_provider_data_sinks(self):
        js = (ROOT / "assets/js/app.js").read_text()
        self.assertNotIn("${message}", js)
        self.assertNotIn("${data.sms_code}", js)
        self.assertNotIn("' + data.sms_text + '", js)
        for tpl in ("templates/user/cart.html", "templates/user/sms.html"):
            self.assertNotIn("copyText('{{", (ROOT / tpl).read_text(), tpl)
        self.assertNotIn("copyText(\\'' + esc", (ROOT / "templates/user/sms.html").read_text())


class WalletTests(TestCase):
    def test_topup_credited_exactly_once(self):
        user = make_user("w@example.com")
        for _ in range(3):   # webhook + verify + retry
            Paystack.process_topup(user.id, "VSMS_TEST_REF", Decimal("500"), {"x": 1})
        user.refresh_from_db()
        self.assertEqual(Transaction.objects.filter(reference="VSMS_TEST_REF", type="topup").count(), 1)
        self.assertEqual(user.balance, Decimal("485.00"))   # 500 minus the default 3% fee


class SeedDemoTests(TestCase):
    def seed(self, *args):
        out = StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_requires_email_and_has_no_default_password(self):
        with self.assertRaises(CommandError):
            self.seed()
        self.seed("--email", "boss@example.com")
        admin = User.objects.get(email="boss@example.com")
        self.assertEqual(admin.role, "admin")
        for old_default in ("ChangeMe123!", "TestUser123!", "admin", "password"):
            self.assertFalse(admin.check_password(old_default))
        self.assertFalse(User.objects.filter(email="testuser@example.com").exists())

    def test_generated_password_works_and_is_shown_once(self):
        out = self.seed("--email", "boss@example.com")
        pw = re.search(r"shown once[^:]*: (\S+)", out).group(1)
        self.assertTrue(User.objects.get(email="boss@example.com").check_password(pw))
        again = self.seed("--email", "boss@example.com")
        self.assertNotIn("Generated password", again)
        self.assertTrue(User.objects.get(email="boss@example.com").check_password(pw))  # not reset

    def test_weak_password_rejected(self):
        with self.assertRaises(CommandError):
            self.seed("--email", "boss@example.com", "--password", "12345678")

    def test_does_not_overwrite_admin_settings(self):
        Setting.set_value("site_rate", "1750")
        self.seed("--skip-admin")
        self.assertEqual(Setting.value("site_rate"), "1750")
        self.assertEqual(Setting.value("topup_fee_threshold"), "1000")

    def test_never_promotes_existing_customer_silently(self):
        make_user("victim@example.com")
        with self.assertRaises(CommandError):
            self.seed("--email", "victim@example.com")
        self.assertEqual(User.objects.get(email="victim@example.com").role, "user")
        self.seed("--email", "victim@example.com", "--promote")
        self.assertEqual(User.objects.get(email="victim@example.com").role, "admin")

    @override_settings(DEBUG=False)
    def test_test_user_needs_force_outside_debug(self):
        with self.assertRaises(CommandError):
            self.seed("--skip-admin", "--with-test-user")
        self.seed("--skip-admin", "--with-test-user", "--force")
        self.assertTrue(User.objects.filter(email="testuser@example.com", role="user").exists())

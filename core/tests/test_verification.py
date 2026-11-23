"""Verification suite: unit, functional, black-box, white-box and sanity tests.

Run:  DJANGO_DEBUG=1 python manage.py test core.tests.test_verification -v 2

No test touches the network. Provider HTTP is mocked at the adapter boundary
(``urlopen`` for the SMS-BUS client, whole adapters for routing and purchase).
"""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from core import services
from core.models import Coupon, CouponUse, Order, Setting, Topup, Transaction, User
from core.services import (
    Paystack, SmsBus, credit_balance, debit_balance, friendly_provider_error,
    money, provider_to_user_ngn,
)

PW = "Str0ng-Pass-Phrase!"


def make_user(email="buyer@example.com", balance="0", **kw):
    return User.objects.create_user(email=email, password=PW, first_name="B", balance=Decimal(balance), **kw)


def make_admin(email="admin@example.com"):
    return User.objects.create_user(email=email, password=PW, first_name="A", role="admin")


def login(email="buyer@example.com"):
    c = Client()
    assert c.login(email=email, password=PW), "login failed"
    return c


def make_order(user, status="PENDING", price="1000.00", **kw):
    defaults = dict(provider="demo", provider_order_id=1234567, phone="2348000000000",
                    country="ng", product="wa", user_price=Decimal(price), status=status,
                    expires_at=timezone.now() + timedelta(minutes=20))
    defaults.update(kw)
    return Order.objects.create(user=user, **defaults)


# ───────────────────────────── UNIT ─────────────────────────────────────────

class MoneyAndPricingUnit(TestCase):
    def test_money_rounds_half_up_to_2dp(self):
        self.assertEqual(money("1.005"), Decimal("1.01"))
        self.assertEqual(money("2.994"), Decimal("2.99"))

    def test_money_bad_input_is_zero(self):
        self.assertEqual(money("abc"), Decimal("0.00"))
        self.assertEqual(money(None), Decimal("0.00"))

    def test_default_pricing_formula(self):
        # 0.50 USD * 1600 * 1.20 * 1.08 = 1036.80
        self.assertEqual(provider_to_user_ngn("0.50"), Decimal("1036.80"))

    def test_pricing_follows_admin_settings(self):
        Setting.set_value("site_rate", "1000")
        Setting.set_value("markup_percent", "0")
        Setting.set_value("extra_fee_percent", "0")
        self.assertEqual(provider_to_user_ngn("2"), Decimal("2000.00"))

    def test_pricing_survives_garbage_setting(self):
        Setting.set_value("site_rate", "not-a-number")
        self.assertEqual(provider_to_user_ngn("1"), money(Decimal("1") * 1600 * Decimal("1.2") * Decimal("1.08")))


class WalletUnit(TestCase):
    def test_debit_success_writes_ledger(self):
        u = make_user(balance="500")
        self.assertTrue(debit_balance(u.id, "200", "buy"))
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("300.00"))
        t = Transaction.objects.get(user=u)
        self.assertEqual((t.type, t.amount, t.balance_before, t.balance_after),
                         ("purchase", Decimal("-200.00"), Decimal("500.00"), Decimal("300.00")))

    def test_debit_insufficient_is_refused_and_untouched(self):
        u = make_user(balance="50")
        self.assertFalse(debit_balance(u.id, "50.01", "buy"))
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("50.00"))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_debit_exact_balance_allowed(self):
        u = make_user(balance="50")
        self.assertTrue(debit_balance(u.id, "50", "buy"))
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("0.00"))

    def test_debit_unknown_user(self):
        self.assertFalse(debit_balance(999999, "1", "x"))

    def test_credit_writes_ledger_with_reference(self):
        u = make_user(balance="10")
        self.assertTrue(credit_balance(u.id, "5.5", "refund", "r", "REF1"))
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("15.50"))
        self.assertEqual(Transaction.objects.get(user=u).reference, "REF1")


class ErrorMappingUnit(TestCase):
    def test_known_tokens(self):
        self.assertIn("No numbers", friendly_provider_error("NO_NUMBERS"))
        self.assertIn("temporarily unavailable", friendly_provider_error("BAD_KEY"))
        self.assertIn("already cancelled", friendly_provider_error("ACCESS_CANCEL"))

    def test_http_5xx_is_provider_down(self):
        self.assertIn("temporarily unavailable", friendly_provider_error("HTTP_503"))

    def test_provider_key_never_leaks_in_messages(self):
        for tok in ("BAD_KEY", "NO_BALANCE", "PROVIDER_DOWN", "NO_NUMBERS"):
            msg = friendly_provider_error(tok).lower()
            self.assertNotIn("5sim", msg)
            self.assertNotIn("sms-bus", msg)


class PaystackUnit(TestCase):
    def setUp(self):
        self.patch = mock.patch.object(Paystack, "secret_key", classmethod(lambda cls: "sk_test_x"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def sig(self, body, key="sk_test_x"):
        return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()

    def test_valid_signature(self):
        body = b'{"event":"x"}'
        self.assertTrue(Paystack.validate_webhook(body, self.sig(body)))

    def test_wrong_signature_and_empty_signature(self):
        body = b'{"event":"x"}'
        self.assertFalse(Paystack.validate_webhook(body, self.sig(body, "other")))
        self.assertFalse(Paystack.validate_webhook(body, ""))
        self.assertFalse(Paystack.validate_webhook(body, None))

    def test_no_secret_configured_rejects_everything(self):
        with mock.patch.object(Paystack, "secret_key", classmethod(lambda cls: "")):
            body = b"{}"
            self.assertFalse(Paystack.validate_webhook(body, self.sig(body, "")))

    def test_fee_tiers(self):
        u = make_user()
        Paystack.process_topup(u.id, "R_LOW", 1000, {})       # <= threshold -> 3%
        Paystack.process_topup(u.id, "R_HIGH", 2000, {})      # > threshold  -> 6%
        low, high = Topup.objects.get(reference="R_LOW"), Topup.objects.get(reference="R_HIGH")
        self.assertEqual((low.fee_amount, low.net_credited), (Decimal("30.00"), Decimal("970.00")))
        self.assertEqual((high.fee_amount, high.net_credited), (Decimal("120.00"), Decimal("1880.00")))
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("2850.00"))

    def test_process_topup_is_idempotent(self):
        u = make_user()
        for _ in range(3):
            Paystack.process_topup(u.id, "R1", 500, {})
        u.refresh_from_db()
        self.assertEqual(u.balance, Decimal("485.00"))
        self.assertEqual(Transaction.objects.filter(reference="R1").count(), 1)


class SmsBusClientUnit(TestCase):
    """SMS-BUS adapter against canned HTTP bodies (not the live service)."""

    def _call(self, body, key="k"):
        resp = mock.MagicMock()
        resp.read.return_value = body.encode() if isinstance(body, str) else body
        resp.__enter__.return_value = resp
        with mock.patch.object(SmsBus, "_key", classmethod(lambda cls: key)), \
             mock.patch("core.services.urlopen", return_value=resp):
            return SmsBus._request("/list/projects")

    def test_no_key_short_circuits_without_network(self):
        with mock.patch.object(SmsBus, "_key", classmethod(lambda cls: "")), \
             mock.patch("core.services.urlopen") as uo:
            self.assertEqual(SmsBus._request("/x"), {"error": "BAD_KEY"})
            uo.assert_not_called()

    def test_success_envelope(self):
        r = self._call(json.dumps({"code": 200, "message": "ok", "data": {"a": 1}}))
        self.assertEqual(r, {"ok": True, "data": {"a": 1}, "message": "ok"})

    def test_error_codes_map_to_tokens(self):
        for code, token in ((50201, "NO_BALANCE"), (50002, "NO_NUMBERS"), (50104, "PROVIDER_DOWN"), (50208, "BAD_KEY")):
            r = self._call(json.dumps({"code": code, "message": "m"}))
            self.assertEqual(r["error"], token, code)

    def test_malformed_bodies(self):
        self.assertEqual(self._call("")["error"], "EMPTY_RESPONSE")
        self.assertEqual(self._call("<html>")["error"], "INVALID_JSON")
        self.assertEqual(self._call("[1,2]")["error"], "INVALID_JSON")

    def test_network_failure_is_provider_down(self):
        from urllib.error import URLError
        with mock.patch.object(SmsBus, "_key", classmethod(lambda cls: "k")), \
             mock.patch("core.services.urlopen", side_effect=URLError("dns")):
            self.assertEqual(SmsBus._request("/x"), {"error": "PROVIDER_DOWN"})

    def test_token_is_sent_as_query_param_only_to_sms_bus(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"code":200,"data":{}}'
        resp.__enter__.return_value = resp
        with mock.patch.object(SmsBus, "_key", classmethod(lambda cls: "SECRET")), \
             mock.patch("core.services.urlopen", return_value=resp) as uo:
            SmsBus._request("/list/projects")
        url = uo.call_args[0][0].full_url
        self.assertTrue(url.startswith("https://sms-bus.com/api/control/list/projects?"))
        self.assertIn("token=SECRET", url)


class RoutingUnit(TestCase):
    """buy_number: preferred server first, automatic fallback to the other."""

    def _adapters(self, smsbus, fivesim):
        a, b = mock.Mock(), mock.Mock()
        a.buy_activation.side_effect = smsbus
        b.buy_activation.side_effect = fivesim
        return a, b

    def _run(self, smsbus, fivesim, provider="smsbus"):
        a, b = self._adapters(smsbus, fivesim)
        with mock.patch.object(services, "demo_mode", return_value=False), \
             mock.patch.object(services, "_live", return_value=True), \
             mock.patch.object(services, "_price_from", return_value=None), \
             mock.patch.object(services, "_country_for", side_effect=lambda p, c: c), \
             mock.patch.object(services, "_service_for", side_effect=lambda p, s: s), \
             mock.patch.dict(services._PROVIDERS, {"smsbus": a, "5sim": b}):
            return services.buy_number("wa", "ng", "any", provider=provider), a, b

    def test_preferred_server_wins_when_it_has_stock(self):
        r, a, b = self._run(lambda *x: {"id": "1", "phone": "+1", "status": "PENDING"}, lambda *x: {"id": "2"})
        self.assertEqual(r["provider"], "smsbus")
        b.buy_activation.assert_not_called()

    def test_falls_back_when_preferred_has_no_numbers(self):
        r, a, b = self._run(lambda *x: {"error": "NO_NUMBERS"}, lambda *x: {"id": "2", "phone": "+2", "status": "PENDING"})
        self.assertEqual(r["provider"], "5sim")
        a.buy_activation.assert_called_once()

    def test_falls_back_when_preferred_raises(self):
        def boom(*a):
            raise RuntimeError("timeout")
        r, _, _ = self._run(boom, lambda *x: {"id": "2", "phone": "+2", "status": "PENDING"})
        self.assertEqual(r["provider"], "5sim")

    def test_both_fail_returns_last_error_and_trail(self):
        r, _, _ = self._run(lambda *x: {"error": "NO_NUMBERS"}, lambda *x: {"error": "NO_BALANCE"})
        self.assertEqual(r["error"], "NO_BALANCE")
        self.assertEqual(len(r["provider_errors"]), 2)

    def test_preference_for_5sim_reorders(self):
        r, a, b = self._run(lambda *x: {"id": "1"}, lambda *x: {"id": "2", "phone": "+2", "status": "PENDING"}, provider="5sim")
        self.assertEqual(r["provider"], "5sim")
        a.buy_activation.assert_not_called()

    def test_demo_mode_never_calls_a_provider(self):
        a, b = mock.Mock(), mock.Mock()
        with mock.patch.object(services, "demo_mode", return_value=True), \
             mock.patch.dict(services._PROVIDERS, {"smsbus": a, "5sim": b}):
            r = services.buy_number("wa", "ng")
        self.assertEqual(r["provider"], "demo")
        a.buy_activation.assert_not_called()
        b.buy_activation.assert_not_called()


class CouponUnit(TestCase):
    def setUp(self):
        from core.views import _coupon_check
        self.check = _coupon_check
        self.u = make_user()

    def coupon(self, **kw):
        d = dict(code="SAVE", discount_type="percent", discount_value=Decimal("10"))
        d.update(kw)
        return Coupon.objects.create(**d)

    def test_percent(self):
        self.coupon()
        r, e = self.check(self.u, "save", "1000")
        self.assertIsNone(e)
        self.assertEqual((r["discount"], r["final_price"]), (Decimal("100.00"), Decimal("900.00")))

    def test_fixed_capped_at_price(self):
        self.coupon(discount_type="fixed", discount_value=Decimal("5000"))
        r, _ = self.check(self.u, "SAVE", "1000")
        self.assertEqual(r["final_price"], Decimal("0.00"))

    def test_unknown_inactive_expired_future(self):
        self.assertIsNotNone(self.check(self.u, "NOPE", "100")[1])
        self.coupon(is_active=False)
        self.assertIsNotNone(self.check(self.u, "SAVE", "100")[1])
        Coupon.objects.all().delete()
        self.coupon(valid_until=timezone.now() - timedelta(days=1))
        self.assertIn("expired", self.check(self.u, "SAVE", "100")[1])
        Coupon.objects.all().delete()
        self.coupon(valid_from=timezone.now() + timedelta(days=1))
        self.assertIn("not active yet", self.check(self.u, "SAVE", "100")[1])

    def test_max_uses_and_one_per_user_and_min_purchase(self):
        c = self.coupon(max_uses=1, uses_count=1)
        self.assertIn("usage limit", self.check(self.u, "SAVE", "100")[1])
        c.max_uses, c.uses_count = None, 0
        c.min_purchase = Decimal("500")
        c.save()
        self.assertIn("Minimum purchase", self.check(self.u, "SAVE", "100")[1])
        CouponUse.objects.create(coupon=c, user=self.u)
        self.assertIn("already used", self.check(self.u, "SAVE", "1000")[1])

    def test_consume_increments_counter(self):
        c = self.coupon()
        order = make_order(self.u)
        self.check(self.u, "SAVE", "1000", consume=True, order=order)
        c.refresh_from_db()
        self.assertEqual(c.uses_count, 1)
        self.assertTrue(CouponUse.objects.filter(coupon=c, user=self.u).exists())


# ───────────────────── FUNCTIONAL / BLACK-BOX (HTTP) ────────────────────────

class AuthFunctional(TestCase):
    def setUp(self):
        cache.clear()

    def test_register_then_login_then_dashboard(self):
        c = Client()
        r = c.post("/register.html", {"name": "New Person", "email": "new@example.com",
                                      "password": PW, "confirm": PW})
        self.assertEqual(r.status_code, 302)                       # auto-login after sign-up
        self.assertTrue(User.objects.filter(email="new@example.com", role="user").exists())
        self.assertEqual(c.get("/user/dashboard.html").status_code, 200)
        c.get("/logout.html")
        r = c.post("/login.html", {"email": "new@example.com", "password": PW})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/user/dashboard.html").status_code, 200)

    def test_register_rejects_mismatch_duplicate_and_weak_password(self):
        make_user("dup@example.com")
        base = {"name": "X", "email": "x@example.com", "password": PW, "confirm": PW}
        for override in ({"confirm": "different"}, {"email": "dup@example.com"}, {"password": "123", "confirm": "123"}):
            r = Client().post("/register.html", {**base, **override})
            self.assertEqual(r.status_code, 200, override)
        self.assertFalse(User.objects.filter(email="x@example.com").exists())

    def test_wrong_password_rejected(self):
        make_user()
        r = Client().post("/login.html", {"email": "buyer@example.com", "password": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("_auth_user_id", r.client.session)

    def test_anonymous_redirected_from_private_pages_and_apis(self):
        c = Client()
        for url in ("/user/dashboard.html", "/user/cart.html", "/admin/dashboard.html", "/api/orders/?order_id=1"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login.html", r["Location"], url)

    def test_logout_ends_session(self):
        make_user()
        c = login()
        c.get("/logout.html")
        self.assertEqual(c.get("/user/dashboard.html").status_code, 302)

    def test_banned_user_is_kicked_out_mid_session(self):
        u = make_user()
        c = login()
        self.assertEqual(c.get("/user/dashboard.html").status_code, 200)
        User.objects.filter(pk=u.pk).update(status="banned")
        self.assertEqual(c.get("/user/dashboard.html").status_code, 302)
        r = c.get("/api/orders/?order_id=1")
        self.assertIn(r.status_code, (302, 401, 403))


class AuthorizationBlackBox(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.admin = make_admin()

    def test_customer_cannot_open_admin_pages(self):
        c = login()
        for page in ("dashboard", "users", "transactions", "providers", "payment", "coupons", "settings"):
            r = c.get(f"/admin/{page}.html")
            self.assertNotEqual(r.status_code, 200, page)

    def test_customer_cannot_run_admin_actions(self):
        c = login()
        r = c.post("/admin/action/", {"action": "adjust_balance", "user_id": self.user.id, "amount": "99999"})
        self.assertEqual(r.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("0.00"))

    def test_admin_pages_render_for_admin(self):
        c = login("admin@example.com")
        for page in ("dashboard", "users", "transactions", "providers", "payment", "coupons", "settings", "broadcast"):
            self.assertEqual(c.get(f"/admin/{page}.html").status_code, 200, page)

    def test_user_cannot_read_another_users_order(self):
        other = make_user("other@example.com")
        o = make_order(other)
        c = login()
        self.assertEqual(c.get(f"/api/orders/?order_id={o.id}").status_code, 404)
        self.assertEqual(c.get(f"/user/sms.html?order_id={o.id}").status_code, 404)

    def test_user_cannot_cancel_another_users_order(self):
        other = make_user("other@example.com", balance="0")
        o = make_order(other)
        c = login()
        r = c.post("/api/orders/", {"action": "cancel", "order_id": o.id})
        self.assertEqual(r.status_code, 404)
        o.refresh_from_db()
        self.assertEqual(o.status, "PENDING")

    def test_http_method_restrictions(self):
        c = login()
        self.assertEqual(c.post("/api/provider/", {}).status_code, 405)
        self.assertEqual(c.get("/api/paystack/init/").status_code, 405)
        self.assertEqual(c.get("/admin/action/").status_code, 405)


class PurchaseAndRefundFunctional(TestCase):
    """The core money path: buy a number, cancel it, get refunded exactly once."""

    PRICE = Decimal("1036.80")  # 0.50 USD at default pricing

    def setUp(self):
        cache.clear()
        self.user = make_user(balance="5000")
        self.c = login()
        self.stack = [
            mock.patch("core.views.catalog_products", return_value={"wa": {"Price": 0.5}}),
            mock.patch("core.views.get_cheap_price", return_value={"cost": 0.5, "count": 10}),
            mock.patch("core.views.buy_number", return_value={"id": "777", "phone": "2347000000", "status": "PENDING",
                                                              "provider": "smsbus", "provider_cost": 0.5}),
            mock.patch("core.views.order_action", return_value={"status": "STATUS_CANCEL"}),
        ]
        self.mocks = [p.start() for p in self.stack]
        for p in self.stack:
            self.addCleanup(p.stop)

    def buy(self, **extra):
        data = {"action": "buy", "country": "ng", "product": "wa", "operator": "any", "server": "1"}
        data.update(extra)
        return self.c.post("/api/orders/", data)

    def test_buy_debits_exact_price_and_creates_order(self):
        r = self.buy().json()
        self.assertTrue(r["success"], r)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000") - self.PRICE)
        o = Order.objects.get()
        self.assertEqual((o.user_id, o.status, o.user_price, o.provider), (self.user.id, "PENDING", self.PRICE, "smsbus"))
        t = Transaction.objects.get(type="purchase")
        self.assertEqual((t.amount, t.order_id), (-self.PRICE, o.id))

    def test_insufficient_balance_buys_nothing(self):
        User.objects.filter(pk=self.user.pk).update(balance=Decimal("10"))
        r = self.buy().json()
        self.assertFalse(r["success"])
        self.assertIn("Insufficient", r["message"])
        self.mocks[2].assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_missing_fields_400(self):
        r = self.c.post("/api/orders/", {"action": "buy", "country": "ng"})
        self.assertEqual(r.status_code, 400)

    def test_provider_failure_does_not_charge(self):
        self.mocks[2].return_value = {"error": "NO_NUMBERS"}
        r = self.buy().json()
        self.assertFalse(r["success"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000.00"))
        self.assertEqual(Order.objects.count(), 0)

    def test_cancel_refunds_once_even_if_repeated(self):
        oid = self.buy().json()["order_id"]
        r1 = self.c.post("/api/orders/", {"action": "cancel", "order_id": oid}).json()
        r2 = self.c.post("/api/orders/", {"action": "cancel", "order_id": oid}).json()
        self.assertTrue(r1["success"])
        self.assertFalse(r2["success"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000.00"))
        self.assertEqual(Transaction.objects.filter(type="refund").count(), 1)
        self.assertEqual(Order.objects.get(pk=oid).status, "CANCELED")

    def test_ban_refunds_and_finish_does_not(self):
        oid = self.buy().json()["order_id"]
        Order.objects.filter(pk=oid).update(status="RECEIVED")
        self.assertTrue(self.c.post("/api/orders/", {"action": "finish", "order_id": oid}).json()["success"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000") - self.PRICE)   # finished = no refund
        self.assertFalse(self.c.post("/api/orders/", {"action": "cancel", "order_id": oid}).json()["success"])

    def test_cannot_cancel_after_sms_received(self):
        oid = self.buy().json()["order_id"]
        Order.objects.filter(pk=oid).update(status="RECEIVED")
        r = self.c.post("/api/orders/", {"action": "cancel", "order_id": oid}).json()
        self.assertFalse(r["success"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000") - self.PRICE)

    def test_unknown_action_400(self):
        oid = self.buy().json()["order_id"]
        self.assertEqual(self.c.post("/api/orders/", {"action": "explode", "order_id": oid}).status_code, 400)

    def test_coupon_applied_and_consumed_once(self):
        Coupon.objects.create(code="HALF", discount_type="percent", discount_value=Decimal("50"))
        r = self.buy(coupon_code="half").json()
        self.assertTrue(r["success"], r)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000") - Decimal("518.40"))
        self.assertEqual(Coupon.objects.get(code="HALF").uses_count, 1)
        self.assertIn("already used", self.buy(coupon_code="half").json()["message"])

    def test_refund_after_coupon_returns_what_was_paid(self):
        Coupon.objects.create(code="HALF", discount_type="percent", discount_value=Decimal("50"))
        oid = self.buy(coupon_code="HALF").json()["order_id"]
        self.c.post("/api/orders/", {"action": "cancel", "order_id": oid})
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("5000.00"))


class PaystackFunctional(TestCase):
    KEY = "sk_test_func"

    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.c = login()
        p = mock.patch.object(Paystack, "secret_key", classmethod(lambda cls: self.KEY))
        q = mock.patch.object(Paystack, "public_key", classmethod(lambda cls: "pk_test_func"))
        p.start(); q.start()
        self.addCleanup(p.stop); self.addCleanup(q.stop)

    def webhook(self, ref, kobo, key=None, event="charge.success"):
        body = json.dumps({"event": event, "data": {"reference": ref, "amount": kobo}}).encode()
        sig = hmac.new((key or self.KEY).encode(), body, hashlib.sha512).hexdigest()
        return Client().post("/api/paystack/webhook/", data=body, content_type="application/json",
                             HTTP_X_PAYSTACK_SIGNATURE=sig)

    def test_init_validates_amount_bounds(self):
        self.assertEqual(self.c.post("/api/paystack/init/", {"amount": "5"}).status_code, 400)
        self.assertEqual(self.c.post("/api/paystack/init/", {"amount": "99999999"}).status_code, 400)
        self.assertEqual(self.c.post("/api/paystack/init/", {"amount": "abc"}).status_code, 400)
        self.assertEqual(Topup.objects.count(), 0)

    def test_init_creates_pending_topup(self):
        with mock.patch.object(Paystack, "initialize", return_value={"status": True, "data": {"authorization_url": "https://x"}}):
            r = self.c.post("/api/paystack/init/", {"amount": "1500"}).json()
        self.assertTrue(r["success"])
        t = Topup.objects.get(reference=r["reference"])
        self.assertEqual((t.status, t.user_id, t.amount_paid), ("pending", self.user.id, Decimal("1500.00")))

    def test_init_gateway_failure_marks_failed(self):
        with mock.patch.object(Paystack, "initialize", return_value={"status": False, "message": "nope"}):
            r = self.c.post("/api/paystack/init/", {"amount": "1500"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(Topup.objects.get().status, "failed")

    def test_webhook_credits_once_and_replay_is_safe(self):
        Topup.objects.create(user=self.user, amount_paid=2000, net_credited=2000, reference="VSMS_1_A")
        self.assertEqual(self.webhook("VSMS_1_A", 200000).status_code, 200)
        self.assertEqual(self.webhook("VSMS_1_A", 200000).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("1880.00"))          # 2000 less 6% fee, once
        self.assertEqual(Transaction.objects.filter(type="topup").count(), 1)

    def test_webhook_bad_signature_credits_nothing(self):
        Topup.objects.create(user=self.user, amount_paid=2000, net_credited=2000, reference="VSMS_1_B")
        self.assertEqual(self.webhook("VSMS_1_B", 200000, key="attacker").status_code, 401)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("0.00"))

    def test_webhook_unknown_reference_and_other_events_ignored(self):
        self.assertEqual(self.webhook("NOPE", 100000).status_code, 200)
        Topup.objects.create(user=self.user, amount_paid=500, net_credited=500, reference="VSMS_1_C")
        self.assertEqual(self.webhook("VSMS_1_C", 50000, event="charge.failed").status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("0.00"))

    def test_verify_credits_amount_reported_by_gateway_not_by_client(self):
        Topup.objects.create(user=self.user, amount_paid=100000, net_credited=100000, reference="VSMS_1_D")
        gateway = {"status": True, "data": {"status": "success", "reference": "VSMS_1_D", "amount": 50000, "currency": "NGN"}}
        with mock.patch.object(Paystack, "verify", return_value=gateway):
            r = self.c.post("/api/paystack/verify/", {"reference": "VSMS_1_D", "amount": "100000"}).json()
        self.assertTrue(r["success"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("485.00"))           # 500 paid, 3% fee

    def test_verify_rejects_failed_wrong_ref_and_wrong_currency(self):
        Topup.objects.create(user=self.user, amount_paid=500, net_credited=500, reference="VSMS_1_E")
        for data in ({"status": "failed", "reference": "VSMS_1_E", "amount": 50000},
                     {"status": "success", "reference": "OTHER", "amount": 50000},
                     {"status": "success", "reference": "VSMS_1_E", "amount": 50000, "currency": "USD"}):
            with mock.patch.object(Paystack, "verify", return_value={"status": True, "data": data}):
                r = self.c.post("/api/paystack/verify/", {"reference": "VSMS_1_E"}).json()
            self.assertFalse(r["success"], data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("0.00"))

    def test_cannot_verify_someone_elses_reference(self):
        other = make_user("other@example.com")
        Topup.objects.create(user=other, amount_paid=500, net_credited=500, reference="VSMS_9_Z")
        r = self.c.post("/api/paystack/verify/", {"reference": "VSMS_9_Z"})
        self.assertEqual(r.status_code, 404)


class ProviderApiBlackBox(TestCase):
    def setUp(self):
        cache.clear()
        make_user()
        self.c = login()

    def test_servers_endpoint_never_leaks_supplier_names(self):
        rows = [{"provider": "smsbus", "server": "1", "label": "Server 1", "available": True},
                {"provider": "5sim", "server": "2", "label": "Server 2", "available": True}]
        with mock.patch("core.views.catalog_servers", return_value=rows):
            r = self.c.get("/api/provider/?action=servers")
        body = r.content.decode().lower()
        self.assertEqual(r.status_code, 200)
        for leak in ("smsbus", "5sim", "sms-bus"):
            self.assertNotIn(leak, body)

    def test_unknown_action_400(self):
        self.assertEqual(self.c.get("/api/provider/?action=wat").status_code, 400)

    def test_services_requires_server_choice(self):
        r = self.c.get("/api/provider/?action=services").json()
        self.assertFalse(r["success"])

    def test_prices_requires_all_selections(self):
        self.assertFalse(self.c.get("/api/provider/?action=prices&server=1").json()["success"])


# ──────────────────────────── WHITE-BOX ─────────────────────────────────────

class WhiteBoxPaths(TestCase):
    def setUp(self):
        cache.clear()

    def test_legacy_provider_order_can_still_be_closed_locally(self):
        u = make_user()
        o = make_order(u, provider="retired-supplier")
        with mock.patch.object(services, "demo_mode", return_value=False):
            self.assertEqual(services.order_action(o, "cancel"), {"status": "CANCELED"})
            self.assertEqual(services.check_order(o), {"status": "PENDING", "sms": []})

    def test_check_order_swallows_adapter_exceptions(self):
        u = make_user()
        o = make_order(u, provider="smsbus")
        boom = mock.Mock()
        boom.check_order.side_effect = RuntimeError("x")
        with mock.patch.object(services, "demo_mode", return_value=False), \
             mock.patch.dict(services._PROVIDERS, {"smsbus": boom}):
            self.assertEqual(services.check_order(o), {"error": "PROVIDER_DOWN"})

    def test_debit_failure_after_provider_purchase_must_not_leave_an_order(self):
        """views.orders_api creates the Order, then calls debit_balance inside the same
        atomic block. If debit fails it ``return``s a 409 from *inside* ``with
        transaction.atomic()``. A return does not roll back, so the Order is committed
        without any payment. Expectation (what a correct build does): no Order row."""
        u = make_user(balance="5000")
        c = login()
        with mock.patch("core.views.catalog_products", return_value={"wa": {"Price": 0.5}}), \
             mock.patch("core.views.get_cheap_price", return_value={"cost": 0.5, "count": 10}), \
             mock.patch("core.views.buy_number", return_value={"id": "5", "phone": "2347", "status": "PENDING",
                                                               "provider": "smsbus", "provider_cost": 0.5}), \
             mock.patch("core.views.debit_balance", return_value=False):
            r = c.post("/api/orders/", {"action": "buy", "country": "ng", "product": "wa", "server": "1"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Order.objects.count(), 0, "unpaid Order was committed")

    def test_debit_failure_should_release_the_provider_number(self):
        """Same path: the number was already bought from the provider. A correct build
        releases it (services.cancel_provider_order) so it is not paid for and orphaned."""
        u = make_user(balance="5000")
        c = login()
        adapter = mock.Mock()
        with mock.patch("core.views.catalog_products", return_value={"wa": {"Price": 0.5}}), \
             mock.patch("core.views.get_cheap_price", return_value={"cost": 0.5, "count": 10}), \
             mock.patch("core.views.buy_number", return_value={"id": "5", "phone": "2347", "status": "PENDING",
                                                               "provider": "smsbus", "provider_cost": 0.5}), \
             mock.patch("core.views.debit_balance", return_value=False), \
             mock.patch.dict(services._PROVIDERS, {"smsbus": adapter}):
            c.post("/api/orders/", {"action": "buy", "country": "ng", "product": "wa", "server": "1"})
        adapter.order_action.assert_called_once()
        self.assertEqual(adapter.order_action.call_args[0][0], "cancel")


# ───────────────────────────── SANITY / SMOKE ───────────────────────────────

class SanitySmoke(TestCase):
    def setUp(self):
        cache.clear()

    def test_public_routes_up(self):
        c = Client()
        for url in ("/", "/login.html", "/register.html", "/healthz/", "/robots.txt",
                    "/.well-known/security.txt", "/security.txt"):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_health_payload(self):
        self.assertEqual(Client().get("/healthz/").json(), {"ok": True, "service": "VerifySMS Django"})

    def test_every_customer_page_renders(self):
        make_user(balance="100")
        c = login()
        with mock.patch("core.views.catalog_servers", return_value=[]):
            for page in ("dashboard", "services", "cart", "topup", "history", "settings", "sms", "tutorials", "notifications"):
                self.assertEqual(c.get(f"/user/{page}.html").status_code, 200, page)

    def test_unknown_pages_404_not_500(self):
        make_user()
        c = login()
        self.assertEqual(c.get("/user/doesnotexist.html").status_code, 404)
        self.assertEqual(Client().get("/no/such/route").status_code, 404)

    def test_migrations_are_in_sync_with_models(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("makemigrations", "--check", "--dry-run", stdout=out)


# ──────────────── NUMBER + VERIFICATION CODE VISIBILITY ─────────────────────

class NumberAndCodeVisibility(TestCase):
    """The bought number and its verification code must show to the buyer and to admins."""

    def setUp(self):
        cache.clear()
        self.buyer = make_user("buyer@example.com")
        self.other = make_user("other@example.com")
        make_admin()
        self.order = make_order(self.buyer, status="RECEIVED", phone="2347011122233",
                                sms_code="483920", sms_text="Your code is 483920", sms_sender="WhatsApp")
        self.secret_order = make_order(self.other, status="RECEIVED", phone="2349900011122",
                                       sms_code="777111", provider_order_id=7654321)

    def assertShows(self, client, url, *needles):
        r = client.get(url)
        self.assertEqual(r.status_code, 200, url)
        body = r.content.decode()
        for n in needles:
            self.assertIn(n, body, f"{url} is missing {n}")
        return body

    def test_buyer_sees_number_and_code_everywhere(self):
        c = login("buyer@example.com")
        for page in ("dashboard", "history", "cart"):
            self.assertShows(c, f"/user/{page}.html", "2347011122233", "483920")
        self.assertShows(c, f"/user/sms.html?order_id={self.order.id}", "2347011122233")

    def test_buyer_still_sees_code_after_order_is_finished(self):
        Order.objects.filter(pk=self.order.pk).update(status="FINISHED")
        c = login("buyer@example.com")
        self.assertShows(c, "/user/history.html", "2347011122233", "483920")
        self.assertShows(c, "/user/dashboard.html", "2347011122233", "483920")

    def test_admin_sees_number_and_code_on_dashboard_and_transactions(self):
        c = login("admin@example.com")
        for page in ("dashboard", "transactions"):
            self.assertShows(c, f"/admin/{page}.html", "2347011122233", "483920", "2349900011122", "777111")

    def test_admin_sees_dash_when_no_code_yet(self):
        Order.objects.filter(pk=self.order.pk).update(sms_code=None, status="PENDING")
        c = login("admin@example.com")
        body = self.assertShows(c, "/admin/transactions.html", "2347011122233")
        self.assertNotIn("483920", body)

    def test_buyer_never_sees_another_users_number_or_code(self):
        c = login("buyer@example.com")
        for page in ("dashboard", "history", "cart"):
            body = c.get(f"/user/{page}.html").content.decode()
            self.assertNotIn("2349900011122", body, page)
            self.assertNotIn("777111", body, page)

    def test_code_is_html_escaped(self):
        Order.objects.filter(pk=self.order.pk).update(sms_code='"><script>x</script>')
        c = login("admin@example.com")
        body = c.get("/admin/transactions.html").content.decode()
        self.assertNotIn("<script>x</script>", body)

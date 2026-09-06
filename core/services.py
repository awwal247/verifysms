"""Business rules and external service adapters for the verification/SMS app."""
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Coupon, CouponUse, Order, Setting, Topup, Transaction, User


def money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return Decimal("0.00")


def setting_number(key, default):
    try:
        return Decimal(Setting.value(key, str(default)))
    except InvalidOperation:
        return Decimal(str(default))


def provider_to_user_ngn(provider_price):
    rate = setting_number("site_rate", 1600)
    markup = setting_number("markup_percent", 20)
    return money(Decimal(str(provider_price)) * rate * (1 + markup / 100))


def format_ngn(amount):
    return f"{Setting.value('currency_symbol', '₦')}{money(amount):,.2f}"


def debit_balance(user_id, amount, description, order=None):
    amount = money(amount)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        if not user or user.balance < amount:
            return False
        before = user.balance
        user.balance = money(before - amount)
        user.save(update_fields=["balance", "updated_at"])
        Transaction.objects.create(
            user=user,
            type="purchase",
            amount=-amount,
            balance_before=before,
            balance_after=user.balance,
            description=description,
            order=order,
        )
    return True


def credit_balance(user_id, amount, kind, description, reference=None):
    amount = money(amount)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        if not user:
            return False
        before = user.balance
        user.balance = money(before + amount)
        user.save(update_fields=["balance", "updated_at"])
        Transaction.objects.create(
            user=user,
            type=kind,
            amount=amount,
            balance_before=before,
            balance_after=user.balance,
            description=description,
            reference=reference,
        )
    return True


def friendly_provider_error(raw):
    raw = str(raw or "").lower().strip()
    messages = {
        "low user balance": "Service temporarily unavailable. Please try again later.",
        "no free phones": "No numbers available for this selection right now. Try a different country or operator.",
        "bad country": "Invalid country selected.",
        "bad operator": "Invalid operator selected.",
        "bad product": "Invalid service selected.",
        "order not found": "Order not found.",
        "order expired": "This order has expired.",
        "order already finished": "This order was already completed.",
        "order already canceled": "This order was already cancelled.",
        "order already banned": "This order was already reported.",
        "connection error": "Connection to provider failed. Please try again.",
        "invalid json response": "Provider returned an unexpected response.",
        "api error 401": "Invalid API key. Check Admin → Providers.",
        "api error 400": "Invalid request to provider.",
        "api error 500": "Provider is temporarily unavailable.",
    }
    for key, message in messages.items():
        if key in raw:
            return message
    return "Provider error. Please try again or contact support." if "api error" in raw else "An error occurred. Please try again."


class FiveSim:
    """5sim adapter with a deterministic demo catalog when no API key is set."""

    base_url = "https://5sim.net/v1"
    demo_catalog = {
        "whatsapp": {
            "nigeria": {"any": {"cost": 0.05, "count": 42}, "mtn": {"cost": 0.06, "count": 18}, "airtel": {"cost": 0.055, "count": 24}},
            "usa": {"any": {"cost": 0.08, "count": 31}},
            "india": {"any": {"cost": 0.04, "count": 58}},
            "united_kingdom": {"any": {"cost": 0.09, "count": 14}},
        },
        "telegram": {
            "nigeria": {"any": {"cost": 0.05, "count": 33}},
            "usa": {"any": {"cost": 0.08, "count": 22}},
            "india": {"any": {"cost": 0.04, "count": 41}},
        },
        "google": {
            "nigeria": {"any": {"cost": 0.07, "count": 19}},
            "usa": {"any": {"cost": 0.10, "count": 17}},
        },
        "facebook": {
            "nigeria": {"any": {"cost": 0.06, "count": 26}},
            "usa": {"any": {"cost": 0.09, "count": 20}},
        },
    }

    @classmethod
    def _key(cls):
        return Setting.value("provider_api_key", "") or settings.FIVESIM_API_KEY

    @classmethod
    def _request(cls, endpoint, authenticated=True):
        headers = {"Accept": "application/json", "User-Agent": "VerifySMS-Django/1.0"}
        if authenticated:
            key = cls._key()
            if not key:
                return {"error": "API key not configured. See Admin → Providers."}
            headers["Authorization"] = f"Bearer {key}"
        try:
            request = Request(cls.base_url + endpoint, headers=headers)
            with urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
            return data if isinstance(data, (dict, list)) else {"error": "invalid json response"}
        except HTTPError as exc:
            return {"error": f"api error {exc.code}"}
        except (URLError, TimeoutError, OSError) as exc:
            return {"error": f"connection error: {exc}"}
        except (ValueError, UnicodeDecodeError):
            return {"error": "invalid json response"}

    @classmethod
    def get_countries(cls):
        if not cls._key():
            return {
                "nigeria": {"iso": {"ng": "Nigeria"}, "text_en": "Nigeria"},
                "usa": {"iso": {"us": "United States"}, "text_en": "United States"},
                "india": {"iso": {"in": "India"}, "text_en": "India"},
                "united_kingdom": {"iso": {"gb": "United Kingdom"}, "text_en": "United Kingdom"},
            }
        data = cls._request("/guest/countries", authenticated=False)
        return {} if "error" in data else data

    @classmethod
    def get_products(cls, country="any", operator="any"):
        if not cls._key():
            if country == "any":
                return {name: {"Qty": sum(v.get("any", {}).get("count", 0) for v in countries.values()), "Category": "activation"} for name, countries in cls.demo_catalog.items()}
            result = {}
            for product, countries in cls.demo_catalog.items():
                info = countries.get(country, {}).get(operator) or countries.get(country, {}).get("any")
                if info:
                    result[product] = {"Price": info["cost"], "Qty": info["count"], "Category": "activation"}
            return result
        data = cls._request(f"/guest/products/{quote(country)}/{quote(operator)}", authenticated=False)
        return {} if "error" in data else data

    @classmethod
    def get_prices(cls, country="", product=""):
        if not cls._key():
            if product:
                return {product: cls.demo_catalog.get(product, {})}
            return cls.demo_catalog
        query = urlencode({key: value for key, value in {"country": country, "product": product}.items() if value})
        data = cls._request("/guest/prices" + (f"?{query}" if query else ""), authenticated=False)
        return {} if "error" in data else data

    @classmethod
    def buy_activation(cls, country, operator, product):
        if not cls._key():
            info = cls.demo_catalog.get(product, {}).get(country, {}).get(operator) or cls.demo_catalog.get(product, {}).get(country, {}).get("any")
            if not info or not info["count"]:
                return {"error": "no free phones"}
            return {
                "id": secrets.randbelow(900000) + 100000,
                "phone": "+2348000000000",
                "product": product,
                "country": country,
                "operator": operator,
                "price": info["cost"],
                "status": "PENDING",
                "expires": (timezone.now() + timedelta(minutes=20)).isoformat(),
                "sms": [],
            }
        return cls._request(f"/user/buy/activation/{quote(country)}/{quote(operator)}/{quote(product)}")

    @classmethod
    def order_action(cls, action, provider_order_id):
        if not cls._key():
            return {"status": {"cancel": "CANCELED", "finish": "FINISHED", "ban": "BANNED"}.get(action, "PENDING")}
        return cls._request(f"/user/{action}/{provider_order_id}")

    @classmethod
    def check_order(cls, provider_order_id):
        if not cls._key():
            return {"status": "PENDING", "sms": []}
        return cls._request(f"/user/check/{provider_order_id}")

    @classmethod
    def sms_inbox(cls, provider_order_id):
        if not cls._key():
            return {"sms": []}
        return cls._request(f"/user/sms/inbox/{provider_order_id}")


class Paystack:
    base_url = "https://api.paystack.co"

    @classmethod
    def secret_key(cls):
        return Setting.value("paystack_secret_key", "") or settings.PAYSTACK_SECRET_KEY

    @classmethod
    def public_key(cls):
        return Setting.value("paystack_public_key", "") or settings.PAYSTACK_PUBLIC_KEY

    @classmethod
    def request(cls, method, endpoint, data=None):
        key = cls.secret_key()
        if not key:
            return {"status": False, "message": "Paystack secret key not set"}
        body = json.dumps(data or {}).encode("utf-8") if method == "POST" else None
        request = Request(
            cls.base_url + endpoint,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                # Paystack's edge (Cloudflare) 403s the default urllib User-Agent
                # with "error code: 1010"; a real UA is required to pass.
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            message = f"Paystack request failed: {exc}"
            if detail:
                try:
                    parsed = json.loads(detail)
                    if isinstance(parsed, dict) and parsed.get("message"):
                        message = f"Paystack request failed (HTTP {exc.code}): {parsed['message']}"
                    else:
                        message = f"{message} — {detail[:300]}"
                except ValueError:
                    message = f"{message} — {detail[:300]}"
            return {"status": False, "message": message}
        except (URLError, OSError, ValueError) as exc:
            return {"status": False, "message": f"Paystack request failed: {exc}"}

    @classmethod
    def initialize(cls, email, amount, reference, metadata=None):
        return cls.request("POST", "/transaction/initialize", {
            "email": email,
            "amount": int(money(amount) * 100),
            "reference": reference,
            "callback_url": f"{settings.SITE_URL}/api/paystack/callback/",
            "metadata": metadata or {},
            "currency": "NGN",
        })

    @classmethod
    def verify(cls, reference):
        return cls.request("GET", f"/transaction/verify/{quote(reference)}")

    @classmethod
    def validate_webhook(cls, payload, signature):
        key = cls.secret_key()
        return bool(key and hmac.compare_digest(hmac.new(key.encode(), payload, hashlib.sha512).hexdigest(), signature or ""))

    @classmethod
    def process_topup(cls, user_id, reference, amount_paid, gateway_response=None):
        existing = Topup.objects.filter(reference=reference).first()
        if existing and existing.status == "success":
            return True
        amount_paid = money(amount_paid)
        fee_percent = setting_number("topup_fee_percent", 3)
        fee = money(amount_paid * fee_percent / 100)
        net = money(amount_paid - fee)
        user = User.objects.get(pk=user_id)
        topup, _ = Topup.objects.update_or_create(
            reference=reference,
            defaults={"user": user, "amount_paid": amount_paid, "fee_amount": fee, "net_credited": net, "status": "success", "gateway_response": json.dumps(gateway_response or {})},
        )
        if not Transaction.objects.filter(reference=reference, type="topup").exists():
            return credit_balance(user_id, net, "topup", f"Wallet top-up via Paystack (Ref: {reference})", reference)
        return True
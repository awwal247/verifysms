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
    extra = setting_number("extra_fee_percent", 8)
    return money(Decimal(str(provider_price)) * rate * (1 + markup / 100) * (1 + extra / 100))


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
    """Map a provider error token onto something the buyer can act on."""
    raw = str(raw or "").upper().strip()
    messages = {
        "BAD_KEY": "Service temporarily unavailable. Please try again later.",
        "NO_BALANCE": "Service temporarily unavailable. Please try again later.",
        "NO_NUMBERS": "No numbers available for this selection right now. Try a different country or service.",
        "BAD_SERVICE": "Invalid service selected.",
        "BAD_ACTION": "Invalid request to provider.",
        "BAD_STATUS": "This order can no longer be updated.",
        "NO_ACTIVATION": "Order not found.",
        "BANNED": "This service has been reported and is no longer available.",
        "SERVICE_UNAVAILABLE_REGION": "This service is not available in your region.",
        "ACCESS_CANCEL": "This order was already cancelled.",
        "EMPTY_RESPONSE": "Provider returned an unexpected response.",
        "INVALID_JSON": "Provider returned an unexpected response.",
        "BAD_COUNTRY": "Invalid country selected.",
    }
    for key, message in messages.items():
        if key in raw:
            return message
    if raw.startswith("HTTP_5") or "TIMEOUT" in raw:
        return "Provider is temporarily unavailable. Please try again."
    if raw.startswith("HTTP_"):
        return "Connection to provider failed. Please try again."
    return "An error occurred. Please try again."


class GrizzlySms:
    """GrizzlySMS adapter — the app's only SMS number provider.

    GrizzlySMS speaks the sms-activate wire protocol: a single
    ``handler_api.php`` endpoint, a bare ``api_key`` query parameter (not a
    bearer header), and responses that are colon-delimited plain text rather
    than JSON. Every call returns a parsed dict; ``{"error": ...}`` marks a
    provider-level failure the caller surfaces to the user.

    Prices and balances are quoted in USD.
    """

    base_url = "https://api.grizzlysms.com/stubs/handler_api.php"

    # GrizzlySMS identifies services by short code, not product name. Keys are
    # the codes the API expects; values are what the storefront shows. Codes
    # not listed here are used verbatim.
    service_labels = {
        "wa": "WhatsApp", "tg": "Telegram", "go": "Google", "fb": "Facebook",
        "ig": "Instagram", "tw": "Twitter", "oi": "Tinder", "am": "Amazon",
        "mm": "Microsoft", "nf": "Netflix", "bb": "Uber", "vi": "Viber",
        "ds": "Discord", "sn": "Snapchat", "tt": "TikTok", "av": "Avito",
        "fd": "Foodpanda", "vk": "VKontakte", "ok": "Odnoklassniki",
    }

    demo_countries = {
        "22": ("Nigeria", "ng"), "18": ("India", "in"), "187": ("United States", "us"),
        "16": ("United Kingdom", "gb"), "6": ("Indonesia", "id"), "12": ("Philippines", "ph"),
        "49": ("Germany", "de"), "36": ("Canada", "ca"), "0": ("Russia", "ru"),
        "2": ("Kazakhstan", "kz"), "10": ("Vietnam", "vn"), "1": ("Ukraine", "ua"),
    }

    demo_prices = {
        "wa": {"22": 0.35, "18": 0.42, "187": 0.55, "16": 0.48},
        "tg": {"22": 0.29, "18": 0.33, "187": 0.45, "16": 0.51},
        "go": {"22": 0.44, "18": 0.52, "187": 0.72},
        "fb": {"22": 0.30, "18": 0.38, "187": 0.61},
        "ig": {"22": 0.41, "18": 0.47, "187": 0.68},
        "tt": {"22": 0.33, "18": 0.40, "187": 0.58},
    }

    @classmethod
    def _key(cls):
        return Setting.value("provider_api_key", "") or settings.GRIZZLY_API_KEY

    @classmethod
    def _request(cls, params):
        """Call the activation endpoint. Returns a parsed dict, or
        ``{"error": <token>}`` — the plain-text wire format is normalised here
        so no caller has to know it."""
        key = cls._key()
        if not key:
            return {"error": "BAD_KEY"}
        query = urlencode({"api_key": key, **{k: v for k, v in params.items() if v not in (None, "")}})
        request = Request(cls.base_url + "?" + query, headers={
            "Accept": "text/plain, application/json",
            "User-Agent": "VerifySMS-Django/1.0",
        })
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            return {"error": f"HTTP_{exc.code}"}
        except (URLError, TimeoutError, OSError):
            return {"error": "SERVICE_UNAVAILABLE_REGION"}
        return cls._parse(body)

    @staticmethod
    def _parse(body):
        """Normalise a GrizzlySMS reply into a dict.

        Success shapes: ``ACCESS_BALANCE:12.34``, ``ACCESS_NUMBER:id:phone``,
        ``STATUS_OK:1234``, ``ACCESS_READY``. JSON replies (getPrices,
        getCountries, getServicesList) decode as-is. A bare error token
        becomes ``{"error": token}``.
        """
        if not body:
            return {"error": "EMPTY_RESPONSE"}
        if body[0] in "{[":
            try:
                return json.loads(body)
            except ValueError:
                return {"error": "INVALID_JSON"}
        head, _, tail = body.partition(":")
        head, tail = head.strip(), tail.strip()
        if head == "ACCESS_NUMBER":
            activation_id, _, phone = tail.partition(":")
            return {"id": activation_id.strip(), "phone": phone.strip()}
        if head in {"ACCESS_BALANCE", "ACCESS_ACTIVATION", "ACCESS_READY",
                    "ACCESS_RETRY_GET", "STATUS_WAIT_CODE", "STATUS_WAIT_RESEND",
                    "STATUS_CANCEL", "STATUS_OK", "STATUS_WAIT_RETRY"}:
            return {"status": head, "value": tail} if tail else {"status": head}
        return {"error": body.split("\n")[0][:120]}

    # -- catalog -----------------------------------------------------------

    @classmethod
    def get_countries(cls):
        """``{country_id: {"id", "eng", "iso"}}``. The id is what every other
        call takes, so it doubles as the storefront's internal slug."""
        if not cls._key():
            return {cid: {"id": int(cid), "eng": name, "iso": iso}
                    for cid, (name, iso) in cls.demo_countries.items()}
        data = cls._request({"action": "getCountries"})
        if not isinstance(data, dict) or "error" in data:
            return {}
        items = data.get("countries") if isinstance(data.get("countries"), list) else data
        out = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id") is not None:
                out[str(item["id"])] = {
                    "id": item["id"],
                    "eng": item.get("eng") or item.get("rus") or str(item["id"]),
                    "iso": str(item.get("iso") or "").lower(),
                }
        return out

    @classmethod
    def get_services(cls):
        """Service-code -> human name, from getServicesList.

        Returns {} when the endpoint is unreachable or the key is absent, so
        callers fall back to the local service_labels map rather than blowing
        up on an offline/demo container.
        """
        try:
            data = cls._request({"action": "getServicesList"})
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for item in data.get("services") or []:
            code = str(item.get("code", "")).strip()
            if code:
                out[code] = str(item.get("name") or code)
        return out

    @classmethod
    def get_products(cls, country="any", operator="any"):
        """``{service_code: {"Price", "Qty", "Category"}}`` for one country.

        GrizzlySMS has no products endpoint; the catalog comes from
        /getPricesV3, which is keyed by service code. That endpoint does not
        expose stock, so a service carrying a price counts as available.
        """
        if not cls._key():
            result = {}
            for code, by_country in cls.demo_prices.items():
                cost = by_country.get(str(country)) or by_country.get("22")
                if cost:
                    result[code] = {"Price": cost, "Qty": 25, "Category": "activation"}
            return result
        if str(country) in ("", "any"):
            return {}
        return {code: {"Price": info["price"], "Qty": info.get("count", 1), "Category": "activation"}
                for code, info in cls._prices_for(str(country), "").items()}

    @classmethod
    def _prices_for(cls, country, product=""):
        """``{service_code: {"price": float, "count": int}}`` for one country."""
        params = {"action": "getPricesV3", "country": str(country)}
        if product:
            params["service"] = product
        data = cls._request(params)
        if not isinstance(data, dict) or "error" in data:
            return {}
        block = data.get(str(country))
        if not isinstance(block, dict):
            return {}
        services = block.get("services") if isinstance(block.get("services"), dict) else block
        out = {}
        for code, info in services.items():
            if not isinstance(info, dict):
                continue
            try:
                price = float(info.get("price", info.get("cost")))
            except (TypeError, ValueError):
                continue
            out[str(code)] = {"price": price, "count": int(info.get("count", info.get("qty", 1)) or 1)}
        return out

    @classmethod
    def get_prices(cls, country="", product=""):
        """``{country_id: {service_code: {"cost", "count"}}}`` — the shape the
        catalog views descend into. Only the requested country is returned."""
        if not cls._key():
            cid = str(country or "22")
            codes = [product] if product else list(cls.demo_prices)
            return {cid: {code: {"cost": cls.demo_prices.get(code, {}).get(cid, 0.4), "count": 25}
                          for code in codes}}
        if str(country) in ("", "any"):
            return {}
        return {str(country): {code: {"cost": info["price"], "count": info["count"]}
                               for code, info in cls._prices_for(country, product).items()}}

    # -- activations -------------------------------------------------------

    @classmethod
    def get_balance(cls):
        if not cls._key():
            return {"balance": 0.0, "demo": True}
        data = cls._request({"action": "getBalance"})
        if "error" in data:
            return data
        try:
            return {"balance": float(data.get("value") or 0)}
        except (TypeError, ValueError):
            return {"error": "INVALID_JSON"}

    @classmethod
    def buy_activation(cls, country, operator, product, max_price=None):
        """Book a number. Returns ``{"id", "phone", "status"}``."""
        if not cls._key():
            return {"id": str(secrets.randbelow(9000000) + 1000000),
                    "phone": "2348000000000", "service": product,
                    "country": country, "status": "PENDING"}
        if str(country) in ("", "any"):
            return {"error": "NO_NUMBERS"}
        data = cls._request({
            "action": "getNumber", "service": product, "country": str(country),
            "maxPrice": max_price if max_price else None,
        })
        if "error" in data:
            return data
        if not data.get("id"):
            return {"error": "NO_NUMBERS"}
        return data

    @classmethod
    def check_order(cls, provider_order_id):
        """Activation status, normalised onto Order.STATUS_CHOICES."""
        if not cls._key():
            return {"status": "PENDING", "sms": []}
        data = cls._request({"action": "getStatusV2", "id": str(provider_order_id)})
        if "error" in data:
            return data
        sms_block = data.get("sms") if isinstance(data, dict) else None
        if isinstance(sms_block, dict):
            return {"status": "RECEIVED", "sms": [{
                "sender": "",
                "text": sms_block.get("text") or sms_block.get("code") or "",
                "code": sms_block.get("code") or "",
                "date": sms_block.get("dateTime") or "",
            }]}
        raw = data.get("status", "") if isinstance(data, dict) else str(data)
        return {"status": cls._normalise_status(raw, data), "sms": []}

    @staticmethod
    def _normalise_status(raw, data=None):
        token = str(raw or "").upper()
        if data and isinstance(data, dict) and data.get("sms"):
            return "RECEIVED"
        if token.startswith("STATUS_OK") or token.startswith("ACCESS_ACTIVATION"):
            return "RECEIVED"
        if token in {"STATUS_CANCEL", "STATUS_CANCELLED"}:
            return "CANCELED"
        if token in {"STATUS_WAIT_RESEND", "STATUS_WAIT_RETRY", "STATUS_WAIT_CODE",
                     "ACCESS_READY", "ACCESS_RETRY_GET"}:
            return "PENDING"
        if token in {"STATUS_TIMEOUT", "EXPIRED"}:
            return "TIMEOUT"
        return "PENDING"

    @classmethod
    def order_action(cls, action, provider_order_id):
        """cancel -> -1, finish -> 6, ban -> 8 (reported)."""
        code = {"cancel": "-1", "finish": "6", "ban": "8"}.get(action)
        if code is None:
            return {"error": "BAD_ACTION"}
        if not cls._key():
            return {"status": {"cancel": "STATUS_CANCEL", "finish": "ACCESS_ACTIVATION",
                               "ban": "STATUS_CANCEL"}[action]}
        return cls._request({"action": "setStatus", "status": code, "id": str(provider_order_id)})

    @classmethod
    def sms_inbox(cls, provider_order_id):
        return {"sms": cls.check_order(provider_order_id).get("sms", [])}


def provider_price(product, country, operator="any"):
    """Best price for one product/country slot.

    There is a single provider now, so this is a thin lookup that keeps the
    callers, which used to compare providers, unchanged in shape. Returns
    ``{"cost", "provider", "count"}`` or None when the slot has no price.
    """
    try:
        prices = GrizzlySms.get_prices(country, product)
    except Exception:
        return None
    if not isinstance(prices, dict) or not prices:
        return None
    block = prices.get(str(country)) or next(iter(prices.values()), None)
    if not isinstance(block, dict):
        return None
    info = block.get(product)
    if not isinstance(info, dict):
        return None
    try:
        cost = float(info.get("cost", info.get("price", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if cost <= 0:
        return None
    return {"cost": cost, "provider": "grizzly", "count": int(info.get("count", 0) or 0)}


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
        # Tiered processing fee: higher rate once the top-up passes the threshold.
        low_pct = setting_number("topup_fee_percent", 3)
        high_pct = setting_number("topup_fee_percent_high", 6)
        threshold = setting_number("topup_fee_threshold", 1000)
        fee_percent = high_pct if amount_paid > threshold else low_pct
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
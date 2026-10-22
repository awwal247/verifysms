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
    raw = str(raw or "").upper().strip().replace(" ", "_")
    messages = {
        "BAD_KEY": "Service temporarily unavailable. Please try again later.",
        "BAD_TOKEN": "Service temporarily unavailable. Please try again later.",
        "PROVIDER_DOWN": "Provider is temporarily unavailable. Please try again.",
        "NO_FREE_PHONES": "No numbers available for this selection right now. Try a different country or service.",
        "BAD_OPERATOR": "Invalid operator selected.",
        "BAD_PRODUCT": "Invalid service selected.",
        "ORDER_NOT_FOUND": "Order not found.",
        "NOT_FOUND": "Order not found.",
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


class FiveSim:
    """5sim.net adapter — the storefront's primary number provider (server 1).

    5sim exposes a JSON REST API with Bearer-token auth. Its catalog endpoints
    are guest-accessible, so countries and prices render even before a key is
    saved; buying and order management need the key. Prices are quoted in US
    dollars, the same unit GrizzlySMS uses, so the storefront has one site
    rate for both servers.
    """

    base_url = "https://5sim.net/v1"

    service_labels = {
        "whatsapp": "WhatsApp", "telegram": "Telegram", "google": "Google",
        "facebook": "Facebook", "instagram": "Instagram", "twitter": "Twitter",
        "tinder": "Tinder", "amazon": "Amazon", "microsoft": "Microsoft",
        "netflix": "Netflix", "uber": "Uber", "viber": "Viber",
        "discord": "Discord", "snapchat": "Snapchat", "tiktok": "TikTok",
        "avito": "Avito", "foodpanda": "Foodpanda", "vk": "VKontakte",
        "ok": "Odnoklassniki", "signal": "Signal",
    }

    @classmethod
    def _key(cls):
        return Setting.value("fivesim_api_key", "") or settings.FIVESIM_API_KEY

    @classmethod
    def configured(cls):
        return bool(cls._key())

    @staticmethod
    def _to_usd(value):
        """Normalise a 5sim amount. 5sim quotes prices in US dollars."""
        try:
            return round(float(value), 6)
        except (TypeError, ValueError, InvalidOperation):
            return 0.0

    @classmethod
    def _request(cls, path, params=None, auth=False):
        """Call one 5sim endpoint. Returns parsed JSON or ``{"error": token}``."""
        key = cls._key()
        if auth and not key:
            return {"error": "BAD_KEY"}
        url = cls.base_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
        headers = {"Accept": "application/json", "User-Agent": "VerifySMS-Django/1.0"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            return {"error": cls._error_token(body, exc.code)}
        except (URLError, TimeoutError, OSError):
            return {"error": "PROVIDER_DOWN"}
        if not body:
            return {"error": "EMPTY_RESPONSE"}
        try:
            return json.loads(body)
        except ValueError:
            return {"error": body[:120]}

    @staticmethod
    def _error_token(body, status_code):
        text = ""
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    text = str(parsed.get("error") or parsed.get("message") or body)
                else:
                    text = str(parsed)
            except ValueError:
                text = body
        token = (text or f"HTTP_{status_code}").strip().splitlines()[0][:120]
        return token or f"HTTP_{status_code}"

    # -- catalog -----------------------------------------------------------

    @classmethod
    def get_countries(cls):
        """``{slug: {"id": slug, "eng": name, "iso": code}}``."""
        data = cls._request("/guest/countries")
        if not isinstance(data, dict) or "error" in data:
            return {}
        out = {}
        for slug, item in data.items():
            if not isinstance(item, dict):
                continue
            iso = item.get("iso")
            if isinstance(iso, dict):
                # 5sim returns {"ng": 1} — the code is the key.
                iso = next(iter(iso), "")
            out[str(slug)] = {
                "id": str(slug),
                "eng": item.get("text_en") or item.get("text_ru") or str(slug),
                "iso": str(iso or "").lower(),
            }
        return out

    @classmethod
    def get_services(cls):
        """5sim has no service-list endpoint; labels come from the local map."""
        return dict(cls.service_labels)

    @classmethod
    def get_products(cls, country="any", operator="any"):
        """``{product: {"Price": usd, "Qty", "Category"}}`` for one country."""
        if str(country) in ("", "any"):
            return {}
        data = cls._request(
            f"/guest/products/{quote(str(country), safe='')}/{quote(str(operator or 'any'), safe='')}"
        )
        if not isinstance(data, dict) or "error" in data:
            return {}
        out = {}
        for product, info in data.items():
            if not isinstance(info, dict):
                continue
            price = cls._to_usd(info.get("Price"))
            try:
                qty = int(float(info.get("Qty", 0) or 0))
            except (TypeError, ValueError):
                qty = 0
            if price > 0 and qty > 0:
                out[str(product)] = {"Price": price, "Qty": qty, "Category": "activation"}
        return out

    @classmethod
    def _price_block(cls, operators):
        """Best operator price for one product: ``{"cost", "count"}`` or None."""
        if not isinstance(operators, dict):
            return None
        entries = [operators] if "cost" in operators else [
            value for value in operators.values() if isinstance(value, dict)
        ]
        costs = [cls._to_usd(entry.get("cost")) for entry in entries if entry.get("cost") is not None]
        costs = [cost for cost in costs if cost > 0]
        if not costs:
            return None
        try:
            count = sum(int(float(entry.get("count", 0) or 0)) for entry in entries)
        except (TypeError, ValueError):
            count = 0
        return {"cost": min(costs), "count": count}

    @classmethod
    def get_operator_prices(cls, country, product):
        """``{operator: {"cost", "count"}}`` for one country/product.

        Unlike :meth:`get_prices`, this keeps the operator dimension intact:
        5sim sells the same product through many carriers (``virtual34``,
        ``virtual51``, ``o2``, …), each with its own price and stock. Folding
        them into one "best" number hides the real offers and makes the
        cheapest quote a blend of unrelated suppliers. Zero-stock operators
        are returned with ``count == 0`` so callers can show them as
        unavailable rather than silently skipping the name.
        """
        if str(country) in ("", "any") or not product:
            return {}
        data = cls._request(
            "/guest/prices", {"country": str(country), "product": str(product)}
        )
        if not isinstance(data, dict) or "error" in data:
            return {}
        block = data.get(str(country))
        if not isinstance(block, dict):
            block = next((value for value in data.values() if isinstance(value, dict)), {})
        operators = block.get(str(product)) if isinstance(block, dict) else None
        if not isinstance(operators, dict):
            return {}
        out = {}
        for name, entry in operators.items():
            if not isinstance(entry, dict):
                continue
            cost = cls._to_usd(entry.get("cost"))
            if cost <= 0:
                continue
            try:
                count = int(float(entry.get("count", 0) or 0))
            except (TypeError, ValueError):
                count = 0
            out[str(name)] = {"cost": cost, "count": count}
        return out

    @classmethod
    def get_prices(cls, country="", product=""):
        """``{country_slug: {product: {"cost": usd, "count"}}}``.

        The endpoint changes shape with its filters: with a country it is
        ``{country: {product: {operator: {...}}}}``; with only a product it is
        ``{product: {country: {operator: {...}}}}``; with neither it returns
        every country. All three are normalised to the country-first shape.
        """
        params = {}
        if str(country) not in ("", "any"):
            params["country"] = str(country)
        if product:
            params["product"] = str(product)
        data = cls._request("/guest/prices", params)
        if not isinstance(data, dict) or "error" in data:
            return {}
        out = {}
        product_only = bool(product) and str(country) in ("", "any")
        for top_key, block in data.items():
            if not isinstance(block, dict):
                continue
            if product_only:
                # top_key is the product; block is country -> operators.
                for country_slug, operators in block.items():
                    price = cls._price_block(operators)
                    if price:
                        out.setdefault(str(country_slug), {})[str(top_key)] = price
                continue
            # top_key is the country; block is product -> operators.
            for product_slug, operators in block.items():
                price = cls._price_block(operators)
                if price:
                    out.setdefault(str(top_key), {})[str(product_slug)] = price
        return out

    # -- activations -------------------------------------------------------

    @classmethod
    def get_balance(cls):
        if not cls._key():
            return {"error": "BAD_KEY"}
        data = cls._request("/user/balance", auth=True)
        if not isinstance(data, dict) or "error" in data:
            return data
        try:
            return {"balance": float(data.get("balance") or 0)}
        except (TypeError, ValueError):
            return {"error": "INVALID_JSON"}

    @classmethod
    def buy_activation(cls, country, operator, product, max_price=None):
        """Book a number. Returns ``{"id", "phone", "status", "price_usd"}``."""
        if str(country) in ("", "any"):
            return {"error": "NO_NUMBERS"}
        data = cls._request(
            f"/user/buy/activation/{quote(str(country), safe='')}/"
            f"{quote(str(operator or 'any'), safe='')}/{quote(str(product), safe='')}",
            auth=True,
        )
        if not isinstance(data, dict) or "error" in data:
            return data if isinstance(data, dict) else {"error": "EMPTY_RESPONSE"}
        if not data.get("id"):
            return {"error": "NO_NUMBERS"}
        return {
            "id": str(data["id"]),
            "phone": str(data.get("phone", "")),
            "status": str(data.get("status", "PENDING")).upper(),
            "price_usd": cls._to_usd(data.get("price")),
        }

    @classmethod
    def check_order(cls, provider_order_id):
        """Activation status, normalised onto Order.STATUS_CHOICES."""
        data = cls._request(f"/user/check/{quote(str(provider_order_id), safe='')}", auth=True)
        if not isinstance(data, dict) or "error" in data:
            return data if isinstance(data, dict) else {"error": "EMPTY_RESPONSE"}
        sms = []
        for message in data.get("sms") or []:
            if not isinstance(message, dict):
                continue
            sms.append({
                "sender": message.get("sender") or "",
                "text": message.get("text") or "",
                "code": message.get("code") or "",
                "date": message.get("created_at") or message.get("date") or "",
            })
        return {"status": cls._normalise_status(data.get("status")), "sms": sms}

    @staticmethod
    def _normalise_status(raw):
        token = str(raw or "").upper()
        if token == "CANCELLED":
            return "CANCELED"
        if token in {"PENDING", "RECEIVED", "FINISHED", "CANCELED", "TIMEOUT"}:
            return token
        return "PENDING"

    @classmethod
    def order_action(cls, action, provider_order_id):
        endpoint = {"cancel": "cancel", "finish": "finish", "ban": "ban"}.get(action)
        if endpoint is None:
            return {"error": "BAD_ACTION"}
        data = cls._request(f"/user/{endpoint}/{quote(str(provider_order_id), safe='')}", auth=True)
        if not isinstance(data, dict) or "error" in data:
            return data if isinstance(data, dict) else {"error": "EMPTY_RESPONSE"}
        return {"status": cls._normalise_status(data.get("status")), "raw": data}

    @classmethod
    def sms_inbox(cls, provider_order_id):
        return {"sms": cls.check_order(provider_order_id).get("sms", [])}


class GrizzlySms:
    """GrizzlySMS adapter — the app's fallback SMS number provider (server 2).

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


# ---------------------------------------------------------------------------
# Provider routing
#
# Two providers sit behind one storefront. 5sim is server 1, GrizzlySMS is
# server 2; the buyer never picks one. The catalog comes from the first
# provider that answers, the quote is the cheapest live price across both,
# and every purchase falls through to the other server on any provider error.
#
# The storefront's internal identifiers stay Grizzly-shaped (numeric country
# ids, short service codes) because that is what the UI and saved orders
# already use. The maps below translate them for 5sim; identifiers 5sim has
# and Grizzly does not (extra countries, products) pass through as-is.
# ---------------------------------------------------------------------------

PROVIDER_ORDER = ("5sim", "grizzly")
PROVIDER_LABELS = {"5sim": "5SIM", "grizzly": "GrizzlySMS", "demo": "Demo"}
_PROVIDERS = {"5sim": FiveSim, "grizzly": GrizzlySms}

# The storefront offers the two suppliers as Server 1 and Server 2. The
# numbering is the owner's: server 1 is GrizzlySMS, server 2 is 5SIM.
SERVER_ORDER = ("grizzly", "5sim")
SERVER_NUMBERS = {"grizzly": "1", "5sim": "2"}

# GrizzlySMS country id -> 5sim country slug. Source: the numbered country
# list published in Grizzly's own docs (grizzlysms.com/docs/activation,
# pulled 2026-10-19). Do NOT guess these ids: they are not the sms-activate
# ids people tend to remember (Nigeria is 19, not 22; India is 22, not 18;
# the Philippines is 4, not 12; Germany is 43, not 49). A wrong id buys a
# working number in the wrong country, which fails after the customer paid.
# 5sim lists 153 countries; 152 have a Grizzly counterpart (no Solomon
# Islands). 5sim does not carry Russia/Ukraine/Belarus, so they are absent.
GRIZZLY_TO_FIVESIM_COUNTRY = {
    "2": "kazakhstan", "4": "philippines", "6": "indonesia", "7": "malaysia",
    "8": "kenya", "9": "tanzania", "10": "vietnam", "11": "kyrgyzstan",
    "13": "israel", "14": "hongkong", "15": "poland", "16": "england",
    "17": "madagascar", "19": "nigeria", "20": "macau", "21": "egypt",
    "22": "india", "23": "ireland", "24": "cambodia", "25": "laos",
    "26": "haiti", "27": "ivorycoast", "28": "gambia", "29": "serbia",
    "31": "southafrica", "32": "romania", "33": "colombia", "34": "estonia",
    "35": "azerbaijan", "36": "canada", "37": "morocco", "38": "ghana",
    "39": "argentina", "40": "uzbekistan", "41": "cameroon", "42": "chad",
    "43": "germany", "44": "lithuania", "45": "croatia", "46": "sweden",
    "48": "netherlands", "49": "latvia", "50": "austria", "52": "thailand",
    "53": "saudiarabia", "54": "mexico", "55": "taiwan", "56": "spain",
    "58": "algeria", "59": "slovenia", "60": "bangladesh", "61": "senegal",
    "63": "czech", "64": "srilanka", "65": "peru", "66": "pakistan",
    "68": "guinea", "70": "venezuela", "71": "ethiopia", "72": "mongolia",
    "73": "brazil", "74": "afghanistan", "75": "uganda", "76": "angola",
    "77": "cyprus", "78": "france", "79": "papuanewguinea", "80": "mozambique",
    "81": "nepal", "82": "belgium", "83": "bulgaria", "84": "hungary",
    "85": "moldova", "86": "italy", "87": "paraguay", "88": "honduras",
    "89": "tunisia", "90": "nicaragua", "91": "easttimor", "92": "bolivia",
    "93": "costarica", "94": "guatemala", "97": "puertorico", "99": "togo",
    "100": "kuwait", "101": "salvador", "103": "jamaica", "104": "tit",
    "105": "ecuador", "106": "swaziland", "107": "oman", "108": "bih",
    "109": "dominicana", "112": "panama", "114": "mauritania", "115": "sierraleone",
    "116": "jordan", "117": "portugal", "118": "barbados", "119": "burundi",
    "120": "benin", "122": "bahamas", "123": "botswana", "124": "belize",
    "128": "georgia", "129": "greece", "130": "guineabissau", "131": "guyana",
    "133": "comoros", "134": "saintkittsandnevis", "135": "liberia", "136": "lesotho",
    "137": "malawi", "138": "namibia", "140": "rwanda", "141": "slovakia",
    "142": "suriname", "143": "tajikistan", "145": "bahrain", "146": "reunion",
    "147": "zambia", "148": "armenia", "150": "congo", "151": "chile",
    "152": "burkinafaso", "154": "gabon", "155": "albania", "156": "uruguay",
    "157": "mauritius", "158": "bhutane", "159": "maldives", "160": "guadeloupe",
    "161": "turkmenistan", "162": "frenchguiana", "163": "finland", "164": "saintlucia",
    "165": "luxembourg", "166": "saintvincentandgrenadines", "167": "equatorialguinea", "168": "djibouti",
    "169": "antiguaandbarbuda", "171": "montenegro", "172": "denmark", "174": "norway",
    "175": "australia", "179": "aruba", "183": "northmacedonia", "184": "seychelles",
    "185": "newcaledonia", "186": "capeverde", "187": "usa", "231": "samoa",
}
FIVESIM_TO_GRIZZLY_COUNTRY = {slug: cid for cid, slug in GRIZZLY_TO_FIVESIM_COUNTRY.items()}

GRIZZLY_TO_FIVESIM_SERVICE = {
    "wa": "whatsapp", "tg": "telegram", "go": "google", "fb": "facebook",
    "ig": "instagram", "tw": "twitter", "oi": "tinder", "am": "amazon",
    "mm": "microsoft", "nf": "netflix", "bb": "uber", "vi": "viber",
    "ds": "discord", "sn": "snapchat", "tt": "tiktok", "av": "avito",
    "fd": "foodpanda", "vk": "vk", "ok": "ok",
}
FIVESIM_TO_GRIZZLY_SERVICE = {slug: code for code, slug in GRIZZLY_TO_FIVESIM_SERVICE.items()}

# Services only one server carries. The canonical id is the 5sim slug because
# Grizzly has no code for them here; they simply do not appear on Server 1.
FIVESIM_ONLY_SERVICES = {"signal"}


def provider_configured():
    """``{provider: bool}`` — whether a key is saved for each server."""
    return {name: bool(adapter._key()) for name, adapter in _PROVIDERS.items()}


def demo_mode():
    """No key on either server — purchases are simulated, as before."""
    return not any(provider_configured().values())


def _live(provider):
    """Whether a server may take part in routing.

    A keyless server only acts as a simulator in demo mode (no key at all).
    Once any real key exists, keyless servers are skipped entirely — otherwise
    Grizzly's demo prices and simulated numbers would compete with, and could
    even beat, a live provider.
    """
    if demo_mode():
        return True
    return bool(_PROVIDERS[provider]._key())


def provider_label(name):
    return PROVIDER_LABELS.get(str(name), str(name))


def server_provider(value):
    """Map anything the UI sends as a server choice to a provider key.

    Accepts ``"1"`` / ``"2"`` (the button numbers), a provider name, or
    nothing / ``"any"`` — the latter means "no explicit server".
    """
    value = str(value or "").strip().lower()
    if value in ("", "any", "all", "auto"):
        return None
    if value in _PROVIDERS:
        return value
    return {"1": "grizzly", "2": "5sim"}.get(value)


def catalog_servers():
    """The two server buttons, in the owner's numbering."""
    return [{
        "key": SERVER_NUMBERS[name],
        "provider": name,
        "label": "Server " + SERVER_NUMBERS[name],
        "name": provider_label(name),
        "available": _live(name),
    } for name in SERVER_ORDER]


def service_label(code):
    code = str(code or "").lower()
    if code in GrizzlySms.service_labels:
        return GrizzlySms.service_labels[code]
    return FiveSim.service_labels.get(GRIZZLY_TO_FIVESIM_SERVICE.get(code, code), code.upper())


def _country_for(provider, canonical):
    canonical = str(canonical or "").strip().lower()
    if provider == "5sim":
        return GRIZZLY_TO_FIVESIM_COUNTRY.get(canonical, canonical or None)
    if canonical.isdigit():
        return canonical
    return FIVESIM_TO_GRIZZLY_COUNTRY.get(canonical)


def _service_for(provider, canonical):
    canonical = str(canonical or "").strip().lower()
    if provider == "5sim":
        return GRIZZLY_TO_FIVESIM_SERVICE.get(canonical, canonical or None)
    if canonical in GRIZZLY_TO_FIVESIM_SERVICE:
        return canonical
    return FIVESIM_TO_GRIZZLY_SERVICE.get(canonical)


def _canonical_country(provider, identifier):
    identifier = str(identifier)
    if provider == "5sim":
        return FIVESIM_TO_GRIZZLY_COUNTRY.get(identifier, identifier)
    return identifier


def _canonical_service(provider, identifier):
    identifier = str(identifier or "").lower()
    if provider == "5sim":
        return FIVESIM_TO_GRIZZLY_SERVICE.get(identifier, identifier)
    return identifier


def _catalog_names(provider):
    """Servers a catalog call may consult, in order."""
    if provider in _PROVIDERS:
        return [provider]
    return list(PROVIDER_ORDER)


def catalog_products(country, provider=None):
    """``{canonical_service: {"Price", "Qty", "Category"}}`` for one country.

    With ``provider`` the answer comes from that server only. Without it the
    first provider with a live catalog wins; an empty answer — timeout, bad
    key, unknown country — hands the request to the next server.
    """
    for name in _catalog_names(provider):
        if not _live(name):
            continue
        adapter = _PROVIDERS[name]
        provider_country = _country_for(name, country)
        if not provider_country:
            continue
        try:
            products = adapter.get_products(provider_country, "any")
        except Exception:
            products = {}
        if products:
            return {_canonical_service(name, code): info for code, info in products.items()}
    return {}


def catalog_services(country="", provider=None):
    """Service picker data. Empty country scans the first few countries.

    ``provider`` limits the list to one server; the service step runs before
    the server is chosen, so the default stays a union of both.
    """
    for name in _catalog_names(provider):
        if not _live(name):
            continue
        adapter = _PROVIDERS[name]
        products = {}
        try:
            if country not in ("", "any"):
                provider_country = _country_for(name, country)
                if provider_country:
                    products = adapter.get_products(provider_country, "any")
            else:
                for probe in list(adapter.get_countries())[:8]:
                    provider_country = _country_for(name, probe)
                    if provider_country:
                        products.update(adapter.get_products(provider_country, "any"))
        except Exception:
            products = {}
        if not products:
            continue
        data = {}
        for code, info in products.items():
            canonical = _canonical_service(name, code)
            # 5sim exposes 200+ products; the storefront keeps the curated
            # short-code set so the picker stays usable. Unknown slugs drop
            # out, except the few services only 5sim carries.
            if canonical not in GRIZZLY_TO_FIVESIM_SERVICE and canonical not in FIVESIM_ONLY_SERVICES:
                continue
            qty = int(info.get("Qty", 0) or 0)
            if qty <= 0:
                continue
            row = data.setdefault(canonical, {
                "name": service_label(canonical), "code": canonical,
                "category": "activation", "qty": 0,
            })
            row["qty"] += qty
        if data:
            return dict(sorted(data.items()))
    return {}


def catalog_countries(product="", provider=None):
    """Country picker data, optionally limited to countries pricing a service.

    ``provider`` limits the list to one server; the default is a union.
    """
    product = str(product or "").strip().lower()
    for name in _catalog_names(provider):
        if not _live(name):
            continue
        adapter = _PROVIDERS[name]
        try:
            labels = adapter.get_countries()
        except Exception:
            labels = {}
        if not labels:
            continue
        rows = {}
        if product:
            service = _service_for(name, product)
            if not service:
                continue
            try:
                prices = adapter.get_prices("", service)
            except Exception:
                prices = {}
            if not prices:
                # Grizzly has no bulk price endpoint — walk its country list.
                for identifier in labels:
                    try:
                        prices.update(adapter.get_prices(identifier, service))
                    except Exception:
                        continue
            for identifier, block in prices.items():
                info = block.get(service) if isinstance(block, dict) else None
                if not isinstance(info, dict):
                    continue
                try:
                    cost = float(info.get("cost", 0) or 0)
                    qty = int(info.get("count", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if cost <= 0 or qty <= 0:
                    continue
                item = labels.get(str(identifier), {})
                row = rows.setdefault(_canonical_country(name, identifier), {
                    "name": str(_canonical_country(name, identifier)),
                    "label": item.get("eng", str(identifier)),
                    "iso": item.get("iso", ""), "qty": 0, "price": None,
                })
                row["qty"] += qty
                if row["price"] is None or cost < row["price"]:
                    row["price"] = cost
        else:
            for identifier, item in labels.items():
                canonical = _canonical_country(name, identifier)
                rows.setdefault(canonical, {
                    "name": str(canonical), "label": item.get("eng", str(identifier)),
                    "iso": item.get("iso", ""), "qty": 0, "price": None,
                })
        if rows:
            return sorted(rows.values(), key=lambda row: (-row["qty"], row["label"]))
    return []


_OPERATOR_LABELS = {
    "any": "Any available number",
    "o2": "O2", "ee": "EE", "three": "Three", "vodafone": "Vodafone",
    "lycamobile": "Lycamobile", "tele2": "Tele2",
}


def _operator_label(name):
    """Human label for a 5sim operator slug."""
    name = str(name or "")
    if name in _OPERATOR_LABELS:
        return _OPERATOR_LABELS[name]
    if name.startswith("virtual") and name[7:].isdigit():
        return "Virtual " + name[7:]
    return name.replace("_", " ").title()


def _operator_rows(provider, country, product):
    """Offer rows for one server only.

    5sim prices each carrier separately, so every stocked operator becomes
    its own row and the "any" row on top is the cheapest of those rows — a
    comparison across operators, never a blend. A server without an operator
    dimension contributes a single "any" row.
    """
    rows = []
    if not _live(provider):
        return rows
    adapter = _PROVIDERS[provider]
    provider_country = _country_for(provider, country)
    provider_service = _service_for(provider, product)
    if not provider_country or not provider_service:
        return rows
    if hasattr(adapter, "get_operator_prices"):
        try:
            operators = adapter.get_operator_prices(provider_country, provider_service)
        except Exception:
            operators = {}
        for name, info in operators.items():
            cost = float(info.get("cost") or 0)
            qty = int(info.get("count") or 0)
            if cost <= 0 or qty <= 0:
                continue
            rows.append({
                "name": name, "label": _operator_label(name), "is_any": False,
                "provider_price": cost,
                "user_price": float(provider_to_user_ngn(cost)),
                "user_price_fmt": format_ngn(provider_to_user_ngn(cost)),
                "qty": qty, "provider": provider,
            })
        if rows:
            rows.sort(key=lambda row: row["provider_price"])
            cheapest = rows[0]
            return [{
                "name": "any", "label": _operator_label("any"), "is_any": True,
                "provider_price": cheapest["provider_price"],
                "user_price": cheapest["user_price"],
                "user_price_fmt": cheapest["user_price_fmt"],
                "qty": cheapest["qty"], "provider": provider,
            }] + rows
        return rows
    price = _price_from(provider, product, country, "any")
    if price:
        return [{
            "name": "any", "label": _operator_label("any"), "is_any": True,
            "provider_price": float(price["cost"]),
            "user_price": float(provider_to_user_ngn(price["cost"])),
            "user_price_fmt": format_ngn(provider_to_user_ngn(price["cost"])),
            "qty": int(price["count"]), "provider": provider,
        }]
    return rows


def catalog_operators(country, product, provider=None):
    """Offers for one country/product, from the chosen server only.

    The listing never mixes Server 1 and Server 2: with ``provider`` set the
    rows come from that server alone. Without it the historical cross-server
    view is kept for older callers.
    """
    product = str(product or "").strip().lower()
    if provider in _PROVIDERS:
        rows = _operator_rows(provider, country, product)
        if rows:
            return rows
        info = catalog_products(country, provider).get(product)
        if not info:
            return []
        cost = float(info.get("Price", 0) or 0)
        return [{
            "name": "any", "label": _operator_label("any"), "is_any": True,
            "provider_price": cost,
            "user_price": float(provider_to_user_ngn(cost)),
            "user_price_fmt": format_ngn(provider_to_user_ngn(cost)),
            "qty": int(info.get("Qty", 0) or 0), "provider": provider,
        }]
    fivesim = _PROVIDERS["5sim"]
    provider_country = _country_for("5sim", country)
    provider_service = _service_for("5sim", product)
    operators = {}
    if _live("5sim") and provider_country and provider_service:
        try:
            operators = fivesim.get_operator_prices(provider_country, provider_service)
        except Exception:
            operators = {}
    per_operator = []
    for name, info in operators.items():
        cost = float(info.get("cost") or 0)
        qty = int(info.get("count") or 0)
        if cost <= 0 or qty <= 0:
            continue
        per_operator.append({
            "name": name, "label": _operator_label(name), "is_any": False,
            "provider_price": cost,
            "user_price": float(provider_to_user_ngn(cost)),
            "user_price_fmt": format_ngn(provider_to_user_ngn(cost)),
            "qty": qty, "provider": "5sim",
        })
    rows = []
    # The "any" row is the cheapest buyable number across every operator and
    # both servers; the backend resolves it at purchase time.
    cheapest = min(per_operator, key=lambda row: row["provider_price"], default=None)
    any_cost = cheapest["provider_price"] if cheapest else None
    any_provider = "5sim" if cheapest else None
    any_qty = cheapest["qty"] if cheapest else 0
    other = _price_from("grizzly", product, country, "any")
    if other and (any_cost is None or other["cost"] < any_cost):
        any_cost, any_provider = float(other["cost"]), "grizzly"
        any_qty = int(other["count"])
    if any_cost is not None:
        rows.append({
            "name": "any", "label": _operator_label("any"), "is_any": True,
            "provider_price": float(any_cost),
            "user_price": float(provider_to_user_ngn(any_cost)),
            "user_price_fmt": format_ngn(provider_to_user_ngn(any_cost)),
            "qty": any_qty, "provider": any_provider,
        })
    rows.extend(sorted(per_operator, key=lambda row: row["provider_price"]))
    if rows:
        return rows
    info = catalog_products(country).get(product)
    if not info:
        return []
    cost = float(info.get("Price", 0) or 0)
    return [{
        "name": "any", "label": _operator_label("any"), "is_any": True,
        "provider_price": cost,
        "user_price": float(provider_to_user_ngn(cost)),
        "user_price_fmt": format_ngn(provider_to_user_ngn(cost)),
        "qty": int(info.get("Qty", 0) or 0), "provider": None,
    }]


def _price_from(provider, product, country, operator="any"):
    """Quote one server for a product.

    For 5sim the operator is honoured: a named operator is quoted on its own
    price/stock, and ``"any"`` picks the cheapest *stocked* operator — the
    comparison is made across operators, not by merging them first. The
    second server sells a country/product slot with no operator dimension,
    so it only answers the ``"any"`` question.
    """
    if not _live(provider):
        return None
    adapter = _PROVIDERS[provider]
    provider_country = _country_for(provider, country)
    provider_service = _service_for(provider, product)
    if not provider_country or not provider_service:
        return None
    operator = str(operator or "any").lower().strip() or "any"
    if hasattr(adapter, "get_operator_prices"):
        try:
            operators = adapter.get_operator_prices(provider_country, provider_service)
        except Exception:
            return None
        if not isinstance(operators, dict) or not operators:
            return None
        if operator != "any":
            info = operators.get(operator)
            if not isinstance(info, dict) or int(info.get("count", 0) or 0) <= 0:
                return None
            return {
                "cost": float(info["cost"]), "count": int(info["count"]),
                "operator": operator,
            }
        stocked = [
            (name, info) for name, info in operators.items()
            if isinstance(info, dict) and float(info.get("cost") or 0) > 0
            and int(info.get("count", 0) or 0) > 0
        ]
        if not stocked:
            return None
        name, info = min(stocked, key=lambda row: float(row[1]["cost"]))
        return {
            "cost": float(info["cost"]), "count": int(info["count"]),
            "operator": name,
        }
    if operator != "any":
        # The server cannot honour a specific carrier.
        return None
    try:
        prices = adapter.get_prices(provider_country, provider_service)
    except Exception:
        return None
    if not isinstance(prices, dict) or "error" in prices or not prices:
        return None
    block = prices.get(str(provider_country)) or next(iter(prices.values()), None)
    info = block.get(provider_service) if isinstance(block, dict) else None
    if not isinstance(info, dict):
        return None
    try:
        cost = float(info.get("cost", info.get("price", 0)) or 0)
        count = int(info.get("count", 0) or 0)
    except (TypeError, ValueError):
        return None
    if cost <= 0:
        return None
    return {"cost": cost, "count": count, "operator": "any"}


def get_cheap_price(product, country, operator="any", provider=None):
    """Cheapest live quote, optionally from one server only.

    With ``operator="any"`` this compares the cheapest stocked operator on
    each server. With a named operator only servers that actually offer that
    operator participate. ``provider`` pins the quote to the server the buyer
    picked, so the price charged matches the listing they saw. Returns
    ``{"cost", "provider", "count", "operator"}`` or None; a server that
    errors, times out or has no stock simply does not participate.
    """
    best = None
    names = [provider] if provider in _PROVIDERS else list(PROVIDER_ORDER)
    for name in names:
        price = _price_from(name, product, country, operator)
        if not price:
            continue
        if best is None or price["cost"] < best["cost"]:
            best = {
                "cost": price["cost"], "provider": name,
                "count": price["count"], "operator": price["operator"],
            }
    return best


def provider_price(product, country, operator="any"):
    """Back-compat wrapper around :func:`get_cheap_price`."""
    return get_cheap_price(product, country, operator)


def buy_number(product, country, operator="any", provider=None):
    """Buy from the chosen server, falling back to the other on any error.

    With ``provider`` the chosen server is tried first and keeps every other
    server as a fallback only if it errors or has no numbers. Without it the
    cheapest quoted server wins, as before.

    Returns the provider order dict extended with ``provider`` and
    ``provider_cost`` (USD), or ``{"error": <last token>}`` when all fail.
    """
    if demo_mode():
        return {
            "id": str(secrets.randbelow(9000000) + 1000000),
            "phone": "2348000000000", "status": "PENDING", "provider": "demo",
            "provider_cost": 0.0,
        }
    operator = str(operator or "any").lower().strip() or "any"
    preferred = provider if provider in _PROVIDERS else None
    names = list(PROVIDER_ORDER)
    if preferred:
        names = [preferred] + [name for name in names if name != preferred]
    candidates = []
    for name in names:
        if not _live(name):
            continue
        adapter = _PROVIDERS[name]
        if operator != "any" and not hasattr(adapter, "get_operator_prices"):
            # A named carrier was chosen; a server without that dimension
            # would hand back a different operator's number.
            continue
        price = _price_from(name, product, country, operator)
        candidates.append({
            "name": name,
            "cost": price["cost"] if price else 0.0,
            "count": price["count"] if price else 0,
            "operator": price["operator"] if price else operator,
            "quoted": bool(price),
        })
    # The picked server goes first. The rest rank cheapest quoted first; a
    # server whose pricing endpoint failed still stays in line as a fallback,
    # because its purchase endpoint may be perfectly alive.
    first = [row for row in candidates if row["name"] == preferred] if preferred else []
    rest = [row for row in candidates if row["name"] != preferred]
    candidates = (
        first
        + sorted((row for row in rest if row["quoted"]), key=lambda row: row["cost"])
        + [row for row in rest if not row["quoted"]]
    )
    errors = []
    for candidate in candidates:
        name = candidate["name"]
        adapter = _PROVIDERS[name]
        provider_country = _country_for(name, country)
        provider_service = _service_for(name, product)
        if not provider_country or not provider_service:
            errors.append((name, "BAD_SERVICE"))
            continue
        buy_operator = candidate.get("operator") or operator
        try:
            result = adapter.buy_activation(provider_country, buy_operator, provider_service)
        except Exception as exc:
            errors.append((name, str(exc)))
            continue
        if isinstance(result, dict) and "error" not in result:
            actual_cost = result.get("price_usd")
            if actual_cost is None:
                actual_cost = candidate["cost"]
            result["provider"] = name
            result["provider_cost"] = float(actual_cost or 0)
            return result
        errors.append((name, (result or {}).get("error", "NO_NUMBERS")))
    token = errors[-1][1] if errors else "NO_NUMBERS"
    return {"error": token, "provider_errors": errors}


def cancel_provider_order(provider, provider_order_id, action="cancel"):
    """Best-effort release of a number bought but not handed to the buyer."""
    adapter = _PROVIDERS.get(str(provider or ""))
    if not adapter:
        return {"error": "BAD_PROVIDER"}
    try:
        return adapter.order_action(action, provider_order_id)
    except Exception:
        return {"error": "PROVIDER_DOWN"}


def check_order(order):
    """Poll the server that owns this order; never falls through to another."""
    provider = str(getattr(order, "provider", "") or "grizzly")
    if provider == "demo" or demo_mode():
        return {"status": order.status if order.status in ("PENDING", "RECEIVED") else "PENDING", "sms": []}
    adapter = _PROVIDERS.get(provider)
    if not adapter:
        return {"error": "BAD_PROVIDER"}
    try:
        return adapter.check_order(order.provider_order_id)
    except Exception:
        return {"error": "PROVIDER_DOWN"}


def order_action(order, action):
    """Cancel/finish/ban on the server that owns this order."""
    provider = str(getattr(order, "provider", "") or "grizzly")
    if provider == "demo" or demo_mode():
        return {"status": {"cancel": "STATUS_CANCEL", "finish": "ACCESS_ACTIVATION",
                           "ban": "STATUS_CANCEL"}.get(action, "STATUS_CANCEL")}
    adapter = _PROVIDERS.get(provider)
    if not adapter:
        return {"error": "BAD_PROVIDER"}
    try:
        return adapter.order_action(action, order.provider_order_id)
    except Exception:
        return {"error": "PROVIDER_DOWN"}


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
"""Business rules and external service adapters for the verification/SMS app."""
import hashlib
import hmac
import json
import secrets
import time
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


# Provider catalogs are large (5sim carries 1,300+ products) and the service
# search hits the live API on every keystroke. A short per-process cache keeps
# the typeahead responsive without serving stale prices for long.
_CATALOG_TTL = 60
_catalog_cache = {}


def _cached(key, loader, ttl=_CATALOG_TTL):
    now = time.monotonic()
    hit = _catalog_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    _catalog_cache[key] = (now, value)
    return value


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
    dollars, the same unit SMS-BUS uses, so the storefront has one site
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
    def get_all_products(cls):
        """The full product catalog across every country.

        ``/guest/products/any/any`` answers with all 1,300+ slugs 5sim sells,
        each with its global stock and starting price — this is the list the
        server-first service step shows, not a curated subset.
        """
        data = cls._request("/guest/products/any/any")
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
                out[str(product)] = {
                    "Price": price, "Qty": qty,
                    "Category": str(info.get("Category") or "activation"),
                }
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


class SmsBus:
    """SMS-BUS adapter — the app's primary number supplier (Server 1).

    The control API answers JSON over plain GET: the token travels in the
    query string and every reply is ``{"code": 200, "data": ...}`` on success
    or ``{"code": <int>, "message": ...}`` on failure. Both shapes are
    normalised here so callers only ever see a plain dict or
    ``{"error": <message>}``.

    Countries and projects are addressed by sms-bus's own numeric ids, so the
    adapter keeps a per-server id index and resolves canonical ISO/service
    identifiers at the boundary. Prices and balances are quoted in USD.
    """

    base_url = "https://sms-bus.com/api/control"

    # Numeric failure codes -> the storefront's error tokens.
    ERROR_TOKENS = {
        401: "BAD_KEY",
        50001: "NO_NUMBERS",   # no service available for this slice
        50002: "NO_NUMBERS",   # no number in stock
        50104: "PROVIDER_DOWN",  # waiting-requests limit reached
        50201: "NO_BALANCE",
        50208: "BAD_KEY",      # account not activated
        50214: "PROVIDER_DOWN",  # account blocked for 24h
        50007: "BAD_STATUS", 50107: "BAD_STATUS", 50108: "BAD_STATUS",
        50109: "BAD_STATUS",
    }

    # Short service code -> display name. The codes double as the canonical
    # service ids shared with 5sim.
    service_labels = {
        "wa": "WhatsApp", "tg": "Telegram", "go": "Google", "fb": "Facebook",
        "ig": "Instagram", "tw": "Twitter", "oi": "Tinder", "am": "Amazon",
        "mm": "Microsoft", "nf": "Netflix", "bb": "Uber", "vi": "Viber",
        "ds": "Discord", "sn": "Snapchat", "tt": "TikTok", "av": "Avito",
        "fd": "Foodpanda", "vk": "VKontakte", "ok": "Odnoklassniki",
    }

    # Demo catalogue, keyed by ISO country code, used while no token is saved.
    demo_countries = {
        "ng": "Nigeria", "in": "India", "us": "United States of America",
        "gb": "United Kingdom", "id": "Indonesia", "ph": "Philippines",
        "de": "Germany", "ca": "Canada", "ru": "Russia", "kz": "Kazakhstan",
        "vn": "Vietnam", "ua": "Ukraine",
    }

    demo_prices = {
        "wa": {"ng": 0.35, "in": 0.42, "us": 0.55, "gb": 0.48},
        "tg": {"ng": 0.29, "in": 0.33, "us": 0.45, "gb": 0.51},
        "go": {"ng": 0.44, "in": 0.52, "us": 0.72},
        "fb": {"ng": 0.30, "in": 0.38, "us": 0.61},
        "ig": {"ng": 0.41, "in": 0.47, "us": 0.68},
        "tt": {"ng": 0.33, "in": 0.40, "us": 0.58},
    }

    @classmethod
    def _key(cls):
        return Setting.value("provider_api_key", "") or settings.SMSBUS_API_KEY

    @classmethod
    def _request(cls, path, params=None):
        key = cls._key()
        if not key:
            return {"error": "BAD_KEY"}
        query = urlencode({"token": key, **{
            k: v for k, v in (params or {}).items() if v not in (None, "")}})
        request = Request(cls.base_url + path + "?" + query, headers={
            "Accept": "application/json",
            "User-Agent": "VerifySMS-Django/1.0",
        })
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            return {"error": f"HTTP_{exc.code}", "code": exc.code}
        except (URLError, TimeoutError, OSError):
            return {"error": "PROVIDER_DOWN"}
        if not body:
            return {"error": "EMPTY_RESPONSE"}
        try:
            payload = json.loads(body)
        except ValueError:
            return {"error": "INVALID_JSON"}
        if not isinstance(payload, dict):
            return {"error": "INVALID_JSON"}
        try:
            code = int(payload.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        if code == 200:
            return {"ok": True, "data": payload.get("data"),
                    "message": str(payload.get("message") or "")}
        # Stable tokens let the storefront show an actionable message; 50103
        # keeps SMS-BUS's own wording because the order layer recognises the
        # "already closed" case.
        message = str(payload.get("message") or "").strip()
        token = cls.ERROR_TOKENS.get(code)
        if token:
            return {"error": token, "code": code, "detail": message}
        return {"error": message or f"CODE_{code}", "code": code}

    @classmethod
    def _project_ids(cls):
        """``{project_code: project_id}``, cached for a minute."""
        def load():
            result = cls._request("/list/projects")
            out = {}
            if "error" in result:
                return out
            block = result.get("data") or {}
            items = block.values() if isinstance(block, dict) else block
            for item in items:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                code = str(item.get("code") or "").lower()
                if code:
                    out[code] = str(item.get("id"))
            return out
        return _cached("smsbus:project-ids", load)

    @classmethod
    def _project_codes(cls):
        return {ident: code for code, ident in cls._project_ids().items()}

    # -- catalog -----------------------------------------------------------

    @classmethod
    def get_countries(cls):
        """``{country_id: {"id", "eng", "iso"}}``."""
        if not cls._key():
            return {iso: {"id": iso, "eng": name, "iso": iso}
                    for iso, name in cls.demo_countries.items()}
        result = cls._request("/list/countries")
        if "error" in result:
            return {}
        block = result.get("data") or {}
        out = {}
        items = block.values() if isinstance(block, dict) else block
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            iso = str(item.get("code") or "").lower()
            out[str(item["id"])] = {
                "id": item["id"],
                "eng": str(item.get("title") or iso or item["id"]),
                "iso": iso,
            }
        return out

    @classmethod
    def get_services(cls):
        """Project code -> human name, from ``/list/projects``."""
        if not cls._key():
            return dict(cls.service_labels)
        result = cls._request("/list/projects")
        if "error" in result:
            return {}
        block = result.get("data") or {}
        out = {}
        items = block.values() if isinstance(block, dict) else block
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").lower()
            if code:
                out[code] = str(item.get("title") or code)
        return out

    @classmethod
    def get_products(cls, country="any", operator="any"):
        """``{service_code: {"Price", "Qty", "Category"}}`` for one country."""
        if not cls._key():
            cid = str(country or "ng")
            out = {}
            for code, by_country in cls.demo_prices.items():
                cost = by_country.get(cid) or by_country.get("ng")
                if cost:
                    out[code] = {"Price": cost, "Qty": 25, "Category": "activation"}
            return out
        if str(country) in ("", "any"):
            return {}
        block = cls.get_prices(str(country)).get(str(country), {})
        return {code: {"Price": info["cost"], "Qty": info["count"], "Category": "activation"}
                for code, info in block.items()
                if info.get("cost", 0) > 0 and info.get("count", 0) > 0}

    @classmethod
    def get_prices(cls, country="", product=""):
        """``{country_id: {service_code: {"cost", "count"}}}`` for one country."""
        cid = str(country or "").strip()
        if not cls._key():
            cid = cid or "ng"
            codes = [product] if product else list(cls.demo_prices)
            return {cid: {code: {"cost": cls.demo_prices.get(code, {}).get(cid, 0.4), "count": 25}
                          for code in codes}}
        if cid in ("", "any"):
            return {}
        result = cls._request("/list/prices", {"country_id": cid})
        if "error" in result:
            return {}
        block = result.get("data") or {}
        codes = cls._project_codes()
        rows = {}
        items = block.values() if isinstance(block, dict) else block
        for item in items:
            if not isinstance(item, dict):
                continue
            code = codes.get(str(item.get("project_id")))
            if not code or (product and code != str(product)):
                continue
            try:
                cost = float(item.get("cost") or 0)
                count = int(item.get("total_count") or 0)
            except (TypeError, ValueError):
                continue
            if cost <= 0:
                continue
            rows[code] = {"cost": cost, "count": count}
        return {cid: rows}

    # -- activations -------------------------------------------------------

    @classmethod
    def get_balance(cls):
        if not cls._key():
            return {"balance": 0.0, "demo": True}
        result = cls._request("/get/balance")
        if "error" in result:
            return result
        data = result.get("data") or {}
        try:
            return {"balance": float(data.get("balance") or 0)}
        except (TypeError, ValueError):
            return {"error": "INVALID_JSON"}

    @classmethod
    def buy_activation(cls, country, operator, product, max_price=None):
        """Book a number. Returns ``{"id", "phone", "status"}``."""
        if not cls._key():
            return {"id": str(secrets.randbelow(9000000) + 1000000),
                    "phone": "2348000000000", "service": product,
                    "country": country, "status": "PENDING"}
        country = str(country or "").strip()
        project_id = cls._project_ids().get(str(product or "").lower())
        if not country or not project_id:
            return {"error": "BAD_SERVICE"}
        result = cls._request("/get/number", {"country_id": country, "project_id": project_id})
        if "error" in result:
            return result
        data = result.get("data") or {}
        if not data.get("request_id") or not data.get("number"):
            return {"error": "NO_NUMBERS"}
        return {"id": str(data["request_id"]), "phone": str(data["number"]), "status": "PENDING"}

    @classmethod
    def check_order(cls, provider_order_id):
        """Poll ``/get/sms``; 50101 means "not yet", 50102 means released."""
        if not cls._key():
            return {"status": "PENDING", "sms": []}
        result = cls._request("/get/sms", {"request_id": str(provider_order_id)})
        if "error" in result:
            if result.get("code") == 50101:
                return {"status": "PENDING", "sms": []}
            if result.get("code") == 50102:
                return {"status": "TIMEOUT", "sms": []}
            return result
        text = result.get("data")
        if isinstance(text, dict):
            text = text.get("code") or text.get("text") or ""
        text = str(text or "").strip()
        if not text:
            return {"status": "PENDING", "sms": []}
        return {"status": "RECEIVED", "sms": [{
            "sender": "", "text": text, "code": text, "date": "",
        }]}

    @classmethod
    def order_action(cls, action, provider_order_id):
        """``cancel`` releases the request; the API has no finish/ban."""
        if action not in {"cancel", "finish", "ban"}:
            return {"error": "BAD_ACTION"}
        if action == "finish":
            return {"status": "FINISHED"}
        if not cls._key():
            return {"status": "CANCELED"}
        result = cls._request("/cancel", {"request_id": str(provider_order_id)})
        if "error" in result:
            return result
        return {"status": "CANCELED"}

    @classmethod
    def sms_inbox(cls, provider_order_id):
        return {"sms": cls.check_order(provider_order_id).get("sms", [])}


# ---------------------------------------------------------------------------
# Provider routing
#
# Two number suppliers sit behind one storefront: SMS-BUS is server 1, 5sim is
# server 2. The buyer never picks one. The catalog comes from the first server
# that answers, the quote is the cheapest live price across both, and every
# purchase falls through to the other server on a provider error.
#
# Canonical identifiers are ISO-3166 country codes and short service codes.
# Neither matches a supplier's own numbering: SMS-BUS addresses countries and
# projects by numeric ids, 5sim by slugs. Both are resolved at runtime from
# each server's catalog, so no hand-maintained id map can drift and send a
# customer a number in the wrong country.
# ---------------------------------------------------------------------------

PROVIDER_ORDER = ("5sim", "smsbus")
# Neutral storefront names. The suppliers' brands are deliberately never
# exposed to customers — a buyer who learns the upstream name can leave the
# storefront and buy the number directly at the source.
PROVIDER_LABELS = {"5sim": "Server 2", "smsbus": "Server 1", "demo": "Demo"}
_PROVIDERS = {"5sim": FiveSim, "smsbus": SmsBus}

# The storefront offers the two suppliers as Server 1 and Server 2. The
# numbering is the owner's: server 1 is SMS-BUS, server 2 is 5SIM.
SERVER_ORDER = ("smsbus", "5sim")
SERVER_NUMBERS = {"smsbus": "1", "5sim": "2"}

# Canonical short service code -> 5sim product slug. SMS-BUS already uses the
# short codes, so only the 5sim side needs a map. Services 5sim carries and
# SMS-BUS does not simply never appear on Server 1.
SERVICE_TO_FIVESIM = {
    "wa": "whatsapp", "tg": "telegram", "go": "google", "fb": "facebook",
    "ig": "instagram", "tw": "twitter", "oi": "tinder", "am": "amazon",
    "mm": "microsoft", "nf": "netflix", "bb": "uber", "vi": "viber",
    "ds": "discord", "sn": "snapchat", "tt": "tiktok", "av": "avito",
    "fd": "foodpanda", "vk": "vk", "ok": "ok",
}
FIVESIM_TO_SERVICE = {slug: code for code, slug in SERVICE_TO_FIVESIM.items()}

# Services only one server carries. The canonical id is the 5sim slug because
# SMS-BUS has no code for them here; they simply do not appear on Server 1.
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
    demo prices and simulated numbers would compete with, and could even beat,
    a live provider.
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
    return {"1": "smsbus", "2": "5sim"}.get(value)


def catalog_servers():
    """The two server buttons, in the owner's numbering."""
    return [{
        "key": SERVER_NUMBERS[name],
        "provider": name,
        "label": "Server " + SERVER_NUMBERS[name],
        "name": provider_label(name),
        "available": _live(name),
    } for name in SERVER_ORDER]


def _country_index(provider):
    """``{iso: native_country_id}`` read once from the provider's catalog."""
    adapter = _PROVIDERS.get(provider)
    if adapter is None:
        return {}

    def load():
        try:
            countries = adapter.get_countries()
        except Exception:
            return {}
        out = {}
        for ident, item in (countries or {}).items():
            if not isinstance(item, dict):
                continue
            iso = str(item.get("iso") or "").lower()
            if len(iso) == 2:
                out[iso] = str(ident)
        return out

    return _cached("country-index:" + provider, load)


def _country_reverse(provider):
    """``{native_country_id: iso}`` — the inverse of ``_country_index``."""
    def load():
        return {ident: iso for iso, ident in _country_index(provider).items()}
    return _cached("country-reverse:" + provider, load)


def service_label(code):
    code = str(code or "").lower()
    if code in SmsBus.service_labels:
        return SmsBus.service_labels[code]
    return FiveSim.service_labels.get(SERVICE_TO_FIVESIM.get(code, code), code.upper())


def _country_for(provider, canonical):
    """Canonical ISO code -> the id that provider's API expects."""
    canonical = str(canonical or "").strip().lower()
    if not canonical:
        return None
    native = _country_index(provider).get(canonical)
    if native:
        return native
    # Unknown to the catalog: pass the identifier through only when it is
    # already shaped like that provider's own id (numeric for SMS-BUS, a slug
    # for 5sim). Guessing never happens here.
    if provider == "smsbus" and canonical.isdigit():
        return canonical
    if provider == "5sim" and not canonical.isdigit():
        return canonical
    return None


def _service_for(provider, canonical):
    canonical = str(canonical or "").strip().lower()
    if not canonical:
        return None
    if provider == "5sim":
        return SERVICE_TO_FIVESIM.get(canonical, canonical)
    return canonical


def _canonical_country(provider, identifier):
    """A provider's own country id -> canonical ISO code."""
    identifier = str(identifier or "").strip().lower()
    if not identifier:
        return ""
    iso = _country_reverse(provider).get(identifier)
    return iso or identifier


def _canonical_service(provider, identifier):
    identifier = str(identifier or "").strip().lower()
    if provider == "5sim":
        return FIVESIM_TO_SERVICE.get(identifier, identifier)
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


POPULAR_SERVICE_NAMES = {
    "whatsapp", "telegram", "google", "amazon", "facebook", "instagram",
    "tiktok", "twitter", "discord", "signal", "uber", "netflix",
    "microsoft", "apple", "yahoo", "paypal",
}


def _service_catalog(provider):
    """Every service one server sells — its full live catalog, uncapped.

    SMS-BUS answers with ``/list/projects``; 5sim has no service-list
    endpoint, so the full products endpoint serves as one. Rows are
    ``{code, name, qty, raw, popular}``, popular consumer names first.
    Cached for a minute: the typeahead looks at this on every keystroke.
    """
    def load():
        rows = {}
        if provider == "5sim":
            for slug, info in FiveSim.get_all_products().items():
                canonical = _canonical_service("5sim", slug)
                row = rows.get(canonical)
                if row is None:
                    row = rows[canonical] = {
                        "code": canonical, "name": service_label(canonical),
                        "qty": 0, "raw": slug,
                    }
                row["qty"] += int(info.get("Qty", 0) or 0)
        elif provider == "smsbus":
            data = SmsBus.get_services()
            if not data:
                data = dict(SmsBus.service_labels)
            for code, name in data.items():
                canonical = _canonical_service("smsbus", code)
                rows[canonical] = {
                    "code": canonical,
                    "name": str(name or service_label(canonical)),
                    "qty": 0, "raw": code,
                }
        values = list(rows.values())
        for row in values:
            row["popular"] = row["name"].strip().lower() in POPULAR_SERVICE_NAMES
        values.sort(key=lambda row: (not row["popular"], row["name"].lower()))
        return values

    return _cached("services:" + str(provider), load)


def catalog_services(provider=None, q=""):
    """Live service list for one server, optionally filtered by ``q``.

    The service step now runs straight after the server choice, before any
    country is known, so this reads the server's own catalog: ``/list/projects``
    for SMS-BUS and all 1,300+ products for 5sim.
    Nothing is truncated to a curated short list.
    """
    rows = []
    for name in _catalog_names(provider):
        if _live(name):
            rows.extend(_service_catalog(name))
    if not rows:
        # Demo container with no keys: keep the local label maps usable.
        for name in _catalog_names(provider):
            labels = FiveSim.service_labels if name == "5sim" else SmsBus.service_labels
            for code, label in labels.items():
                canonical = _canonical_service(name, code)
                rows.append({
                    "code": canonical, "name": str(label),
                    "qty": 0, "raw": code,
                    "popular": str(label).strip().lower() in POPULAR_SERVICE_NAMES,
                })
    if not rows:
        return []

    query = str(q or "").strip().lower()
    if query:
        starts, contains = [], []
        for row in rows:
            name = str(row["name"]).lower()
            code = str(row["code"]).lower()
            raw = str(row.get("raw") or "").lower()
            if name.startswith(query) or code.startswith(query):
                starts.append(row)
            elif query in name or query in code or query in raw:
                contains.append(row)
        rows = starts + contains
    return rows


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
            if not rows and not prices:
                # The bulk price call failed (bad key, timeout): still show
                # every country and let the price step report availability,
                # rather than dead-ending the flow at this step.
                for identifier, item in labels.items():
                    canonical = _canonical_country(name, identifier)
                    rows.setdefault(canonical, {
                        "name": str(canonical), "label": item.get("eng", str(identifier)),
                        "iso": item.get("iso", ""), "qty": 0, "price": None,
                    })
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
    other = _price_from("smsbus", product, country, "any")
    if other and (any_cost is None or other["cost"] < any_cost):
        any_cost, any_provider = float(other["cost"]), "smsbus"
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


def server_price_board(country, product, provider):
    """Price board for the chosen server plus the other one for comparison.

    Each entry carries the ``get_cheap_price`` best price, every operator
    row that server offers, and a fixed ``label`` ("Server 1", "Server 2").
    The chosen server sorts first; prices from the two are never blended.
    """
    chosen = provider if provider in _PROVIDERS else None
    board = []
    for name in SERVER_ORDER:
        rows = catalog_operators(country, product, name)
        best = get_cheap_price(product, country, "any", provider=name)
        entry = {
            "provider": name,
            "key": SERVER_NUMBERS[name],
            "label": "Server " + SERVER_NUMBERS[name],
            "name": provider_label(name),
            "selected": name == chosen,
            "available": bool(rows or best),
            "best": None,
            "rows": rows,
        }
        if best:
            user_price = provider_to_user_ngn(best["cost"])
            entry["best"] = {
                "provider_price": float(best["cost"]),
                "user_price": float(user_price),
                "user_price_fmt": format_ngn(user_price),
                "qty": int(best["count"]),
                "operator": best["operator"],
            }
        board.append(entry)
    if chosen:
        board.sort(key=lambda entry: not entry["selected"])
    return board


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
    provider = str(getattr(order, "provider", "") or "smsbus")
    if provider == "demo" or demo_mode():
        return {"status": order.status if order.status in ("PENDING", "RECEIVED") else "PENDING", "sms": []}
    adapter = _PROVIDERS.get(provider)
    if not adapter:
        # A legacy order from a supplier this build no longer integrates:
        # keep its stored status, nothing upstream can be polled any more.
        return {"status": order.status, "sms": []}
    try:
        return adapter.check_order(order.provider_order_id)
    except Exception:
        return {"error": "PROVIDER_DOWN"}


def order_action(order, action):
    """Cancel/finish/ban on the server that owns this order."""
    provider = str(getattr(order, "provider", "") or "smsbus")
    if provider == "demo" or demo_mode():
        return {"status": {"cancel": "STATUS_CANCEL", "finish": "ACCESS_ACTIVATION",
                           "ban": "STATUS_CANCEL"}.get(action, "STATUS_CANCEL")}
    adapter = _PROVIDERS.get(provider)
    if not adapter:
        # Legacy order: close it locally so the customer can still cancel or
        # finish it after the supplier was retired.
        return {"status": {"cancel": "CANCELED", "finish": "FINISHED",
                           "ban": "BANNED"}.get(action, "CANCELED")}
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
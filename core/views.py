import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Coupon, CouponUse, Order, Setting, Topup, Transaction, User
from .services import FiveSim, Paystack, credit_balance, debit_balance, format_ngn, friendly_provider_error, money, provider_to_user_ngn


USER_PAGES = {"dashboard", "services", "cart", "topup", "history", "settings", "sms"}
ADMIN_PAGES = {"dashboard", "users", "transactions", "providers", "payment", "coupons", "settings"}


def _page_context(request, page):
    user = request.user
    context = {"page": page, "current_user": user}
    if getattr(user, "is_authenticated", False):
        context["wallet_balance"] = format_ngn(user.balance)
        context["active_orders_count"] = Order.objects.filter(user=user, status__in=("PENDING", "RECEIVED")).count()
    return context


def home(request):
    return render(request, "index.html")


@require_http_methods(["GET", "POST"])
def login_page(request):
    if request.user.is_authenticated:
        return redirect("/admin/dashboard.html" if request.user.role == "admin" else "/user/dashboard.html")
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        user = authenticate(request, username=email, password=password)
        if user and user.status == "active":
            login(request, user)
            return redirect(request.GET.get("redirect") or ("/admin/dashboard.html" if user.role == "admin" else "/user/dashboard.html"))
        messages.error(request, "Invalid email or password.")
    return render(request, "login.html")


@require_http_methods(["GET", "POST"])
def register_page(request):
    if request.user.is_authenticated:
        return redirect("/user/dashboard.html")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm", "")
        if not name or not email or not password:
            messages.error(request, "All fields are required.")
        elif len(password) < 6:
            messages.error(request, "Password must be at least 6 characters.")
        elif password != confirm:
            messages.error(request, "Passwords do not match.")
        elif User.objects.filter(email=email).exists():
            messages.error(request, "An account with this email already exists.")
        else:
            user = User.objects.create_user(email=email, password=password, first_name=name, role="user")
            user.save()
            login(request, user)
            return redirect("/user/dashboard.html")
    return render(request, "register.html")


def logout_page(request):
    logout(request)
    return redirect("/")


def _maintenance_redirect(request):
    return Setting.value("maintenance_mode", "0") == "1" and request.user.role != "admin"


@login_required(login_url="/login.html")
def user_page(request, page):
    if request.user.status != "active":
        logout(request)
        return redirect("/login.html?error=banned")
    if _maintenance_redirect(request):
        return render(request, "maintenance.html")
    if page not in USER_PAGES:
        return render(request, "404.html", status=404)
    context = _page_context(request, page)
    if page == "dashboard":
        context.update(
            total_orders=Order.objects.filter(user=request.user).count(),
            active_orders=Order.objects.filter(user=request.user, status__in=("PENDING", "RECEIVED")).count(),
            total_spent=format_ngn(Order.objects.filter(user=request.user).aggregate(total=Sum("user_price"))["total"] or Decimal("0")),
            total_topups=Topup.objects.filter(user=request.user, status="success").aggregate(total=Sum("net_credited"))["total"] or Decimal("0"),
            recent_orders=Order.objects.filter(user=request.user).order_by("-created_at")[:3],
            recent_transactions=Transaction.objects.filter(user=request.user).order_by("-created_at")[:3],
        )
    elif page == "cart":
        context["orders"] = Order.objects.filter(user=request.user, status__in=("PENDING", "RECEIVED")).order_by("-created_at")
    elif page == "history":
        context["orders"] = Order.objects.filter(user=request.user).order_by("-created_at")
        context["transactions"] = Transaction.objects.filter(user=request.user).order_by("-created_at")
    elif page == "services":
        context["site_rate"] = Setting.value("site_rate", "1600")
        context["markup_percent"] = Setting.value("markup_percent", "20")
    elif page == "topup":
        context.update(
            min_topup=Setting.value("min_topup", "100"),
            max_topup=Setting.value("max_topup", "500000"),
            fee_percent=Setting.value("topup_fee_percent", "3"),
            fee_high=Setting.value("topup_fee_percent_high", "6"),
            fee_threshold=Setting.value("topup_fee_threshold", "1000"),
            paystack_public_key=Paystack.public_key(),
            recent_topups=Topup.objects.filter(user=request.user).order_by("-created_at")[:6],
        )
    elif page == "sms":
        order_id = request.GET.get("order_id")
        qs = Order.objects.filter(user=request.user, status__in=("PENDING", "RECEIVED"))
        if order_id:
            try:
                context["order"] = qs.get(pk=order_id)
            except Order.DoesNotExist:
                return redirect("/user/cart.html")
        else:
            # Default to the most useful active order: one with an SMS first.
            context["order"] = qs.order_by("-status", "-created_at").first()
        context["active_count"] = qs.count()
    elif page == "settings":
        context["total_spent"] = format_ngn(Order.objects.filter(user=request.user).aggregate(total=Sum("user_price"))["total"] or Decimal("0"))
    if request.method == "POST" and page == "settings":
        name = request.POST.get("name", "").strip()
        if name:
            parts = name.split(" ", 1)
            request.user.first_name, request.user.last_name = parts[0], parts[1] if len(parts) > 1 else ""
            request.user.save(update_fields=["first_name", "last_name", "updated_at"])
            messages.success(request, "Profile updated successfully.")
        new_password = request.POST.get("new_password", "")
        if new_password:
            if len(new_password) < 6:
                messages.error(request, "Password must be at least 6 characters.")
            else:
                request.user.set_password(new_password)
                request.user.save(update_fields=["password", "updated_at"])
                login(request, request.user)
                messages.success(request, "Password changed successfully.")
        return redirect("/user/settings.html")
    return render(request, f"user/{page}.html", context)


@login_required(login_url="/login.html")
def admin_page(request, page):
    if request.user.role != "admin":
        return redirect("/user/dashboard.html")
    if request.method == "POST":
        return admin_action(request)
    context = _page_context(request, page)
    if page == "dashboard":
        context.update(
            total_users=User.objects.filter(role="user").count(),
            total_orders=Order.objects.count(),
            pending_orders=Order.objects.filter(status="PENDING").count(),
            total_revenue=abs(Transaction.objects.filter(type="purchase").aggregate(total=Sum("amount"))["total"] or Decimal("0")),
            users=User.objects.filter(role="user").order_by("-date_joined")[:5],
            orders=Order.objects.select_related("user").order_by("-created_at")[:8],
        )
    elif page == "users":
        query = request.GET.get("q", "").strip()
        users = User.objects.filter(role="user").filter(Q(email__icontains=query) | Q(first_name__icontains=query)) if query else User.objects.filter(role="user")
        context["users"] = users.annotate(order_count=Count("orders")).order_by("-date_joined")
    elif page == "transactions":
        context["orders"] = Order.objects.select_related("user").order_by("-created_at")
        context["transactions"] = Transaction.objects.select_related("user").order_by("-created_at")
        context["topups"] = Topup.objects.select_related("user").order_by("-created_at")
    elif page == "coupons":
        context["coupons"] = Coupon.objects.order_by("-created_at")
        context["now"] = timezone.now()
    elif page == "providers":
        context["provider_configured"] = bool(FiveSim._key())
        context["demo_mode"] = not bool(FiveSim._key())
    elif page == "payment":
        context["payment_configured"] = bool(Paystack.secret_key())
        context["public_key"] = Paystack.public_key()
        context["fee_pct"] = Setting.value("topup_fee_percent", "3")
        context["min_topup"] = Setting.value("min_topup", "100")
        context["max_topup"] = Setting.value("max_topup", "500000")
        context["topups"] = Topup.objects.select_related("user").order_by("-created_at")[:10]
    elif page == "settings":
        context["settings"] = {key: Setting.value(key, default) for key, default in {
            "site_name": "VerifySMS", "site_rate": "1600", "markup_percent": "20",
            "extra_fee_percent": "8", "topup_fee_percent": "3",
            "topup_fee_percent_high": "6", "topup_fee_threshold": "1000",
            "min_topup": "100", "max_topup": "500000",
            "sms_poll_interval": "5", "maintenance_mode": "0",
            "support_whatsapp": "2348086218152",
        }.items()}
    return render(request, f"admin/{page}.html", context)


def _json(data, status=200):
    return JsonResponse(data, status=status)


def support_info(request):
    """Public contact info for the site-wide customer-care button."""
    return _json({
        "whatsapp": Setting.value("support_whatsapp", "2348086218152").strip(),
        "label": Setting.value("support_label", "Customer Care").strip(),
    })


def _normalise_sms(msg):
    """Turn a 5sim SMS entry into a display-friendly dict, parsing an OTP
    code out of the text when 5sim does not return one explicitly."""
    if not isinstance(msg, dict):
        msg = {}
    text = (msg.get("text") or "").strip()
    code = (msg.get("code") or "").strip()
    if not code:
        found = re.findall(r"(?<![0-9])[0-9]{4,8}(?![0-9])", text)
        if len(found) == 1:
            code = found[0]
    return {
        "id": str(msg.get("id") or ""),
        "sender": msg.get("sender") or "",
        "text": text,
        "code": code,
        "date": msg.get("created_at") or msg.get("date") or "",
    }


@login_required(login_url="/login.html")
@require_http_methods(["GET"])
def provider_api(request):
    action = request.GET.get("action", "")
    if action == "services":
        raw = FiveSim.get_products("any", "any")
        if not raw or "error" in raw:
            return _json({"success": False, "message": "Could not reach the provider. Check API key or try again."})
        data = {
            name: {"name": name, "category": str(info.get("Category", "activation")).lower(), "qty": int(info.get("Qty", 0))}
            for name, info in raw.items()
            if str(info.get("Category", "activation")).lower() == "activation" and int(info.get("Qty", 0)) > 0
        }
        return _json({"success": True, "data": dict(sorted(data.items()))})
    if action == "countries":
        product = request.GET.get("product", "").strip().lower()
        prices = FiveSim.get_prices("", product)
        country_map = prices.get(product, prices if product in prices else {})
        labels = FiveSim.get_countries()
        countries = {}
        for country, operators in country_map.items():
            qty = sum(int(info.get("count", 0)) for info in operators.values() if isinstance(info, dict))
            if qty:
                item = labels.get(country, {})
                iso_data = item.get("iso", {}) if isinstance(item, dict) else {}
                countries[country] = {
                    "name": country,
                    "label": item.get("text_en", country.replace("_", " ").title()) if isinstance(item, dict) else country.title(),
                    "iso": next(iter(iso_data), ""),
                    "qty": qty,
                }
        return _json({"success": True, "data": sorted(countries.values(), key=lambda x: (-x["qty"], x["label"]))})
    if action == "operators":
        country, product = request.GET.get("country", "").lower(), request.GET.get("product", "").lower()
        prices = FiveSim.get_prices(country, product)
        # 5sim nests /guest/prices by PRODUCT when only ?product= is sent, but
        # by COUNTRY when both ?country= and ?product= are sent. Accept either
        # layout and descend to the operator dict (the innermost level).
        country_map = {}
        if isinstance(prices, dict):
            if product in prices and isinstance(prices.get(product), dict):
                inner = prices[product]
                country_map = inner.get(country, {}) if isinstance(inner, dict) else {}
            elif country in prices and isinstance(prices.get(country), dict):
                inner = prices[country]
                country_map = inner.get(product, {}) if isinstance(inner, dict) else {}
        output = []
        for name, info in (country_map if isinstance(country_map, dict) else {}).items():
            if not isinstance(info, dict):
                continue
            output.append({
                "name": name, "label": name.replace("_", " ").title(), "is_any": name == "any",
                "provider_price": float(info.get("cost", 0)), "user_price": float(provider_to_user_ngn(info.get("cost", 0))),
                "user_price_fmt": format_ngn(provider_to_user_ngn(info.get("cost", 0))), "qty": int(info.get("count", 0)),
            })
        return _json({"success": True, "data": output})
    return _json({"success": False, "message": "Unknown action."}, 400)


def _coupon_check(user, code, price, consume=False, order=None):
    code = code.upper().strip()
    coupon = Coupon.objects.filter(code=code, is_active=True).first()
    if not coupon:
        return None, "Invalid or expired coupon code."
    now = timezone.now()
    if coupon.valid_from and now < coupon.valid_from:
        return None, "This coupon is not active yet."
    if coupon.valid_until and now > coupon.valid_until:
        return None, "This coupon has expired."
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        return None, "This coupon has reached its usage limit."
    if CouponUse.objects.filter(coupon=coupon, user=user).exists():
        return None, "You have already used this coupon."
    if price and coupon.min_purchase > money(price):
        return None, f"Minimum purchase of {format_ngn(coupon.min_purchase)} required for this coupon."
    reg_date = user.date_joined.date()
    if coupon.target_users == "new" and coupon.target_reg_from and reg_date < coupon.target_reg_from:
        return None, "This coupon is for newly registered users only."
    if coupon.target_users == "old" and coupon.target_reg_to and reg_date > coupon.target_reg_to:
        return None, "This coupon is for existing users only."
    if coupon.target_users == "date_range" and ((coupon.target_reg_from and reg_date < coupon.target_reg_from) or (coupon.target_reg_to and reg_date > coupon.target_reg_to)):
        return None, "Your account is not eligible for this coupon."
    discount = money(price) * coupon.discount_value / 100 if coupon.discount_type == "percent" else min(coupon.discount_value, money(price))
    discount = money(discount)
    if consume:
        CouponUse.objects.create(coupon=coupon, user=user, order=order)
        Coupon.objects.filter(pk=coupon.pk).update(uses_count=coupon.uses_count + 1)
    return {"coupon": coupon, "discount": discount, "final_price": max(Decimal("0"), money(price) - discount)}, None


@login_required(login_url="/login.html")
@require_http_methods(["GET", "POST"])
def orders_api(request):
    if request.method == "GET":
        order_id = request.GET.get("order_id")
        order = get_object_or_404(Order, pk=order_id, user=request.user)
        data = FiveSim.check_order(order.provider_order_id)
        messages = []
        if "error" not in data:
            messages = [_normalise_sms(m) for m in (data.get("sms") or [])]
            latest = messages[-1] if messages else {}
            fields = {"status": str(data.get("status", order.status)).upper(), "sms_code": latest.get("code") or "", "sms_text": latest.get("text") or "", "sms_sender": latest.get("sender") or ""}
            if fields["status"] != order.status or fields["sms_code"]:
                for key, value in fields.items():
                    if value is not None:
                        setattr(order, key, value or None)
                order.save()
        return _json({"success": True, "status": order.status, "sms_code": order.sms_code, "sms_text": order.sms_text, "sms_sender": order.sms_sender, "phone": order.phone, "expires_at": order.expires_at, "messages": messages})
    action = request.POST.get("action", "")
    if action == "buy":
        country = request.POST.get("country", "").lower().strip()
        operator = request.POST.get("operator", "any").lower().strip()
        product = request.POST.get("product", "").lower().strip()
        if not country or not product:
            return _json({"success": False, "message": "Please complete all selections."}, 400)
        products = FiveSim.get_products(country, operator)
        product_info = products.get(product)
        if not product_info:
            return _json({"success": False, "message": "This service is no longer available in the selected country. Please choose another."})
        provider_price = Decimal(str(product_info.get("Price", product_info.get("cost", 0))))
        user_price = provider_to_user_ngn(provider_price)
        coupon_result, coupon_error = _coupon_check(request.user, request.POST.get("coupon_code", ""), user_price) if request.POST.get("coupon_code") else (None, None)
        if coupon_error:
            return _json({"success": False, "message": coupon_error})
        final_price = coupon_result["final_price"] if coupon_result else user_price
        if request.user.balance < final_price:
            return _json({"success": False, "message": f"Insufficient wallet balance. You need {format_ngn(final_price)}."})
        provider_order = FiveSim.buy_activation(country, operator, product)
        if "error" in provider_order:
            return _json({"success": False, "message": friendly_provider_error(provider_order["error"])})
        with transaction.atomic():
            order = Order.objects.create(
                user=request.user, provider_order_id=int(provider_order.get("id", uuid.uuid4().int % 900000)),
                phone=str(provider_order.get("phone", "+2348000000000")), country=country, operator=operator,
                product=product, provider_cost=provider_price, user_price=final_price,
                status=str(provider_order.get("status", "PENDING")).upper(),
                expires_at=timezone.now() + timedelta(minutes=20),
            )
            if not debit_balance(request.user.id, final_price, f"Purchase: {product.title()} number", order):
                return _json({"success": False, "message": "Your balance changed before checkout. Please try again."}, 409)
            if coupon_result:
                _coupon_check(request.user, request.POST["coupon_code"], user_price, consume=True, order=order)
        return _json({"success": True, "message": f"Number purchased for {format_ngn(final_price)}.", "order_id": order.id, "phone": order.phone, "status": order.status})
    order = get_object_or_404(Order, pk=request.POST.get("order_id"), user=request.user)
    if action not in {"cancel", "finish", "ban"}:
        return _json({"success": False, "message": "Unknown action."}, 400)
    allowed = {"cancel": {"PENDING"}, "finish": {"PENDING", "RECEIVED"}, "ban": {"PENDING", "RECEIVED"}}
    if order.status not in allowed[action]:
        return _json({"success": False, "message": "This order can no longer be updated."})
    result = FiveSim.order_action(action, order.provider_order_id)
    if "error" in result and "already" not in result["error"].lower():
        return _json({"success": False, "message": friendly_provider_error(result["error"])})
    order.status = {"cancel": "CANCELED", "finish": "FINISHED", "ban": "BANNED"}[action]
    order.save(update_fields=["status", "updated_at"])
    if action in {"cancel", "ban"} and not Transaction.objects.filter(reference=f"REF_{action.upper()}_{order.id}").exists():
        credit_balance(order.user_id, order.user_price, "refund", f"Refund: {action} order #{order.id}", f"REF_{action.upper()}_{order.id}")
    message = f"{format_ngn(order.user_price)} has been refunded to your wallet." if action in {"cancel", "ban"} else "Order marked as completed."
    return _json({"success": True, "message": message})


@login_required(login_url="/login.html")
@require_http_methods(["GET"])
def coupon_api(request):
    if request.GET.get("action") != "validate":
        return _json({"success": False, "message": "Unknown action."}, 400)
    result, error = _coupon_check(request.user, request.GET.get("code", ""), request.GET.get("price", "0"))
    if error:
        return _json({"success": False, "message": error})
    coupon = result["coupon"]
    return _json({"success": True, "coupon_id": coupon.id, "code": coupon.code, "discount_type": coupon.discount_type, "discount_value": float(coupon.discount_value), "discount_ngn": float(result["discount"]), "final_price": float(result["final_price"]), "description": coupon.description, "message": f"{int(coupon.discount_value)}% discount applied!" if coupon.discount_type == "percent" else f"{format_ngn(coupon.discount_value)} off applied!"})


@login_required(login_url="/login.html")
@require_http_methods(["POST"])
def admin_action(request):
    if request.user.role != "admin":
        return _json({"success": False, "message": "Administrator access required."}, 403)
    action = request.POST.get("action", "")
    if action in {"ban", "activate", "delete"}:
        user = get_object_or_404(User, pk=request.POST.get("user_id"), role="user")
        if action == "ban":
            user.status = "banned"
            user.save(update_fields=["status", "updated_at"])
            messages.success(request, f"{user.email} has been banned.")
        elif action == "activate":
            user.status = "active"
            user.save(update_fields=["status", "updated_at"])
            messages.success(request, f"{user.email} is active again.")
        else:
            user.delete()
            messages.success(request, "User deleted.")
        return redirect("/admin/users.html")
    if action == "adjust_balance":
        user = get_object_or_404(User, pk=request.POST.get("user_id"), role="user")
        amount = money(request.POST.get("amount", "0"))
        if amount <= 0:
            messages.error(request, "Enter a positive amount.")
        else:
            adjust_type = request.POST.get("adjust_type", "credit")
            if adjust_type == "debit":
                if not debit_balance(user.id, amount, "Admin balance adjustment"):
                    messages.error(request, "User does not have enough balance.")
                else:
                    messages.success(request, "Balance debited.")
            else:
                credit_balance(user.id, amount, "adjustment", "Admin balance adjustment")
                messages.success(request, "Balance credited.")
        return redirect("/admin/users.html")
    if action == "save_settings":
        allowed = {"site_name", "site_rate", "markup_percent", "extra_fee_percent", "topup_fee_percent", "topup_fee_percent_high", "topup_fee_threshold", "min_topup", "max_topup", "sms_poll_interval", "maintenance_mode", "paystack_public_key", "paystack_secret_key", "provider_api_key", "support_whatsapp"}
        for key in allowed:
            if key in request.POST:
                Setting.set_value(key, request.POST.get(key, ""))
        messages.success(request, "Settings saved successfully.")
        return redirect("/admin/settings.html")
    if action == "save_provider":
        Setting.set_value("provider_api_key", request.POST.get("provider_api_key", ""))
        messages.success(request, "Provider settings saved.")
        return redirect("/admin/providers.html")
    if action == "save_payment":
        Setting.set_value("paystack_public_key", request.POST.get("paystack_public_key", ""))
        secret_key = request.POST.get("paystack_secret_key", "")
        if secret_key:
            Setting.set_value("paystack_secret_key", secret_key)
        messages.success(request, "Payment settings saved.")
        return redirect("/admin/payment.html")
    if action in {"create_coupon", "toggle_coupon", "delete_coupon"}:
        if action == "create_coupon":
            code = request.POST.get("code", "").upper().strip()
            if not code:
                messages.error(request, "Coupon code is required.")
            elif Coupon.objects.filter(code=code).exists():
                messages.error(request, "That coupon code already exists.")
            else:
                valid_until = request.POST.get("valid_until", "").strip()
                try:
                    valid_until = datetime.fromisoformat(valid_until) if valid_until else None
                    if valid_until is not None and not timezone.is_aware(valid_until):
                        valid_until = timezone.make_aware(valid_until)
                except ValueError:
                    valid_until = None
                    messages.warning(request, "Expiry date ignored — invalid format.")
                Coupon.objects.create(
                    code=code, description=request.POST.get("description", ""), discount_type=request.POST.get("discount_type", "percent"),
                    discount_value=money(request.POST.get("discount_value", "0")), min_purchase=money(request.POST.get("min_purchase", "0")),
                    max_uses=int(request.POST["max_uses"]) if request.POST.get("max_uses") else None, target_users=request.POST.get("target_users", "all"),
                    valid_until=valid_until,
                )
                messages.success(request, "Coupon created.")
        else:
            coupon = get_object_or_404(Coupon, pk=request.POST.get("coupon_id"))
            if action == "toggle_coupon":
                coupon.is_active = not coupon.is_active
                coupon.save(update_fields=["is_active"])
            else:
                coupon.delete()
            messages.success(request, "Coupon updated.")
        return redirect("/admin/coupons.html")
    if action == "admin_password":
        new_password = request.POST.get("new_password", "")
        confirm_password = request.POST.get("confirm_password", "")
        if len(new_password) < 6:
            messages.error(request, "Password must be at least 6 characters.")
        elif new_password != confirm_password:
            messages.error(request, "Passwords do not match.")
        else:
            request.user.set_password(new_password)
            request.user.save(update_fields=["password", "updated_at"])
            update_session_auth_hash(request, request.user)
            messages.success(request, "Password updated successfully.")
        return redirect("/admin/settings.html")
    return _json({"success": False, "message": "Unknown admin action."}, 400)


@login_required(login_url="/login.html")
@require_http_methods(["POST"])
def paystack_init(request):
    try:
        amount = money(request.POST.get("amount", "0"))
    except Exception:
        amount = Decimal("0")
    minimum, maximum = money(Setting.value("min_topup", "100")), money(Setting.value("max_topup", "500000"))
    if amount < minimum or amount > maximum:
        return _json({"success": False, "message": f"Top-up must be between {format_ngn(minimum)} and {format_ngn(maximum)}."}, 400)
    if not Paystack.public_key() or not Paystack.secret_key():
        return _json({"success": False, "message": "Payment gateway not configured. Contact support."}, 503)
    reference = f"VSMS_{request.user.id}_{uuid.uuid4().hex[:18].upper()}"
    Topup.objects.create(user=request.user, amount_paid=amount, fee_amount=0, net_credited=amount, reference=reference, status="pending")
    response = Paystack.initialize(request.user.email, amount, reference, {"user_id": request.user.id})
    if not response.get("status"):
        Topup.objects.filter(reference=reference).update(status="failed", gateway_response=json.dumps(response))
        return _json({"success": False, "message": response.get("message", "Could not initialize payment.")}, 502)
    return _json({"success": True, "public_key": Paystack.public_key(), "reference": reference, "authorization_url": response.get("data", {}).get("authorization_url")})


@login_required(login_url="/login.html")
@require_http_methods(["POST"])
def paystack_verify(request):
    reference = request.POST.get("reference", "").strip()
    topup = get_object_or_404(Topup, reference=reference, user=request.user)
    response = Paystack.verify(reference)
    data = response.get("data", {})
    if not response.get("status") or data.get("status") != "success":
        Topup.objects.filter(pk=topup.pk).update(status="failed", gateway_response=json.dumps(response))
        return _json({"success": False, "message": "Payment could not be verified."})
    amount_paid = Decimal(data.get("amount", 0)) / 100
    Paystack.process_topup(request.user.id, reference, amount_paid, response)
    request.user.refresh_from_db()
    return _json({"success": True, "message": f"{format_ngn(amount_paid)} payment verified.", "balance": str(request.user.balance)})


@csrf_exempt
@require_http_methods(["POST"])
def paystack_webhook(request):
    signature = request.headers.get("X-Paystack-Signature", "")
    if not Paystack.validate_webhook(request.body, signature):
        return HttpResponse("Invalid signature", status=401)
    payload = json.loads(request.body.decode("utf-8"))
    if payload.get("event") == "charge.success":
        data = payload.get("data", {})
        reference = data.get("reference")
        topup = Topup.objects.filter(reference=reference).first()
        if topup:
            Paystack.process_topup(topup.user_id, reference, Decimal(data.get("amount", 0)) / 100, payload)
    return HttpResponse("OK")


def paystack_callback(request):
    reference = request.GET.get("reference", "")
    if request.user.is_authenticated and reference:
        return redirect(f"/user/topup.html?reference={reference}")
    return redirect("/login.html")


def health(request):
    return _json({"ok": True, "service": "VerifySMS Django"})


def deploy_setup(request):
    """One-time remote setup endpoint: runs migrations + seed_demo.

    Protected by a secret token so it can be triggered from a browser
    without shell/SSH access (e.g. from a phone). Requires the
    DEPLOY_SETUP_TOKEN env var to be set — if it isn't set, this endpoint
    refuses to run at all, so it's inert until you deliberately turn it on.
    Delete this view (and the URL route pointing to it) once you're done
    with initial setup; it's not something to leave live long-term.
    """
    import io
    from django.conf import settings as dj_settings
    from django.core.management import call_command

    expected_token = getattr(dj_settings, "DEPLOY_SETUP_TOKEN", "")
    if not expected_token:
        return _json({"success": False, "message": "DEPLOY_SETUP_TOKEN is not set on the server."}, 403)
    if request.GET.get("token", "") != expected_token:
        return _json({"success": False, "message": "Invalid or missing token."}, 403)

    output = io.StringIO()
    try:
        call_command("migrate", interactive=False, stdout=output)
        if request.GET.get("seed", "1") != "0":
            call_command("seed_demo", stdout=output)
    except Exception as exc:
        return _json({"success": False, "message": str(exc), "log": output.getvalue()}, 500)
    return _json({"success": True, "log": output.getvalue()})
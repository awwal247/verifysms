from django.urls import path, re_path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login.html", views.login_page, name="login"),
    path("register.html", views.register_page, name="register"),
    path("logout.html", views.logout_page, name="logout"),
    path("healthz/", views.health, name="health"),
    path("deploy-setup/", views.deploy_setup, name="deploy-setup"),
    re_path(r"^user/(?P<page>[a-z]+)\.html$", views.user_page, name="user-page"),
    re_path(r"^admin/(?P<page>[a-z]+)\.html$", views.admin_page, name="admin-page"),
    path("admin/action/", views.admin_action, name="admin-action"),
    path("api/support/", views.support_info, name="support-info"),
    path("api/provider/", views.provider_api, name="provider-api"),
    path("api/orders/", views.orders_api, name="orders-api"),
    path("api/coupon/", views.coupon_api, name="coupon-api"),
    path("api/paystack/init/", views.paystack_init, name="paystack-init"),
    path("api/paystack/verify/", views.paystack_verify, name="paystack-verify"),
    path("api/paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
    path("api/paystack/callback/", views.paystack_callback, name="paystack-callback"),
]
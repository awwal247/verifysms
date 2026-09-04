from django.contrib import admin

from .models import Coupon, CouponUse, Order, Setting, Topup, Transaction, User

for model in (User, Order, Transaction, Topup, Coupon, CouponUse, Setting):
    admin.site.register(model)
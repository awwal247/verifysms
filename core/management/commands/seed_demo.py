from decimal import Decimal

from django.core.management.base import BaseCommand

from core.models import Coupon, Setting, User


class Command(BaseCommand):
    help = "Create the default VerifySMS settings and a local admin account."

    def add_arguments(self, parser):
        parser.add_argument("--email", default="admin@dev.com")
        parser.add_argument("--password", default="Admin_Dev")
        parser.add_argument("--test-email", default="test@dev.com")
        parser.add_argument("--test-password", default="Dev_user")

    def handle(self, *args, **options):
        defaults = {
            "site_name": ("VerifySMS", "Site name"),
            "site_tagline": ("Instant SMS verification numbers", "Site tagline"),
            "currency_symbol": ("₦", "Currency symbol"),
            "site_rate": ("1600", "NGN per provider USD"),
            "markup_percent": ("20", "Provider price markup"),
            "topup_fee_percent": ("3", "Wallet top-up fee"),
            "min_topup": ("500", "Minimum wallet top-up"),
            "max_topup": ("500000", "Maximum wallet top-up"),
            "sms_poll_interval": ("5", "SMS polling interval"),
            "maintenance_mode": ("0", "Maintenance mode"),
        }
        for key, (value, description) in defaults.items():
            Setting.set_value(key, value, description)
        email = options["email"].lower()
        user, created = User.objects.get_or_create(email=email, defaults={"role": "admin", "first_name": "VerifySMS Admin", "is_staff": True, "is_superuser": True})
        user.role, user.is_staff, user.is_superuser, user.status = "admin", True, True, "active"
        if created or not user.check_password(options["password"]):
            user.set_password(options["password"])
        user.save()
        Coupon.objects.get_or_create(
            code="WELCOME20",
            defaults={"description": "Welcome discount", "discount_type": "percent", "discount_value": Decimal("20"), "min_purchase": Decimal("0"), "max_uses": 100},
        )
        test_email = options["test_email"].lower()
        test_user, test_created = User.objects.get_or_create(
            email=test_email,
            defaults={"role": "user", "first_name": "Test User", "status": "active"},
        )
        test_user.role, test_user.status = "user", "active"
        if test_created or not test_user.check_password(options["test_password"]):
            test_user.set_password(options["test_password"])
        test_user.save()
        self.stdout.write(self.style.SUCCESS(f"Seeded settings. Admin: {email}"))
        self.stdout.write(self.style.SUCCESS(f"Test user (for Paystack test-card top-ups): {test_email}"))
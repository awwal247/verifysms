"""Seed baseline settings, a starter coupon and the first admin account.

Safe by default:
  * No built-in passwords. The admin password comes from --password, the
    SEED_ADMIN_PASSWORD environment variable, or is generated randomly and
    printed ONCE (store it in a password manager, then change it in the UI).
  * Idempotent: re-running never resets an existing password and never
    overwrites settings an admin has already changed.
  * An existing non-admin account is never silently promoted to admin
    (pass --promote to do it deliberately).
  * The demo customer account is opt-in (--with-test-user) and, outside
    DEBUG, additionally needs --force.
"""
import os
import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from core.models import Coupon, Setting, User

# Seeded only when the key does not exist yet. Mirrors the defaults the admin
# settings page and the services layer fall back to. No secrets belong here.
DEFAULT_SETTINGS = {
    "site_name": ("VerifySMS", "Site name"),
    "site_tagline": ("Instant SMS verification numbers", "Site tagline"),
    "currency_symbol": ("₦", "Currency symbol"),
    "site_rate": ("1600", "NGN per provider USD"),
    "markup_percent": ("20", "Provider price markup"),
    "extra_fee_percent": ("8", "Extra fee on purchases"),
    "topup_fee_percent": ("3", "Wallet top-up fee"),
    "topup_fee_percent_high": ("6", "Top-up fee above the threshold"),
    "topup_fee_threshold": ("1000", "Top-up amount where the higher fee starts"),
    "min_topup": ("100", "Minimum wallet top-up"),
    "max_topup": ("500000", "Maximum wallet top-up"),
    "sms_poll_interval": ("5", "SMS polling interval"),
    "maintenance_mode": ("0", "Maintenance mode"),
    "active_provider": ("auto", "Routing is automatic: Server 1 first, Server 2 fallback."),
}


def _check_password(password, user=None):
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise CommandError("Password rejected: " + " ".join(exc.messages))


class Command(BaseCommand):
    help = "Seed default settings, the WELCOME20 coupon and the first admin account (no default passwords)."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=os.getenv("SEED_ADMIN_EMAIL", ""),
                            help="Admin email (or SEED_ADMIN_EMAIL). Required.")
        parser.add_argument("--password", default="",
                            help="Admin password (or SEED_ADMIN_PASSWORD). Omit to generate one.")
        parser.add_argument("--reset-password", action="store_true",
                            help="Replace the password of an EXISTING admin account.")
        parser.add_argument("--promote", action="store_true",
                            help="Allow promoting an existing non-admin account to admin.")
        parser.add_argument("--with-test-user", action="store_true",
                            help="Also create a customer account for Paystack test-card top-ups.")
        parser.add_argument("--test-email", default="testuser@example.com")
        parser.add_argument("--test-password", default="")
        parser.add_argument("--force", action="store_true",
                            help="Allow --with-test-user when DEBUG is off (never do this on a public site).")
        parser.add_argument("--skip-admin", action="store_true", help="Seed settings and coupon only.")

    @transaction.atomic
    def handle(self, *args, **opts):
        created_settings = self._seed_settings()
        self._seed_coupon()
        self.stdout.write(self.style.SUCCESS(f"Settings: {created_settings} created, "
                                             f"{len(DEFAULT_SETTINGS) - created_settings} already present (left untouched)."))
        if not opts["skip_admin"]:
            self._seed_admin(opts)
        if opts["with_test_user"]:
            self._seed_test_user(opts)

    # ── settings / coupon ────────────────────────────────────────────────
    def _seed_settings(self):
        created = 0
        for key, (value, description) in DEFAULT_SETTINGS.items():
            _, was_created = Setting.objects.get_or_create(
                setting_key=key, defaults={"setting_value": value, "description": description})
            created += was_created
        return created

    def _seed_coupon(self):
        Coupon.objects.get_or_create(
            code="WELCOME20",
            defaults={"description": "Welcome discount", "discount_type": "percent",
                      "discount_value": Decimal("20"), "min_purchase": Decimal("0"), "max_uses": 100},
        )

    # ── admin ────────────────────────────────────────────────────────────
    def _seed_admin(self, opts):
        email = (opts["email"] or "").strip().lower()
        if not email:
            raise CommandError("--email (or SEED_ADMIN_EMAIL) is required to create the admin account. "
                               "Use --skip-admin to seed settings only.")
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(f"{email!r} is not a valid email address.")

        supplied = opts["password"] or os.getenv("SEED_ADMIN_PASSWORD", "")
        user = User.objects.filter(email=email).first()

        if user is None:
            generated = not supplied
            password = supplied or secrets.token_urlsafe(18)
            _check_password(password)
            User.objects.create_user(email=email, password=password, first_name="VerifySMS Admin",
                                     role="admin", status="active", is_superuser=True)
            self.stdout.write(self.style.SUCCESS(f"Created admin: {email}"))
            if generated:
                self.stdout.write(self.style.WARNING(
                    f"Generated password (shown once, not stored anywhere else): {password}"))
            return

        if user.role != "admin":
            if not opts["promote"]:
                raise CommandError(f"{email} already exists as a regular customer. Refusing to make it an admin; "
                                   "re-run with --promote if that is intentional.")
            user.role, user.is_superuser, user.status = "admin", True, "active"
            user.save()
            self.stdout.write(self.style.WARNING(f"Promoted existing account to admin: {email}"))
        else:
            self.stdout.write(f"Admin {email} already exists - unchanged.")

        if opts["reset_password"]:
            generated = not supplied
            password = supplied or secrets.token_urlsafe(18)
            _check_password(password, user)
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS("Admin password reset."))
            if generated:
                self.stdout.write(self.style.WARNING(f"Generated password (shown once): {password}"))

    # ── optional demo customer ───────────────────────────────────────────
    def _seed_test_user(self, opts):
        if not settings.DEBUG and not opts["force"]:
            raise CommandError("--with-test-user is for local/staging only. Re-run with --force to override "
                               "(never on a public production site).")
        email = opts["test_email"].strip().lower()
        user = User.objects.filter(email=email).first()
        if user is not None:
            self.stdout.write(f"Test user {email} already exists - unchanged.")
            return
        generated = not opts["test_password"]
        password = opts["test_password"] or secrets.token_urlsafe(14)
        _check_password(password)
        User.objects.create_user(email=email, password=password, first_name="Test User", role="user", status="active")
        self.stdout.write(self.style.SUCCESS(f"Created test user: {email} (starts with a zero balance)"))
        if generated:
            self.stdout.write(self.style.WARNING(f"Generated test-user password (shown once): {password}"))

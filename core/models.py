from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.base_user import BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("role", "admin")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    role = models.CharField(max_length=10, choices=(("user", "User"), ("admin", "Admin")), default="user")
    status = models.CharField(
        max_length=12,
        choices=(("active", "Active"), ("banned", "Banned"), ("suspended", "Suspended")),
        default="active",
    )
    email_verified = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def save(self, *args, **kwargs):
        self.is_staff = self.role == "admin" or self.is_superuser
        super().save(*args, **kwargs)


class Setting(models.Model):
    setting_key = models.CharField(max_length=100, unique=True)
    setting_value = models.TextField(blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def value(cls, key, default=""):
        row = cls.objects.filter(setting_key=key).first()
        return row.setting_value if row else default

    @classmethod
    def set_value(cls, key, value, description=""):
        cls.objects.update_or_create(
            setting_key=key,
            defaults={"setting_value": str(value), "description": description},
        )


class Order(models.Model):
    CATEGORY_CHOICES = (("activation", "Activation"), ("hosting", "Hosting"))
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("RECEIVED", "Received"),
        ("CANCELED", "Canceled"),
        ("TIMEOUT", "Timeout"),
        ("FINISHED", "Finished"),
        ("BANNED", "Banned"),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    provider_order_id = models.BigIntegerField()
    phone = models.CharField(max_length=30)
    country = models.CharField(max_length=80)
    operator = models.CharField(max_length=80, default="any")
    product = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="activation")
    provider_cost = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    user_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="PENDING")
    sms_code = models.CharField(max_length=50, blank=True, null=True)
    sms_text = models.TextField(blank=True, null=True)
    sms_sender = models.CharField(max_length=100, blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Transaction(models.Model):
    TYPE_CHOICES = (
        ("topup", "Topup"),
        ("purchase", "Purchase"),
        ("refund", "Refund"),
        ("adjustment", "Adjustment"),
    )
    STATUS_CHOICES = (("pending", "Pending"), ("success", "Success"), ("failed", "Failed"))
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transactions")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    description = models.CharField(max_length=255, blank=True, null=True)
    reference = models.CharField(max_length=150, blank=True, null=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, blank=True, null=True, related_name="transactions")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="success")
    created_at = models.DateTimeField(auto_now_add=True)


class Topup(models.Model):
    STATUS_CHOICES = (("pending", "Pending"), ("success", "Success"), ("failed", "Failed"))
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="topups")
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    net_credited = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=150, unique=True)
    payment_gateway = models.CharField(max_length=50, default="paystack")
    gateway_response = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def fee_rate(self):
        """Effective fee percentage for this top-up (for display)."""
        try:
            if not self.amount_paid:
                return ""
            return f"{(self.fee_amount / self.amount_paid) * 100:.1f}"
        except Exception:
            return ""


class SmsMessage(models.Model):
    """A single inbound SMS for an order, kept permanently so the customer can
    reopen the page and read the code after the order has ended."""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="sms_messages")
    provider_message_id = models.CharField(max_length=64, blank=True, default="")
    sender = models.CharField(max_length=100, blank=True, default="")
    text = models.TextField(blank=True, default="")
    code = models.CharField(max_length=50, blank=True, default="")
    provider_date = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "provider_message_id"],
                condition=~models.Q(provider_message_id=""),
                name="uniq_order_provider_sms",
            )
        ]

    @property
    def display_sender(self):
        return self.sender or "Notification"


class Coupon(models.Model):
    DISCOUNT_CHOICES = (("percent", "Percent"), ("fixed", "Fixed"))
    TARGET_CHOICES = (("all", "All"), ("new", "New"), ("old", "Old"), ("date_range", "Date range"))
    code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_CHOICES, default="percent")
    discount_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    min_purchase = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    max_uses = models.PositiveIntegerField(blank=True, null=True)
    uses_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(blank=True, null=True)
    valid_until = models.DateTimeField(blank=True, null=True)
    target_users = models.CharField(max_length=12, choices=TARGET_CHOICES, default="all")
    target_reg_from = models.DateField(blank=True, null=True)
    target_reg_to = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CouponUse(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="uses")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="coupon_uses")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("coupon", "user"), name="one_coupon_use_per_user")]
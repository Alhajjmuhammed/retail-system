"""
The platform's own business: plans, tenants, subscriptions, invoices.

A tenant is one retail business. The subscription lives here, plan limits count
branches, and a lapsed subscription never deletes anything -- it drops the shop
to read-only.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.features import (
    ALL_LIMITS,
    FEATURES_BY_KEY,
    LIMIT_BRANCHES,
    LIMIT_HISTORY_DAYS,
    LIMIT_PRODUCTS,
    LIMIT_USERS,
    LimitExceeded,
)
from apps.core.models import TimeStampedModel


class Plan(TimeStampedModel):
    """
    A subscription tier.

    Priced per branch, never per user -- a shop with four cashiers on one till
    should not pay four times.
    """

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    price_monthly = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price_annual = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="TZS")

    trial_days = models.PositiveIntegerField(default=14)
    is_public = models.BooleanField(default=True, help_text="Offered at signup.")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_monthly"]

    def __str__(self):
        return self.name

    def feature_keys(self) -> set[str]:
        """
        What this plan includes, asked once.

        Every permission that depends on a feature asks this, and a page
        checks a lot of permissions: it was seven identical queries to render
        a product list. The answer cannot change inside one request, and the
        plan object is fetched fresh for the next one, so the memo lives
        exactly as long as it is true. Anything that edits a plan's features
        and then reads them back calls :meth:`forget_features` first.
        """
        if "_feature_keys" not in self.__dict__:
            self.__dict__["_feature_keys"] = set(
                self.features.values_list("feature_key", flat=True)
            )
        return self.__dict__["_feature_keys"]

    def forget_features(self) -> None:
        self.__dict__.pop("_feature_keys", None)

    def refresh_from_db(self, *args, **kwargs):
        """
        Reloading the row reloads what it includes.

        Without this the memo outlived the thing it was remembering: a test
        edited a plan's features, called ``refresh_from_db`` as anybody
        would, and was told the old answer.
        """
        self.forget_features()
        return super().refresh_from_db(*args, **kwargs)

    def limit(self, key: str) -> int | None:
        """None means unlimited."""
        row = self.limits.filter(key=key).first()
        return row.value if row else None


class PlanFeature(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="features")
    feature_key = models.CharField(max_length=40)

    class Meta:
        unique_together = [("plan", "feature_key")]

    def __str__(self):
        feature = FEATURES_BY_KEY.get(self.feature_key)
        return feature.label if feature else self.feature_key


class PlanLimit(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="limits")
    key = models.CharField(max_length=40, choices=ALL_LIMITS)
    value = models.PositiveIntegerField(
        null=True, blank=True, help_text="Blank means unlimited."
    )

    class Meta:
        unique_together = [("plan", "key")]

    def __str__(self):
        return f"{self.get_key_display()}: {self.value or 'unlimited'}"


class TenantStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"


class Tenant(TimeStampedModel):
    """One retail business. The root of everything a shop owns."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=60, unique=True)
    legal_name = models.CharField(max_length=160, blank=True)

    tin = models.CharField("TIN", max_length=30, blank=True)
    vrn = models.CharField("VAT number", max_length=30, blank=True)

    country = models.CharField(max_length=2, default="TZ")
    currency = models.CharField(max_length=3, default="TZS")
    timezone = models.CharField(max_length=64, default="Africa/Dar_es_Salaam")

    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    logo = models.ImageField(upload_to="tenants/logos/", blank=True)

    status = models.CharField(
        max_length=16, choices=TenantStatus.choices, default=TenantStatus.ACTIVE
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    # -- plan questions -----------------------------------------------------

    @property
    def active_subscription(self):
        """
        The subscription, or None.

        Accessing a missing one-to-one raises rather than returning None, so
        every `if subscription is None` guard written against the plain
        attribute was dead code.
        """
        try:
            return self.subscription
        except Subscription.DoesNotExist:
            return None

    def has_feature(self, key: str) -> bool:
        """
        The only way to ask what a plan includes.

        There is no `if plan == "business"` anywhere in this system. Add a
        feature key instead.
        """
        subscription = self.active_subscription
        if subscription is None or not subscription.is_usable:
            return False
        return key in subscription.plan.feature_keys()

    def limit_for(self, key: str) -> int | None:
        subscription = self.active_subscription
        if subscription is None:
            # No subscription means no allowance at all, not unlimited.
            return 0
        return subscription.plan.limit(key)

    def usage_of(self, key: str) -> int:
        from apps.accounts.models import Membership
        from apps.catalog.models import Product
        from apps.org.models import Branch

        counters = {
            LIMIT_BRANCHES: lambda: Branch.objects_all.filter(
                tenant=self, is_active=True
            ).count(),
            LIMIT_USERS: lambda: Membership.objects_all.filter(
                tenant=self, is_active=True
            ).count(),
            LIMIT_PRODUCTS: lambda: Product.objects_all.filter(
                tenant=self, is_active=True
            ).count(),
        }
        counter = counters.get(key)
        if counter is None:
            return 0
        # Counted for *this* tenant whatever the request is bound to: joining
        # shop B while signed in to shop A must not count A's rows (or none).
        from apps.core.context import unscoped

        with unscoped():
            return counter()

    def within_limit(self, key: str, *, adding: int = 1) -> bool:
        allowed = self.limit_for(key)
        if allowed is None:
            return True
        return self.usage_of(key) + adding <= allowed

    def history_start(self):
        """
        The earliest day this plan lets the shop look back to, or None.

        "Days of sales history" was a plan limit nothing ever applied. Data is
        never deleted -- an upgrade shows it all again -- it is only not shown.
        """
        days = self.limit_for(LIMIT_HISTORY_DAYS)
        if days is None:
            return None  # unlimited
        # No subscription means no allowance: today only, not everything.
        return timezone.localdate() - timedelta(days=max(days, 1) - 1)

    def enforce_limit(self, key: str, *, adding: int = 1) -> None:
        """
        Checked at write time, never at read time.

        A shop that downgrades keeps its data; it simply cannot add more.
        """
        if not self.within_limit(key, adding=adding):
            label = dict(ALL_LIMITS).get(key, key)
            raise LimitExceeded(key, self.limit_for(key), label)


class SubscriptionStatus(models.TextChoices):
    TRIALING = "trialing", "Trial"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past due"
    GRACE = "grace", "Grace period"
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"


class BillingCycle(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    ANNUAL = "annual", "Annual"


class Subscription(TimeStampedModel):
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    cycle = models.CharField(
        max_length=10, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )
    status = models.CharField(
        max_length=16,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.TRIALING,
    )

    trial_ends_at = models.DateTimeField(null=True, blank=True)
    period_start = models.DateTimeField(default=timezone.now)
    period_end = models.DateTimeField(null=True, blank=True)
    grace_ends_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # Billing is per branch. Recorded at invoice time so a mid-period change
    # does not silently rewrite what was charged.
    branches_billed = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.tenant} on {self.plan}"

    @property
    def is_usable(self) -> bool:
        """Can this shop still do anything at all?"""
        return self.status != SubscriptionStatus.CANCELLED

    @property
    def is_read_only(self) -> bool:
        """Suspended shops keep their data and lose the ability to transact."""
        return self.status in {
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.CANCELLED,
        }

    @property
    def days_until_due(self) -> int | None:
        if self.period_end is None:
            return None
        return (self.period_end - timezone.now()).days

    def begin(self):
        """
        Start the subscription as the plan intends.

        A plan with no trial days is not a trial of zero length. The Free
        plan has none, and every shop that signs up lands on it, so this used
        to create shops already past the end of a trial they never had. The
        nightly job then read that as a shop which had failed to pay for
        something free: past due, then grace, then suspended -- a free shop
        stopped from selling within days of opening.
        """
        from apps.tenancy.billing import period_after

        if self.plan.trial_days:
            self.status = SubscriptionStatus.TRIALING
            self.trial_ends_at = timezone.now() + timedelta(days=self.plan.trial_days)
            self.period_end = self.trial_ends_at
        else:
            # On the plan, not on trial. Billing already knows how to roll a
            # free plan's period forward; it simply never got the chance.
            from datetime import datetime, time

            start, end = period_after(self, timezone.localdate())
            self.status = SubscriptionStatus.ACTIVE
            self.trial_ends_at = None
            self.period_start = timezone.make_aware(datetime.combine(start, time.min))
            self.period_end = timezone.make_aware(datetime.combine(end, time.max))
        self.save(update_fields=["status", "trial_ends_at", "period_start",
                                 "period_end", "updated_at"])

    # The old name, kept because "start the subscription" is what callers mean.
    start_trial = begin

    def enter_grace(self):
        self.status = SubscriptionStatus.GRACE
        self.grace_ends_at = timezone.now() + timedelta(
            days=settings.SUBSCRIPTION_GRACE_DAYS
        )
        self.save(update_fields=["status", "grace_ends_at", "updated_at"])


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    PAID = "paid", "Paid"
    VOID = "void", "Void"
    UNCOLLECTIBLE = "uncollectible", "Uncollectible"


class Invoice(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="invoices")
    number = models.CharField(max_length=30, unique=True)
    period_start = models.DateField()
    period_end = models.DateField()

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="TZS")

    status = models.CharField(
        max_length=16, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT
    )
    due_date = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-period_start"]

    def __str__(self):
        return self.number

    @property
    def outstanding(self) -> Decimal:
        paid = self.payments.filter(status=PaymentStatus.SUCCEEDED).aggregate(
            total=models.Sum("amount")
        )["total"] or Decimal("0")
        return self.total - paid


class PaymentMethod(models.TextChoices):
    MPESA = "mpesa", "M-Pesa"
    TIGOPESA = "tigopesa", "Tigo Pesa"
    AIRTELMONEY = "airtelmoney", "Airtel Money"
    CARD = "card", "Card"
    BANK = "bank", "Bank transfer"
    MANUAL = "manual", "Recorded manually"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


class Payment(TimeStampedModel):
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="payments"
    )
    method = models.CharField(max_length=16, choices=PaymentMethod.choices)
    provider = models.CharField(max_length=40, blank=True)
    provider_ref = models.CharField(max_length=120, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount}"


class UsageSnapshot(models.Model):
    """
    A daily count per tenant.

    Two jobs: billing a plan priced per branch, and showing you on the platform
    dashboard which shops are pressing against their limits and worth calling.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="usage_snapshots"
    )
    date = models.DateField()
    branches = models.PositiveIntegerField(default=0)
    users = models.PositiveIntegerField(default=0)
    products = models.PositiveIntegerField(default=0)
    sales_count = models.PositiveIntegerField(default=0)
    sales_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        unique_together = [("tenant", "date")]
        ordering = ["-date"]

    def __str__(self):
        return f"{self.tenant} on {self.date}"

"""
The shops themselves.

A product belongs to the tenant; a quantity belongs to a branch. That split is
the reason multi-branch can be switched on later without reshaping anything.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.features import LIMIT_BRANCHES
from apps.core.models import TenantModel


class Branch(TenantModel):
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]
        verbose_name_plural = "branches"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Plan limits are enforced at write time, never at read time. A shop
        # that downgrades keeps every branch it has; it just cannot add one.
        # Switching an archived branch back on counts as adding one: archive,
        # add a new branch, reactivate the old one used to beat the limit.
        reviving = (
            not self._state.adding and self.is_active
            and type(self).objects_all.filter(pk=self.pk, is_active=False).exists()
        )
        if (self._state.adding and self.is_active) or reviving:
            from apps.core.context import get_current_tenant

            # tenant_id, not tenant: the descriptor raises when the FK is unset,
            # and on a new row it usually is -- TenantModel.save fills it from
            # the request context a moment later.
            tenant = self.tenant if self.tenant_id else get_current_tenant()
            if tenant is not None:
                tenant.enforce_limit(LIMIT_BRANCHES)
        super().save(*args, **kwargs)


class Register(TenantModel):
    """A till. One branch can have several; each opens its own shift."""

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="registers")
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=20, blank=True)
    printer_config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["branch__name", "name"]
        unique_together = [("branch", "name")]

    def __str__(self):
        return f"{self.branch} / {self.name}"


class CostMethod(models.TextChoices):
    WEIGHTED_AVERAGE = "weighted_average", "Weighted average"
    LAST_COST = "last_cost", "Last cost paid"


class TenantSettings(TenantModel):
    """
    Per-business configuration.

    ``extra`` is deliberate room to grow: a new module can store its own
    settings here without a migration, and only promotes them to real columns
    once they have proved they are permanent.
    """

    receipt_header = models.TextField(blank=True)
    receipt_footer = models.TextField(blank=True)
    show_tin_on_receipt = models.BooleanField(default=True)
    # A picture against each line on a quotation or invoice. Some shops sell
    # things that look alike and want it; others want plain paperwork and a
    # column of thumbnails is in the way. The picker always shows one --
    # that is about choosing the right product, not about the document.
    show_product_images = models.BooleanField(default=True)

    # What the shop's own invoice says and shows. Every shop's terms differ
    # -- when payment is due, who owns the goods until it arrives, how long
    # a claim can be made -- so they are typed here rather than written into
    # the system, and each switch decides whether that part prints at all.
    invoice_terms = models.TextField(blank=True)
    show_terms_on_invoice = models.BooleanField(default=True)
    show_amount_in_words = models.BooleanField(default=True)
    show_bank_details = models.BooleanField(default=True)

    # Who decides the price on a quotation or invoice. Off, a product's price
    # is a suggestion staff type over -- a quote is a negotiation. On, a
    # product goes in at its catalogue price (the customer's list first) and
    # only typed lines (delivery, labour) are priced by hand.
    lock_document_prices = models.BooleanField(default=False)
    # The most a quotation or invoice may take off, line and whole document
    # together: 40% on a line and 40% on the total is 64% off, not 40%.
    max_document_discount = models.DecimalField(
        max_digits=5, decimal_places=2, default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)])

    cost_method = models.CharField(
        max_length=20,
        choices=CostMethod.choices,
        default=CostMethod.WEIGHTED_AVERAGE,
        help_text="Drives every margin figure. Changing it later re-bases history.",
    )
    negative_stock_allowed = models.BooleanField(
        default=True,
        help_text="Offline POS can oversell. Blocking it loses real sales.",
    )
    low_stock_alerts = models.BooleanField(default=True)
    expiry_warning_days = models.PositiveIntegerField(default=30)

    default_tax_rate = models.ForeignKey(
        "catalog.TaxRate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    fiscal_provider = models.CharField(max_length=40, blank=True)
    fiscal_config = models.JSONField(default=dict, blank=True)

    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "tenant settings"

    def __str__(self):
        return f"Settings for {self.tenant}"


class BankAccount(TenantModel):
    """
    Where a customer sends the money.

    Several, because a shop invoicing a hotel in dollars and a duka down the
    road in shillings quotes a different account for each, and the account
    printed on the invoice has to be the one that can receive what is being
    asked for. Printed on invoices and proformas, never on a receipt.
    """

    bank_name = models.CharField(max_length=120)
    branch = models.CharField(max_length=120, blank=True)
    account_name = models.CharField(max_length=160)
    account_number = models.CharField(max_length=60)
    swift_code = models.CharField("SWIFT code", max_length=20, blank=True)
    # Which money this account takes. An invoice shows the account that
    # matches what it is asking for.
    currency = models.CharField(max_length=3, default="TZS")
    is_active = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "bank_name"]

    def __str__(self):
        return f"{self.bank_name} {self.account_number} ({self.currency})"


class DeviceKind(models.TextChoices):
    TILL = "till", "POS"
    PHONE = "phone", "Phone"


class Device(TenantModel):
    """
    A till or phone bound to a branch.

    Registered once, then trusted to hold a cached catalogue and a queue of
    offline sales. ``last_sync_at`` is what the platform health page watches.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="devices")
    device_id = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=60, blank=True)
    kind = models.CharField(max_length=10, choices=DeviceKind.choices, default=DeviceKind.TILL)

    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    app_version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    # Sales waiting on the device at its last contact. A device that is both
    # silent and holding sales is money nobody can see yet.
    queued = models.PositiveIntegerField(default=0)
    # "Forgotten": off the list, but the row stays so a switched-off (lost
    # or stolen) device stays blocked. Deleting it let the same phone
    # register again as a brand-new, active till.
    hidden = models.BooleanField(default=False)

    class Meta:
        ordering = ["branch__name", "label"]

    def __str__(self):
        return self.label or self.device_id

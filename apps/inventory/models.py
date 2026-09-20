"""
Stock.

``StockMovement`` is the ledger and the only thing that ever changes a
quantity. ``StockItem`` is a cached sum of it, kept current as movements are
written and rebuildable from scratch at any time. If the two ever disagree,
the ledger is right.

Nothing here is ever edited or deleted. A correction is an opposing entry,
which is what keeps a stock figure defensible months later when an owner asks
where forty crates went.
"""

from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.models import BranchModel, LedgerModel, SyncableModel, TenantModel


class MovementReason(models.TextChoices):
    OPENING = "opening", "Opening stock"
    PURCHASE = "purchase", "Goods received"
    SALE = "sale", "Sold"
    RETURN = "return", "Returned by customer"
    SUPPLIER_RETURN = "supplier_return", "Returned to supplier"
    TRANSFER_OUT = "transfer_out", "Transferred out"
    TRANSFER_IN = "transfer_in", "Transferred in"
    ADJUSTMENT = "adjustment", "Adjusted"
    WASTAGE = "wastage", "Damaged or expired"
    COUNT = "count", "Stock count"

    # New reasons can be added without a schema change -- manufacturing,
    # consignment, samples. The ledger does not care what caused a movement,
    # only that one happened.


# Reasons a shop should not be able to fire by hand; they are consequences of
# something else happening.
SYSTEM_REASONS = {
    MovementReason.SALE,
    MovementReason.RETURN,
    MovementReason.TRANSFER_IN,
    MovementReason.TRANSFER_OUT,
    MovementReason.PURCHASE,
}


class StockMovement(LedgerModel, SyncableModel):
    """
    One signed change to one variant at one branch.

    ``source_type``/``source_id`` point back at whatever caused it -- a sale, a
    goods receipt, a transfer line -- so any quantity can be traced to a
    document without a foreign key per cause.
    """

    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.PROTECT, related_name="movements"
    )
    batch = models.ForeignKey(
        "inventory.Batch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )

    qty_delta = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    reason = models.CharField(max_length=20, choices=MovementReason.choices)
    note = models.CharField(max_length=200, blank=True)

    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=40, blank=True)

    # The running total after this movement, stamped at write time. Without it
    # a historical stock report has to replay the whole ledger.
    balance_after = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "branch", "variant", "-created_at"]),
            models.Index(fields=["tenant", "reason", "-created_at"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def __str__(self):
        return f"{self.variant} {self.qty_delta:+} ({self.get_reason_display()})"

    @property
    def value(self) -> Decimal:
        if self.unit_cost is None:
            return Decimal("0")
        return self.qty_delta * self.unit_cost


class StockItem(BranchModel):
    """
    The current quantity, cached.

    A projection of the ledger, not a second source of truth. It exists so a
    till showing a hundred products does not sum a million rows.
    """

    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.CASCADE, related_name="stock_items"
    )

    qty_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    # Held by an unfinished sale or an in-flight transfer.
    qty_reserved = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    reorder_level = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    reorder_qty = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    preferred_supplier = models.ForeignKey(
        "purchasing.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_for",
    )
    bin_location = models.CharField(max_length=40, blank=True)

    # Weighted average, recomputed on every inbound movement. Tenants using
    # last-cost get the most recent purchase price here instead.
    avg_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_counted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("branch", "variant")]
        ordering = ["variant__product__name"]
        indexes = [models.Index(fields=["tenant", "branch", "variant"])]

    def __str__(self):
        return f"{self.variant} at {self.branch}: {self.qty_on_hand}"

    @property
    def available(self) -> Decimal:
        return self.qty_on_hand - self.qty_reserved

    @property
    def is_low(self) -> bool:
        if self.reorder_level is None:
            return False
        return self.qty_on_hand <= self.reorder_level

    @property
    def stock_value(self) -> Decimal:
        return self.qty_on_hand * self.avg_cost


class Batch(TenantModel):
    """
    A delivery of a perishable thing.

    Sold first-expiry-first-out, which is the behaviour a pharmacy or a food
    shop needs and the reason expiry cannot just be a field on the product.
    """

    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.CASCADE, related_name="batches"
    )
    batch_no = models.CharField(max_length=40, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    manufacture_date = models.DateField(null=True, blank=True)
    received_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        ordering = ["expiry_date", "batch_no"]
        indexes = [models.Index(fields=["tenant", "variant", "expiry_date"])]

    def __str__(self):
        label = self.batch_no or "batch"
        return f"{label} ({self.expiry_date or 'no expiry'})"

    @property
    def is_expired(self) -> bool:
        return self.expiry_date is not None and self.expiry_date < timezone.localdate()

    def days_to_expiry(self):
        if self.expiry_date is None:
            return None
        return (self.expiry_date - timezone.localdate()).days


class TransferStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class Transfer(TenantModel):
    """
    Stock moving between two shops.

    Two-sided on purpose: it leaves when sent and arrives only when the other
    branch accepts. Goods in transit are visible to both and belong to
    neither, which is where shrinkage otherwise hides.
    """

    reference = models.CharField(max_length=30)
    from_branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, related_name="transfers_in"
    )
    status = models.CharField(
        max_length=12, choices=TransferStatus.choices, default=TransferStatus.DRAFT
    )
    note = models.CharField(max_length=200, blank=True)

    sent_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return f"{self.reference}: {self.from_branch} to {self.to_branch}"

    @property
    def is_in_transit(self) -> bool:
        return self.status == TransferStatus.SENT

    @property
    def has_discrepancy(self) -> bool:
        return any(
            line.qty_received is not None and line.qty_received != line.qty_sent
            for line in self.lines.all()
        )


class TransferLine(TenantModel):
    transfer = models.ForeignKey(Transfer, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalog.Variant", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, null=True, blank=True)
    qty_sent = models.DecimalField(max_digits=14, decimal_places=3)
    # Null until the receiving branch counts it. A difference is the record of
    # what went missing on the road.
    qty_received = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    def __str__(self):
        return f"{self.variant} x {self.qty_sent}"

    @property
    def shortfall(self):
        if self.qty_received is None:
            return None
        return self.qty_sent - self.qty_received


class CountStatus(models.TextChoices):
    OPEN = "open", "Counting"
    APPLIED = "applied", "Applied"
    CANCELLED = "cancelled", "Cancelled"


class StockCount(BranchModel):
    reference = models.CharField(max_length=30)
    status = models.CharField(
        max_length=12, choices=CountStatus.choices, default=CountStatus.OPEN
    )
    note = models.CharField(max_length=200, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    @property
    def total_variance_value(self) -> Decimal:
        return sum((line.variance_value for line in self.lines.all()), Decimal("0"))


class StockCountLine(TenantModel):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalog.Variant", on_delete=models.PROTECT)
    # What the system believed at the moment counting started.
    system_qty = models.DecimalField(max_digits=14, decimal_places=3)
    counted_qty = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [("count", "variant")]

    def __str__(self):
        return f"{self.variant}: {self.counted_qty} of {self.system_qty}"

    @property
    def variance(self):
        if self.counted_qty is None:
            return None
        return self.counted_qty - self.system_qty

    @property
    def variance_value(self) -> Decimal:
        variance = self.variance
        if variance is None:
            return Decimal("0")
        return variance * self.unit_cost

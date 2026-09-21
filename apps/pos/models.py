"""
Selling.

A sale is written once and never edited. A mistake is corrected by a void or a
return, both of which leave the original standing. That is what makes the cash
variance at the end of a shift mean something, and it is what the revenue
authority expects.
"""

from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.models import BranchModel, SyncableModel, TenantModel

# --------------------------------------------------------------------------
# Shifts
# --------------------------------------------------------------------------

class ShiftStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class Shift(BranchModel):
    """
    One person, one till, one stretch of trading.

    The variance between what the drawer should hold and what it does is the
    single number that makes an owner trust the system, because it is the
    thing they currently cannot see at all.
    """

    register = models.ForeignKey(
        "org.Register", on_delete=models.PROTECT, related_name="shifts"
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="shifts"
    )
    status = models.CharField(
        max_length=10, choices=ShiftStatus.choices, default=ShiftStatus.OPEN
    )

    opened_at = models.DateTimeField(default=timezone.now)
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    closed_at = models.DateTimeField(null=True, blank=True)
    counted_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    expected_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    variance = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    variance_note = models.CharField(max_length=200, blank=True)
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-opened_at"]
        indexes = [models.Index(fields=["tenant", "branch", "-opened_at"])]

    def __str__(self):
        return f"{self.user} on {self.register} ({self.opened_at:%d %b %H:%M})"

    @property
    def is_open(self) -> bool:
        return self.status == ShiftStatus.OPEN

    def cash_taken(self) -> Decimal:
        """
        Cash that went into the drawer this shift.

        Part-refunded sales count in full: the money was taken at the time,
        and what came back out is subtracted separately by ``cash_refunded``.
        Excluding them here would make every shift with a refund look short.

        Voided sales are excluded, because voiding hands the money back.
        """
        total = self.sales.exclude(status=SaleStatus.VOIDED).aggregate(
            total=models.Sum(
                "payments__amount",
                filter=models.Q(payments__method=PaymentMethod.CASH),
            )
        )["total"]
        return total or Decimal("0")

    def cash_refunded(self) -> Decimal:
        """Refunds paid out of this drawer. Mobile-money refunds do not count."""
        total = self.returns.aggregate(total=models.Sum("cash_amount"))["total"]
        return total or Decimal("0")

    def cash_movements_total(self) -> Decimal:
        """Already signed: pay-outs and banking are stored negative."""
        total = self.cash_movements.aggregate(total=models.Sum("amount"))["total"]
        return total or Decimal("0")

    def compute_expected_cash(self) -> Decimal:
        return (
            self.opening_float
            + self.cash_taken()
            - self.cash_refunded()
            + self.cash_movements_total()
        )


class CashMovementKind(models.TextChoices):
    PAY_IN = "pay_in", "Money put in"
    PAY_OUT = "pay_out", "Money taken out"
    DROP = "drop", "Banked"


class CashMovement(TenantModel):
    """Money in or out of the drawer that is not a sale."""

    shift = models.ForeignKey(
        Shift, on_delete=models.CASCADE, related_name="cash_movements"
    )
    kind = models.CharField(max_length=10, choices=CashMovementKind.choices)
    # Signed: a pay-out is negative, so the expected total is a plain sum.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=200)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount}"


# --------------------------------------------------------------------------
# Carts -- the basket, portable between devices
# --------------------------------------------------------------------------

class CartStatus(models.TextChoices):
    OPEN = "open", "Open"
    HELD = "held", "Held"
    CONVERTED = "converted", "Sold"
    ABANDONED = "abandoned", "Abandoned"


class Cart(BranchModel, SyncableModel):
    """
    A basket that can move between devices.

    Staff builds it on a phone next to the customer, then either takes payment
    there or hands it to the till by its short code. That portability is why
    the cart is a server object and not local state on one screen.
    """

    register = models.ForeignKey(
        "org.Register", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="carts",
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="carts"
    )
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="carts",
    )
    price_list = models.ForeignKey(
        "catalog.PriceList", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    status = models.CharField(
        max_length=12, choices=CartStatus.choices, default=CartStatus.OPEN
    )
    # Short and readable, because a cashier types it while a customer waits.
    handoff_code = models.CharField(max_length=8, blank=True, db_index=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["tenant", "branch", "status"])]

    def __str__(self):
        return self.handoff_code or f"Cart {self.pk}"

    @property
    def subtotal(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def item_count(self) -> Decimal:
        return sum((line.qty for line in self.lines.all()), Decimal("0"))


class AddedVia(models.TextChoices):
    SCAN = "scan", "Scanner"
    CAMERA = "camera", "Phone camera"
    SEARCH = "search", "Search"
    TILE = "tile", "Tile"
    PLU = "plu", "Short code"
    MANUAL = "manual", "Typed in"


class CartLine(TenantModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.PROTECT, null=True, blank=True
    )
    # Free text for an open item, which has no catalogue entry by definition.
    description = models.CharField(max_length=160, blank=True)

    qty = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # How it reached the basket. One column, and it tells an owner exactly how
    # much of their turnover went through a typed price.
    added_via = models.CharField(
        max_length=10, choices=AddedVia.choices, default=AddedVia.SEARCH
    )
    note = models.CharField(max_length=120, blank=True)
    # Set when an open item from a phone basket is sold at a till, so the
    # same approved line cannot be sold again by somebody without the right.
    sold_in = models.ForeignKey(
        "pos.Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.label} x {self.qty}"

    @property
    def label(self) -> str:
        return self.description or str(self.variant)

    @property
    def line_total(self) -> Decimal:
        return (self.qty * self.unit_price) - self.discount


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------

class SaleStatus(models.TextChoices):
    COMPLETED = "completed", "Completed"
    VOIDED = "voided", "Voided"
    REFUNDED = "refunded", "Refunded"
    PART_REFUNDED = "part_refunded", "Partly refunded"


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    MPESA = "mpesa", "M-Pesa"
    TIGOPESA = "tigopesa", "Tigo Pesa"
    AIRTELMONEY = "airtelmoney", "Airtel Money"
    CARD = "card", "Card"
    CREDIT = "credit", "On account"
    VOUCHER = "voucher", "Voucher"


class Sale(BranchModel, SyncableModel):
    """
    A completed transaction. Written once, never edited.

    ``client_uuid`` comes from the device, so a retried offline sync stores it
    once however many times it is sent.
    """

    number = models.CharField(max_length=30, db_index=True)
    register = models.ForeignKey(
        "org.Register", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales",
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales"
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="sales"
    )
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales",
    )
    # Somebody who is not on the books. Most sales over a counter are to a
    # person the shop will never see again, but a few need a name on them: a
    # delivery, something put aside, a thing that may come back. Neither is
    # required, and typing one does not add anybody to the customer list.
    buyer_name = models.CharField(max_length=80, blank=True)
    buyer_phone = models.CharField(max_length=30, blank=True)

    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    status = models.CharField(
        max_length=14, choices=SaleStatus.choices, default=SaleStatus.COMPLETED
    )
    sold_at = models.DateTimeField(default=timezone.now, db_index=True)

    # Who did it, and who approved it if it needed approval.
    authorised_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    void_reason = models.CharField(max_length=200, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    # Recorded because the money was taken, but something about it did not
    # match what the server allows -- a price below today's, say. The owner
    # decides; nothing is silently rewritten.
    needs_review = models.BooleanField(default=False, db_index=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-sold_at"]
        unique_together = [("tenant", "number")]
        indexes = [
            models.Index(fields=["tenant", "branch", "-sold_at"]),
            models.Index(fields=["tenant", "status", "-sold_at"]),
            models.Index(fields=["tenant", "-sold_at"]),
        ]

    def __str__(self):
        return self.number

    @property
    def paid(self) -> Decimal:
        return self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    @property
    def cost_total(self) -> Decimal:
        """From the snapshots on the lines, never from today's cost price."""
        return sum((line.cost_total for line in self.lines.all()), Decimal("0"))

    @property
    def margin(self) -> Decimal:
        return self.total - self.tax_total - self.cost_total

    @property
    def refunded_total(self) -> Decimal:
        return sum((r.total for r in self.returns.all()), Decimal("0"))


class SaleLine(TenantModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.PROTECT, null=True, blank=True
    )
    batch = models.ForeignKey(
        "inventory.Batch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # The name as it was sold, so a later product rename does not rewrite
    # history or an old receipt.
    description = models.CharField(max_length=160)

    qty = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    # Snapshot taken at the moment of sale. Margin reports must never be
    # recomputed against today's cost, or last year's profit changes every
    # time a supplier raises a price.
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    added_via = models.CharField(
        max_length=10, choices=AddedVia.choices, default=AddedVia.SEARCH
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} x {self.qty}"

    @property
    def cost_total(self) -> Decimal:
        return self.qty * self.unit_cost

    @property
    def qty_returned(self) -> Decimal:
        return sum((rl.qty for rl in self.return_lines.all()), Decimal("0"))

    @property
    def returnable(self) -> Decimal:
        """How much of this line can still come back."""
        return max(self.qty - self.qty_returned, Decimal("0"))

    @property
    def unit_refund(self) -> Decimal:
        """What one unit refunds, discount included -- for the refund preview."""
        return (self.line_total / self.qty).quantize(Decimal("0.01")) if self.qty else Decimal("0")


class SalePayment(TenantModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=14, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=60, blank=True)
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount}"


class Return(BranchModel, SyncableModel):
    """A refund. The original sale stays exactly as it was."""

    number = models.CharField(max_length=30, db_index=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="returns"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="returns"
    )
    authorised_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    reason = models.CharField(max_length=200)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # How the money went back, split by where it came from: cash out of the
    # drawer, debt taken off the customer's account, mobile money or card.
    # A split sale refunded "in cash" used to pay all of it from the drawer
    # while the customer still owed the credit part.
    cash_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    method = models.CharField(
        max_length=14, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    restock = models.BooleanField(
        default=True, help_text="Damaged goods come back without re-entering stock."
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "number")]

    def __str__(self):
        return self.number


class ReturnLine(TenantModel):
    return_doc = models.ForeignKey(
        Return, on_delete=models.CASCADE, related_name="lines"
    )
    sale_line = models.ForeignKey(
        SaleLine, on_delete=models.PROTECT, related_name="return_lines"
    )
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class FiscalStatus(models.TextChoices):
    PENDING = "pending", "Waiting to send"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    NOT_REQUIRED = "not_required", "Not required"


class FiscalReceipt(TenantModel):
    """
    The revenue authority's copy.

    Cannot be submitted offline, so it queues. The customer's paper receipt
    prints immediately and the fiscal copy follows when the line comes back.
    """

    sale = models.OneToOneField(
        Sale, on_delete=models.CASCADE, related_name="fiscal_receipt"
    )
    provider = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=14, choices=FiscalStatus.choices, default=FiscalStatus.PENDING
    )
    receipt_no = models.CharField(max_length=60, blank=True)
    verification_code = models.CharField(max_length=120, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.sale.number}: {self.get_status_display()}"

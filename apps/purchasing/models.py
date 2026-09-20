"""
Buying.

Goods received is where stock rises and cost price is set, which makes it the
half that decides every margin figure downstream. It works with or without a
purchase order, because a lot of stock in these shops arrives at the back door
with no paperwork at all.
"""

from decimal import Decimal

from django.db import models

from apps.core.models import BranchModel, TenantModel


class Supplier(TenantModel):
    name = models.CharField(max_length=120)
    contact_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    tin = models.CharField("TIN", max_length=30, blank=True)
    payment_terms_days = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return self.name

    @property
    def balance(self) -> Decimal:
        """What the shop still owes this supplier."""
        invoiced = self.invoices.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")
        paid = self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")
        return invoiced - paid


class POStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent to supplier"
    PARTIAL = "partial", "Partly received"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class PurchaseOrder(BranchModel):
    reference = models.CharField(max_length=30)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=12, choices=POStatus.choices, default=POStatus.DRAFT)
    expected_date = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True)

    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def is_fully_received(self) -> bool:
        return all(line.qty_outstanding <= 0 for line in self.lines.all())

    def refresh_status(self):
        if self.status in {POStatus.CANCELLED, POStatus.DRAFT}:
            return
        received_any = any(line.qty_received > 0 for line in self.lines.all())
        if self.is_fully_received:
            self.status = POStatus.RECEIVED
        elif received_any:
            self.status = POStatus.PARTIAL
        self.save(update_fields=["status", "updated_at"])


class PurchaseOrderLine(TenantModel):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalog.Variant", on_delete=models.PROTECT)
    qty_ordered = models.DecimalField(max_digits=14, decimal_places=3)
    qty_received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.variant} x {self.qty_ordered}"

    @property
    def line_total(self) -> Decimal:
        return self.qty_ordered * self.unit_cost

    @property
    def qty_outstanding(self) -> Decimal:
        return self.qty_ordered - self.qty_received


class GoodsReceipt(BranchModel):
    """
    Stock arriving.

    The purchase order is optional. Requiring one would mean half the
    deliveries these shops actually take could not be recorded at all.
    """

    reference = models.CharField(max_length=30)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="receipts")
    order = models.ForeignKey(
        PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="receipts",
    )
    received_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    received_at = models.DateTimeField(auto_now_add=True)
    supplier_note = models.CharField(max_length=60, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))


class GoodsReceiptLine(TenantModel):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalog.Variant", on_delete=models.PROTECT)
    order_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)

    batch_no = models.CharField(max_length=40, blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.variant} x {self.qty}"

    @property
    def line_total(self) -> Decimal:
        return self.qty * self.unit_cost


class SupplierInvoice(TenantModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoices")
    receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    number = models.CharField(max_length=40)
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-invoice_date"]
        unique_together = [("tenant", "supplier", "number")]

    def __str__(self):
        return f"{self.supplier} {self.number}"

    @property
    def paid(self) -> Decimal:
        return self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.paid


class SupplierPayment(TenantModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(
        SupplierInvoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=20, default="cash")
    reference = models.CharField(max_length=60, blank=True)
    paid_at = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    # One payment spread over several bills is several rows sharing this;
    # taking it back takes back all of them.
    batch = models.UUIDField(null=True, blank=True, db_index=True)
    # The till pay-out, when the supplier was paid from a drawer.
    cash_movement = models.ForeignKey(
        "pos.CashMovement", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-paid_at"]

    def __str__(self):
        return f"{self.supplier}: {self.amount}"

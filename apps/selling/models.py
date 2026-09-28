"""
Selling to a business, which is paperwork before it is money.

The till is the other half of this shop: scan, take the money, print a
receipt, done. A customer who is itself a business asks first what it would
cost, then wants an invoice to pay against, then wants a note signed when the
goods arrive. Three documents, in that order, and days or weeks between them.

    Quotation  -- what it would cost. No stock, no money, no debt.
       |  accepted
    Invoice    -- what the customer owes, and by when.
       |  goods go out, in one trip or several
    Delivery   -- what was handed over, and who signed for it.

Where the stock leaves and where the sale lands is the decision that shapes
all of this: it happens at *delivery*. An invoice raised on Monday for goods
that go out on Friday is money owed on Monday and a sale on Friday, and the
stock is right on both days.

`Invoice` here is the shop's invoice to its customer. `tenancy.Invoice` is a
different thing entirely -- the platform's monthly bill to the shop.
"""

from decimal import Decimal

from django.db import models

from apps.core.models import BranchModel, TenantModel


class QuotationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"


class Quotation(BranchModel):
    """
    An offer, with a date it stops being one.

    It is deliberately inert: nothing here touches stock, the customer's
    balance or the day's takings. A shop that quotes ten jobs and wins two
    should see two sales, not ten.
    """

    reference = models.CharField(max_length=30)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="quotations"
    )
    status = models.CharField(
        max_length=10, choices=QuotationStatus.choices, default=QuotationStatus.DRAFT
    )
    issued_on = models.DateField()
    valid_until = models.DateField()
    note = models.TextField(blank=True, help_text="Printed under the lines.")

    sent_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    # -- money ------------------------------------------------------------
    # Worked out from the lines rather than stored: a quotation is edited
    # until it is sent, and a stored total is a total that goes stale.

    @property
    def net(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def tax(self) -> Decimal:
        return sum((line.tax_amount for line in self.lines.all()), Decimal("0"))

    @property
    def total(self) -> Decimal:
        """Prices include VAT, so the total is the sum of the lines."""
        return self.net

    @property
    def is_open(self) -> bool:
        return self.status in {QuotationStatus.DRAFT, QuotationStatus.SENT}

    def has_expired(self, today) -> bool:
        return self.is_open and self.valid_until < today

    @property
    def live_invoice(self):
        """The invoice this offer became, unless that invoice was cancelled."""
        return next((inv for inv in self.invoices.all() if inv.status != "void"), None)


class QuotationLine(TenantModel):
    """
    A line on an offer.

    The price is copied, never looked up later: a quotation that quietly
    reprices itself when the shop changes a shelf price is not an offer.
    The description is copied too, so a renamed product does not rewrite
    paperwork the customer is holding.
    """

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.PROTECT, null=True, blank=True
    )
    description = models.CharField(max_length=200)
    qty = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.description} x {self.qty}"

    @property
    def line_total(self) -> Decimal:
        from apps.pos.services import money

        return money(self.qty * self.unit_price)

    @property
    def tax_amount(self) -> Decimal:
        """VAT inside the line, the same way the till works it out."""
        from apps.pos.services import ZERO, _tax_for

        if not self.tax_rate:
            return ZERO
        return _tax_for(self)

    @property
    def discount(self) -> Decimal:
        """A quotation has no per-line discount; `_tax_for` expects one."""
        return Decimal("0")


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Owed"
    PART_PAID = "part_paid", "Part paid"
    PAID = "paid", "Paid"
    VOID = "void", "Cancelled"


class Invoice(BranchModel):
    """
    What the customer owes, and by when.

    Issuing it is what creates the debt -- one charge on the customer's own
    account, the same account the till uses for credit sales, so a business
    that both buys over the counter and takes delivery on account has one
    balance and one statement rather than two halves that have to be added up
    by hand.

    It does not move stock. Goods leave on a delivery note, and that is where
    the sale lands in the reports.
    """

    reference = models.CharField(max_length=30)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="invoices"
    )
    quotation = models.ForeignKey(
        Quotation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invoices",
    )
    status = models.CharField(
        max_length=10, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT
    )
    issued_on = models.DateField()
    due_on = models.DateField()
    note = models.TextField(blank=True)

    issued_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    @property
    def net(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def tax(self) -> Decimal:
        return sum((line.tax_amount for line in self.lines.all()), Decimal("0"))

    @property
    def total(self) -> Decimal:
        """Prices include VAT, so the total is the sum of the lines."""
        return self.net

    @property
    def paid(self) -> Decimal:
        """
        Taken from the customer's account rather than counted here.

        Money has one home. A second running total on the invoice is a second
        number to go wrong, and the one on the statement is the one a
        customer will quote back down the phone.
        """
        from apps.customers.models import CreditKind

        # Read through .all() so a list page's prefetch is used, not a query
        # per invoice. A payment undone from the customer's page leaves an
        # "undo:" adjustment against the same invoice; it puts the money back.
        total = Decimal("0")
        for row in self.credit_entries.all():
            undone = row.kind == CreditKind.ADJUSTMENT and row.reference.startswith("undo:")
            if row.kind == CreditKind.PAYMENT or undone:
                total -= row.amount
        return total

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid

    @property
    def is_owed(self) -> bool:
        return self.status in {InvoiceStatus.OPEN, InvoiceStatus.PART_PAID}

    def is_overdue(self, today) -> bool:
        return self.is_owed and self.due_on < today

    @property
    def can_deliver(self) -> bool:
        """
        Whether goods may still go out against this invoice.

        Deliberately says nothing about payment. A hotel that pays the whole
        thing up front still has to be sent its order, and hiding the button
        the moment the money landed left the goods stuck in the store with no
        way to record them leaving.
        """
        return (self.status not in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}
                and not self.delivered_everything)

    @property
    def delivered_everything(self) -> bool:
        return all(line.qty_outstanding <= 0 for line in self.lines.all())

    @property
    def has_deliveries(self) -> bool:
        return any(note.voided_at is None for note in self.deliveries.all())


class InvoiceLine(TenantModel):
    """One line. Priced when the invoice was written, never looked up again."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey(
        "catalog.Variant", on_delete=models.PROTECT, null=True, blank=True
    )
    description = models.CharField(max_length=200)
    qty = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.description} x {self.qty}"

    @property
    def line_total(self) -> Decimal:
        from apps.pos.services import money

        return money(self.qty * self.unit_price)

    @property
    def tax_amount(self) -> Decimal:
        from apps.pos.services import ZERO, _tax_for

        if not self.tax_rate:
            return ZERO
        return _tax_for(self)

    @property
    def discount(self) -> Decimal:
        return Decimal("0")

    @property
    def qty_delivered(self) -> Decimal:
        """Across every delivery note that has not been cancelled."""
        rows = self.deliveries.select_related("note")
        return sum((row.qty for row in rows if row.note.voided_at is None), Decimal("0"))

    @property
    def qty_outstanding(self) -> Decimal:
        return self.qty - self.qty_delivered


class DeliveryNote(BranchModel):
    """
    What went out on this trip, and who signed for it.

    This is where the stock leaves and where the sale lands. An invoice for
    goods that have not moved is money owed, not a sale -- and a lorry that
    takes half the order today and half on Friday writes two of these.
    """

    reference = models.CharField(max_length=30)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="deliveries"
    )
    delivered_on = models.DateField()
    received_by = models.CharField(
        max_length=120, blank=True, help_text="Who took it, if you know now."
    )
    note = models.TextField(blank=True)
    sale = models.OneToOneField(
        "pos.Sale", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_note",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("tenant", "reference")]

    def __str__(self):
        return self.reference

    @property
    def customer(self):
        return self.invoice.customer

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))


class DeliveryLine(TenantModel):
    note = models.ForeignKey(DeliveryNote, on_delete=models.CASCADE, related_name="lines")
    invoice_line = models.ForeignKey(
        InvoiceLine, on_delete=models.PROTECT, related_name="deliveries"
    )
    qty = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        ordering = ["invoice_line__position", "id"]

    def __str__(self):
        return f"{self.invoice_line.description} x {self.qty}"

    @property
    def line_total(self) -> Decimal:
        from apps.pos.services import money

        return money(self.qty * self.invoice_line.unit_price)

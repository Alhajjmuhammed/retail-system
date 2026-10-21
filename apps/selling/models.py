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
    # Per cent off the whole thing, on top of anything taken off a line --
    # the "and five off the lot" that closes an order.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # The currency it is written in, and what one unit of it was worth in
    # the shop's own on the day -- frozen, so a rate changed tomorrow does
    # not rewrite an offer already in the customer's hands.
    currency = models.CharField(max_length=3, blank=True)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, default=1)
    # Whether its prices are before VAT, with VAT added on top. Taken from
    # the shop's setting when the document is started and kept: switching
    # the setting later must not change what an issued invoice comes to.
    # Empty on documents from before the setting -- those included VAT.
    vat_on_top = models.BooleanField(null=True, default=None)

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

    # -- currency ----------------------------------------------------------

    @property
    def shop(self):
        """
        The document's shop, without a query when it is the one the request
        is already about -- a list of invoices read it once per row.
        """
        from apps.core.context import get_current_tenant

        current = get_current_tenant()
        if current is not None and current.pk == self.tenant_id:
            return current
        return self.tenant

    @property
    def doc_currency(self) -> str:
        """What it is written in; a document from before currencies is the shop's."""
        from apps.core.currency import main_currency

        return (self.currency or main_currency(self.shop)).upper()

    @property
    def main_currency(self) -> str:
        from apps.core.currency import main_currency

        return main_currency(self.shop)

    @property
    def is_foreign(self) -> bool:
        return self.doc_currency != self.main_currency

    @property
    def money_step(self) -> str:
        """The step for an amount box: to the cent, in every currency."""
        return "0.01"

    @property
    def money_places(self) -> int:
        """
        Two decimals, shillings included. Paperwork is not cash: 10 at 3.65
        is 36.50, and an invoice that printed 37 was simply wrong. Only the
        till rounds to a coin somebody can hand over.
        """
        return 2

    @property
    def adds_vat(self) -> bool:
        """Prices before VAT, VAT added on top."""
        return bool(self.vat_on_top)

    def save(self, *args, **kwargs):
        if self._state.adding and self.vat_on_top is None:
            from apps.selling.services import paperwork_rules

            self.vat_on_top = not paperwork_rules().prices_include_tax
        super().save(*args, **kwargs)

    def to_main(self, amount) -> Decimal:
        """An amount on this document, in the shop's own currency."""
        from apps.core.currency import round_to

        if not self.is_foreign:
            return amount
        # To the cent: the paperwork keeps its cents in every currency.
        return round_to(Decimal(str(amount)) * Decimal(self.exchange_rate), "USD")

    @property
    def total_main(self) -> Decimal:
        """The total in the shop's own currency, at the frozen rate."""
        return self.to_main(self.total)

    @property
    def rate_label(self) -> str:
        from apps.core.currency import describe_rate

        return describe_rate(self.doc_currency, self.exchange_rate, self.shop)

    def _money(self, amount) -> Decimal:
        """To the cent, whatever the currency (see money_places)."""
        from decimal import ROUND_HALF_UP

        return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def before_discount(self) -> Decimal:
        """Every line at full price: what the customer would have paid."""
        return sum((line.gross_total for line in self.lines.all()), Decimal("0"))

    @property
    def discount_total(self) -> Decimal:
        """All that came off, line discounts and the whole-document one."""
        return self.before_discount - self.total

    @property
    def lines_total(self) -> Decimal:
        """The lines added up, after any discount on a line of its own."""
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def order_discount(self) -> Decimal:
        """Money off the whole document, on top of anything off a line."""
        if not self.discount_percent:
            return Decimal("0")
        return self._money(self.lines_total * self.discount_percent / Decimal("100"))

    @property
    def net(self) -> Decimal:
        return self.lines_total - self.order_discount

    def total_in_words(self, currency=None) -> str:
        """The total written out, for the line under it on the document."""
        from apps.core.amounts import money_in_words

        return money_in_words(self.total, currency or self.doc_currency)

    @property
    def net_of_tax(self) -> Decimal:
        """
        The goods without the VAT in them.

        Prices here include VAT, so the total is what the customer pays. A
        document that prints the same figure as Subtotal and again as Total,
        with a VAT line between them, reads as though the tax were added on
        top and the arithmetic were wrong. Subtotal + VAT = Total.
        """
        return self.total - self.tax

    @property
    def tax(self) -> Decimal:
        """
        VAT inside what is actually charged.

        A discount on the whole document lowers every line's share of it, so
        the tax is scaled by the same proportion. Left unscaled it would
        declare tax on money the customer never paid.
        """
        from decimal import ROUND_HALF_UP

        from apps.pos.services import MONEY

        raw = sum((line.tax_amount for line in self.lines.all()), Decimal("0"))
        gross = self.lines_total
        if not self.discount_percent or gross <= 0:
            return raw
        return (raw * self.net / gross).quantize(MONEY, rounding=ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        """
        What the customer pays. With prices that include VAT, the sum of the
        lines; with VAT on top, the lines and then the VAT.
        """
        if self.adds_vat:
            return self._money(self.net + self.tax)
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
    # Per cent off this one line: "ten off the cement, nothing off the rest".
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.description} x {self.qty}"

    @property
    def document(self):
        return self.quotation if hasattr(self, "quotation_id") else self.invoice

    @property
    def gross_total(self) -> Decimal:
        """What the line came to before anything was taken off it."""
        return self.document._money(self.qty * self.unit_price)

    @property
    def item_code(self) -> str:
        """
        The code that prints in the Item column.

        The shop's own code first, because that is the one it controls and
        quotes down the phone. A product it never gave a code to prints the
        factory's, so the column is not blank on half the lines.
        """
        product = getattr(self.variant, "product", None)
        if product is None:
            return ""
        return product.sku or product.factory_code or ""

    @property
    def discount(self) -> Decimal:
        """
        Money off this line. `_tax_for` takes it off before working out the
        VAT, which is what a discounted price means: there is less tax
        inside it.
        """
        if not self.discount_percent:
            return Decimal("0")
        return self.document._money(
            self.qty * self.unit_price * self.discount_percent / Decimal("100"))

    @property
    def line_total(self) -> Decimal:
        # The same expression `_tax_for` works from, so the tax can never
        # disagree with the figure it was taken out of.
        return self.document._money((self.qty * self.unit_price) - self.discount)

    @property
    def tax_amount(self) -> Decimal:
        """VAT inside the line, the same way the till works it out."""
        from apps.pos.services import ZERO, _tax_for

        if not self.tax_rate:
            return ZERO
        if self.document.adds_vat:
            # On top of the line, not inside it: 18% on 100 is 18.
            from decimal import ROUND_HALF_UP

            return (self.line_total * Decimal(str(self.tax_rate)) / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _tax_for(self, self.document.doc_currency)


class InvoiceStatus(models.TextChoices):
    # The stored value stays "draft" -- it is what every existing row holds.
    # What a person sees is "Proforma", because that is the document: it
    # prints as one, carries a PRO number, and nobody owes anything on it.
    DRAFT = "draft", "Proforma"
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

    Whether issuing it also moves stock is the shop's choice: by default the
    goods leave at once, on a delivery note made for everything on it, and
    the sale lands in the reports then. Switched off, they leave only when
    somebody writes a delivery note.
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
    # Per cent off the whole invoice, on top of anything off a line.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # The currency it is written in, and what one unit of it was worth in
    # the shop's own on the day -- frozen, so a rate changed tomorrow does
    # not rewrite an offer already in the customer's hands.
    currency = models.CharField(max_length=3, blank=True)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, default=1)
    # Whether its prices are before VAT, with VAT added on top. Taken from
    # the shop's setting when the document is started and kept: switching
    # the setting later must not change what an issued invoice comes to.
    # Empty on documents from before the setting -- those included VAT.
    vat_on_top = models.BooleanField(null=True, default=None)
    # The number it carried as a proforma. Kept after issuing, so the
    # sequence never hands the same proforma number to two customers.
    proforma_reference = models.CharField(max_length=30, blank=True, db_index=True)

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

    # -- currency ----------------------------------------------------------

    @property
    def shop(self):
        """
        The document's shop, without a query when it is the one the request
        is already about -- a list of invoices read it once per row.
        """
        from apps.core.context import get_current_tenant

        current = get_current_tenant()
        if current is not None and current.pk == self.tenant_id:
            return current
        return self.tenant

    @property
    def doc_currency(self) -> str:
        """What it is written in; a document from before currencies is the shop's."""
        from apps.core.currency import main_currency

        return (self.currency or main_currency(self.shop)).upper()

    @property
    def main_currency(self) -> str:
        from apps.core.currency import main_currency

        return main_currency(self.shop)

    @property
    def is_foreign(self) -> bool:
        return self.doc_currency != self.main_currency

    @property
    def money_step(self) -> str:
        """The step for an amount box: to the cent, in every currency."""
        return "0.01"

    @property
    def money_places(self) -> int:
        """
        Two decimals, shillings included. Paperwork is not cash: 10 at 3.65
        is 36.50, and an invoice that printed 37 was simply wrong. Only the
        till rounds to a coin somebody can hand over.
        """
        return 2

    @property
    def adds_vat(self) -> bool:
        """Prices before VAT, VAT added on top."""
        return bool(self.vat_on_top)

    def save(self, *args, **kwargs):
        if self._state.adding and self.vat_on_top is None:
            from apps.selling.services import paperwork_rules

            self.vat_on_top = not paperwork_rules().prices_include_tax
        super().save(*args, **kwargs)

    def to_main(self, amount) -> Decimal:
        """An amount on this document, in the shop's own currency."""
        from apps.core.currency import round_to

        if not self.is_foreign:
            return amount
        # To the cent: the paperwork keeps its cents in every currency.
        return round_to(Decimal(str(amount)) * Decimal(self.exchange_rate), "USD")

    @property
    def total_main(self) -> Decimal:
        """The total in the shop's own currency, at the frozen rate."""
        return self.to_main(self.total)

    @property
    def rate_label(self) -> str:
        from apps.core.currency import describe_rate

        return describe_rate(self.doc_currency, self.exchange_rate, self.shop)

    def _money(self, amount) -> Decimal:
        """To the cent, whatever the currency (see money_places)."""
        from decimal import ROUND_HALF_UP

        return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def before_discount(self) -> Decimal:
        """Every line at full price: what the customer would have paid."""
        return sum((line.gross_total for line in self.lines.all()), Decimal("0"))

    @property
    def discount_total(self) -> Decimal:
        """All that came off, line discounts and the whole-document one."""
        return self.before_discount - self.total

    @property
    def lines_total(self) -> Decimal:
        """The lines added up, after any discount on a line of its own."""
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))

    @property
    def order_discount(self) -> Decimal:
        """Money off the whole document, on top of anything off a line."""
        if not self.discount_percent:
            return Decimal("0")
        return self._money(self.lines_total * self.discount_percent / Decimal("100"))

    @property
    def net(self) -> Decimal:
        return self.lines_total - self.order_discount

    def total_in_words(self, currency=None) -> str:
        """The total written out, for the line under it on the document."""
        from apps.core.amounts import money_in_words

        return money_in_words(self.total, currency or self.doc_currency)

    @property
    def net_of_tax(self) -> Decimal:
        """
        The goods without the VAT in them.

        Prices here include VAT, so the total is what the customer pays. A
        document that prints the same figure as Subtotal and again as Total,
        with a VAT line between them, reads as though the tax were added on
        top and the arithmetic were wrong. Subtotal + VAT = Total.
        """
        return self.total - self.tax

    @property
    def tax(self) -> Decimal:
        """
        VAT inside what is actually charged.

        A discount on the whole document lowers every line's share of it, so
        the tax is scaled by the same proportion. Left unscaled it would
        declare tax on money the customer never paid.
        """
        from decimal import ROUND_HALF_UP

        from apps.pos.services import MONEY

        raw = sum((line.tax_amount for line in self.lines.all()), Decimal("0"))
        gross = self.lines_total
        if not self.discount_percent or gross <= 0:
            return raw
        return (raw * self.net / gross).quantize(MONEY, rounding=ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        """
        What the customer pays. With prices that include VAT, the sum of the
        lines; with VAT on top, the lines and then the VAT.
        """
        if self.adds_vat:
            return self._money(self.net + self.tax)
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
        # The account is kept in the shop's own currency; each entry against
        # an invoice also carries what it was in the invoice's, so a dollar
        # invoice is paid down in dollars.
        total = Decimal("0")
        for row in self.credit_entries.all():
            undone = row.kind == CreditKind.ADJUSTMENT and row.reference.startswith("undo:")
            if row.kind == CreditKind.PAYMENT or undone:
                total -= row.doc_amount if row.doc_amount is not None else row.amount
        return total

    @property
    def paid_main(self) -> Decimal:
        """Paid, in the shop's own currency: what came off the account."""
        from apps.customers.models import CreditKind

        total = Decimal("0")
        for row in self.credit_entries.all():
            undone = row.kind == CreditKind.ADJUSTMENT and row.reference.startswith("undo:")
            if row.kind == CreditKind.PAYMENT or undone:
                total -= row.amount
        return total

    @property
    def charged_main(self) -> Decimal:
        """What issuing it put on the account, in the shop's own currency."""
        from apps.customers.models import CreditKind

        for row in self.credit_entries.all():
            if row.kind == CreditKind.CHARGE:
                return row.amount
        return self.total_main

    @property
    def balance_main(self) -> Decimal:
        return self.charged_main - self.paid_main

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
    # Per cent off this one line: "ten off the cement, nothing off the rest".
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.description} x {self.qty}"

    @property
    def document(self):
        return self.quotation if hasattr(self, "quotation_id") else self.invoice

    @property
    def gross_total(self) -> Decimal:
        """What the line came to before anything was taken off it."""
        return self.document._money(self.qty * self.unit_price)

    @property
    def item_code(self) -> str:
        """
        The code that prints in the Item column.

        The shop's own code first, because that is the one it controls and
        quotes down the phone. A product it never gave a code to prints the
        factory's, so the column is not blank on half the lines.
        """
        product = getattr(self.variant, "product", None)
        if product is None:
            return ""
        return product.sku or product.factory_code or ""

    @property
    def discount(self) -> Decimal:
        """Money off this line, taken off before the VAT is worked out."""
        if not self.discount_percent:
            return Decimal("0")
        return self.document._money(
            self.qty * self.unit_price * self.discount_percent / Decimal("100"))

    @property
    def line_total(self) -> Decimal:
        return self.document._money((self.qty * self.unit_price) - self.discount)

    @property
    def tax_amount(self) -> Decimal:
        from apps.pos.services import ZERO, _tax_for

        if not self.tax_rate:
            return ZERO
        if self.document.adds_vat:
            # On top of the line, not inside it: 18% on 100 is 18.
            from decimal import ROUND_HALF_UP

            return (self.line_total * Decimal(str(self.tax_rate)) / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _tax_for(self, self.document.doc_currency)

    @property
    def charged_share(self) -> Decimal:
        """
        The fraction of the gross price this line is actually charged at,
        after the discount on the line and the one on the whole invoice.

        The delivery works from this. A sale posted at the undiscounted
        price would put money in the day's figures that the customer was
        never asked for, and declare VAT on it.
        """
        one = Decimal("1")
        hundred = Decimal("100")
        off_line = one - (self.discount_percent / hundred)
        off_all = one - (self.invoice.discount_percent / hundred)
        return off_line * off_all

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
        """This delivery's share of what the invoice actually charges."""
        source = self.invoice_line
        share = source.charged_share
        if source.invoice.adds_vat:
            # The customer pays the VAT on top, so the goods leaving are
            # worth that much more.
            share *= 1 + Decimal(str(source.tax_rate)) / Decimal("100")
        return source.invoice._money(self.qty * source.unit_price * share)

    @property
    def line_total_main(self) -> Decimal:
        """The same, in the shop's own currency: what the sale records."""
        return self.invoice_line.invoice.to_main(self.line_total)

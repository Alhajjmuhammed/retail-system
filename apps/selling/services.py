"""
What a quotation does, as distinct from what it is.

Kept out of the views because two of these -- expiring and accepting -- will
be called by the nightly job and by the invoice side as well, and a rule that
lives in a view is a rule the rest of the system cannot reach.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.context import get_current_user
from apps.selling.models import Quotation, QuotationStatus


def next_reference(model, prefix: str) -> str:
    """
    A number a shop can read down the phone: QUO260001.

    Per tenant and per year, longest-first so the ten-thousandth document
    does not sort below the ninth and start the count again.
    """
    from django.db.models.functions import Length

    year = timezone.localdate().year
    stem = f"{prefix}{year % 100:02d}"
    last = (
        model.objects.filter(reference__startswith=stem)
        .order_by(Length("reference").desc(), "-reference")
        .values_list("reference", flat=True)
        .first()
    )
    try:
        counter = int(last[len(stem):]) + 1 if last else 1
    except (TypeError, ValueError):
        counter = model.objects.filter(reference__startswith=stem).count() + 1
    return f"{stem}{counter:04d}"


@transaction.atomic
def send(quotation):
    """Mark it as given to the customer. The date it was sent is the date
    the shop will be asked about later."""
    quotation.status = QuotationStatus.SENT
    quotation.sent_at = timezone.now()
    quotation.save(update_fields=["status", "sent_at", "updated_at"])
    return quotation


@transaction.atomic
def decide(quotation, *, accepted, user=None):
    """
    The customer's answer. Only a quotation that went out can be answered:
    reviving an expired or declined offer at its old price is a new offer,
    and that is what Copy is for.
    """
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    if quotation.status != QuotationStatus.SENT:
        raise ValueError(
            f"{quotation.reference} is {quotation.get_status_display().lower()}. "
            "Only a quotation that has been sent can be accepted or declined."
        )
    quotation.status = (
        QuotationStatus.ACCEPTED if accepted else QuotationStatus.DECLINED
    )
    quotation.decided_at = timezone.now()
    quotation.decided_by = user or get_current_user()
    quotation.save(update_fields=["status", "decided_at", "decided_by", "updated_at"])
    return quotation


def expire_overdue(today=None):
    """
    Quotations whose date has passed.

    Run nightly. A price held open for ever is a price the shop has stopped
    choosing -- and the customer who rings in March about a January quote
    should be told it has lapsed, not quietly given January's price.
    """
    today = today or timezone.localdate()
    return Quotation.objects.filter(
        status__in=[QuotationStatus.DRAFT, QuotationStatus.SENT],
        valid_until__lt=today,
    ).update(status=QuotationStatus.EXPIRED, updated_at=timezone.now())


def copy_for_new(quotation):
    """
    The same offer again, with today's dates.

    Shops quote the same job repeatedly; retyping fifteen lines is how a
    price gets mistyped.
    """
    from datetime import timedelta

    from apps.core.numbering import save_with_number

    today = timezone.localdate()
    fresh = save_with_number(
        Quotation(
            branch=quotation.branch,
            customer=quotation.customer,
            issued_on=today,
            valid_until=today + timedelta(
                days=(quotation.valid_until - quotation.issued_on).days or 14),
            note=quotation.note,
        ),
        field="reference",
        generate=lambda: next_reference(Quotation, "QUO"),
    )
    for line in quotation.lines.all():
        fresh.lines.create(
            variant=line.variant, description=line.description, qty=line.qty,
            unit_price=line.unit_price, tax_rate=line.tax_rate, position=line.position,
        )
    return fresh


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

@transaction.atomic
def invoice_from_quotation(quotation, *, terms_days=14):
    """
    The offer, become a bill.

    The lines are copied rather than shared: an invoice is what the customer
    agreed to, and editing the quotation afterwards must not rewrite it.

    Only an accepted quotation, and only once: a second invoice from the same
    offer is the same order billed twice.
    """
    from datetime import timedelta

    from apps.core.numbering import save_with_number
    from apps.selling.models import Invoice, InvoiceStatus

    # Locked, so two clicks on "Make an invoice" cannot both find no invoice.
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    if quotation.status != QuotationStatus.ACCEPTED:
        raise ValueError(
            f"{quotation.reference} is {quotation.get_status_display().lower()}. "
            "Only an accepted quotation becomes an invoice."
        )
    existing = quotation.invoices.exclude(status=InvoiceStatus.VOID).first()
    if existing is not None:
        raise ValueError(f"{quotation.reference} is already invoiced as {existing.reference}.")

    today = timezone.localdate()
    invoice = save_with_number(
        Invoice(
            branch=quotation.branch,
            customer=quotation.customer,
            quotation=quotation,
            issued_on=today,
            due_on=today + timedelta(days=terms_days),
            note=quotation.note,
        ),
        field="reference",
        generate=lambda: next_reference(Invoice, "INV"),
    )
    for line in quotation.lines.all():
        invoice.lines.create(
            variant=line.variant, description=line.description, qty=line.qty,
            unit_price=line.unit_price, tax_rate=line.tax_rate, position=line.position,
        )
    return invoice


def _locked(invoice):
    """
    The invoice again, row-locked for the rest of the transaction.

    Every step that writes money or goods against an invoice takes this first,
    so a double tap on a phone, or two people on one invoice, run one after
    the other and the second sees what the first did.
    """
    from apps.selling.models import Invoice

    return Invoice.objects.select_for_update().select_related("customer").get(pk=invoice.pk)


def _lock_customer(customer):
    """Locked like the customer page does it: two writes at once both read
    the same balance and wrote the same "balance after"."""
    from apps.customers.models import Customer

    Customer.objects.select_for_update().filter(pk=customer.pk).first()


@transaction.atomic
def issue(invoice):
    """
    Send it, and create the debt.

    One charge on the customer's own account -- the same account a credit
    sale at the till posts to -- so the statement is the whole story.

    A customer with a credit limit is held to it here as at the till: an
    invoice is credit by another name. A limit of 0 means none has been set.
    """
    from apps.customers.models import CreditKind, CreditTransaction
    from apps.selling.models import InvoiceStatus

    invoice = _locked(invoice)
    if invoice.status != InvoiceStatus.DRAFT:
        return invoice

    customer = invoice.customer
    _lock_customer(customer)
    total = invoice.total
    balance = customer.balance
    if customer.credit_limit > 0 and balance + total > customer.credit_limit:
        raise ValueError(
            f"{customer.name} already owes {balance:,.0f} and may owe up to "
            f"{customer.credit_limit:,.0f}. This invoice of {total:,.0f} would take "
            "them over. Raise their limit on the customer page, or take a payment first."
        )

    CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=customer,
        kind=CreditKind.CHARGE,
        amount=total,
        balance_after=balance + total,
        invoice=invoice,
        reference=invoice.reference,
        note="Invoice",
    )
    invoice.status = InvoiceStatus.OPEN
    invoice.issued_at = timezone.now()
    invoice.save(update_fields=["status", "issued_at", "updated_at"])
    return invoice


@transaction.atomic
def take_payment(invoice, *, amount, method="cash", reference="", user=None, shift=None):
    """
    Money against this invoice, which is money off the account.

    Part-payments are the normal case: a hotel pays half on delivery and the
    rest at the end of the month, and an invoice that can only be paid whole
    forces somebody to lie about which.

    Cash handed over goes into ``shift``'s drawer when one is given, the same
    as a payment taken on the customer's page. Without it the evening count
    showed the cashier over by exactly what the hotel paid.

    Returns the cash movement, or None when no drawer took it.
    """
    from apps.customers.models import CreditKind, CreditTransaction

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("A payment has to be more than nothing.")

    invoice = _locked(invoice)
    if not invoice.is_owed:
        raise ValueError(f"{invoice.reference} is {invoice.get_status_display().lower()}; "
                         "nothing is owed on it.")
    customer = invoice.customer
    _lock_customer(customer)
    if amount > invoice.balance:
        raise ValueError(
            f"That is more than the {invoice.balance:,.0f} still owed on {invoice.reference}."
        )

    entry = CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=customer,
        kind=CreditKind.PAYMENT,
        amount=-amount,
        balance_after=customer.balance - amount,
        invoice=invoice,
        reference=invoice.reference,
        method=method,
        note=reference[:200],
    )

    movement = None
    if method == "cash" and shift is not None:
        from apps.pos.models import CashMovementKind
        from apps.pos.services import record_cash_movement

        movement = record_cash_movement(
            shift, kind=CashMovementKind.PAY_IN, amount=amount,
            reason=f"Invoice {invoice.reference}: {customer.name}"[:200])
        entry.cash_movement = movement
        entry.save(update_fields=["cash_movement", "updated_at"])

    refresh_payment_status(invoice)
    return movement


def refresh_payment_status(invoice):
    """
    Owed, part paid or paid, from the payments against it.

    Called after anything that changes them: a payment here, a payment taken
    or undone on the customer's page.
    """
    from apps.selling.models import InvoiceStatus

    if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}:
        return invoice
    # Forget any prefetch: the rows have just changed.
    getattr(invoice, "_prefetched_objects_cache", {}).pop("credit_entries", None)
    balance = invoice.balance
    if balance <= 0:
        invoice.status = InvoiceStatus.PAID
    elif invoice.paid > 0:
        invoice.status = InvoiceStatus.PART_PAID
    else:
        invoice.status = InvoiceStatus.OPEN
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


@transaction.atomic
def void_invoice(invoice, *, user=None):
    """
    Cancel it, and take the debt back off the account.

    Refused once anything has been delivered or paid: those are facts, and a
    document that can be made to disappear after the goods have gone is not
    a record of anything. A delivery that came back is cancelled first, on
    its own; then the invoice can go.
    """
    from apps.customers.models import CreditKind, CreditTransaction
    from apps.selling.models import InvoiceStatus

    invoice = _locked(invoice)
    if invoice.status == InvoiceStatus.VOID:
        return invoice
    if invoice.has_deliveries or invoice.paid > 0:
        raise ValueError(
            "Something has already been delivered or paid against this invoice."
        )

    if invoice.status == InvoiceStatus.OPEN:
        customer = invoice.customer
        _lock_customer(customer)
        total = invoice.total
        CreditTransaction.objects.create(
            tenant=invoice.tenant,
            customer=customer,
            kind=CreditKind.ADJUSTMENT,
            amount=-total,
            balance_after=customer.balance - total,
            invoice=invoice,
            reference=invoice.reference,
            note="Invoice cancelled",
        )

    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = timezone.now()
    invoice.voided_by = user or get_current_user()
    invoice.save(update_fields=["status", "voided_at", "voided_by", "updated_at"])
    return invoice


# --------------------------------------------------------------------------
# Delivery notes
# --------------------------------------------------------------------------

@transaction.atomic
def deliver(note, *, user=None):
    """
    Goods out: stock falls and the sale lands, on the day of the trip.

    It goes through the till's own `complete_sale` rather than writing rows
    here. That function is where cost is captured, where stock moves, where
    the fiscal copy is queued -- a second path through any of that would be
    a second set of rounding decisions and a second thing to get wrong.

    The sale carries the invoice, and the invoice already charged the
    customer, so the credit payment on it settles nothing twice.

    Two things it does differently from a counter sale:
      * the shop's "no selling below zero" setting holds. A lorry loaded
        with goods the store does not have is a mistake worth stopping.
      * each line keeps the VAT rate on the invoice, not whatever the
        product carries today, so the sale and the invoice agree.
    """
    from apps.inventory.services import InsufficientStock
    from apps.pos.models import AddedVia, CartLine, PaymentMethod
    from apps.pos.services import complete_sale, new_cart, price

    invoice = _locked(note.invoice)
    lines = list(note.lines.select_related("invoice_line__variant__product"))
    if not lines:
        raise ValueError("Nothing on this delivery note.")

    # Checked again under the lock: two people sending the last 10 bags at
    # once each passed the form's check against the other's stale figure.
    # This note's own lines are already counted in qty_delivered.
    for row in lines:
        source = row.invoice_line
        if source.qty_delivered > source.qty:
            left = source.qty - source.qty_delivered + row.qty
            raise ValueError(f"{source.description}: only {left:,.3f} left to send.")

    cart = new_cart(user=user, branch=note.branch, customer=invoice.customer)
    for row in lines:
        source = row.invoice_line
        # Written directly rather than through add_to_cart, which merges
        # lines for the same product and takes the product's own VAT rate.
        CartLine.objects.create(
            tenant=cart.tenant, cart=cart, variant=source.variant,
            description=source.description[:160], qty=row.qty,
            unit_price=price(source.unit_price), tax_rate=source.tax_rate,
            added_via=AddedVia.MANUAL if source.variant is None else AddedVia.SEARCH,
        )

    total = sum((row.line_total for row in lines), Decimal("0"))
    try:
        sale = complete_sale(
            cart,
            [{"method": PaymentMethod.CREDIT, "amount": total}],
            user=user,
            invoice=invoice,
            respect_stock_setting=True,
        )
    except InsufficientStock as short:
        raise ValueError(
            f"Not enough {short.variant} in stock: {short.available:,.3f} here, "
            f"{short.wanted:,.3f} on this trip."
        ) from short
    note.sale = sale
    note.save(update_fields=["sale", "updated_at"])
    return note


@transaction.atomic
def cancel_delivery(note, *, reason="", user=None):
    """
    Goods that came back, or a trip recorded by mistake.

    The sale it made is voided, so the stock returns and the day's takings
    drop, and the quantities are owed to the customer again on the invoice.
    The debt stays: the invoice made it, and cancelling the invoice (once
    nothing else has gone out or been paid) is what takes it away.
    """
    from apps.pos.services import void_sale
    from apps.selling.models import DeliveryNote

    _locked(note.invoice)
    note = DeliveryNote.objects.select_for_update().get(pk=note.pk)
    if note.voided_at is not None:
        raise ValueError(f"{note.reference} is already cancelled.")
    if note.sale_id:
        void_sale(note.sale, reason=reason or f"Delivery {note.reference} cancelled",
                  user=user, for_delivery=True)
    note.voided_at = timezone.now()
    note.voided_by = user or get_current_user()
    note.save(update_fields=["voided_at", "voided_by", "updated_at"])
    return note

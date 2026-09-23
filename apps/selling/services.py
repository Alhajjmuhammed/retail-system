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

    today = timezone.localdate()
    fresh = Quotation.objects.create(
        branch=quotation.branch,
        reference=next_reference(Quotation, "QUO"),
        customer=quotation.customer,
        issued_on=today,
        valid_until=today + timedelta(days=(quotation.valid_until - quotation.issued_on).days or 14),
        note=quotation.note,
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

def invoice_from_quotation(quotation, *, terms_days=14):
    """
    The offer, become a bill.

    The lines are copied rather than shared: an invoice is what the customer
    agreed to, and editing the quotation afterwards must not rewrite it.
    """
    from datetime import timedelta

    from apps.selling.models import Invoice

    today = timezone.localdate()
    invoice = Invoice.objects.create(
        branch=quotation.branch,
        reference=next_reference(Invoice, "INV"),
        customer=quotation.customer,
        quotation=quotation,
        issued_on=today,
        due_on=today + timedelta(days=terms_days),
        note=quotation.note,
    )
    for line in quotation.lines.all():
        invoice.lines.create(
            variant=line.variant, description=line.description, qty=line.qty,
            unit_price=line.unit_price, tax_rate=line.tax_rate, position=line.position,
        )
    return invoice


@transaction.atomic
def issue(invoice):
    """
    Send it, and create the debt.

    One charge on the customer's own account -- the same account a credit
    sale at the till posts to -- so the statement is the whole story.
    """
    from apps.customers.models import CreditKind, CreditTransaction
    from apps.selling.models import InvoiceStatus

    if invoice.status != InvoiceStatus.DRAFT:
        return invoice

    total = invoice.total
    CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=invoice.customer,
        kind=CreditKind.CHARGE,
        amount=total,
        balance_after=invoice.customer.balance + total,
        invoice=invoice,
        reference=invoice.reference,
        note="Invoice",
    )
    invoice.status = InvoiceStatus.OPEN
    invoice.issued_at = timezone.now()
    invoice.save(update_fields=["status", "issued_at", "updated_at"])
    return invoice


@transaction.atomic
def take_payment(invoice, *, amount, method="cash", reference="", user=None):
    """
    Money against this invoice, which is money off the account.

    Part-payments are the normal case: a hotel pays half on delivery and the
    rest at the end of the month, and an invoice that can only be paid whole
    forces somebody to lie about which.
    """
    from decimal import Decimal

    from apps.customers.models import CreditKind, CreditTransaction

    amount = Decimal(str(amount))
    CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=invoice.customer,
        kind=CreditKind.PAYMENT,
        amount=-amount,
        balance_after=invoice.customer.balance - amount,
        invoice=invoice,
        reference=invoice.reference,
        method=method,
        note=reference[:200],
    )
    refresh_payment_status(invoice)
    return invoice


def refresh_payment_status(invoice):
    from apps.selling.models import InvoiceStatus

    if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}:
        return invoice
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
    a record of anything.
    """
    from apps.customers.models import CreditKind, CreditTransaction
    from apps.selling.models import InvoiceStatus

    if invoice.has_deliveries or invoice.paid > 0:
        raise ValueError(
            "Something has already been delivered or paid against this invoice."
        )

    if invoice.status == InvoiceStatus.OPEN:
        total = invoice.total
        CreditTransaction.objects.create(
            tenant=invoice.tenant,
            customer=invoice.customer,
            kind=CreditKind.ADJUSTMENT,
            amount=-total,
            balance_after=invoice.customer.balance - total,
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
    """
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    lines = list(note.lines.select_related("invoice_line__variant"))
    if not lines:
        raise ValueError("Nothing on this delivery note.")

    cart = new_cart(user=user, branch=note.branch, customer=note.invoice.customer)
    for row in lines:
        source = row.invoice_line
        line = add_to_cart(
            cart, source.variant, qty=row.qty, unit_price=source.unit_price,
            description=source.description,
        )
        # An open item goes in at no VAT; an invoiced one carries the rate the
        # customer was quoted, labour and delivery included.
        if source.variant is None and source.tax_rate and line.tax_rate != source.tax_rate:
            line.tax_rate = source.tax_rate
            line.save(update_fields=["tax_rate"])

    total = sum((row.line_total for row in lines), Decimal("0"))
    sale = complete_sale(
        cart,
        [{"method": PaymentMethod.CREDIT, "amount": total}],
        user=user,
        invoice=note.invoice,
    )
    note.sale = sale
    note.save(update_fields=["sale", "updated_at"])
    return note

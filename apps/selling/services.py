"""
What a quotation does, as distinct from what it is.

Kept out of the views because two of these -- expiring and accepting -- will
be called by the nightly job and by the invoice side as well, and a rule that
lives in a view is a rule the rest of the system cannot reach.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.context import get_current_user
from apps.selling.models import Quotation, QuotationStatus


def next_reference(model, prefix: str, field: str = "reference") -> str:
    """
    A number a shop can read down the phone: QUO260001.

    Per tenant and per year, longest-first so the ten-thousandth document
    does not sort below the ninth and start the count again.

    `field` exists for the proforma: its number is kept in a column of its
    own, because `reference` is overwritten with the invoice number when it
    is issued. Counting from `reference` there would hand the next customer
    a proforma number another customer is already holding.
    """
    from django.db.models.functions import Length

    year = timezone.localdate().year
    stem = f"{prefix}{year % 100:02d}"
    lookup = {f"{field}__startswith": stem}
    last = (
        model.objects.filter(**lookup)
        .order_by(Length(field).desc(), f"-{field}")
        .values_list(field, flat=True)
        .first()
    )
    try:
        counter = int(last[len(stem):]) + 1 if last else 1
    except (TypeError, ValueError):
        counter = model.objects.filter(**lookup).count() + 1
    return f"{stem}{counter:04d}"


def save_as_proforma(invoice):
    """
    Give a new invoice its proforma number, in both columns.

    `reference` is what the document shows and is replaced by the invoice
    number when it is issued. `proforma_reference` keeps the proforma
    number for good, and the sequence counts from that column -- otherwise
    issuing PRO260001 would free the number for the next customer.
    """
    from apps.core.numbering import save_with_number
    from apps.selling.models import Invoice

    saved = save_with_number(
        invoice,
        field="reference",
        generate=lambda: next_reference(Invoice, "PRO", field="proforma_reference"),
    )
    Invoice.objects.filter(pk=saved.pk).update(proforma_reference=saved.reference)
    saved.proforma_reference = saved.reference
    return saved


@transaction.atomic
def send(quotation):
    """Mark it as given to the customer. The date it was sent is the date
    the shop will be asked about later."""
    check_document_discounts(quotation)
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

    from apps.core.currency import rate_for
    from apps.core.numbering import save_with_number

    rules = paperwork_rules()
    check_document_discounts(quotation, rules)
    currency = quotation.doc_currency
    rate = rate_for(currency)
    current = (prices_in(quotation.customer, currency, rate)
               if rules.lock_document_prices else {})

    today = timezone.localdate()
    fresh = save_with_number(
        Quotation(
            branch=quotation.branch,
            customer=quotation.customer,
            issued_on=today,
            valid_until=today + timedelta(
                days=(quotation.valid_until - quotation.issued_on).days or 14),
            note=quotation.note,
            discount_percent=quotation.discount_percent,
            currency=currency,
            exchange_rate=rate,
        ),
        field="reference",
        generate=lambda: next_reference(Quotation, "QUO"),
    )
    for line in quotation.lines.all():
        # The same offer again is today's offer: with prices locked, a
        # product is quoted at today's list price, not last month's.
        unit_price = (current.get(line.variant_id, line.unit_price) if line.variant_id
                      else line.unit_price)
        fresh.lines.create(
            variant=line.variant, description=line.description, qty=line.qty,
            unit_price=unit_price, tax_rate=line.tax_rate,
            discount_percent=line.discount_percent, position=line.position,
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
    check_document_discounts(quotation)

    today = timezone.localdate()
    invoice = save_as_proforma(
        Invoice(
            branch=quotation.branch,
            customer=quotation.customer,
            quotation=quotation,
            issued_on=today,
            due_on=today + timedelta(days=terms_days),
            note=quotation.note,
            # The offer they accepted was the discounted one. Billing the
            # full price is charging more than was agreed.
            discount_percent=quotation.discount_percent,
            # And in the currency and at the rate it was offered in: a
            # customer who accepted USD 176.50 is billed USD 176.50.
            currency=quotation.doc_currency,
            exchange_rate=quotation.exchange_rate,
            # What the customer accepted: VAT inside or on top, as quoted.
            vat_on_top=quotation.adds_vat,
        ),
    )
    for line in quotation.lines.all():
        invoice.lines.create(
            variant=line.variant, description=line.description, qty=line.qty,
            unit_price=line.unit_price, tax_rate=line.tax_rate,
            discount_percent=line.discount_percent, position=line.position,
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
    from apps.selling.models import Invoice, InvoiceStatus

    invoice = _locked(invoice)
    if invoice.status != InvoiceStatus.DRAFT:
        return invoice
    check_document_discounts(invoice)

    customer = invoice.customer
    _lock_customer(customer)
    # The account is kept in the shop's own currency. A dollar invoice goes
    # on it at the rate frozen on the invoice.
    total = invoice.total_main
    balance = customer.balance
    if customer.credit_limit > 0 and balance + total > customer.credit_limit:
        raise ValueError(
            f"{customer.name} already owes {balance:,.2f} and may owe up to "
            f"{customer.credit_limit:,.2f}. This invoice of {total:,.2f} would take "
            "them over. Raise their limit on the customer page, or take a payment first."
        )

    # The invoice number is spent here, not when the draft was started. A
    # proforma that is never issued therefore leaves no hole in the invoice
    # sequence, which is the first thing anybody auditing it looks for.
    #
    # Saved before the charge is written, and through `save_with_number`, so
    # that two people issuing at the same moment do not both pick INV260004
    # and one of them get an error. The charge then carries the number that
    # actually stuck.
    invoice.status = InvoiceStatus.OPEN
    invoice.issued_at = timezone.now()
    if invoice.reference.startswith("PRO"):
        from apps.core.numbering import save_with_number

        if not invoice.proforma_reference:
            invoice.proforma_reference = invoice.reference
        save_with_number(
            invoice,
            field="reference",
            generate=lambda: next_reference(Invoice, "INV"),
        )
    else:
        invoice.save(update_fields=["status", "issued_at", "updated_at"])

    CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=customer,
        kind=CreditKind.CHARGE,
        amount=total,
        doc_amount=invoice.total if invoice.is_foreign else None,
        balance_after=balance + total,
        invoice=invoice,
        reference=invoice.reference,
        note="Invoice" + (f" {invoice.doc_currency} {invoice.total:,.2f} at "
                          f"{invoice.rate_label}" if invoice.is_foreign else ""),
    )
    if paperwork_rules().stock_out_on_invoice:
        # The shop counts an invoice as the sale: the goods leave now, on a
        # delivery note made for everything on it. Short of stock (when the
        # shop does not sell below zero) refuses the whole issue -- this runs
        # inside the same transaction.
        send_everything(invoice)
    return invoice


def send_everything(invoice, *, user=None):
    """A delivery note for all that is still to go out, sent now."""
    from apps.accounts.models import Membership
    from apps.core.numbering import save_with_number
    from apps.selling.models import DeliveryNote

    wanted = [line for line in invoice.lines.all() if line.qty_outstanding > 0]
    if not wanted:
        return None
    # The sale is somebody's. Issued from a page, it is whoever issued it;
    # from a script with nobody signed in, the shop's owner.
    user = user or get_current_user()
    if user is None:
        owner = (Membership.objects.filter(role__is_owner_role=True, is_active=True)
                 .select_related("user").order_by("created_at").first())
        user = owner.user if owner else None
    note = save_with_number(
        DeliveryNote(branch=invoice.branch, invoice=invoice,
                     delivered_on=timezone.localdate(),
                     note="Sent when the invoice was issued."),
        field="reference",
        generate=lambda: next_reference(DeliveryNote, "DN"),
    )
    for line in wanted:
        note.lines.create(invoice_line=line, qty=line.qty_outstanding)
    return deliver(note, user=user)


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
            f"That is more than the {invoice.doc_currency} {invoice.balance:,.2f} still "
            f"owed on {invoice.reference}."
        )

    # What comes off the account, in the shop's own currency. The payment
    # that clears the invoice takes exactly what is left of the charge, so
    # rounding each part-payment can never leave a shilling behind.
    if invoice.is_foreign:
        main = (invoice.balance_main if amount == invoice.balance
                else invoice.to_main(amount))
    else:
        main = amount

    entry = CreditTransaction.objects.create(
        tenant=invoice.tenant,
        customer=customer,
        kind=CreditKind.PAYMENT,
        amount=-main,
        doc_amount=-amount if invoice.is_foreign else None,
        balance_after=customer.balance - main,
        invoice=invoice,
        reference=invoice.reference,
        method=method,
        note=reference[:200],
    )

    movement = None
    # Cash in the shop's own currency goes in the drawer. Foreign notes are
    # counted with the POS's own foreign cash, not added to the shilling
    # count, where they would show the cashier over.
    if method == "cash" and shift is not None and not invoice.is_foreign:
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
    if invoice.paid > 0:
        raise ValueError("Something has already been paid against this invoice.")
    if invoice.has_deliveries:
        if not paperwork_rules().stock_out_on_invoice:
            raise ValueError(
                "Something has already been delivered against this invoice."
            )
        # Where issuing sent the goods, cancelling brings them back: each
        # delivery is cancelled, so the stock returns and the sale comes
        # off the day's figures.
        for note in invoice.deliveries.filter(voided_at__isnull=True):
            cancel_delivery(note, reason=f"Invoice {invoice.reference} cancelled", user=user)

    if invoice.status == InvoiceStatus.OPEN:
        customer = invoice.customer
        _lock_customer(customer)
        # Exactly what issuing it put on the account, whatever the rate is now.
        total = invoice.charged_main
        CreditTransaction.objects.create(
            tenant=invoice.tenant,
            customer=customer,
            kind=CreditKind.ADJUSTMENT,
            amount=-total,
            doc_amount=-invoice.total if invoice.is_foreign else None,
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
    from apps.pos.services import complete_sale, money, new_cart, price

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
        # Whatever the invoice took off comes off here too, as an amount,
        # so the sale the till records is the money the customer was asked
        # for -- and the VAT inside it is worked out of that same figure.
        # A sale is always in the shop's own currency, whatever the invoice
        # was written in: the price goes over at the invoice's frozen rate.
        unit_price = price(source.unit_price * invoice.exchange_rate) if invoice.is_foreign \
            else price(source.unit_price)
        if invoice.adds_vat:
            # The till's prices include VAT; this one had it added on top.
            unit_price = price(unit_price * (1 + source.tax_rate / Decimal("100")))
        gross = money(row.qty * unit_price)
        CartLine.objects.create(
            tenant=cart.tenant, cart=cart, variant=source.variant,
            description=source.description[:160], qty=row.qty,
            unit_price=unit_price, tax_rate=source.tax_rate,
            discount=max(gross - row.line_total_main, Decimal("0")),
            added_via=AddedVia.MANUAL if source.variant is None else AddedVia.SEARCH,
        )

    total = sum((row.line_total_main for row in lines), Decimal("0"))
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


def paperwork_rules():
    """The shop's own say over prices and discounts on its paperwork."""
    from apps.core.context import get_current_tenant
    from apps.org.models import TenantSettings

    row, _ = TenantSettings.objects.get_or_create(tenant=get_current_tenant())
    return row


def combined_discount(line_percent, document_percent) -> Decimal:
    """
    What a line really comes off by: its own discount, then the document's
    on what is left. 40% and 40% is 64%, which is what a limit has to see.
    """
    hundred = Decimal("100")
    left = (hundred - Decimal(line_percent)) * (hundred - Decimal(document_percent)) / hundred
    return (hundred - left).quantize(Decimal("0.01"))


def check_discount(line_percent, document_percent, rules=None):
    """Refuse a discount above the shop's limit. Owners are held to it too:
    the limit is theirs to raise in Settings, not to step around."""
    rules = rules or paperwork_rules()
    limit = rules.max_document_discount
    total = combined_discount(line_percent or 0, document_percent or 0)
    if total > limit:
        def pct(value):
            # 20.00 reads as 20, 12.50 as 12.5.
            return format(Decimal(value).normalize(), "f")

        raise ValueError(
            f"The most this shop allows off is {pct(limit)}%. "
            f"This comes to {pct(total)}% off"
            + (" with the line and whole-document discounts together."
               if line_percent and document_percent else ".")
            + " The owner can change the limit under Settings, Business."
        )


def check_product_rules(variant, unit_price, line_percent, document_percent, rate=1):
    """
    What the product itself says, the same two rules the POS keeps:
    "can be discounted" and "never sell below". The floor is checked against
    what one unit really comes to after both discounts, so 10% on the line
    and 10% on the whole cannot walk a price under it between them.
    """
    if variant is None:
        return
    product = variant.product
    if not product.discount_allowed and (line_percent or document_percent):
        raise ValueError(
            f"{product.name} cannot be discounted: it is set that way on the product."
        )
    floor = product.min_price
    if floor:
        hundred = Decimal("100")
        # The floor is in the shop's own currency; a dollar price is held to
        # it at the document's rate.
        each = (Decimal(unit_price) * Decimal(rate) * (hundred - Decimal(line_percent or 0))
                * (hundred - Decimal(document_percent or 0)) / (hundred * hundred))
        if each < floor:
            raise ValueError(
                f"{product.name} is never sold below {floor:,.2f}. "
                f"This comes to {each:,.2f} each."
            )


def check_document_discounts(document, rules=None):
    """
    Every line of a quotation or invoice within the shop's limit and its
    product's own rules, together with the whole-document discount. Run where
    a document moves on -- sent, copied, invoiced, issued -- so one written
    before a rule was set cannot carry an old price or discount past it.
    """
    rules = rules or paperwork_rules()
    lines = list(document.lines.select_related("variant__product"))
    deepest = max((line.discount_percent for line in lines), default=0)
    check_discount(deepest, document.discount_percent, rules)
    for line in lines:
        check_product_rules(line.variant, line.unit_price, line.discount_percent,
                            document.discount_percent, document.exchange_rate)


def locked_price(variant, customer, rules=None, document=None):
    """
    The price a product must go in at when the shop has locked prices, or
    None when staff may type their own. Typed lines (no product) are never
    locked: delivery and labour have no catalogue price.
    """
    rules = rules or paperwork_rules()
    if variant is None or not rules.lock_document_prices:
        return None
    amount = prices_for(customer).get(variant.pk)
    if amount is not None and document is not None and document.is_foreign:
        from apps.core.currency import from_main

        amount = from_main(amount, document.exchange_rate)
    if amount is None:
        raise ValueError(
            f"{variant} has no price yet, and prices are locked on quotations "
            "and invoices. Set its price on the product first."
        )
    return amount


@transaction.atomic
def change_currency(document, code):
    """
    Move a document still being written into another currency, at today's
    rate. Every line keeps its value: its price goes over through the shop's
    own currency, so 2,500 shillings is USD 1.00 at 2,500 and back again.
    Only while it is a draft -- once sent or issued, the currency is part of
    what the customer was given.
    """
    from apps.core.currency import from_main, main_currency, rate_for
    from apps.pos.services import price

    code = (code or "").upper()
    if document.status != "draft":
        raise ValueError(f"{document.reference} has gone out; its currency stays as it is.")
    if code == document.doc_currency:
        return document
    new_rate = rate_for(code)
    main = main_currency()
    for line in document.lines.all():
        in_main = line.unit_price * document.exchange_rate
        line.unit_price = price(in_main) if code == main else from_main(in_main, new_rate)
        line.save(update_fields=["unit_price", "updated_at"])
    document.currency = code
    document.exchange_rate = new_rate
    document.save(update_fields=["currency", "exchange_rate", "updated_at"])
    return document


def prices_in(customer, currency, rate):
    """
    What each product would be charged at for this customer, in ``currency``
    at ``rate``. The shop's own currency needs no converting; another one is
    worked out from the shop's price, to the cent.
    """
    from apps.core.currency import from_main, main_currency

    prices = prices_for(customer)
    if (currency or "").upper() == main_currency():
        return prices
    return {key: from_main(value, rate) for key, value in prices.items()}


def prices_for(customer):
    """
    What each product would be charged at for this customer.

    Used to fill a line in the moment a product is picked, so nobody types a
    shelf price from memory. A customer on their own price list gets that
    list's price; everybody else gets the default one.

    The figure is a suggestion and stays editable -- a quoted price is a
    negotiation, and the shop has the last word on it.
    """
    from apps.catalog.models import Price

    wanted = getattr(customer, "price_list", None)
    rows = Price.objects.filter(min_qty=1).select_related("price_list")
    if wanted is not None and wanted.is_active:
        rows = rows.filter(Q(price_list=wanted) | Q(price_list__is_default=True))
        # Default list first, the customer's own list second, because the
        # later row overwrites the earlier one and theirs is the one that
        # counts. Sorted the other way round, a wholesale customer was
        # quietly quoted the retail price.
        rows = rows.order_by("-price_list__is_default")
    else:
        rows = rows.filter(price_list__is_default=True)
    return {row.variant_id: row.amount for row in rows}

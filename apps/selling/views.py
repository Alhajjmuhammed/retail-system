"""
Quotations: writing an offer, sending it, and being told the answer.

Built the way a purchase order is built -- the document first, then a line at
a time on its own page. It works with no JavaScript at all, which matters for
a shop quoting from a phone on a bad connection, and it means a half-finished
quotation is a real saved document rather than something lost on a refresh.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.catalog.models import TaxRate, Variant
from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.listing import modal_or_page, paginate
from apps.core.numbering import save_with_number
from apps.core.parsing import BadInput, date_or, int_or, parse_decimal
from apps.customers.models import Customer
from apps.selling import services
from apps.selling.models import (
    DeliveryNote,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Quotation,
    QuotationLine,
    QuotationStatus,
)

# Long enough for a customer to think about it, short enough that the shop is
# not held to a price it quoted last season.
DEFAULT_VALID_DAYS = 14


@login_required
@requires("quote.view")
def quotation_list(request):
    status = request.GET.get("status", "")
    term = request.GET.get("q", "").strip()

    quotations = Quotation.objects.select_related("customer").prefetch_related("lines")
    if status:
        quotations = quotations.filter(status=status)
    if term:
        quotations = quotations.filter(reference__icontains=term) | quotations.filter(
            customer__name__icontains=term
        )

    return render(request, "selling/quotations.html", {
        **paginate(request, quotations.order_by("-created_at")),
        "status": status,
        "q": term,
        "statuses": QuotationStatus.choices,
        "today": timezone.localdate(),
    })


@login_required
@requires("quote.manage")
def quotation_create(request):
    if request.method == "POST":
        customer = Customer.objects.filter(
            pk=int_or(request.POST.get("customer")), is_active=True).first()
        if customer is None:
            messages.error(request, "Choose who the quotation is for.")
            return redirect("selling:quotation_list")

        today = timezone.localdate()
        days = int_or(request.POST.get("valid_days")) or DEFAULT_VALID_DAYS
        quotation = save_with_number(
            Quotation(
                customer=customer,
                branch=request.branch,
                issued_on=today,
                valid_until=today + timedelta(days=days),
                note=request.POST.get("note", "").strip()[:500],
            ),
            field="reference",
            generate=lambda: services.next_reference(Quotation, "QUO"),
        )
        audit.record("quote.created", obj=quotation, ip=audit.client_ip(request))
        return redirect("selling:quotation_detail", pk=quotation.pk)

    return modal_or_page(
        request, "selling/_quotation_form.html",
        {"customers": Customer.objects.filter(is_active=True).order_by("name"),
         "chosen": int_or(request.GET.get("customer")),
         "valid_days": DEFAULT_VALID_DAYS},
        title="New quotation", back=reverse("selling:quotation_list"),
    )


@login_required
@requires("quote.view", branch=branch_of(Quotation))
def quotation_detail(request, pk):
    quotation = get_object_or_404(
        Quotation.objects.select_related("customer").prefetch_related(
            "lines__variant__product"),
        pk=pk,
    )
    may_edit = request.membership.can("quote.manage")

    if request.method == "POST":
        if not may_edit:
            messages.error(request, "You may not change a quotation.")
            return redirect("selling:quotation_detail", pk=pk)
        return _act_on(request, quotation)

    return render(request, "selling/quotation_detail.html", {
        "quotation": quotation,
        "variants": (Variant.objects.filter(is_active=True, product__is_active=True)
                     .select_related("product").order_by("product__name", "name")),
        "may_edit": may_edit,
        "editable": quotation.status == QuotationStatus.DRAFT,
        "taxes": TaxRate.objects.filter(is_active=True).order_by("-is_default", "name"),
        "today": timezone.localdate(),
    })


def _act_on(request, quotation):
    """Everything the detail page's buttons do, in one place."""
    action = request.POST.get("action")
    editable = quotation.status == QuotationStatus.DRAFT

    if action == "add_line":
        if not editable:
            messages.error(request, "A quotation that has been sent cannot be changed. "
                                    "Copy it instead.")
            return redirect("selling:quotation_detail", pk=quotation.pk)
        return _add_line(request, quotation)

    if action == "remove_line" and editable:
        line = quotation.lines.filter(pk=int_or(request.POST.get("line"))).first()
        if line is not None:
            line.delete()
            messages.success(request, "Line removed.")
        return redirect("selling:quotation_detail", pk=quotation.pk)

    if action == "header" and editable:
        valid_until = date_or(request.POST.get("valid_until"))
        if valid_until is not None:
            quotation.valid_until = valid_until
        quotation.note = request.POST.get("note", "").strip()[:500]
        quotation.save(update_fields=["valid_until", "note", "updated_at"])
        messages.success(request, "Saved.")
        return redirect("selling:quotation_detail", pk=quotation.pk)

    if action == "send":
        if not editable:
            messages.error(request, f"{quotation.reference} has already gone out.")
            return redirect("selling:quotation_detail", pk=quotation.pk)
        if not quotation.lines.exists():
            messages.error(request, "There is nothing on this quotation yet.")
            return redirect("selling:quotation_detail", pk=quotation.pk)
        services.send(quotation)
        audit.record("quote.sent", obj=quotation, ip=audit.client_ip(request))
        messages.success(request, f"{quotation.reference} marked as sent.")
        return redirect("selling:quotation_detail", pk=quotation.pk)

    if action in {"accept", "decline"}:
        try:
            services.decide(quotation, accepted=action == "accept", user=request.user)
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:quotation_detail", pk=quotation.pk)
        audit.record(f"quote.{action}ed", obj=quotation, ip=audit.client_ip(request))
        messages.success(
            request,
            f"{quotation.reference} accepted." if action == "accept"
            else f"{quotation.reference} marked as declined.",
        )
        return redirect("selling:quotation_detail", pk=quotation.pk)

    if action == "copy":
        fresh = services.copy_for_new(quotation)
        audit.record("quote.copied", obj=fresh, ip=audit.client_ip(request))
        messages.success(request, f"{fresh.reference} is a copy of {quotation.reference}.")
        return redirect("selling:quotation_detail", pk=fresh.pk)

    messages.error(request, "Nothing to do.")
    return redirect("selling:quotation_detail", pk=quotation.pk)


def _add_line(request, quotation):
    """
    A product, or a line typed by hand.

    Typed lines matter: half of what a shop like this quotes for is delivery,
    fitting or labour, and none of that is in the product list.
    """
    variant = Variant.objects.filter(
        pk=int_or(request.POST.get("variant")), is_active=True
    ).select_related("product").first()
    description = request.POST.get("description", "").strip()[:200]

    try:
        qty = parse_decimal(request.POST.get("qty"), "Quantity", positive=True)
        price = parse_decimal(request.POST.get("unit_price"), "Price", minimum=0)
    except BadInput as bad:
        messages.error(request, str(bad))
        return redirect("selling:quotation_detail", pk=quotation.pk)

    if variant is None and not description:
        messages.error(request, "Choose a product, or type what you are quoting for.")
        return redirect("selling:quotation_detail", pk=quotation.pk)

    tax = TaxRate.objects.filter(pk=int_or(request.POST.get("tax_rate"))).first()
    if tax is None and variant is not None:
        tax = variant.product.tax_rate

    QuotationLine.objects.create(
        quotation=quotation,
        variant=variant,
        description=description or str(variant),
        qty=qty,
        unit_price=price,
        tax_rate=tax.rate if tax else 0,
        position=(quotation.lines.count() + 1) * 10,
    )
    messages.success(request, "Line added.")
    return redirect("selling:quotation_detail", pk=quotation.pk)


@login_required
@requires("quote.view", branch=branch_of(Quotation))
def quotation_print(request, pk):
    """The document itself, on the shop's letterhead, ready for a printer."""
    quotation = get_object_or_404(
        Quotation.objects.select_related("customer").prefetch_related("lines"), pk=pk
    )
    return render(request, "selling/quotation_print.html", {
        "quotation": quotation,
        "tenant": request.tenant,
        "back": reverse("selling:quotation_detail", args=[quotation.pk]),
    })


@login_required
@requires("quote.manage", branch=branch_of(Quotation))
@require_POST
def quotation_delete(request, pk):
    """Only a draft. Once it has gone to a customer it is a record of what
    the shop offered, and those are not tidied away."""
    quotation = get_object_or_404(Quotation, pk=pk)
    if quotation.status != QuotationStatus.DRAFT:
        messages.error(request, f"{quotation.reference} has been sent. It stays.")
        return redirect("selling:quotation_detail", pk=pk)

    reference = quotation.reference
    audit.record("quote.removed", obj=quotation, ip=audit.client_ip(request))
    quotation.delete()
    messages.success(request, f"{reference} removed.")
    return redirect("selling:quotation_list")


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

DEFAULT_TERMS_DAYS = 14


@login_required
@requires("invoice.view")
def invoice_list(request):
    status = request.GET.get("status", "")
    term = request.GET.get("q", "").strip()

    invoices = Invoice.objects.select_related("customer").prefetch_related(
        "lines", "credit_entries", "deliveries")
    if status == "overdue":
        invoices = invoices.filter(
            status__in=[InvoiceStatus.OPEN, InvoiceStatus.PART_PAID],
            due_on__lt=timezone.localdate(),
        )
    elif status:
        invoices = invoices.filter(status=status)
    if term:
        invoices = invoices.filter(reference__icontains=term) | invoices.filter(
            customer__name__icontains=term)

    return render(request, "selling/invoices.html", {
        **paginate(request, invoices.order_by("-created_at")),
        "status": status,
        "q": term,
        "statuses": InvoiceStatus.choices,
        "today": timezone.localdate(),
    })


@login_required
@requires("invoice.manage")
def invoice_create(request):
    """A bill from nothing, or a bill from an offer somebody accepted."""
    quotation = Quotation.objects.filter(
        pk=int_or(request.GET.get("quotation") or request.POST.get("quotation"))
    ).first()
    if quotation is not None and not request.membership.covers_branch(quotation.branch):
        # The quotation's own page is branch-checked; billing it must be too.
        messages.error(request, f"{quotation.reference} belongs to {quotation.branch.name}.")
        return redirect("selling:invoice_list")

    if request.method == "POST":
        terms = int_or(request.POST.get("terms_days"))
        terms = DEFAULT_TERMS_DAYS if terms is None else terms

        if quotation is not None:
            try:
                invoice = services.invoice_from_quotation(quotation, terms_days=terms)
            except ValueError as refused:
                messages.error(request, str(refused))
                return redirect("selling:quotation_detail", pk=quotation.pk)
        else:
            customer = Customer.objects.filter(
                pk=int_or(request.POST.get("customer")), is_active=True).first()
            if customer is None:
                messages.error(request, "Choose who the invoice is for.")
                return redirect("selling:invoice_list")
            today = timezone.localdate()
            invoice = save_with_number(
                Invoice(
                    customer=customer, branch=request.branch, issued_on=today,
                    due_on=today + timedelta(days=terms),
                    note=request.POST.get("note", "").strip()[:500],
                ),
                field="reference",
                generate=lambda: services.next_reference(Invoice, "INV"),
            )
        audit.record("invoice.created", obj=invoice, ip=audit.client_ip(request))
        return redirect("selling:invoice_detail", pk=invoice.pk)

    return modal_or_page(
        request, "selling/_invoice_form.html",
        {"customers": Customer.objects.filter(is_active=True).order_by("name"),
         "quotation": quotation,
         "terms_days": DEFAULT_TERMS_DAYS},
        title="New invoice", back=reverse("selling:invoice_list"),
    )


@login_required
@requires("invoice.view", branch=branch_of(Invoice))
def invoice_detail(request, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer", "quotation").prefetch_related(
            "lines__variant__product", "credit_entries", "deliveries__lines"),
        pk=pk,
    )
    if request.method == "POST":
        return _act_on_invoice(request, invoice)

    return render(request, "selling/invoice_detail.html", {
        "invoice": invoice,
        "variants": (Variant.objects.filter(is_active=True, product__is_active=True)
                     .select_related("product").order_by("product__name", "name")),
        "taxes": TaxRate.objects.filter(is_active=True).order_by("-is_default", "name"),
        "editable": invoice.status == InvoiceStatus.DRAFT,
        "may_manage": request.membership.can("invoice.manage"),
        "may_take_payment": request.membership.can("invoice.payment"),
        "may_deliver": request.membership.can("delivery.manage"),
        "methods": PAYMENT_METHODS,
        "today": timezone.localdate(),
    })


PAYMENT_METHODS = [("cash", "Cash"), ("mpesa", "M-Pesa"), ("tigopesa", "Tigo Pesa"),
                   ("airtelmoney", "Airtel Money"), ("bank", "Bank transfer"),
                   ("card", "Card")]


def _act_on_invoice(request, invoice):
    action = request.POST.get("action")
    # Locked for the rest of the request: a double tap on "Issue it" or
    # "Record it" used to run twice against the same unchanged invoice.
    invoice = Invoice.objects.select_for_update().select_related("customer").get(pk=invoice.pk)
    editable = invoice.status == InvoiceStatus.DRAFT
    may_manage = request.membership.can("invoice.manage")

    if action in {"add_line", "remove_line", "header", "issue", "void"} and not may_manage:
        messages.error(request, "You may not change an invoice.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "add_line":
        if not editable:
            messages.error(request, "An invoice that has been issued cannot be changed. "
                                    "Cancel it and write another one.")
            return redirect("selling:invoice_detail", pk=invoice.pk)
        return _add_invoice_line(request, invoice)

    if action == "remove_line" and editable:
        line = invoice.lines.filter(pk=int_or(request.POST.get("line"))).first()
        if line is not None:
            line.delete()
            messages.success(request, "Line removed.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "header" and editable:
        due_on = date_or(request.POST.get("due_on"))
        if due_on is not None:
            invoice.due_on = due_on
        invoice.note = request.POST.get("note", "").strip()[:500]
        invoice.save(update_fields=["due_on", "note", "updated_at"])
        messages.success(request, "Saved.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "issue":
        if not editable:
            return redirect("selling:invoice_detail", pk=invoice.pk)
        if not invoice.lines.exists():
            messages.error(request, "There is nothing on this invoice yet.")
            return redirect("selling:invoice_detail", pk=invoice.pk)
        total = invoice.total
        if not request.membership.can("invoice.manage", value=total):
            # The role's "biggest invoice they may issue". Owners have none.
            limit = request.membership.check_permission("invoice.manage").limit or 0
            messages.error(
                request,
                f"You may issue invoices up to {limit:,.0f}. This one is {total:,.0f}: "
                "ask the owner or a manager to issue it.",
            )
            return redirect("selling:invoice_detail", pk=invoice.pk)
        try:
            services.issue(invoice)
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:invoice_detail", pk=invoice.pk)
        invoice.refresh_from_db()
        audit.record("invoice.issued", obj=invoice, ip=audit.client_ip(request))
        messages.success(
            request,
            f"{invoice.reference} issued. {invoice.customer.name} owes "
            f"{total:,.0f}, due {invoice.due_on.day} {invoice.due_on:%b}.",
        )
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "payment":
        if not request.membership.can("invoice.payment"):
            messages.error(request, "You may not record a payment.")
            return redirect("selling:invoice_detail", pk=invoice.pk)
        try:
            amount = parse_decimal(request.POST.get("amount"), "Amount", positive=True,
                                   places=2)
        except BadInput as bad:
            messages.error(request, str(bad))
            return redirect("selling:invoice_detail", pk=invoice.pk)
        method = request.POST.get("method", "cash")
        if method not in dict(PAYMENT_METHODS):
            method = "cash"

        drawer = None
        if method == "cash":
            from apps.pos.views import _open_shift_for

            drawer = _open_shift_for(request)
        try:
            movement = services.take_payment(
                invoice, amount=amount, method=method,
                reference=request.POST.get("reference", "").strip(),
                user=request.user, shift=drawer,
            )
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:invoice_detail", pk=invoice.pk)
        audit.record("invoice.payment", obj=invoice, after={"amount": str(amount)},
                     ip=audit.client_ip(request))
        messages.success(
            request,
            f"{amount:,.0f} received against {invoice.reference}."
            + (" Added to your POS drawer." if movement is not None else
               " No POS is open, so it is not in any drawer count." if method == "cash"
               else ""),
        )
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "void":
        try:
            services.void_invoice(invoice, user=request.user)
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:invoice_detail", pk=invoice.pk)
        audit.record("invoice.voided", obj=invoice, ip=audit.client_ip(request))
        messages.warning(request, f"{invoice.reference} cancelled.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if action == "cancel_delivery":
        if not request.membership.can("delivery.manage", branch=invoice.branch):
            messages.error(request, "You may not cancel a delivery.")
            return redirect("selling:invoice_detail", pk=invoice.pk)
        note = invoice.deliveries.filter(pk=int_or(request.POST.get("note"))).first()
        if note is None:
            messages.error(request, "That delivery is not on this invoice.")
            return redirect("selling:invoice_detail", pk=invoice.pk)
        try:
            services.cancel_delivery(
                note, reason=request.POST.get("reason", "").strip()[:200],
                user=request.user)
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:invoice_detail", pk=invoice.pk)
        audit.record("delivery.cancelled", obj=note, ip=audit.client_ip(request))
        messages.warning(
            request,
            f"{note.reference} cancelled. The stock is back and those goods are owed "
            f"to {invoice.customer.name} again on this invoice.",
        )
        return redirect("selling:invoice_detail", pk=invoice.pk)

    messages.error(request, "Nothing to do.")
    return redirect("selling:invoice_detail", pk=invoice.pk)


def _add_invoice_line(request, invoice):
    variant = Variant.objects.filter(
        pk=int_or(request.POST.get("variant")), is_active=True
    ).select_related("product").first()
    description = request.POST.get("description", "").strip()[:200]

    try:
        qty = parse_decimal(request.POST.get("qty"), "Quantity", positive=True)
        price = parse_decimal(request.POST.get("unit_price"), "Price", minimum=0)
    except BadInput as bad:
        messages.error(request, str(bad))
        return redirect("selling:invoice_detail", pk=invoice.pk)

    if variant is None and not description:
        messages.error(request, "Choose a product, or type what you are charging for.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    tax = TaxRate.objects.filter(pk=int_or(request.POST.get("tax_rate"))).first()
    if tax is None and variant is not None:
        tax = variant.product.tax_rate

    InvoiceLine.objects.create(
        invoice=invoice, variant=variant, description=description or str(variant),
        qty=qty, unit_price=price, tax_rate=tax.rate if tax else 0,
        position=(invoice.lines.count() + 1) * 10,
    )
    messages.success(request, "Line added.")
    return redirect("selling:invoice_detail", pk=invoice.pk)


@login_required
@requires("invoice.view", branch=branch_of(Invoice))
def invoice_print(request, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer").prefetch_related("lines"), pk=pk)
    return render(request, "selling/invoice_print.html", {
        "invoice": invoice,
        "tenant": request.tenant,
        "back": reverse("selling:invoice_detail", args=[invoice.pk]),
    })


# --------------------------------------------------------------------------
# Delivery notes
# --------------------------------------------------------------------------

@login_required
@requires("delivery.manage", branch=branch_of(Invoice))
def delivery_create(request, pk):
    """
    What is going out on this trip.

    Opens with everything still outstanding filled in, because most trips
    take the lot; a part load is typed over it.
    """
    invoice = get_object_or_404(Invoice.objects.select_related("customer"), pk=pk)
    if request.method == "POST":
        # Locked before anything is read: two people sending the same goods
        # at once each counted the other's load as still in the store.
        invoice = Invoice.objects.select_for_update().select_related("customer").get(
            pk=invoice.pk)

    if invoice.status == InvoiceStatus.DRAFT:
        messages.error(request, "Issue the invoice before sending goods against it.")
        return redirect("selling:invoice_detail", pk=invoice.pk)
    if invoice.status == InvoiceStatus.VOID:
        messages.error(request, f"{invoice.reference} was cancelled.")
        return redirect("selling:invoice_detail", pk=invoice.pk)

    outstanding = [line for line in invoice.lines.all() if line.qty_outstanding > 0]

    if request.method == "POST":
        wanted = []
        for line in outstanding:
            raw = request.POST.get(f"qty:{line.pk}", "")
            if not raw.strip():
                continue
            try:
                qty = parse_decimal(raw, f"Quantity for {line.description}", minimum=0)
            except BadInput as bad:
                messages.error(request, str(bad))
                return redirect("selling:delivery_create", pk=invoice.pk)
            if qty <= 0:
                continue
            if qty > line.qty_outstanding:
                messages.error(
                    request,
                    f"{line.description}: only {line.qty_outstanding:,.3f} left to send.",
                )
                return redirect("selling:delivery_create", pk=invoice.pk)
            wanted.append((line, qty))

        if not wanted:
            messages.error(request, "Type what is going out on this trip.")
            return redirect("selling:delivery_create", pk=invoice.pk)

        try:
            # One savepoint for the note and its sale: goods that are not in
            # the store take the whole trip back, rather than leaving a note
            # behind with nothing sent on it.
            with transaction.atomic():
                note = save_with_number(
                    DeliveryNote(
                        branch=invoice.branch, invoice=invoice,
                        delivered_on=(date_or(request.POST.get("delivered_on"))
                                      or timezone.localdate()),
                        received_by=request.POST.get("received_by", "").strip()[:120],
                        note=request.POST.get("note", "").strip()[:500],
                    ),
                    field="reference",
                    generate=lambda: services.next_reference(DeliveryNote, "DN"),
                )
                for line, qty in wanted:
                    note.lines.create(invoice_line=line, qty=qty)
                services.deliver(note, user=request.user)
        except ValueError as refused:
            messages.error(request, str(refused))
            return redirect("selling:delivery_create", pk=invoice.pk)

        audit.record("delivery.sent", obj=note, ip=audit.client_ip(request))
        messages.success(
            request,
            f"{note.reference} sent. Stock has come down and the sale is recorded.",
        )
        return redirect("selling:delivery_print", pk=note.pk)

    return render(request, "selling/delivery_form.html", {
        "invoice": invoice,
        "lines": outstanding,
        "today": timezone.localdate(),
    })


@login_required
@requires("invoice.view", branch=branch_of(DeliveryNote))
def delivery_print(request, pk):
    note = get_object_or_404(
        DeliveryNote.objects.select_related("invoice__customer").prefetch_related(
            "lines__invoice_line"),
        pk=pk,
    )
    return render(request, "selling/delivery_print.html", {
        "note": note,
        "tenant": request.tenant,
        "back": reverse("selling:invoice_detail", args=[note.invoice_id]),
    })

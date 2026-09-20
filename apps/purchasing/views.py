"""Suppliers, orders and receiving."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.catalog.models import Variant
from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.deletion import remove_or_archive
from apps.core.numbering import save_with_number
from apps.core.parsing import BadInput, date_or, decimal_or_none, int_or, parse_decimal
from apps.purchasing.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
)
from apps.purchasing.services import approve_order, next_reference, post_receipt


@login_required
@requires("supplier.manage")
def supplier_list(request):
    # Annotated rather than the `balance` property, which ran two aggregates
    # per supplier.
    # Two subqueries, not two joins: joining bills and payments multiplied
    # rows, and `distinct=True` then dropped bills that shared an amount.
    def total(model):
        return Coalesce(Subquery(
            model.objects.filter(supplier=OuterRef("pk")).values("supplier")
            .annotate(t=Sum("amount")).values("t"),
            output_field=DecimalField(),
        ), Decimal("0"))

    from django.db.models import Q

    from apps.core.listing import paginate

    view = request.GET.get("view", "active")
    suppliers = (
        Supplier.objects.annotate(invoiced=total(SupplierInvoice), settled=total(SupplierPayment))
        .annotate(owed=F("invoiced") - F("settled"))
        .order_by("name")
    )
    active = suppliers.filter(is_active=True)
    summary = {
        "count": active.count(),
        "owing": active.filter(owed__gt=0).count(),
        "owed": sum((s.owed for s in active.filter(owed__gt=0)), Decimal("0")),
        "archived": Supplier.objects.filter(is_active=False).count(),
    }
    if view == "owing":
        suppliers = active.filter(owed__gt=0)
    elif view == "archived":
        suppliers = suppliers.filter(is_active=False)
    else:
        view = "active"
        suppliers = active
    term = request.GET.get("q", "").strip()
    if term:
        suppliers = suppliers.filter(
            Q(name__icontains=term) | Q(contact_name__icontains=term) | Q(phone__icontains=term)
        )
    return render(request, "purchasing/suppliers.html", {
        **paginate(request, suppliers), "view": view, "q": term, "summary": summary,
    })


@login_required
@requires("supplier.manage")
def supplier_form(request, pk=None):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from apps.core.listing import close_modal, modal_or_page

    supplier = get_object_or_404(Supplier, pk=pk) if pk else None
    errors = []
    values = None

    if request.method == "POST":
        values = request.POST
        # Cut to what the columns hold: a long paste used to be a 500.
        fields = {
            "name": request.POST.get("name", "").strip()[:120],
            "contact_name": request.POST.get("contact_name", "").strip()[:120],
            "phone": request.POST.get("phone", "").strip()[:30],
            "email": request.POST.get("email", "").strip()[:254],
            "tin": request.POST.get("tin", "").strip()[:30],
            "payment_terms_days": min(max(int_or(request.POST.get("payment_terms_days"), 0), 0), 365),
            "address": request.POST.get("address", "").strip()[:500],
        }
        if not fields["name"]:
            errors.append("A name is required.")
        clash = Supplier.objects.filter(name__iexact=fields["name"])
        if supplier is not None:
            clash = clash.exclude(pk=supplier.pk)
        clash = clash.first() if fields["name"] else None
        if clash is not None:
            # Two suppliers with one name hit the unique index as a 500.
            errors.append(f"There is already a supplier called {clash.name}"
                          + ("" if clash.is_active else " (removed; switch it back on instead)") + ".")
        if fields["email"]:
            try:
                validate_email(fields["email"])
            except ValidationError:
                errors.append("That email address does not look right.")
        if not errors:
            before = audit.snapshot(supplier) if supplier else None
            if supplier is None:
                supplier = Supplier.objects.create(**fields)
                audit.record("supplier.created", obj=supplier, ip=audit.client_ip(request))
            else:
                for key, value in fields.items():
                    setattr(supplier, key, value)
                supplier.save()
                audit.record("supplier.updated", obj=supplier, before=before,
                             after=audit.snapshot(supplier), ip=audit.client_ip(request))
            messages.success(request, f"{supplier.name} saved.")
            from django.utils.http import url_has_allowed_host_and_scheme

            nxt = request.POST.get("next", "")
            if not url_has_allowed_host_and_scheme(nxt, {request.get_host()},
                                                   require_https=request.is_secure()):
                nxt = reverse("purchasing:supplier_list")
            return close_modal(request, nxt)

    return modal_or_page(
        request, "purchasing/_supplier_form.html",
        {"supplier": supplier, "errors": errors, "values": values,
         "next": request.GET.get("next", "")},
        title=supplier.name if supplier else "New supplier",
        back=reverse("purchasing:supplier_list"),
    )


@login_required
@requires("supplier.manage")
@require_POST
def supplier_restore(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk, is_active=False)
    supplier.is_active = True
    supplier.save(update_fields=["is_active", "updated_at"])
    audit.record("supplier.restored", obj=supplier, ip=audit.client_ip(request))
    messages.success(request, f"{supplier.name} is back on your list.")
    return redirect("purchasing:supplier_list")


@login_required
@requires("supplier.manage")
def supplier_detail(request, pk):
    from django.db.models import Prefetch

    supplier = get_object_or_404(Supplier, pk=pk)
    # Paid and left-to-pay worked out in the query: `outstanding` per bill ran
    # an aggregate each time the page mentioned it (85 queries for 5 bills).
    paid = Coalesce(Subquery(
        SupplierPayment.objects.filter(invoice=OuterRef("pk")).values("invoice")
        .annotate(t=Sum("amount")).values("t"), output_field=DecimalField()), Decimal("0"))
    bills = supplier.invoices.select_related("receipt").annotate(paid_sum=paid) \
        .annotate(left=F("amount") - F("paid_sum"))
    lines = Prefetch("lines", queryset=GoodsReceiptLine.objects.all())
    return render(
        request,
        "purchasing/supplier_detail.html",
        {
            "supplier": supplier,
            "balance": supplier.balance,
            "today": timezone.localdate(),
            "invoices": bills.order_by("-invoice_date")[:30],
            "open_invoices": list(bills.filter(left__gt=0).order_by("invoice_date")),
            "payments": supplier.payments.select_related("invoice").order_by("-paid_at", "-pk")[:50],
            "on_account": supplier.payments.filter(invoice__isnull=True)
            .aggregate(t=Sum("amount"))["t"] or 0,
            "orders": supplier.orders.filter(branch__in=request.membership.branches())
            .exclude(status=POStatus.CANCELLED).order_by("-created_at")[:10],
            "receipts": supplier.receipts.filter(branch__in=request.membership.branches())
            .prefetch_related(lines).order_by("-received_at")[:20],
        },
    )


@login_required
@requires("supplier.pay", value=lambda r, **kw: decimal_or_none(r.POST.get("amount")) or 0)
def supplier_pay(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)

    if request.method == "POST":
        try:
            amount = parse_decimal(request.POST.get("amount"), "Amount", positive=True, places=2)
        except BadInput as exc:
            messages.error(request, str(exc))
            return redirect("purchasing:supplier_detail", pk=pk)

        invoice = None
        if request.POST.get("invoice"):
            # Scoped to this shop *and* this supplier: a raw id used to be
            # stored as given, so a payment could land on anybody's invoice.
            invoice = get_object_or_404(
                SupplierInvoice, pk=int_or(request.POST.get("invoice")), supplier=supplier
            )
            if amount > invoice.outstanding:
                messages.error(
                    request,
                    f"{invoice.number} only has {invoice.outstanding:,.0f} left to pay.",
                )
                return redirect("purchasing:supplier_detail", pk=pk)

        method = request.POST.get("method", "cash")
        if method not in {"cash", "mpesa", "bank"}:
            method = "cash"
        common = {
            "supplier": supplier, "method": method,
            "reference": request.POST.get("reference", "").strip()[:60],
            "paid_at": date_or(request.POST.get("paid_at"), timezone.localdate()),
        }
        import uuid

        from django.db import transaction

        common["batch"] = uuid.uuid4()
        with transaction.atomic():
            # Locked: two payments at once both read the same outstanding
            # figure and settled one bill twice.
            Supplier.objects.select_for_update().filter(pk=supplier.pk).first()
            if invoice is not None:
                if amount > invoice.outstanding:
                    messages.error(request, f"{invoice.number} only has "
                                            f"{invoice.outstanding:,.0f} left to pay.")
                    return redirect("purchasing:supplier_detail", pk=pk)
                parts = [(invoice, amount)]
            else:
                # Not tied to one bill: settle the oldest first. Left unlinked,
                # the supplier's balance fell while every bill still showed
                # unpaid, and the two figures on one page disagreed.
                parts, left = [], amount
                for bill in supplier.invoices.order_by("invoice_date", "pk"):
                    if left <= 0:
                        break
                    due = bill.outstanding
                    if due > 0:
                        take = min(due, left)
                        parts.append((bill, take))
                        left -= take
                if left > 0:
                    parts.append((None, left))  # paid ahead: credit with them
            drawer = None
            if method == "cash" and request.POST.get("from_drawer") == "on":
                # Paid out of the till: the drawer expects that much less, or
                # the cashier looks short by exactly this payment.
                from apps.pos.models import CashMovementKind
                from apps.pos.services import record_cash_movement
                from apps.pos.views import _open_shift_for

                drawer = _open_shift_for(request)
                if drawer is None:
                    messages.error(request, "No till of yours is open, so it cannot come out "
                                            "of a drawer.")
                    return redirect("purchasing:supplier_detail", pk=pk)
                if amount > drawer.compute_expected_cash():
                    messages.error(request, "Your drawer should only hold "
                                            f"{drawer.compute_expected_cash():,.0f}.")
                    return redirect("purchasing:supplier_detail", pk=pk)
                common["cash_movement"] = record_cash_movement(
                    drawer, kind=CashMovementKind.PAY_OUT, amount=-amount,
                    reason=f"Paid supplier {supplier.name}"[:200])
            payments = [SupplierPayment.objects.create(invoice=bill, amount=part, **common)
                        for bill, part in parts]
        for payment in payments:
            audit.record("supplier.paid", obj=payment, ip=audit.client_ip(request))
        settled = [p.invoice.number for p in payments if p.invoice]
        messages.success(
            request,
            f"{amount:,.0f} paid to {supplier.name}"
            + (f" — settles {', '.join(settled)}" if settled else "")
            + (" (part in advance)" if any(p.invoice is None for p in payments) else "") + ".",
        )
        return redirect("purchasing:supplier_detail", pk=pk)

    return redirect("purchasing:supplier_detail", pk=pk)


@login_required
# A bill is money the shop owes: whoever may pay suppliers records it, not
# everyone who keeps the supplier list.
@requires("supplier.pay")
@require_POST
def supplier_bill(request, pk):
    """Record a bill that did not come from a delivery here: transport, services."""
    supplier = get_object_or_404(Supplier, pk=pk)
    try:
        amount = parse_decimal(request.POST.get("amount"), "Amount", positive=True, places=2)
    except BadInput as exc:
        messages.error(request, str(exc))
        return redirect("purchasing:supplier_detail", pk=pk)
    number = request.POST.get("number", "").strip()[:40]
    if not number:
        messages.error(request, "Give the bill its number from the supplier's paperwork.")
        return redirect("purchasing:supplier_detail", pk=pk)
    if supplier.invoices.filter(number__iexact=number).exists():
        messages.error(request, f"Bill {number} from {supplier} is already recorded.")
        return redirect("purchasing:supplier_detail", pk=pk)
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            bill = SupplierInvoice.objects.create(
                supplier=supplier, number=number, amount=amount,
                invoice_date=date_or(request.POST.get("invoice_date"), timezone.localdate()),
                due_date=date_or(request.POST.get("due_date")),
                note=request.POST.get("note", "")[:200],
            )
    except IntegrityError:
        messages.error(request, f"Bill {number} from {supplier} is already recorded.")
        return redirect("purchasing:supplier_detail", pk=pk)
    from apps.purchasing.services import apply_advances

    apply_advances(supplier)  # money already paid ahead settles it
    audit.record("supplier.billed", obj=bill, ip=audit.client_ip(request))
    messages.success(request, f"Bill {bill.number} for {amount:,.0f} recorded.")
    return redirect("purchasing:supplier_detail", pk=pk)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@login_required
@requires("po.manage")
def order_list(request):
    from django.db.models import Q

    from apps.core.listing import paginate

    orders = (
        PurchaseOrder.objects.select_related("supplier", "branch", "approved_by")
        .filter(branch__in=request.membership.branches(include_closed=True))
        .prefetch_related("lines")
        .order_by("-created_at")
    )
    status = request.GET.get("status", "")
    if status in POStatus.values:
        orders = orders.filter(status=status)
    else:
        status = ""
    term = request.GET.get("q", "").strip()
    if term:
        orders = orders.filter(Q(reference__icontains=term) | Q(supplier__name__icontains=term))
    return render(request, "purchasing/orders.html", {
        **paginate(request, orders), "status": status, "q": term, "statuses": POStatus.choices,
    })


@login_required
@requires("po.manage")
def order_create(request):
    from apps.core.listing import modal_or_page

    if request.branch is None:
        messages.error(request, "Choose the branch the goods are for first.")
        return redirect("purchasing:order_list")
    if request.method == "POST":
        supplier = get_object_or_404(Supplier, pk=int_or(request.POST.get("supplier")),
                                     is_active=True)
        order = save_with_number(
            PurchaseOrder(
                supplier=supplier,
                branch=request.branch,
                expected_date=date_or(request.POST.get("expected_date")),
                note=request.POST.get("note", "").strip()[:500],
            ),
            field="reference",
            generate=lambda: next_reference(PurchaseOrder, "PO"),
        )
        audit.record("po.created", obj=order, ip=audit.client_ip(request))
        return redirect("purchasing:order_detail", pk=order.pk)

    return modal_or_page(
        request, "purchasing/_order_form.html",
        {"suppliers": Supplier.objects.filter(is_active=True).order_by("name"),
         "chosen": int_or(request.GET.get("supplier"))},
        title="New order", back=reverse("purchasing:order_list"),
    )


@login_required
@requires("po.manage", branch=branch_of(PurchaseOrder))
def order_detail(request, pk):
    order = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier").prefetch_related(
            "lines__variant__product"
        ),
        pk=pk,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_line":
            if order.status != POStatus.DRAFT:
                messages.error(request, "Lines can only be added while the order is a draft.")
                return redirect("purchasing:order_detail", pk=pk)
            variant = Variant.objects.filter(pk=int_or(request.POST.get("variant")),
                                             is_active=True).first()
            if variant is None:
                messages.error(request, "Choose a product.")
                return redirect("purchasing:order_detail", pk=pk)
            try:
                qty = parse_decimal(request.POST.get("qty"), "Quantity", positive=True)
                cost = parse_decimal(request.POST.get("unit_cost"), "Unit cost", minimum=0)
            except BadInput as exc:
                messages.error(request, str(exc))
                return redirect("purchasing:order_detail", pk=pk)
            existing = order.lines.filter(variant=variant, unit_cost=cost).first()
            if existing is not None:
                # The same product at the same price again: one line, more of it.
                existing.qty_ordered += qty
                existing.save(update_fields=["qty_ordered"])
            else:
                PurchaseOrderLine.objects.create(
                    order=order, variant=variant, qty_ordered=qty, unit_cost=cost,
                )
        elif action == "approve" and order.status != POStatus.DRAFT:
            # Re-approving a received or cancelled order used to send it
            # straight back to "sent".
            messages.error(request, f"{order.reference} is not a draft.")
        elif action == "approve" and not order.lines.exists():
            messages.error(request, "Add at least one line before approving.")
        elif action == "approve":
            decision = request.membership.check_permission(
                "po.approve", branch=order.branch, value=order.total
            )
            if not decision.allowed:
                messages.error(request, decision.reason)
            else:
                approve_order(order, user=request.user)
                audit.record("po.approved", obj=order, ip=audit.client_ip(request))
                messages.success(request, f"{order.reference} approved.")
        return redirect("purchasing:order_detail", pk=pk)

    approve = request.membership.check_permission(
        "po.approve", branch=order.branch, value=order.total
    ) if order.status == POStatus.DRAFT else None
    return render(request, "purchasing/order_detail.html",
                  {"order": order, "variants": _variants(),
                   "may_approve": bool(approve and approve.allowed),
                   "approve_reason": approve.reason if approve and not approve.allowed else "",
                   "receipts": order.receipts.order_by("-received_at")})


@login_required
@requires("po.manage", branch=branch_of(PurchaseOrder))
def order_print(request, pk):
    """The order as a page to print or save as PDF and send to the supplier."""
    from apps.org.models import TenantSettings

    order = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "branch", "approved_by")
        .prefetch_related("lines__variant__product__base_unit"),
        pk=pk,
    )
    return render(request, "purchasing/order_print.html",
                  {"order": order, "settings": TenantSettings.objects.first()})


# --------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------

@login_required
@requires("stock.receive")
def receipt_list(request):
    from django.db.models import Q

    from apps.core.listing import paginate

    receipts = (
        GoodsReceipt.objects.select_related("supplier", "order", "received_by", "branch")
        .filter(branch__in=request.membership.branches(include_closed=True))
        .prefetch_related("lines")
        .order_by("-received_at")
    )
    term = request.GET.get("q", "").strip()
    if term:
        receipts = receipts.filter(
            Q(reference__icontains=term) | Q(supplier__name__icontains=term)
            | Q(supplier_note__icontains=term)
        )
    return render(request, "purchasing/receipts.html", {**paginate(request, receipts), "q": term})


@login_required
@requires("stock.receive")
def receipt_create(request):
    """
    Receive goods, with or without an order.

    A lot of stock in these shops arrives at the back door with no paperwork,
    and requiring an order would mean half of it never gets recorded.
    """
    from apps.core.listing import modal_or_page

    if request.branch is None:
        messages.error(request, "Choose the branch the goods arrived at first.")
        return redirect("purchasing:receipt_list")
    order = None
    order_pk = request.POST.get("order") or request.GET.get("order")
    if order_pk:
        # Through the tenant-scoped manager. The raw id used to be stored as
        # given, and posting then marked another shop's order "received".
        # And only this branch's: opening the form showed another branch's
        # order and supplier.
        order = get_object_or_404(PurchaseOrder, pk=int_or(order_pk),
                                  branch__in=request.membership.branches())

    if request.method == "POST":
        supplier = get_object_or_404(Supplier, pk=int_or(request.POST.get("supplier")),
                                     is_active=True)
        if order is not None and order.supplier_id != supplier.pk:
            messages.error(request, f"{order.reference} is from {order.supplier}, not {supplier}.")
            return redirect(f"{request.path}?order={order.pk}")
        if order is not None and order.branch_id != getattr(request.branch, "pk", None):
            messages.error(request, f"{order.reference} is for {order.branch}. Receive it there.")
            return redirect("purchasing:order_detail", pk=order.pk)
        if order is not None and order.status not in {POStatus.SENT, POStatus.PARTIAL}:
            messages.error(request, f"{order.reference} is {order.get_status_display().lower()}"
                                    " -- only an approved order can be received against.")
            return redirect("purchasing:order_detail", pk=order.pk)
        receipt = save_with_number(
            GoodsReceipt(
                supplier=supplier,
                branch=request.branch,
                order=order,
                received_by=request.user,
                supplier_note=request.POST.get("supplier_note", "").strip()[:60],
            ),
            field="reference",
            generate=lambda: next_reference(GoodsReceipt, "GR"),
        )
        return redirect("purchasing:receipt_detail", pk=receipt.pk)

    return modal_or_page(
        request, "purchasing/_receipt_form.html",
        {"suppliers": Supplier.objects.filter(is_active=True).order_by("name"), "order": order},
        title="Receive goods", back=reverse("purchasing:receipt_list"),
    )


def _receipt_value(request, **kwargs):
    """What putting this delivery into stock is worth, for the limit."""
    if request.method != "POST" or request.POST.get("action") != "post":
        return None
    receipt = GoodsReceipt.objects.filter(pk=kwargs.get("pk")).first()
    return receipt.total if receipt else None


@login_required
@requires("stock.receive", value=_receipt_value, branch=branch_of(GoodsReceipt))
def receipt_detail(request, pk):
    receipt = get_object_or_404(
        GoodsReceipt.objects.select_related("supplier").prefetch_related(
            "lines__variant__product"
        ),
        pk=pk,
    )
    posted = receipt.lines.exists() and _is_posted(receipt)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"add_line", "fill_from_order"} and posted:
                raise ValueError(f"{receipt.reference} is already in stock. Start a new receipt.")
            if action == "add_line":
                variant = Variant.objects.filter(pk=int_or(request.POST.get("variant")),
                                                 is_active=True).first()
                if variant is None:
                    raise ValueError("Choose a product.")
                qty = parse_decimal(request.POST.get("qty"), "Quantity", positive=True)
                cost = parse_decimal(request.POST.get("unit_cost"), "Unit cost", minimum=0)
                GoodsReceiptLine.objects.create(
                    receipt=receipt,
                    variant=variant,
                    order_line=_order_line_for(receipt, variant),
                    qty=qty,
                    unit_cost=cost,
                    batch_no=request.POST.get("batch_no", "")[:40],
                    expiry_date=date_or(request.POST.get("expiry_date")),
                )
            elif action == "fill_from_order" and receipt.order_id:
                # Everything still outstanding on the order, ready to adjust --
                # once. Pressing it twice used to double every line.
                already = set(receipt.lines.exclude(order_line=None)
                              .values_list("order_line_id", flat=True))
                for line in receipt.order.lines.all():
                    if line.qty_outstanding > 0 and line.pk not in already:
                        GoodsReceiptLine.objects.create(
                            receipt=receipt, variant=line.variant, order_line=line,
                            qty=line.qty_outstanding, unit_cost=line.unit_cost,
                        )
            elif action == "post":
                post_receipt(receipt, user=request.user)
                audit.record("goods.received", obj=receipt, ip=audit.client_ip(request))
                messages.success(
                    request, f"{receipt.reference} received. Stock and costs updated."
                )
        except ValueError as exc:  # BadInput is a ValueError
            messages.error(request, str(exc))
        return redirect("purchasing:receipt_detail", pk=pk)

    return render(
        request,
        "purchasing/receipt_detail.html",
        {"receipt": receipt, "posted": posted, "variants": _variants()},
    )


def _variants():
    """Products to pick from, instead of typing a raw id."""
    return (
        Variant.objects.filter(is_active=True, product__is_active=True)
        .select_related("product")
        .order_by("product__name", "name")
    )


def _order_line_for(receipt, variant):
    """
    The order line this delivery fills, if any.

    Never set before, so no order ever moved to "partly" or "fully received".
    """
    if not receipt.order_id:
        return None
    # Only a line with something still to come. Linking extra goods to a
    # fully received line made the whole delivery impossible to post.
    return (
        receipt.order.lines.filter(variant=variant, qty_received__lt=F("qty_ordered"))
        .order_by("pk").first()
    )


@login_required
@requires("po.manage", branch=branch_of(PurchaseOrder))
@require_POST
def order_cancel(request, pk):
    """Cancel an order. Anything already received against it stays received."""
    order = get_object_or_404(PurchaseOrder, pk=pk)
    if order.status == POStatus.RECEIVED:
        messages.error(request, "This order has already been received in full.")
    else:
        order.status = POStatus.CANCELLED
        order.save(update_fields=["status", "updated_at"])
        audit.record("po.cancelled", obj=order, ip=audit.client_ip(request))
        messages.success(request, f"{order.reference} cancelled.")
    return redirect("purchasing:order_list")


@login_required
@requires("po.manage", branch=branch_of(PurchaseOrderLine, "order.branch"))
@require_POST
def order_line_delete(request, pk):
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("order"), pk=pk)
    order = line.order
    if order.status != POStatus.DRAFT:
        messages.error(request, "This order has already been sent.")
    elif line.qty_received:
        messages.error(request, "Some of this line has already arrived.")
    else:
        line.delete()
        messages.success(request, "Line removed.")
    return redirect("purchasing:order_detail", pk=order.pk)


@login_required
@requires("stock.receive", branch=branch_of(GoodsReceiptLine, "receipt.branch"))
@require_POST
def receipt_line_delete(request, pk):
    line = get_object_or_404(GoodsReceiptLine.objects.select_related("receipt"), pk=pk)
    receipt = line.receipt
    if _is_posted(receipt):
        messages.error(
            request,
            "This delivery is already in stock. Adjust the stock instead of "
            "changing the record of what arrived.",
        )
    else:
        line.delete()
        messages.success(request, "Line removed.")
    return redirect("purchasing:receipt_detail", pk=receipt.pk)


@login_required
@requires("stock.receive", branch=branch_of(GoodsReceipt))
@require_POST
def receipt_delete(request, pk):
    """
    Delete a delivery that was never put into stock.

    Once posted it is part of the stock ledger and the way back is a wastage
    or an adjustment, not deletion.
    """
    receipt = get_object_or_404(GoodsReceipt, pk=pk)
    if _is_posted(receipt):
        messages.error(
            request,
            f"{receipt.reference} is already in stock and cannot be deleted. "
            "Adjust the stock instead.",
        )
        return redirect("purchasing:receipt_detail", pk=pk)

    reference = receipt.reference
    audit.record("goods.receipt_deleted", obj=receipt, ip=audit.client_ip(request))
    receipt.delete()
    messages.success(request, f"{reference} deleted.")
    return redirect("purchasing:receipt_list")


@login_required
@requires("supplier.manage")
@require_POST
def supplier_delete(request, pk):
    """Gone if nothing was ever bought from them, switched off if it was."""
    supplier = get_object_or_404(Supplier, pk=pk)
    name = supplier.name
    owed = supplier.balance
    if owed > 0:
        # Archiving hid the debt: an unpaid supplier vanished from the list
        # and from every "owed" figure.
        messages.error(request, f"You still owe {name} {owed:,.0f}. Settle it before removing them.")
        return redirect("purchasing:supplier_list")
    if owed < 0:
        messages.error(request, f"{name} holds {-owed:,.0f} you paid ahead. "
                                "Use it or get it back before removing them.")
        return redirect("purchasing:supplier_list")
    outcome = remove_or_archive(supplier, label=name)

    if not outcome.blocked:
        audit.record("supplier.archived" if outcome.archived else "supplier.removed",
                     obj=supplier, ip=audit.client_ip(request))
    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("purchasing:supplier_list")


def _payment_group(pk):
    payment = SupplierPayment.objects.filter(pk=pk).first()
    if payment is None:
        return SupplierPayment.objects.none()
    if payment.batch is None:
        return SupplierPayment.objects.filter(pk=payment.pk)
    return SupplierPayment.objects.filter(batch=payment.batch)


@login_required
@requires("supplier.pay",
          value=lambda r, **kw: _payment_group(kw.get("pk")).aggregate(t=Sum("amount"))["t"] or 0)
@require_POST
def supplier_payment_reverse(request, pk):
    """
    Take back a payment typed by mistake -- wrong supplier, wrong amount.

    Deleted, not edited, with the whole of it kept in the activity log.
    """
    payment = get_object_or_404(SupplierPayment.objects.select_related("supplier"), pk=pk)
    supplier = payment.supplier
    reason = request.POST.get("reason", "").strip()[:200]
    if not reason:
        messages.error(request, "Say why the payment is being taken back.")
        return redirect("purchasing:supplier_detail", pk=supplier.pk)
    # The whole payment, not one slice of it: one payment spread over three
    # bills used to be taken back a third at a time.
    from django.db import transaction

    with transaction.atomic():
        # Locked and re-read inside the lock: two clicks both put the cash
        # back in the drawer's figures, for one payment.
        # Locked by id first: Postgres refuses FOR UPDATE with the outer
        # joins that select_related adds for the nullable bill and movement.
        locked = list(_payment_group(pk).select_for_update().values_list("pk", flat=True))
        group = list(SupplierPayment.objects.filter(pk__in=locked)
                     .select_related("invoice", "cash_movement__shift"))
        if not group:
            messages.info(request, "That payment has already been taken back.")
            return redirect("purchasing:supplier_detail", pk=supplier.pk)
        total = sum((p.amount for p in group), Decimal("0"))
        audit.record("supplier.payment_reversed", obj=supplier,
                     before={"amount": str(total), "method": payment.method,
                             "reference": payment.reference, "paid_at": str(payment.paid_at),
                             "bills": [p.invoice.number for p in group if p.invoice]},
                     after={"reason": reason}, ip=audit.client_ip(request))
        movement = next((p.cash_movement for p in group if p.cash_movement_id), None)
        if movement is not None and not request.membership.covers_branch(movement.shift.branch):
            # It came out of a drawer at a branch this person does not run.
            messages.error(request, f"This payment came out of a till at "
                                    f"{movement.shift.branch.name}. Somebody there takes it back.")
            return redirect("purchasing:supplier_detail", pk=supplier.pk)
        till_note = ""
        if movement is not None and movement.shift.closed_at is None:
            from apps.pos.models import CashMovementKind
            from apps.pos.services import record_cash_movement

            record_cash_movement(movement.shift, kind=CashMovementKind.PAY_IN,
                                 amount=-movement.amount,
                                 reason=f"Supplier payment taken back: {supplier.name}"[:200])
            till_note = " Its cash is back in the till's expected figure."
        elif movement is not None:
            till_note = " It came out of a till that is already counted; check that cash-up."
        SupplierPayment.objects.filter(pk__in=[p.pk for p in group]).delete()
    messages.success(request, f"Payment of {total:,.0f} taken back.{till_note}")
    return redirect("purchasing:supplier_detail", pk=supplier.pk)


def _is_posted(receipt) -> bool:
    """
    Already in stock (or billed).

    Judged only by stock movements, a delivery of things the shop does not
    count stock for looked unposted for ever: it could be edited and posted
    again, and the second bill was never raised.
    """
    from apps.inventory.models import StockMovement

    return (
        StockMovement.objects.filter(
            source_type="GoodsReceipt", source_id=str(receipt.pk)
        ).exists()
        or receipt.invoices.exists()
    )

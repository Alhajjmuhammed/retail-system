"""
Stock screens.

Every quantity shown here is the cached projection; every change made here
goes through ``inventory.services``. No view writes to StockItem directly.
"""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.numbering import save_with_number
from apps.core.parsing import decimal_or_none, int_or, parse_decimal
from apps.inventory.models import (
    CountStatus,
    StockCount,
    StockCountLine,
    StockItem,
    StockMovement,
    Transfer,
    TransferLine,
    TransferStatus,
)
from apps.inventory.services import (
    InsufficientStock,
    StockChanged,
    adjust,
    apply_count,
    expiring_soon,
    receive_transfer,
    send_transfer,
)
from apps.org.models import Branch
from apps.purchasing.services import next_reference


@login_required
@requires("stock.view")
def stock_list(request):
    term = request.GET.get("q", "").strip()
    view = request.GET.get("view", "all")
    if request.branch is None:
        messages.info(request, "Choose a branch to see its stock.")
        return redirect("core:dashboard")

    items = (
        StockItem.objects.select_related(
            "variant__product", "variant__product__base_unit", "branch"
        )
        .filter(branch=request.branch, variant__product__is_active=True)
    )

    if term:
        items = items.filter(
            Q(variant__product__name__icontains=term)
            | Q(variant__product__sku__icontains=term)
        )
    if view == "low":
        items = items.filter(
            reorder_level__isnull=False, qty_on_hand__lte=F("reorder_level")
        )
    elif view == "negative":
        items = items.filter(qty_on_hand__lt=0)
    elif view == "dead":
        items = items.filter(qty_on_hand__gt=0, last_counted_at__isnull=True)

    from apps.core.listing import paginate

    here = StockItem.objects.filter(branch=request.branch, variant__product__is_active=True)
    context = {
        **paginate(request, items.order_by("variant__product__name")),
        "q": term,
        "view": view,
        "counts": {
            "low": here.filter(reorder_level__isnull=False,
                               qty_on_hand__lte=F("reorder_level")).count(),
            "negative": here.filter(qty_on_hand__lt=0).count(),
            "dead": here.filter(qty_on_hand__gt=0, last_counted_at__isnull=True).count(),
        },
        "can_see_cost": request.membership.can("product.view_cost"),
        "totals": items.aggregate(
            value=Sum(F("qty_on_hand") * F("avg_cost")), units=Sum("qty_on_hand")
        ),
    }
    if request.htmx:
        return render(request, "inventory/_stock_rows.html", context)
    return render(request, "inventory/stock_list.html", context)


@login_required
@requires("stock.view")
def movements(request):
    """The ledger. Every change to every quantity, filterable by cause."""
    rows = (
        StockMovement.objects.select_related(
            "variant__product", "branch", "created_by"
        )
        .filter(branch=request.branch)
    )

    from apps.inventory.models import MovementReason as _Reason

    reason = request.GET.get("reason", "")
    if reason not in _Reason.values:
        reason = ""
    if reason:
        rows = rows.filter(reason=reason)

    term = request.GET.get("q", "").strip()
    if term:
        rows = rows.filter(variant__product__name__icontains=term)

    from apps.core.listing import paginate
    from apps.inventory.models import MovementReason

    context = {**paginate(request, rows), "reason": reason, "q": term,
               "reasons": MovementReason.choices}
    if request.htmx:
        return render(request, "inventory/_movement_rows.html", context)
    return render(request, "inventory/movements.html", context)


def _adjust_code(request, **kwargs):
    # Writing stock off is its own permission with its own ceiling; it used
    # to be ignored entirely, so a write-off only needed "adjust".
    return "stock.wastage" if request.POST.get("wastage") == "on" else "stock.adjust"


def _adjust_value(request, **kwargs):
    """What the adjustment is worth, for the limit: see inventory.valuation."""
    from apps.inventory.valuation import limit_value

    if request.method != "POST":
        return None
    item = StockItem.objects.select_related("variant").filter(pk=kwargs.get("pk")).first()
    new_qty = decimal_or_none(request.POST.get("new_qty"))
    if item is None or new_qty is None:
        return None
    return limit_value(item.variant, new_qty - item.qty_on_hand, item.avg_cost)


@login_required
@requires(_adjust_code, value=_adjust_value, branch=branch_of(StockItem))
def stock_adjust(request, pk):
    item = get_object_or_404(
        StockItem.objects.select_related("variant__product", "branch"), pk=pk
    )

    if request.method == "POST":
        reason_text = request.POST.get("reason", "").strip()
        new_qty = decimal_or_none(request.POST.get("new_qty"))
        error = ""
        if new_qty is None or new_qty < 0:
            error = "Enter the quantity actually on the shelf: zero or more."
        elif not reason_text:
            error = "A reason is required for every adjustment."
        elif new_qty == item.qty_on_hand:
            error = "That is what the system already says; nothing to change."
        elif request.POST.get("wastage") == "on" and new_qty > item.qty_on_hand:
            # Wastage only ever takes stock away; ticked on an increase it
            # let a write-off-only role add stock.
            error = "Damage or expiry lowers stock. Untick it to record more stock."
        if error:
            return render(request, "inventory/stock_adjust.html",
                          {"item": item, "error": error, "values": request.POST})
        try:
            movement = adjust(
                variant=item.variant,
                new_qty=new_qty,
                expected_qty=decimal_or_none(request.POST.get("seen_qty")),
                reason_text=reason_text[:200],
                # The item's own branch. It used to be whichever branch the
                # person was signed in to, while the audit named this item.
                branch=item.branch,
                user=request.user,
                wastage=request.POST.get("wastage") == "on",
            )
        except (InsufficientStock, StockChanged) as exc:
            item.refresh_from_db()
            return render(request, "inventory/stock_adjust.html",
                          {"item": item, "error": str(exc), "values": request.POST})

        if movement is not None:
            audit.record(
                "stock.adjusted", obj=item,
                before={"qty": str(item.qty_on_hand)},
                after={"qty": request.POST.get("new_qty"), "reason": reason_text},
                ip=audit.client_ip(request),
            )
            messages.success(request, f"{item.variant} adjusted.")
        return redirect("inventory:stock_list")

    return render(request, "inventory/stock_adjust.html", {"item": item})


# --------------------------------------------------------------------------
# Transfers
# --------------------------------------------------------------------------

@login_required
@requires("stock.transfer")
def transfer_list(request):
    from apps.core.listing import paginate

    mine = request.membership.branches(include_closed=True)
    transfers = (
        Transfer.objects.select_related("from_branch", "to_branch", "sent_by")
        .filter(Q(from_branch__in=mine) | Q(to_branch__in=mine))
        .prefetch_related("lines")
        .order_by("-created_at")
    )
    status = request.GET.get("status", "")
    if status in TransferStatus.values:
        transfers = transfers.filter(status=status)
    else:
        status = ""
    direction = request.GET.get("direction", "")
    if direction == "in" and request.branch is not None:
        transfers = transfers.filter(to_branch=request.branch)
    elif direction == "out" and request.branch is not None:
        transfers = transfers.filter(from_branch=request.branch)
    else:
        direction = ""
    return render(request, "inventory/transfers.html", {
        **paginate(request, transfers),
        "transfers": None, "status": status, "direction": direction,
        "statuses": TransferStatus.choices,
    })


@login_required
@requires("stock.transfer")
def transfer_create(request):
    if request.branch is None:
        messages.error(request, "Choose the branch you are sending from first.")
        return redirect("inventory:transfer_list")
    if request.method == "POST":
        to_branch = get_object_or_404(Branch, pk=int_or(request.POST.get("to_branch")),
                                      is_active=True)
        if to_branch.pk == request.branch.pk:
            messages.error(request, "A transfer has to go to a different branch.")
            return redirect("inventory:transfer_create")
        transfer = save_with_number(
            Transfer(
                from_branch=request.branch,
                to_branch=to_branch,
                note=request.POST.get("note", "").strip()[:200],
            ),
            field="reference",
            generate=lambda: next_reference(Transfer, "TR"),
        )
        return redirect("inventory:transfer_detail", pk=transfer.pk)

    branches = Branch.objects.filter(is_active=True).exclude(pk=request.branch.pk)
    template = "inventory/_transfer_form.html" if request.htmx else "inventory/transfer_form.html"
    return render(request, template, {"branches": branches, "modal": bool(request.htmx)})


@login_required
@requires("stock.transfer")
def transfer_detail(request, pk):
    transfer = get_object_or_404(
        Transfer.objects.select_related("from_branch", "to_branch")
        .prefetch_related("lines__variant__product"),
        pk=pk,
    )
    membership = request.membership
    at_sender = membership.covers_branch(transfer.from_branch)
    at_receiver = membership.covers_branch(transfer.to_branch)
    if not (at_sender or at_receiver):
        raise PermissionDenied("This transfer is between branches you do not work in.")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"add_line", "send"} and not at_sender:
                raise ValueError(f"Only {transfer.from_branch} can change or send this.")
            if action == "receive" and not (
                at_receiver and request.branch is not None
                and request.branch.pk == transfer.to_branch_id
            ):
                raise ValueError(f"Receive this while working in {transfer.to_branch}.")
            if action == "add_line":
                from django.db import transaction as db

                with db.atomic():
                    # Locked and re-read: a line added while "send" was running
                    # was never taken from the sender but was received.
                    locked = Transfer.objects.select_for_update().get(pk=transfer.pk)
                    if locked.status != TransferStatus.DRAFT:
                        # Adding a line after sending, then receiving it, made
                        # stock out of nothing.
                        raise ValueError("Lines can only be added before the transfer is sent.")
                    _add_transfer_line(request, locked)
            elif action == "send":
                send_transfer(transfer, user=request.user)
                audit.record("transfer.sent", obj=transfer, ip=audit.client_ip(request))
                messages.success(request, f"{transfer.reference} sent.")
            elif action == "receive":
                counted = {}
                for line in transfer.lines.all():
                    raw = request.POST.get(f"qty:{line.pk}")
                    qty = line.qty_sent if raw in (None, "") else decimal_or_none(raw)
                    if qty is None or qty < 0 or qty > line.qty_sent:
                        raise ValueError(
                            f"{line.variant}: between 0 and {line.qty_sent:g} can arrive."
                        )
                    counted[line.pk] = qty
                receive_transfer(transfer, counted, user=request.user)
                audit.record("transfer.received", obj=transfer, ip=audit.client_ip(request))
                messages.success(request, f"{transfer.reference} received.")
        except (ValueError, InsufficientStock) as exc:
            messages.error(request, str(exc))
        return redirect("inventory:transfer_detail", pk=pk)

    candidates = []
    if transfer.status == TransferStatus.DRAFT and at_sender:
        # What the sending branch actually has: typing a product's database
        # id was the only way to add a line before.
        candidates = (
            StockItem.objects.select_related("variant__product", "variant__product__base_unit")
            .filter(branch=transfer.from_branch, qty_on_hand__gt=0,
                    variant__product__is_active=True)
            .exclude(variant__in=transfer.lines.values("variant"))
            .order_by("variant__product__name")
        )
    return render(
        request,
        "inventory/transfer_detail.html",
        {
            "transfer": transfer,
            "candidates": candidates,
            "can_send": transfer.status == TransferStatus.DRAFT and at_sender,
            "can_receive": (
                at_receiver
                and request.branch is not None
                and transfer.status == TransferStatus.SENT
                and transfer.to_branch_id == request.branch.pk
            ),
        },
    )


@login_required
@requires("stock.transfer", branch=branch_of(Transfer, "from_branch"))
@require_POST
def transfer_cancel(request, pk):
    """
    Cancel a transfer.

    Only before it is sent. Once stock has left a branch the movement exists
    and the way back is to send it the other way, not to erase it.
    """
    from django.db import transaction as db

    with db.atomic():
        # Locked and re-read: cancelling just as it was sent used to overwrite
        # "sent" and strand the stock that had already left.
        transfer = get_object_or_404(Transfer.objects.select_for_update(), pk=pk)
        if transfer.status != TransferStatus.DRAFT:
            messages.error(
                request,
                f"{transfer.reference} has already been sent. Transfer it back instead.",
            )
        else:
            transfer.status = TransferStatus.CANCELLED
            transfer.save(update_fields=["status", "updated_at"])
            audit.record("transfer.cancelled", obj=transfer, ip=audit.client_ip(request))
            messages.success(request, f"{transfer.reference} cancelled.")
    return redirect("inventory:transfer_list")


@login_required
@requires("stock.transfer", branch=branch_of(TransferLine, "transfer.from_branch"))
@require_POST
def transfer_line_delete(request, pk):
    from django.db import transaction as db

    with db.atomic():
        line = get_object_or_404(TransferLine.objects.select_related("transfer"), pk=pk)
        # The transfer itself locked: a line removed as it was sent took its
        # stock out of the sender and never arrived anywhere.
        transfer = Transfer.objects.select_for_update().get(pk=line.transfer_id)
        if transfer.status != TransferStatus.DRAFT:
            messages.error(request, "This transfer has already been sent.")
        else:
            line.delete()
            messages.success(request, "Line removed.")
    return redirect("inventory:transfer_detail", pk=transfer.pk)


@login_required
@requires("stock.count", branch=branch_of(StockCount))
@require_POST
def count_cancel(request, pk):
    """Abandon a count that was started by mistake. Applied ones stay."""
    count = get_object_or_404(StockCount, pk=pk)
    if count.status != CountStatus.OPEN:
        messages.error(request, "This count has already been applied.")
    else:
        count.status = CountStatus.CANCELLED
        count.save(update_fields=["status", "updated_at"])
        audit.record("count.cancelled", obj=count, ip=audit.client_ip(request))
        messages.success(request, f"{count.reference} cancelled.")
    return redirect("inventory:count_list")


def _add_transfer_line(request, transfer):
    from apps.catalog.models import Variant
    from apps.core.parsing import BadInput

    variant = Variant.objects.filter(pk=int_or(request.POST.get("variant"))).first()
    if variant is None:
        raise ValueError("Choose a product to send.")
    try:
        qty = parse_decimal(request.POST.get("qty"), "Quantity", positive=True)
    except BadInput as exc:
        raise ValueError(str(exc)) from None
    if not variant.product.base_unit.allows_decimal and qty != qty.to_integral_value():
        raise ValueError(f"{variant} is counted in whole {variant.product.base_unit.code}.")
    have = StockItem.objects.filter(branch=transfer.from_branch, variant=variant) \
        .values_list("qty_on_hand", flat=True).first() or 0
    if qty > have:
        raise ValueError(f"{transfer.from_branch} has {have:g} of {variant}; cannot send {qty:g}.")
    line = transfer.lines.filter(variant=variant).first()
    if line is not None:
        # A second add of the same product used to make a second line.
        line.qty_sent += qty
        if line.qty_sent > have:
            raise ValueError(f"{transfer.from_branch} has {have:g} of {variant}.")
        line.save(update_fields=["qty_sent"])
    else:
        TransferLine.objects.create(transfer=transfer, variant=variant, qty_sent=qty)


# --------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------

@login_required
@requires("stock.count")
def count_list(request):
    from apps.core.listing import paginate

    counts = (
        StockCount.objects.filter(branch=request.branch)
        .select_related("applied_by")
        .prefetch_related("lines")
        .order_by("-created_at")
    )
    status = request.GET.get("status", "")
    if status in CountStatus.values:
        counts = counts.filter(status=status)
    else:
        status = ""
    return render(request, "inventory/counts.html", {
        **paginate(request, counts), "status": status, "statuses": CountStatus.choices,
        "open_count": StockCount.objects.filter(branch=request.branch,
                                                status=CountStatus.OPEN).first(),
    })


@login_required
@requires("stock.count")
@require_POST
def count_create(request):
    """
    Start a count.

    The system quantity is frozen onto each line now, so that what the counter
    is measured against is what the system believed when they started, not
    what it believes after an hour of trading.
    """
    if request.branch is None:
        messages.error(request, "Choose the branch you are counting first.")
        return redirect("inventory:count_list")
    already = StockCount.objects.filter(branch=request.branch, status=CountStatus.OPEN).first()
    if already is not None:
        # Two open counts froze two different "system" figures, and applying
        # both counted every difference twice.
        messages.info(request, f"{already.reference} is still open. Finish or cancel it first.")
        return redirect("inventory:count_detail", pk=already.pk)
    count = save_with_number(
        StockCount(branch=request.branch),
        field="reference",
        generate=lambda: next_reference(StockCount, "SC"),
    )
    items = StockItem.objects.select_related("variant").filter(branch=request.branch)
    StockCountLine.objects.bulk_create(
        [
            StockCountLine(
                tenant=request.tenant,
                count=count,
                variant=item.variant,
                system_qty=item.qty_on_hand,
                unit_cost=item.avg_cost,
            )
            for item in items
        ]
    )
    return redirect("inventory:count_detail", pk=count.pk)


@login_required
@requires("stock.count", branch=branch_of(StockCount))
def count_detail(request, pk):
    count = get_object_or_404(
        StockCount.objects.prefetch_related("lines__variant__product"), pk=pk
    )
    if not request.membership.covers_branch(count.branch):
        raise PermissionDenied("This count belongs to a branch you do not work in.")

    if request.method == "POST":
        if count.status != CountStatus.OPEN:
            # Applied or cancelled counts are history; editing them rewrote it.
            messages.error(request, f"{count.reference} is closed and cannot be changed.")
            return redirect("inventory:count_detail", pk=pk)
        if request.POST.get("action") == "apply":
            # Both buttons share one form: save what was typed before applying,
            # or the numbers on the screen are thrown away and the count closes.
            changed = []
            for line in count.lines.all():
                raw = request.POST.get(f"qty:{line.pk}")
                if raw is None:
                    continue
                qty = decimal_or_none(raw)
                if raw.strip() and (qty is None or qty < 0):
                    messages.error(request, f"{line.variant}: not a number. Nothing applied.")
                    return redirect("inventory:count_detail", pk=pk)
                if qty != line.counted_qty:
                    line.counted_qty = qty
                    changed.append(line)
            StockCountLine.objects.bulk_update(changed, ["counted_qty"])

            # A count is an adjustment of every line at once. Without this a
            # clerk capped at 50,000 of write-offs typed 0 and wrote off
            # 240,000 unchallenged.
            from apps.inventory.valuation import limit_value

            worth = sum(
                (limit_value(line.variant, line.variance, line.unit_cost)
                 for line in count.lines.select_related("variant").exclude(counted_qty=None)
                 if line.variance),
                Decimal("0"),
            )
            losses = sum(
                (limit_value(line.variant, line.variance, line.unit_cost)
                 for line in count.lines.select_related("variant").exclude(counted_qty=None)
                 if line.variance and line.variance < 0),
                Decimal("0"),
            )
            decision = request.membership.check_permission(
                "stock.adjust", branch=count.branch, value=worth)
            if decision.allowed and losses:
                # Stock that is simply gone is a write-off, and has its own,
                # usually tighter, ceiling.
                decision = request.membership.check_permission(
                    "stock.wastage", branch=count.branch, value=losses)
            if not decision.allowed:
                messages.error(request, f"The differences are worth {worth:,.0f}. "
                                        f"{decision.reason} Your counts are saved; ask a "
                                        "manager to apply them.")
                return redirect("inventory:count_detail", pk=pk)
            try:
                applied = apply_count(count, user=request.user)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                audit.record("stock.counted", obj=count, ip=audit.client_ip(request))
                messages.success(request, f"{applied} differences applied.")
            return redirect("inventory:count_detail", pk=pk)

        bad, changed = [], []
        for line in count.lines.all():
            raw = request.POST.get(f"qty:{line.pk}")
            if raw is None:
                continue
            qty = decimal_or_none(raw)
            if raw.strip() and (qty is None or qty < 0):
                bad.append(str(line.variant))
                continue
            if qty != line.counted_qty:
                line.counted_qty = qty
                changed.append(line)
        # One statement, not one UPDATE per line of a thousand-line count.
        StockCountLine.objects.bulk_update(changed, ["counted_qty"])
        if bad:
            messages.error(request, "Saved, except these, which are not numbers: "
                                    + ", ".join(bad[:5]) + ("…" if len(bad) > 5 else ""))
        else:
            messages.success(request, "Count saved.")
        return redirect("inventory:count_detail", pk=pk)

    return render(
        request,
        "inventory/count_detail.html",
        {"count": count, "editable": count.status == CountStatus.OPEN},
    )


@login_required
@requires("stock.batches")
def batch_list(request):
    days = min(max(int_or(request.GET.get("days"), 30), 1), 3650)
    return render(
        request,
        "inventory/batches.html",
        {
            "batches": expiring_soon(days=days, branch=request.branch)
            .select_related("variant__product")
            .filter(expiry_date__gte=timezone.localdate()),
            "days": days,
            # This branch's, and only what is still on the shelf: every
            # branch's, sold-out ones included, used to be listed.
            "expired": expiring_soon(days=0, branch=request.branch)
            .select_related("variant__product")
            .filter(expiry_date__lt=timezone.localdate())[:100],
        },
    )

"""
Till screens.

``/pos/`` is the one client-side page in the system: Django serves it, Tailwind
styles it, and once loaded it holds the catalogue and the open basket in
IndexedDB so it keeps selling with no connection. Everything else here is
ordinary server-rendered Django.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.features import OFFLINE_POS
from apps.core.parsing import BadInput, decimal_or_none, int_or, parse_decimal
from apps.org.models import Register
from apps.pos.models import (
    CashMovementKind,
    PaymentMethod,
    Sale,
    SaleLine,
    SaleStatus,
    Shift,
    ShiftStatus,
)
from apps.pos.services import (
    ShiftAlreadyOpen,
    close_shift,
    create_return,
    open_shift,
    quantum,
    record_cash_movement,
    void_sale,
)


def _open_shift_for(request, branch=None):
    """This person's open drawer, at `branch` (their current one by default)."""
    return Shift.objects.filter(
        branch=branch or request.branch, user=request.user, status=ShiftStatus.OPEN
    ).select_related("register").first()


def service_worker(request):
    """
    The till's offline copy-keeper, served from /pos/ so it may look after /pos/.

    A service worker only controls pages under its own path, and static files
    live under /static/ -- so it is read from there and served from here.
    """
    from django.contrib.staticfiles import finders
    from django.http import HttpResponse

    path = finders.find("js/sw.js")
    with open(path, encoding="utf-8") as handle:
        response = HttpResponse(handle.read(), content_type="application/javascript")
    # Browsers check for a new worker on every visit; never let a proxy pin an old one.
    response["Cache-Control"] = "no-cache"
    return response


@login_required
@requires("pos.operate")
def till(request):
    """
    The selling screen.

    Permissions are resolved once here and handed to the page, because the
    till has to keep enforcing them when it cannot reach the server.
    """
    shift = _open_shift_for(request)
    if shift is None:
        return redirect("pos:shift_open")

    membership = request.membership
    discount = membership.check_permission("pos.discount")
    mobile_pay = membership.check_permission("pos.mobile_payment")

    config = {
        "branch": request.branch.name,
        "register": shift.register.name,
        "shift_id": shift.pk,
        # The till keeps its data per shop and person; these name it.
        "tenant_id": request.tenant.pk,
        "offline": request.tenant.has_feature(OFFLINE_POS),
        "user_id": request.user.pk,
        "currency": request.tenant.currency,
        # The smallest amount this shop's money comes in. The till has to
        # round each line the way the server does, or the cashier asks for
        # one figure and the system records another.
        "money_step": float(quantum(request.tenant.currency)),
        "cashier": request.user.name,
        "permissions": {
            "discount": bool(discount),
            "discount_limit": float(discount.limit) if discount.limit else None,
            "price_override": membership.can("pos.price_override"),
            "open_item": membership.can("pos.open_item"),
            "open_item_limit": (
                float(membership.check_permission("pos.open_item").limit)
                if membership.check_permission("pos.open_item").limit else None
            ),
            "void": membership.can("pos.void"),
            "refund": membership.can("pos.refund"),
            "credit": membership.can("credit.grant"),
            # The ceiling too, not only yes/no: the till took an over-limit
            # sale on account, the goods left, and the server then refused it.
            "credit_limit": (
                float(membership.check_permission("credit.grant").limit)
                if membership.check_permission("credit.grant").limit else None
            ),
            "mobile_payment": bool(mobile_pay),
        },
        "endpoints": {
            "catalog": "/api/v1/sync/catalog/",
            "customers": "/api/v1/sync/customers/",
            "sales": "/api/v1/sync/sales/",
            "status": "/api/v1/sync/status/",
            "cart_pull": "/api/v1/sync/carts/",
            "devices": "/api/v1/sync/devices/",
        },
    }

    return render(
        request,
        "pos/till.html",
        {"shift": shift, "config": json.dumps(config), "tiles": _tiles(request),
         "tile_categories": _tile_categories(request),
         "offline": config["offline"]},
    )


def _tile_categories(request):
    from apps.catalog.services import tile_categories

    return tile_categories(_tiles(request))


def _tiles(request):
    from apps.catalog.services import tiles

    return tiles(branch=request.branch)


@login_required
@requires("pos.operate")
def shift_open(request):
    existing = _open_shift_for(request)
    if existing is not None:
        return redirect("pos:till")

    registers = Register.objects.filter(branch=request.branch, is_active=True)

    if request.method == "POST":
        # From this branch only: any register pk used to be accepted.
        register = get_object_or_404(
            Register, pk=int_or(request.POST.get("register")), branch=request.branch,
            is_active=True,
        )
        try:
            shift = open_shift(
                register=register,
                opening_float=max(decimal_or_none(request.POST.get("opening_float")) or 0, 0),
            )
        except ShiftAlreadyOpen as exc:
            messages.error(request, str(exc))
            return redirect("pos:shift_open")

        audit.record("shift.opened", obj=shift, ip=audit.client_ip(request))
        return redirect("pos:till")

    # What each till is doing right now, and what it last closed with. The
    # page was one small box in the middle of an empty screen, and the thing
    # a person actually wants to know before they start -- is anyone else on
    # this till, and how much was in the drawer last night -- was nowhere.
    registers = list(registers)
    running = {
        shift.register_id: shift
        for shift in Shift.objects.filter(register__in=registers, status=ShiftStatus.OPEN)
        .select_related("user")
    }
    last = {}
    for shift in (Shift.objects.filter(register__in=registers, closed_at__isnull=False)
                  .select_related("user").order_by("register_id", "-closed_at")):
        last.setdefault(shift.register_id, shift)

    tills = [{"register": r, "running": running.get(r.pk), "last": last.get(r.pk)}
             for r in registers]
    free = [t for t in tills if t["running"] is None]
    return render(request, "pos/shift_open.html", {
        "registers": registers, "tills": tills, "free": free,
        # Whatever the drawer was left with is the sensible float to suggest.
        "suggested": (free[0]["last"].counted_cash if free and free[0]["last"] else 0),
    })


@login_required
@requires("cashup.perform")
def shift_close(request):
    shift = _open_shift_for(request)
    if shift is None:
        messages.info(request, "You have no open shift.")
        return redirect("core:dashboard")

    expected = shift.compute_expected_cash()

    if request.method == "POST":
        try:
            counted = parse_decimal(request.POST.get("counted_cash"), "Counted cash", minimum=0)
        except BadInput as exc:
            messages.error(request, str(exc))
            return redirect("pos:shift_close")
        close_shift(shift, counted_cash=counted, note=request.POST.get("note", "")[:200])
        audit.record(
            "shift.closed", obj=shift,
            after={"expected": str(shift.expected_cash),
                   "counted": str(shift.counted_cash),
                   "variance": str(shift.variance)},
            ip=audit.client_ip(request),
        )
        return redirect("pos:shift_report", pk=shift.pk)

    return render(
        request,
        "pos/shift_close.html",
        {
            "shift": shift,
            "expected": expected,
            "sales_count": shift.sales.filter(status=SaleStatus.COMPLETED).count(),
            "cash_taken": shift.cash_taken(),
            "cash_refunded": shift.cash_refunded(),
            "movements": shift.cash_movements.all(),
        },
    )


@login_required
@requires("cashup.approve", branch=branch_of(Shift))
@require_POST
def shift_force_close(request, pk):
    """
    Close a drawer somebody walked away from.

    `open_shift` refuses a second shift on a register, so one left open kept
    that till locked until its cashier came back. A manager counts the drawer
    and closes it; the variance still lands on the cashier who left it.
    """
    shift = get_object_or_404(Shift.objects.select_related("register", "user"), pk=pk)
    if not shift.is_open:
        messages.info(request, "That shift is already closed.")
        return redirect("finance:cashups")
    try:
        counted = parse_decimal(request.POST.get("counted_cash"), "Counted cash", minimum=0)
    except BadInput as exc:
        messages.error(request, str(exc))
        return redirect("finance:cashups")
    note = f"Closed by {request.user.name}. " + request.POST.get("note", "")
    close_shift(shift, counted_cash=counted, note=note.strip()[:200])
    audit.record("shift.force_closed", obj=shift,
                 after={"cashier": shift.user.email, "counted": str(counted),
                        "variance": str(shift.variance)},
                 ip=audit.client_ip(request))
    messages.success(request, f"{shift.register} is free again. Variance {shift.variance:,.0f}.")
    return redirect("pos:shift_report", pk=shift.pk)


@login_required
@requires("pos.operate", branch=branch_of(Shift))
def shift_report(request, pk):
    """The Z-report: what this shift sold, took and was short by."""
    shift = get_object_or_404(Shift.objects.select_related("register", "user", "branch", "approved_by"), pk=pk)
    # Your own drawer, or anybody's if checking drawers is your job. Any
    # cashier could read any other cashier's Z-report before.
    if shift.user_id != request.user.pk and not request.membership.can(
        "cashup.approve", branch=shift.branch
    ):
        raise PermissionDenied("Only your own shift, unless you approve cash-ups.")
    # Every sale that took money, refunded or not -- the same set the drawer's
    # expected cash counts. Refunds are shown separately below; leaving
    # refunded sales out made the Z-report disagree with the cash-up.
    sales = shift.sales.exclude(status=SaleStatus.VOIDED)

    by_method = (
        sales.values("payments__method")
        .annotate(total=Sum("payments__amount"), count=Count("id"))
        .order_by("-total")
    )

    return render(
        request,
        "pos/shift_report.html",
        {
            "shift": shift,
            "sales": sales.order_by("-sold_at")[:50],
            "totals": sales.aggregate(
                count=Count("id"), value=Sum("total"), tax=Sum("tax_total")
            ),
            "by_method": [
                {"label": dict(PaymentMethod.choices).get(row["payments__method"], row["payments__method"] or "—"),
                 "total": row["total"], "count": row["count"],
                 "on_account": row["payments__method"] == PaymentMethod.CREDIT}
                for row in by_method
            ],
            "refunds": shift.returns.aggregate(
                total=Sum("total"), cash=Sum("cash_amount"), count=Count("id")
            ),
            "movements": shift.cash_movements.all(),
            "expected": shift.expected_cash if shift.closed_at else shift.compute_expected_cash(),
            "cash_taken": shift.cash_taken(),
            "money_in": sales.aggregate(v=Sum("payments__amount", filter=~Q(payments__method=PaymentMethod.CREDIT)))["v"] or 0,
        },
    )


def _movement_value(request, **kwargs):
    return decimal_or_none(request.POST.get("amount")) or 0


@login_required
@requires("cash.movement", value=_movement_value)
@require_POST
def cash_movement(request):
    shift = _open_shift_for(request)
    if shift is None:
        messages.error(request, "Open your till first: money in or out goes through a drawer.")
        return redirect("pos:shift_open")

    kind = request.POST.get("kind", CashMovementKind.PAY_OUT)
    if kind not in CashMovementKind.values:
        messages.error(request, "Choose money in, money out or banked.")
        return redirect("pos:shift_close")
    try:
        amount = parse_decimal(request.POST.get("amount"), "Amount", positive=True, places=2)
    except BadInput as exc:
        messages.error(request, str(exc))
        return redirect("pos:shift_close")
    reason = request.POST.get("reason", "").strip()[:200]
    if not reason:
        messages.error(request, "Say what the money was for.")
        return redirect("pos:shift_close")
    # The sign comes from the kind, never from what was typed: a "money in"
    # of -50,000 used to hide 50,000 missing from the drawer.
    if kind in {CashMovementKind.PAY_OUT, CashMovementKind.DROP}:
        amount = -amount

    if amount < 0 and -amount > shift.compute_expected_cash():
        # More than the drawer should hold: the count would go negative and
        # a real shortfall would be hidden behind it.
        messages.error(request, f"Your drawer should only hold "
                                f"{shift.compute_expected_cash():,.0f}.")
        return redirect("pos:shift_close")

    movement = record_cash_movement(shift, kind=kind, amount=amount, reason=reason)
    audit.record("cash.moved", obj=movement, ip=audit.client_ip(request))
    messages.success(request, f"{movement.get_kind_display()}: {abs(amount):,.0f} recorded.")
    return redirect("pos:shift_close")


# --------------------------------------------------------------------------
# Sales history
# --------------------------------------------------------------------------

@login_required
@requires("report.sales")
def sale_list(request):
    from django.db.models import Count, Q, Sum

    from apps.core.listing import paginate
    from apps.core.parsing import date_or

    branches = request.membership.branches(include_closed=True)
    sales = (
        Sale.objects.select_related("user", "customer", "branch")
        .prefetch_related("payments")
        .filter(branch__in=branches)
        .order_by("-sold_at")
    )
    if request.GET.get("review") == "1":
        sales = sales.filter(needs_review=True)
    floor = request.tenant.history_start()
    if floor:
        sales = sales.filter(sold_at__date__gte=floor)

    term = request.GET.get("q", "").strip()
    if term:
        sales = sales.filter(Q(number__icontains=term) | Q(customer__name__icontains=term))
    status = request.GET.get("status", "")
    if status:
        sales = sales.filter(status=status)
    start, end = date_or(request.GET.get("from")), date_or(request.GET.get("to"))
    if start:
        sales = sales.filter(sold_at__date__gte=start)
    if end:
        sales = sales.filter(sold_at__date__lte=end)
    cashier = int_or(request.GET.get("cashier"), 0)
    if cashier:
        sales = sales.filter(user_id=cashier)
    branch_id = int_or(request.GET.get("branch"), 0)
    if branch_id:
        sales = sales.filter(branch_id=branch_id)
    method = request.GET.get("method", "")
    if method in PaymentMethod.values:
        sales = sales.filter(payments__method=method).distinct()

    totals = sales.exclude(status=SaleStatus.VOIDED).aggregate(n=Count("id"), value=Sum("total"))
    from apps.accounts.models import User

    context = {
        **paginate(request, sales),
        "q": term, "status": status, "statuses": SaleStatus.choices,
        "start": start, "end": end, "cashier": cashier, "branch_id": branch_id, "method": method,
        "methods": PaymentMethod.choices, "totals": totals,
        "branches": branches if branches.count() > 1 else None,
        "cashiers": User.objects.filter(sales__branch__in=branches).distinct().order_by("name"),
    }
    context["sales"] = context["page"].object_list
    if request.htmx:
        return render(request, "pos/_sale_rows.html", context)
    return render(request, "pos/sale_list.html", context)


@login_required
@requires("report.sales", branch=branch_of(Sale))
def sale_detail(request, pk):
    sale = get_object_or_404(
        Sale.objects.select_related("user", "customer", "branch", "register")
        .prefetch_related("lines__return_lines", "payments", "returns__lines"),
        pk=pk,
    )
    from apps.accounts.models import AuditLog

    voided = None
    if sale.voided_at:
        voided = AuditLog.objects.select_related("user", "authorised_by").filter(
            action="sale.voided", object_type="Sale", object_id=str(sale.pk)
        ).first()
    return render(
        request,
        "pos/sale_detail.html",
        {"sale": sale, "can_see_cost": request.membership.can("product.view_cost"),
         "returns": sale.returns.select_related("user").prefetch_related("lines__sale_line")
         .order_by("created_at"),
         "voided": voided, "open_shift": _open_shift_for(request)},
    )


@login_required
@requires("pos.void", value=lambda r, **kw: _sale_total(kw.get("pk")), branch=branch_of(Sale))
@require_POST
def sale_void(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    reason = request.POST.get("reason", "").strip()[:200]
    if not reason:
        messages.error(request, "A reason is required to void a sale.")
        return redirect("pos:sale_detail", pk=pk)
    if sale.shift_id and sale.shift.closed_at is not None:
        # Its cash-up is counted and closed. Voiding would change a closed
        # drawer's figures after the fact; a refund goes through today's.
        messages.error(request, f"{sale.number}'s shift is already closed and counted. "
                                "Refund it instead, from an open till.")
        return redirect("pos:sale_detail", pk=pk)

    cash_back = sale.payments.filter(method=PaymentMethod.CASH).aggregate(
        t=Sum("amount"))["t"] or 0
    from django.db import transaction

    try:
        with transaction.atomic():
            void_sale(sale, reason=reason, user=request.user,
                      authorised_by=getattr(request, "authorised_by", None))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("pos:sale_detail", pk=pk)

    audit.record(
        "sale.voided", obj=sale, after={"reason": reason}, ip=audit.client_ip(request)
    )
    messages.success(
        request,
        f"{sale.number} voided and stock put back."
        + (" Its cash was never counted in a drawer, so no cash-up changes: hand the money "
           "back from the till as it stands." if sale.shift_id is None and cash_back > 0 else ""),
    )
    return redirect("pos:sale_detail", pk=pk)


def _refund_value(request, pk):
    """
    What this refund is worth, for the refund limit: the lines being
    returned, not the whole sale. Opening the page checks nothing yet.
    """
    if request.method != "POST":
        return None
    from decimal import Decimal

    from apps.pos.models import Return

    total = Decimal("0")
    for line in SaleLine.objects.filter(sale_id=pk):
        qty = decimal_or_none(request.POST.get(f"qty:{line.pk}")) or 0
        if qty > 0 and line.qty:
            total += line.line_total / line.qty * qty
    # Plus what this sale has already had back: a 2,000,000 sale could be
    # refunded in four 500,000 steps under a 500,000 limit.
    already = Return.objects.filter(sale_id=pk).aggregate(t=Sum("total"))["t"] or 0
    return total + already


def _sale_total(pk):
    sale = Sale.objects.filter(pk=pk).values_list("total", flat=True).first()
    return sale or 0


@login_required
@requires("pos.refund", value=lambda r, **kw: _refund_value(r, kw.get("pk")), branch=branch_of(Sale))
def sale_return(request, pk):
    sale = get_object_or_404(
        Sale.objects.prefetch_related("lines__return_lines"), pk=pk
    )

    if request.method == "POST":
        quantities = {}
        for line in sale.lines.all():
            raw = request.POST.get(f"qty:{line.pk}")
            if raw:
                qty = decimal_or_none(raw)
                if qty is None or qty < 0:
                    messages.error(request, f"The quantity for {line.description} is not a number.")
                    return redirect("pos:sale_return", pk=pk)
                quantities[line.pk] = qty

        method = request.POST.get("method", PaymentMethod.CASH)
        drawer = _open_shift_for(request)
        if method == PaymentMethod.CASH and drawer is None:
            # Otherwise the refund came out of no drawer's expected cash, and
            # the money simply vanished from the cash-up.
            messages.error(request, "A cash refund comes out of a drawer. Open your till first.")
            return redirect("pos:sale_return", pk=pk)
        try:
            doc = create_return(
                sale,
                quantities,
                reason=request.POST.get("reason", "")[:200],
                method=method,
                restock=request.POST.get("restock") == "on",
                user=request.user,
                shift=drawer,
                require_drawer=True,
                authorised_by=getattr(request, "authorised_by", None),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("pos:sale_return", pk=pk)

        audit.record("sale.returned", obj=doc, ip=audit.client_ip(request))
        messages.success(request, f"{doc.total} refunded on {doc.number}.")
        return redirect(_sale_home(request, sale))

    return render(request, "pos/sale_return.html",
                  {"sale": sale, "back": _sale_home(request, sale)})


def _sale_home(request, sale):
    """
    Where to go after acting on a sale: its page for whoever may see the
    shop's sales, the dashboard (with its own shift list) for till staff.
    """
    if request.membership.can("report.sales", branch=sale.branch):
        return reverse("pos:sale_detail", args=[sale.pk])
    return reverse("core:dashboard")


@login_required
@requires("pos.reprint", branch=branch_of(Sale))
def receipt(request, pk):
    """The paper receipt. Printed from the browser to a thermal printer."""
    sale = get_object_or_404(
        Sale.objects.select_related("branch", "user", "customer")
        .prefetch_related("lines", "payments"),
        pk=pk,
    )
    from apps.org.models import TenantSettings

    return render(
        request,
        "pos/receipt.html",
        {
            "sale": sale,
            "settings": TenantSettings.objects.first(),
            "printed_at": timezone.now(),
            "back": _sale_home(request, sale),
        },
    )


@login_required
@requires("fiscal.manage")
def fiscal_receipts(request):
    """
    The revenue authority's copy of every sale.

    Submission needs the internet and selling does not, so receipts queue
    while a shop is offline. This is where an owner sees what is still
    waiting and what failed -- the difference between compliant and hoping.
    """
    from apps.core.listing import paginate
    from apps.pos.models import FiscalReceipt, FiscalStatus

    # Only the branches this person looks after, like every other list.
    mine = FiscalReceipt.objects.filter(
        sale__branch__in=request.membership.branches(include_closed=True)
    )
    rows = mine.select_related("sale").order_by("-created_at")

    status = request.GET.get("status", "")
    if status not in FiscalStatus.values:
        status = ""
    if status:
        rows = rows.filter(status=status)

    counts = {
        value: mine.filter(status=value).count()
        for value, _label in FiscalStatus.choices
    }

    if request.method == "POST" and request.POST.get("action") == "retry":
        reset = mine.filter(status=FiscalStatus.FAILED).update(
            status=FiscalStatus.PENDING, attempts=0, error=""
        )
        audit.record("fiscal.requeued", after={"count": reset},
                     ip=audit.client_ip(request))
        messages.success(request, f"{reset} receipt{'' if reset == 1 else 's'} re-queued.")
        return redirect("pos:fiscal_receipts")

    from apps.org.models import TenantSettings

    settings_row = TenantSettings.objects.first()

    return render(
        request,
        "pos/fiscal.html",
        {
            **paginate(request, rows),
            "status": status,
            "statuses": FiscalStatus.choices,
            "counts": counts,
            "provider": settings_row.fiscal_provider if settings_row else "",
        },
    )

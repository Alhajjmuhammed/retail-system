"""
Reports.

Read-only, and every figure comes from the snapshots already stored -- cost
from ``SaleLine.unit_cost``, never from today's price. A margin report that
changes when a supplier raises a price is worse than no report.
"""

import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, F, Max, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.core.decorators import requires
from apps.core.parsing import date_or
from apps.finance.models import Expense
from apps.inventory.models import StockItem
from apps.pos.models import Return, ReturnLine, SaleLine, Shift
from apps.reports.services import COUNTED, refunds_for, sales_in

PRESETS = [("today", "Today"), ("7d", "Last 7 days"), ("30d", "Last 30 days"),
           ("month", "This month"), ("last_month", "Last month")]


def _range(request):
    """
    The period asked for. A typo in the address bar used to be a 500 page;
    now anything unreadable falls back to the last 30 days.
    """
    today = timezone.localdate()
    preset = request.GET.get("preset", "")
    if preset == "today":
        start, end = today, today
    elif preset == "7d":
        start, end = today - timedelta(days=6), today
    elif preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        start = date_or(request.GET.get("from"), today - timedelta(days=29))
        end = date_or(request.GET.get("to"), today)
    if start > end:
        start, end = end, start
    floor = request.tenant.history_start()
    if floor and start < floor:
        start = max(floor, start)
        if end < start:
            end = start
    return start, end


def _branches(request):
    """
    Only branches this person works in -- never the whole business by default.

    Closed branches included: their past sales still happened. ``?branch=``
    narrows it to one of them, never widens it.
    """
    from apps.core.parsing import int_or

    mine = request.membership.branches(include_closed=True)
    chosen = int_or(request.GET.get("branch"))
    if chosen:
        return mine.filter(pk=chosen)
    return mine


def _context(request, start, end, **extra):
    """What every report page needs for its period and branch pickers."""
    from urllib.parse import urlencode

    from apps.core.parsing import int_or

    keep = {"from": start.isoformat(), "to": end.isoformat()}
    branch = int_or(request.GET.get("branch"))
    if branch:
        keep["branch"] = branch
    return {
        "start": start, "end": end, "presets": PRESETS,
        "preset": request.GET.get("preset", ""),
        "all_branches": request.membership.branches(include_closed=True),
        "branch_id": branch, "keep": urlencode(keep), **extra,
    }


# Which sales count, and what a refund does to them, is defined once in
# apps.reports.services -- the dashboard reads the same definitions. They
# used to be written out separately here and disagreed about fully refunded
# sales.


def _sales(request, start, end):
    return sales_in(_branches(request), start, end)


def _refunds(request, start, end):
    """Money given back on sales made in the period."""
    return refunds_for(_sales(request, start, end))


@login_required
@requires("report.sales")
def index(request):
    start, end = _range(request)
    sales = _sales(request, start, end)

    from apps.pos.models import PaymentMethod

    daily = list(
        sales.annotate(day=TruncDate("sold_at"))
        .values("day")
        .annotate(count=Count("id"), value=Sum("total"), tax=Sum("tax_total"))
        .order_by("day")
    )
    back = {
        row["day"]: row["total"]
        for row in Return.objects.filter(sale__in=sales)
        .annotate(day=TruncDate("sale__sold_at")).values("day")
        .annotate(total=Sum("total"))
    }
    for row in daily:
        row["refunded"] = back.get(row["day"]) or Decimal("0")
        row["net"] = (row["value"] or Decimal("0")) - row["refunded"]

    labels = dict(PaymentMethod.choices)
    by_method = [
        {"label": labels.get(row["payments__method"], row["payments__method"] or "—"),
         "total": row["total"], "on_account": row["payments__method"] == PaymentMethod.CREDIT}
        for row in sales.values("payments__method")
        .annotate(total=Sum("payments__amount")).order_by("-total")
        if row["payments__method"]
    ]

    totals = sales.aggregate(count=Count("id"), value=Sum("total"), tax=Sum("tax_total"))
    refunded = _refunds(request, start, end)
    totals["refunded"] = refunded
    totals["net"] = (totals["value"] or Decimal("0")) - refunded
    totals["average"] = (totals["net"] / totals["count"]) if totals["count"] else 0

    return render(
        request,
        "reports/index.html",
        _context(request, start, end, daily=daily, by_method=by_method, totals=totals,
                 peak=max((row["net"] for row in daily), default=0)),
    )


@login_required
@requires("report.margin")
def margin(request):
    start, end = _range(request)
    returned = (
        ReturnLine.objects.filter(sale_line=OuterRef("pk"))
        .values("sale_line")
        .annotate(total=Sum("qty"))
        .values("total")
    )
    lines = SaleLine.objects.filter(
        sale__branch__in=_branches(request),
        sale__status__in=COUNTED,
        sale__sold_at__date__gte=start,
        sale__sold_at__date__lte=end,
        qty__gt=0,
    ).annotate(
        kept=F("qty") - Coalesce(
            Subquery(returned, output_field=DecimalField()), Decimal("0"),
            output_field=DecimalField(),
        ),
    )

    # Only what stayed sold counts: a part-refunded line used to report its
    # whole revenue and profit.
    # By product, not by the text on the receipt: two products sharing a
    # name merged into one row and a renamed product split into two.
    rows = (
        lines.values("variant")
        .annotate(
            item=Max("description"),
            # Not `qty`: that alias would shadow the model field and F("qty")
            # below would then resolve to the aggregate instead of the column.
            qty_sold=Sum("kept"),
            revenue=Sum(
                (F("line_total") - F("tax_amount")) * F("kept") / F("qty"),
                output_field=DecimalField(),
            ),
            cost=Sum(F("kept") * F("unit_cost"), output_field=DecimalField()),
        )
        .order_by("-revenue")
    )
    product_count = rows.count()
    rows = rows[:100]

    enriched = []
    for row in rows:
        revenue = row["revenue"] or Decimal("0")
        cost = row["cost"] or Decimal("0")
        profit = revenue - cost
        enriched.append(
            {
                **row,
                "profit": profit,
                "pct": (profit / revenue * 100) if revenue else Decimal("0"),
            }
        )

    expenses = Expense.objects.filter(
        branch__in=_branches(request), spent_at__gte=start, spent_at__lte=end
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0")))["total"]

    # Over every product, not only the hundred shown.
    everything = lines.aggregate(
        revenue=Sum((F("line_total") - F("tax_amount")) * F("kept") / F("qty"),
                    output_field=DecimalField()),
        cost=Sum(F("kept") * F("unit_cost"), output_field=DecimalField()),
    )
    gross = (everything["revenue"] or Decimal("0")) - (everything["cost"] or Decimal("0"))

    return render(
        request,
        "reports/margin.html",
        _context(request, start, end, rows=enriched, product_count=product_count,
                 revenue=everything["revenue"] or Decimal("0"),
                 gross=gross, expenses=expenses, net=gross - expenses),
    )


@login_required
@requires("report.stock")
def stock_value(request):
    from apps.core.listing import paginate

    items = (
        StockItem.objects.select_related("variant__product", "branch")
        .filter(branch__in=_branches(request), qty_on_hand__gt=0)
        .annotate(value=F("qty_on_hand") * F("avg_cost"))
        .order_by("-value")
    )
    term = request.GET.get("q", "").strip()
    if term:
        items = items.filter(variant__product__name__icontains=term)
    today = timezone.localdate()
    return render(
        request,
        "reports/stock_value.html",
        {
            **_context(request, today, today),
            **paginate(request, items),
            "q": term,
            # Quantities for everyone with the report; money only for those
            # who may see cost prices -- the Stock page already hid them.
            "can_see_cost": request.membership.can("product.view_cost"),
            "no_cost": items.filter(avg_cost=0).count(),
            "total": items.aggregate(
                total=Coalesce(Sum(F("qty_on_hand") * F("avg_cost")), Decimal("0"))
            )["total"],
        },
    )


@login_required
@requires("report.staff")
def staff(request):
    """
    Per cashier: what they sold, and what their drawer was short by.

    The second column is the reason this report exists.
    """
    start, end = _range(request)
    sales = _sales(request, start, end)

    # By person, not by name: two Jumas used to merge into one row. And a
    # cashier with a short drawer but no sales still belongs in this report.
    sold = {
        row["user"]: row
        for row in sales.values("user", "user__name")
        .annotate(count=Count("id"), value=Sum("total"))
    }
    variances = {
        row["user"]: row
        for row in Shift.objects.filter(
            branch__in=_branches(request),
            closed_at__date__gte=start, closed_at__date__lte=end,
        )
        .values("user", "user__name")
        .annotate(variance=Sum("variance"), shifts=Count("id"))
    }
    refunded = {
        row["sale__user"]: row["total"]
        for row in Return.objects.filter(sale__in=sales).values("sale__user")
        .annotate(total=Sum("total"))
    }
    combined = []
    for user_id in set(sold) | set(variances):
        a, b = sold.get(user_id, {}), variances.get(user_id, {})
        value = a.get("value") or Decimal("0")
        back = refunded.get(user_id) or Decimal("0")
        combined.append({
            "user__name": a.get("user__name") or b.get("user__name"),
            "count": a.get("count", 0), "value": value - back, "refunded": back,
            "variance": b.get("variance"), "shifts": b.get("shifts", 0),
        })
    combined.sort(key=lambda row: row["value"], reverse=True)

    return render(
        request,
        "reports/staff.html",
        _context(request, start, end, rows=combined),
    )


@login_required
@requires("report.export")
def export(request):
    start, end = _range(request)
    back = Return.objects.filter(sale=OuterRef("pk")).values("sale") \
        .annotate(t=Sum("total")).values("t")
    sales = (
        _sales(request, start, end)
        .select_related("user", "customer", "branch")
        .annotate(refunded=Coalesce(Subquery(back, output_field=DecimalField()), Decimal("0")))
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="sales-{start}-{end}.csv"'

    writer = csv.writer(response)
    writer.writerow(
        ["Receipt", "Date", "Branch", "Cashier", "Customer", "Subtotal",
         "Discount", "VAT", "Total", "Refunded", "Kept", "Status"]
    )
    for sale in sales.order_by("sold_at").iterator(chunk_size=500):
        writer.writerow([
            sale.number, timezone.localtime(sale.sold_at).strftime("%Y-%m-%d %H:%M"),
            _cell(sale.branch.name), _cell(sale.user.name),
            _cell(sale.customer.name if sale.customer else ""),
            sale.subtotal, sale.discount_total, sale.tax_total, sale.total,
            sale.refunded, sale.total - sale.refunded, sale.get_status_display(),
        ])
    return response


def _cell(text):
    """A spreadsheet runs a cell starting with = + - @ as a formula."""
    from apps.catalog.imports import safe_cell

    return safe_cell(text)

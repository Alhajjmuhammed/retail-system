"""
What the shop took, and what it kept.

These live here rather than in a view because the dashboard and the reports
answer the same questions and have to give the same answers. They disagreed
once already -- over fully refunded sales -- and a shop that reads one figure
on the front page and a different one on the report stops trusting both.

Every function takes the branches the reader may see. None of them consults
the request, so a management command or a test can call them directly.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Max, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce, TruncDate, TruncHour

from apps.pos.models import Return, ReturnLine, Sale, SaleLine, SaleStatus

# Every sale that took money. Refunded ones count here and their refunds are
# subtracted, so a sale and its refund always net out.
COUNTED = [SaleStatus.COMPLETED, SaleStatus.PART_REFUNDED, SaleStatus.REFUNDED]

ZERO = Decimal("0")


def sales_in(branches, start, end):
    return Sale.objects.filter(
        branch__in=branches, status__in=COUNTED,
        sold_at__date__gte=start, sold_at__date__lte=end,
    )


def refunds_for(sales) -> Decimal:
    """Money given back on those sales."""
    return Return.objects.filter(sale__in=sales).aggregate(
        total=Coalesce(Sum("total"), ZERO)
    )["total"]


def takings(branches, start, end) -> dict:
    """Headline money for a period: what came in, what went back, what is left."""
    sales = sales_in(branches, start, end)
    totals = sales.aggregate(count=Count("id"), gross=Coalesce(Sum("total"), ZERO))
    refunded = refunds_for(sales)
    net = totals["gross"] - refunded
    return {
        "count": totals["count"], "gross": totals["gross"], "refunded": refunded,
        "net": net,
        "average": (net / totals["count"]) if totals["count"] else ZERO,
    }


def _kept_lines(branches, start, end):
    """
    Sale lines with the refunded quantity taken off.

    A part-refunded line used to report its whole revenue and its whole
    profit, which flattered every margin figure in the system.
    """
    returned = (
        ReturnLine.objects.filter(sale_line=OuterRef("pk"))
        .values("sale_line").annotate(total=Sum("qty")).values("total")
    )
    return SaleLine.objects.filter(
        sale__branch__in=branches, sale__status__in=COUNTED,
        sale__sold_at__date__gte=start, sale__sold_at__date__lte=end,
        qty__gt=0,
    ).annotate(
        kept=F("qty") - Coalesce(
            Subquery(returned, output_field=DecimalField()), ZERO,
            output_field=DecimalField(),
        ),
    )


def gross_profit(branches, start, end) -> dict:
    """
    Revenue less what the goods cost, from the cost stored on the line at the
    time of sale -- never from today's cost price.
    """
    totals = _kept_lines(branches, start, end).aggregate(
        revenue=Sum((F("line_total") - F("tax_amount")) * F("kept") / F("qty"),
                    output_field=DecimalField()),
        cost=Sum(F("kept") * F("unit_cost"), output_field=DecimalField()),
    )
    revenue = totals["revenue"] or ZERO
    cost = totals["cost"] or ZERO
    profit = revenue - cost
    return {
        "revenue": revenue, "cost": cost, "profit": profit,
        "pct": (profit / revenue * 100) if revenue else ZERO,
    }


def top_products(branches, start, end, limit=5) -> list:
    """
    What sold best, by money rather than by count: two hundred sweets are not
    a better day than four sacks of cement.

    Grouped by product, not by the words printed on the receipt -- two
    products sharing a name used to merge into one row.
    """
    rows = (
        _kept_lines(branches, start, end).values("variant")
        .annotate(
            item=Max("description"),
            qty_sold=Sum("kept"),
            value=Sum(F("line_total") * F("kept") / F("qty"), output_field=DecimalField()),
        )
        .filter(value__gt=0).order_by("-value")[:limit]
    )
    rows = list(rows)
    peak = max((r["value"] or ZERO for r in rows), default=ZERO)
    for row in rows:
        row["pct"] = int((row["value"] or ZERO) / peak * 100) if peak else 0
    return rows


def by_day(branches, start, end) -> list:
    """
    One row per day in the period, including the quiet ones.

    Days with no sales have to appear or the line joins Monday to Friday and
    draws a week that never happened.
    """
    sales = sales_in(branches, start, end)
    took = {
        row["day"]: row
        for row in sales.annotate(day=TruncDate("sold_at")).values("day")
        .annotate(count=Count("id"), value=Coalesce(Sum("total"), ZERO))
    }
    back = {
        row["day"]: row["total"]
        for row in Return.objects.filter(sale__in=sales)
        .annotate(day=TruncDate("sale__sold_at")).values("day")
        .annotate(total=Sum("total"))
    }
    out, day = [], start
    while day <= end:
        row = took.get(day)
        value = (row["value"] if row else ZERO) - (back.get(day) or ZERO)
        out.append({
            "key": day, "day": day, "label": day.strftime("%a %-d %b"),
            "short": day.strftime("%-d %b"),
            "value": value, "count": row["count"] if row else 0,
        })
        day += timedelta(days=1)
    return out


def by_hour(branches, day) -> list:
    """
    One row per trading hour, for a dashboard showing a single day.

    A day plotted as one point is not a chart, and a shopkeeper looking at
    today wants to know whether the morning or the evening is carrying it.
    """
    sales = sales_in(branches, day, day)
    took = {
        row["hour"].hour: row
        for row in sales.annotate(hour=TruncHour("sold_at")).values("hour")
        .annotate(count=Count("id"), value=Coalesce(Sum("total"), ZERO))
        if row["hour"] is not None
    }
    busy = sorted(took) or [8, 20]
    first, last = min(busy[0], 8), max(busy[-1], 20)
    out = []
    for hour in range(first, last + 1):
        row = took.get(hour)
        out.append({
            "key": hour, "day": day, "label": f"{hour:02d}:00", "short": f"{hour:02d}",
            "value": row["value"] if row else ZERO,
            "count": row["count"] if row else 0,
        })
    return out


def profit_by(branches, start, end, hourly=False) -> dict:
    """
    Gross profit split into the same buckets as :func:`by_day` and
    :func:`by_hour`, keyed the same way, so a card's figure and the little
    line under it are always the same series.
    """
    bucket = TruncHour("sale__sold_at") if hourly else TruncDate("sale__sold_at")
    rows = (
        _kept_lines(branches, start, end).annotate(at=bucket).values("at")
        .annotate(
            revenue=Sum((F("line_total") - F("tax_amount")) * F("kept") / F("qty"),
                        output_field=DecimalField()),
            cost=Sum(F("kept") * F("unit_cost"), output_field=DecimalField()),
        )
    )
    out = {}
    for row in rows:
        if row["at"] is None:
            continue
        key = row["at"].hour if hourly else row["at"]
        out[key] = (row["revenue"] or ZERO) - (row["cost"] or ZERO)
    return out

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse


@login_required
def dashboard(request):
    """
    The first screen after signing in, shaped by what this person does.

    The owner sees the money and where it came from; a cashier sees their own
    shift; a new shop sees what to set up. Every figure covers only the
    branches this person works in, and only what their role may see -- it
    used to show the whole shop's takings to every cashier.
    """
    from datetime import timedelta

    from django.db.models import Count, F, Q, Sum
    from django.utils import timezone

    from apps.accounts.models import Membership
    from apps.catalog.models import Product
    from apps.core import charts, periods
    from apps.org.models import Branch
    from apps.pos.models import PaymentMethod, Sale, SaleStatus, Shift, ShiftStatus
    from apps.reports import services as figures

    membership = request.membership
    can = membership.can
    # Closed branches included, as the reports count them: their sales
    # still happened this week. Read once: as a queryset this was re-run for
    # every figure on the page.
    branches = list(membership.branches(include_closed=True))
    now = timezone.now()
    today = timezone.localdate()
    period = periods.resolve(request, floor=request.tenant.history_start())
    context = {"period": period, "periods": periods.PERIODS}

    # -- money, for those who may see it ------------------------------------
    if can("report.sales"):
        start, end = period["start"], period["end"]
        took = figures.takings(branches, start, end)
        before = figures.takings(branches, period["previous_start"], period["previous_end"])

        # A single day is plotted hour by hour: one point is not a chart, and
        # the shopkeeper wants to know whether the morning or the evening is
        # carrying the day.
        rows = (figures.by_hour(branches, start) if period["single_day"]
                else figures.by_day(branches, start, end))

        # "Today" is the only single-day period on offer, so when that is
        # what was asked for the figures are already in hand.
        day = took if period["single_day"] else figures.takings(branches, today, today)
        context["today"] = {"value": day["net"], "count": day["count"],
                            "refunded": day["refunded"]}

        cards = [
            {"label": "Takings", "value": took["net"], "money": True,
             "change": charts.change(took["net"], before["net"]),
             "hint": f"{took['refunded']:,.0f} refunded" if took["refunded"]
                     else "nothing refunded",
             "spark": charts.spark([r["value"] for r in rows]),
             "url": reverse("reports:index")},
            {"label": "Sales", "value": took["count"], "money": False,
             "change": charts.change(took["count"], before["count"]),
             "hint": f"{day['count']} today",
             "spark": charts.spark([r["count"] for r in rows]),
             "url": reverse("pos:sale_list")},
            {"label": "Average sale", "value": took["average"], "money": True,
             "change": charts.change(took["average"], before["average"]),
             "hint": "per sale in this period",
             "spark": charts.spark([
                 (r["value"] / r["count"]) if r["count"] else 0 for r in rows]),
             "url": reverse("reports:index")},
        ]
        if can("report.margin"):
            made = figures.gross_profit(branches, start, end)
            was = figures.gross_profit(branches, period["previous_start"],
                                       period["previous_end"])
            buckets = figures.profit_by(branches, start, end, hourly=period["single_day"])
            cards.append({
                "label": "Profit on goods", "value": made["profit"], "money": True,
                "change": charts.change(made["profit"], was["profit"]),
                # Not "12% margin": the shopkeeper's question is how much of
                # each hundred she keeps. Rent and wages are not in it, which
                # the Profit report says in full.
                "hint": f"{made['pct']:,.0f} in every 100 is profit",
                "spark": charts.spark([buckets.get(r["key"], 0) for r in rows]),
                "url": reverse("reports:margin"),
            })

        sales = figures.sales_in(branches, start, end)
        labels = dict(PaymentMethod.choices)
        paid = [
            {"label": labels.get(row["payments__method"], row["payments__method"]),
             "total": row["total"]}
            for row in sales.values("payments__method")
            .annotate(total=Sum("payments__amount")).order_by("-total")
            if row["payments__method"]
        ]
        paid_peak = max((row["total"] or 0 for row in paid), default=0)
        for row in paid:
            row["pct"] = int((row["total"] or 0) / paid_peak * 100) if paid_peak else 0

        context.update({
            "cards": cards,
            "chart": charts.curve(rows),
            "chart_rows": rows,
            "top": figures.top_products(branches, start, end),
            "by_method": paid,
            "recent": sales.select_related("customer", "branch").order_by("-sold_at")[:10],
        })

    # -- who did what, for whoever may read it ------------------------------
    if can("report.staff"):
        from apps.accounts.audit_views import visible_to
        from apps.core.audit import describe

        context["feed"] = [
            describe(row)
            for row in visible_to(membership)
            .select_related("user", "authorised_by", "branch")
            .order_by("-created_at")[:7]
        ]

    # -- what needs somebody -----------------------------------------------
    attention = []

    def flag(n, text, url, level="warn"):
        if n:
            attention.append({"n": n, "text": text, "url": url, "level": level})

    if can("report.sales"):
        flag(Sale.objects.filter(branch__in=branches, needs_review=True).count(),
             "sale(s) to check -- recorded outside what the shop allows",
             reverse("pos:sale_list") + "?review=1", "danger")
    if can("cashup.approve"):
        flag(Shift.objects.filter(branch__in=branches, status=ShiftStatus.OPEN,
                                  opened_at__lt=now - timedelta(hours=14)).count(),
             "till(s) left open since yesterday", reverse("finance:cashups"), "danger")
        flag(Shift.objects.filter(branch__in=branches, closed_at__isnull=False, approved_by__isnull=True)
             .exclude(variance=0).count(),
             "cash-up(s) with a difference waiting for approval", reverse("finance:cashups"))
    if can("stock.view"):
        from apps.inventory.models import StockItem

        flag(StockItem.objects.filter(branch__in=branches, reorder_level__isnull=False,
                                      qty_on_hand__lte=F("reorder_level")).count(),
             "product(s) running low", reverse("inventory:stock_list") + "?view=low")
    if can("stock.batches") and request.branch is not None:
        from apps.inventory.services import expiring_soon

        flag(expiring_soon(days=30, branch=request.branch).count(),
             "batch(es) expiring within 30 days", reverse("inventory:batch_list") + "?days=30")
    if can("supplier.pay") or can("supplier.manage"):
        from apps.purchasing.models import SupplierInvoice

        due = SupplierInvoice.objects.filter(due_date__lte=today).annotate(
            paid=Sum("payments__amount")
        ).filter(Q(paid__isnull=True) | Q(paid__lt=F("amount")))
        flag(due.count(), "supplier bill(s) due or overdue", reverse("purchasing:supplier_list"))
    if can("customer.manage"):
        from apps.customers.models import Customer

        over = Customer.objects.filter(credit_limit__gt=0).annotate(
            owed=Sum("credit_transactions__amount")
        ).filter(owed__gt=F("credit_limit"))
        flag(over.count(), "customer(s) over their credit limit",
             reverse("customers:customer_list") + "?view=over")
    if can("fiscal.manage"):
        from apps.pos.models import FiscalReceipt, FiscalStatus

        flag(FiscalReceipt.objects.filter(status=FiscalStatus.FAILED,
                                          sale__branch__in=branches).count(),
             "receipt(s) the tax authority did not accept", reverse("pos:fiscal_receipts") + "?status=failed",
             "danger")
    if can("register.manage"):
        from apps.org.models import Device

        flag(Device.objects.filter(branch__in=branches, is_active=True, queued__gt=0).count(),
             "till(s) holding sales they have not sent", reverse("org:devices"))
    context["attention"] = attention
    # A role with nothing ticked yet: say so, rather than "nothing needs you".
    from apps.core.permissions import registry

    context["no_access"] = not any(can(spec.code) for spec in registry.all())

    # -- the person at the till ---------------------------------------------
    if can("pos.operate"):
        shift = Shift.objects.filter(user=request.user, status=ShiftStatus.OPEN,
                                     branch__in=branches).select_related("register").first()
        if shift is not None:
            mine = shift.sales.exclude(status=SaleStatus.VOIDED).aggregate(v=Sum("total"), n=Count("id"))
            context["my_shift"] = {
                "shift": shift, "value": mine["v"] or 0, "count": mine["n"] or 0,
                # The till staff's own receipts, to reprint or refund: they
                # cannot see the shop's sales list.
                "sales": shift.sales.select_related("customer").order_by("-sold_at")[:10],
                "may_reprint": can("pos.reprint"),
            }
        # A prompt to open a till is for till staff, not for an owner
        # looking at the day's figures.
        context["may_sell"] = not can("report.sales")

    # -- the handful of things this person actually does --------------------
    # Built here rather than in the template so the page knows whether there
    # is an aside at all: an empty third column beside a cashier's screen is
    # a third of the page saying nothing.
    quick = [
        ("pos.mobile_cart", "pos:phone", "tag", "Sell on phone"),
        ("product.manage", "catalog:product_create", "plus", "Add a product"),
        ("stock.receive", "purchasing:receipt_create", "layers", "Receive goods"),
        ("expense.create", "finance:expense_list", "wallet", "Record an expense"),
        ("report.sales", "reports:index", "chart", "Sales reports"),
    ]
    context["quick_actions"] = [
        {"url": reverse(route), "icon": icon, "label": label}
        for code, route, icon, label in quick if can(code)
    ]

    # -- a new shop: what to set up, for whoever can set it up --------------
    product_count = Product.objects.filter(is_active=True).count()
    steps = []
    if can("register.manage"):
        steps.append({"label": "Check your branch and tills", "done": Branch.objects.filter(is_active=True).exists(),
                      "url": reverse("org:branches")})
    if can("product.manage"):
        steps.append({"label": "Add your products", "done": product_count > 0,
                      "url": reverse("catalog:product_create")})
    if can("user.manage"):
        steps.append({"label": "Add your staff", "done": Membership.objects.filter(is_active=True).count() > 1,
                      "url": reverse("accounts:staff")})
    if can("pos.operate"):
        steps.append({"label": "Make your first sale", "done": Sale.objects.exists(),
                      "url": reverse("pos:till")})
    done = sum(1 for step in steps if step["done"])
    # Gone once everything is done -- it used to stay for ever.
    context.update({"steps": steps if done < len(steps) else [], "done_count": done})

    return render(request, "core/dashboard.html", context)


def healthz(request):
    """
    Liveness for a load balancer.

    Returning "ok" without touching anything meant a server with a dead
    database stayed in rotation happily answering nothing.
    """
    from django.core.cache import cache
    from django.db import connection

    problems = []

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        problems.append(f"database: {exc}")

    try:
        cache.set("healthz", "1", 5)
        if cache.get("healthz") != "1":
            problems.append("cache: value did not come back")
    except Exception as exc:
        problems.append(f"cache: {exc}")

    if problems:
        return HttpResponse(
            "\n".join(problems), content_type="text/plain", status=503
        )
    return HttpResponse("ok", content_type="text/plain")

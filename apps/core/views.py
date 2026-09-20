from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse


@login_required
def dashboard(request):
    """
    The first screen after signing in, shaped by what this person does.

    The owner sees the day's money and what needs attention; a cashier sees
    their own shift; a new shop sees what to set up. Every figure covers only
    the branches this person works in, and only what their role may see --
    it used to show the whole shop's takings to every cashier.
    """
    from datetime import timedelta

    from django.db.models import Count, F, Q, Sum
    from django.utils import timezone

    from apps.accounts.models import Membership
    from apps.catalog.models import Product
    from apps.org.models import Branch
    from apps.pos.models import PaymentMethod, Return, Sale, SaleStatus, Shift, ShiftStatus

    membership = request.membership
    can = membership.can
    # Closed branches included, as the reports count them: their sales
    # still happened this week.
    branches = membership.branches(include_closed=True)
    now = timezone.now()
    today = timezone.localdate()
    context = {}

    # -- money, for those who may see it ------------------------------------
    if can("report.sales"):
        kept = [SaleStatus.COMPLETED, SaleStatus.PART_REFUNDED, SaleStatus.REFUNDED]
        sales = Sale.objects.filter(branch__in=branches, status__in=kept)

        def day_total(day):
            took = sales.filter(sold_at__date=day).aggregate(v=Sum("total"), n=Count("id"))
            # Refunds on that day's sales, as the Sales report counts them:
            # a refund always nets against the sale it undoes.
            back = Return.objects.filter(sale__in=sales.filter(sold_at__date=day)) \
                .aggregate(v=Sum("total"))["v"] or 0
            return {"value": (took["v"] or 0) - back, "count": took["n"] or 0, "refunded": back}

        week = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            week.append({"day": day, **day_total(day)})
        peak = max((d["value"] for d in week), default=0) or 1
        for d in week:
            d["pct"] = int(d["value"] / peak * 100) if d["value"] > 0 else 0
        context.update({
            "today": week[-1], "yesterday": week[-2], "week": week,
            "week_total": sum(d["value"] for d in week),
            "by_method": [
                {"label": dict(PaymentMethod.choices).get(row["payments__method"], row["payments__method"]),
                 "total": row["total"]}
                for row in sales.filter(sold_at__date=today).values("payments__method")
                .annotate(total=Sum("payments__amount")).order_by("-total")
                if row["payments__method"]
            ],
            "top_today": sales.filter(sold_at__date=today).exclude(status=SaleStatus.REFUNDED)
            .values("lines__description")
            .annotate(qty=Sum("lines__qty"), value=Sum("lines__line_total"))
            .order_by("-value")[:5],
        })

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

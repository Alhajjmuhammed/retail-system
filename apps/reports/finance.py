"""
The money reports: what came in, what is owed each way, what the paperwork
says, what was made, what the business is worth, the VAT, and what was
bought and sold most.

The sales report only ever saw the till. A customer paying off an account,
an invoice settled by bank transfer, an expense, a supplier's bill -- none of
it reached a report, and nothing said what was still unpaid. These read the
same records the rest of the system writes, and add nothing of their own:
money has one home, and a report that kept its own totals would be a second
set of books.

All amounts are in the shop's own currency. A document written in another
is counted at the rate frozen on it.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

ZERO = Decimal("0")
BUCKETS = [("0_30", "0-30 days"), ("31_60", "31-60 days"),
           ("61_90", "61-90 days"), ("90_plus", "Over 90 days")]


def _bucket(days):
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


# --------------------------------------------------------------------------
# 1. Money in
# --------------------------------------------------------------------------

def money_in(branches, start, end, whole_shop=True) -> dict:
    """
    Every shilling received in the period, wherever it was taken: at the
    POS, on a customer's account, against an invoice. Selling on account is
    not money in, so it is left out; refunds paid back are taken off.

    Account and invoice payments belong to the shop, not to a branch, so they
    are counted only for somebody who sees the whole shop (``whole_shop``).
    """
    from apps.customers.models import CreditKind, CreditTransaction
    from apps.pos.models import PaymentMethod, Return, SalePayment
    from apps.reports.services import COUNTED

    labels = dict(PaymentMethod.choices)
    labels.update({"bank": "Bank transfer", "mpesa": "M-Pesa", "cash": "Cash"})
    by_method = defaultdict(lambda: ZERO)
    by_day = defaultdict(lambda: ZERO)
    sources = {"pos": ZERO, "accounts": ZERO, "invoices": ZERO, "refunds": ZERO}

    pos = (SalePayment.objects
           .filter(sale__branch__in=branches, sale__status__in=COUNTED,
                   sale__sold_at__date__gte=start, sale__sold_at__date__lte=end)
           .exclude(method=PaymentMethod.CREDIT)
           .annotate(day=TruncDate("sale__sold_at"))
           .values("method", "day")
           .annotate(total=Sum(F("amount") - F("change_given"))))
    for row in pos:
        by_method[row["method"]] += row["total"] or ZERO
        by_day[row["day"]] += row["total"] or ZERO
        sources["pos"] += row["total"] or ZERO

    refunds = (Return.objects
               .filter(sale__branch__in=branches,
                       created_at__date__gte=start, created_at__date__lte=end)
               .annotate(day=TruncDate("created_at"))
               .values("method", "day")
               .annotate(paid=Sum(F("cash_amount") + F("other_amount"))))
    for row in refunds:
        paid = row["paid"] or ZERO
        by_method[row["method"]] -= paid
        by_day[row["day"]] -= paid
        sources["refunds"] -= paid

    if whole_shop:
        received = (CreditTransaction.objects
                    .filter(created_at__date__gte=start, created_at__date__lte=end)
                    .filter(Q(kind=CreditKind.PAYMENT)
                            | Q(kind=CreditKind.ADJUSTMENT, reference__startswith="undo:"))
                    .annotate(day=TruncDate("created_at"))
                    .values("method", "day", "kind", "invoice_id")
                    .annotate(total=Sum("amount")))
        for row in received:
            # Payments are stored negative; an undo puts them back.
            amount = -(row["total"] or ZERO)
            method = row["method"] or "cash"
            by_method[method] += amount
            by_day[row["day"]] += amount
            sources["invoices" if row["invoice_id"] else "accounts"] += amount

    total = sum(sources.values(), ZERO)
    methods = sorted(
        ({"method": m, "label": labels.get(m, m.title()), "total": v}
         for m, v in by_method.items() if v),
        key=lambda r: -r["total"])
    days = []
    cursor = start
    while cursor <= end:
        days.append({"day": cursor, "total": by_day.get(cursor, ZERO)})
        cursor += timedelta(days=1)
    peak = max((d["total"] for d in days), default=ZERO)
    return {"total": total, "sources": sources, "methods": methods, "days": days,
            "peak": peak, "whole_shop": whole_shop}


# --------------------------------------------------------------------------
# 2. Owed to you
# --------------------------------------------------------------------------

def receivables(today=None) -> dict:
    """
    Who owes the shop, how much, and since when.

    A customer's balance is the sum of their account. Its age comes from the
    charges that make it up, oldest paid first: whatever is still owed sits
    in the most recent charges, and its age is the age of those.
    """
    from apps.customers.models import CreditKind, CreditTransaction, Customer
    from apps.selling.models import Invoice, InvoiceStatus

    today = today or timezone.localdate()
    balances = dict(
        CreditTransaction.objects.values("customer").annotate(b=Sum("amount"))
        .filter(b__gt=0).values_list("customer", "b"))
    customers = {c.pk: c for c in Customer.objects.filter(pk__in=balances)}
    # Every charge of everybody who owes, newest first, in one read.
    charges_of = defaultdict(list)
    for charge in (CreditTransaction.objects.filter(customer_id__in=balances, amount__gt=0)
                   .exclude(kind=CreditKind.REFUND).only("customer_id", "amount", "created_at")
                   .order_by("-created_at")):
        charges_of[charge.customer_id].append(charge)
    rows, totals = [], {key: ZERO for key, _ in BUCKETS}
    for pk, balance in balances.items():
        left = balance
        aged = {key: ZERO for key, _ in BUCKETS}
        oldest = None
        for charge in charges_of[pk]:
            if left <= 0:
                break
            part = min(left, charge.amount)
            age = (today - timezone.localdate(charge.created_at)).days
            aged[_bucket(age)] += part
            oldest = age
            left -= part
        if left > 0:          # older than anything on record
            aged["90_plus"] += left
        for key in aged:
            totals[key] += aged[key]
        rows.append({"customer": customers.get(pk), "balance": balance, "aged": aged,
                     "oldest_days": oldest or 0,
                     "limit": getattr(customers.get(pk), "credit_limit", ZERO)})
    rows.sort(key=lambda r: -r["balance"])

    overdue = []
    for inv in (Invoice.objects.filter(status__in=[InvoiceStatus.OPEN, InvoiceStatus.PART_PAID],
                                       due_on__lt=today)
                .select_related("customer").prefetch_related("lines", "credit_entries")
                .order_by("due_on")):
        overdue.append({"invoice": inv, "days": (today - inv.due_on).days,
                        "balance": inv.balance, "balance_main": inv.balance_main})

    in_credit = list(
        CreditTransaction.objects.values("customer__name").annotate(b=Sum("amount"))
        .filter(b__lt=0).order_by("b"))
    return {"rows": rows, "totals": totals, "total": sum(totals.values(), ZERO),
            "overdue": overdue, "overdue_total": sum((o["balance_main"] for o in overdue), ZERO),
            "in_credit": in_credit, "in_credit_total": -sum((r["b"] for r in in_credit), ZERO)}


# --------------------------------------------------------------------------
# 3. Owed to suppliers
# --------------------------------------------------------------------------

def payables(today=None) -> dict:
    """What the shop owes each supplier, and which bills are unpaid or late."""
    from apps.purchasing.models import Supplier, SupplierInvoice, SupplierPayment

    today = today or timezone.localdate()
    invoiced = dict(SupplierInvoice.objects.values("supplier").annotate(t=Sum("amount"))
                    .values_list("supplier", "t"))
    paid = dict(SupplierPayment.objects.values("supplier").annotate(t=Sum("amount"))
                .values_list("supplier", "t"))
    suppliers = {s.pk: s for s in Supplier.objects.filter(pk__in=set(invoiced) | set(paid))}
    rows = []
    for pk, supplier in suppliers.items():
        balance = (invoiced.get(pk) or ZERO) - (paid.get(pk) or ZERO)
        if balance:
            rows.append({"supplier": supplier, "balance": balance,
                         "invoiced": invoiced.get(pk) or ZERO, "paid": paid.get(pk) or ZERO})
    rows.sort(key=lambda r: -r["balance"])

    bills = []
    for bill in (SupplierInvoice.objects.select_related("supplier")
                 .prefetch_related("payments").order_by("due_date", "invoice_date")):
        outstanding = bill.outstanding
        if outstanding > 0:
            late = bool(bill.due_date and bill.due_date < today)
            bills.append({"bill": bill, "outstanding": outstanding, "late": late,
                          "days_late": (today - bill.due_date).days if late else 0})
    owed = sum((r["balance"] for r in rows if r["balance"] > 0), ZERO)
    return {"rows": rows, "bills": bills, "total": owed,
            "late_total": sum((b["outstanding"] for b in bills if b["late"]), ZERO)}


# --------------------------------------------------------------------------
# 4. Invoices, proformas and quotations
# --------------------------------------------------------------------------

def paperwork(branches, start, end, today=None) -> dict:
    """
    What was quoted, proposed and billed in the period, and how it went:
    invoices by state with their money, proformas still waiting, quotations
    won and lost, and who was billed most.
    """
    from apps.selling.models import Invoice, InvoiceStatus, Quotation, QuotationStatus

    today = today or timezone.localdate()
    invoices = list(
        Invoice.objects.filter(branch__in=branches, created_at__date__gte=start,
                               created_at__date__lte=end)
        .select_related("customer").prefetch_related("lines", "credit_entries"))
    states = {key: {"label": label, "count": 0, "total": ZERO}
              for key, label in InvoiceStatus.choices}
    states["overdue"] = {"label": "Past due", "count": 0, "total": ZERO}
    billed = ZERO
    collected = ZERO
    by_customer = defaultdict(lambda: {"billed": ZERO, "paid": ZERO, "count": 0})
    for inv in invoices:
        total = inv.total_main
        states[inv.status]["count"] += 1
        states[inv.status]["total"] += total
        if inv.is_owed and inv.due_on < today:
            states["overdue"]["count"] += 1
            states["overdue"]["total"] += inv.balance_main
        if inv.status not in (InvoiceStatus.DRAFT, InvoiceStatus.VOID):
            billed += total
            collected += inv.paid_main
            entry = by_customer[inv.customer.name]
            entry["billed"] += total
            entry["paid"] += inv.paid_main
            entry["count"] += 1
    top_customers = sorted(({"name": k, **v, "owed": v["billed"] - v["paid"]}
                            for k, v in by_customer.items()),
                           key=lambda r: -r["billed"])[:10]

    quotes = list(Quotation.objects.filter(branch__in=branches, created_at__date__gte=start,
                                           created_at__date__lte=end)
                  .prefetch_related("lines"))
    q_states = {key: {"label": label, "count": 0, "total": ZERO}
                for key, label in QuotationStatus.choices}
    for q in quotes:
        q_states[q.status]["count"] += 1
        q_states[q.status]["total"] += q.total_main
    decided = sum(q_states[s]["count"] for s in ("accepted", "declined", "expired"))
    win_rate = (Decimal(q_states["accepted"]["count"]) / decided * 100) if decided else None
    return {"states": states, "billed": billed, "collected": collected,
            "outstanding": billed - collected, "top_customers": top_customers,
            "q_states": q_states, "quote_count": len(quotes), "win_rate": win_rate,
            "invoice_count": len(invoices)}


# --------------------------------------------------------------------------
# 5. Profit and loss
# --------------------------------------------------------------------------

def _months(start, end):
    cursor = start.replace(day=1)
    while cursor <= end:
        nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cursor, start), min(nxt - timedelta(days=1), end)
        cursor = nxt


def profit_and_loss(branches, start, end) -> dict:
    """
    Sales (VAT taken off, refunds taken off) less what the goods cost, less
    expenses by category, month by month.
    """
    from apps.finance.models import Expense
    from apps.reports.services import gross_profit

    months, cats = [], set()
    for m_start, m_end in _months(start, end):
        gp = gross_profit(branches, m_start, m_end)
        spent = dict(Expense.objects.filter(branch__in=branches, spent_at__gte=m_start,
                                            spent_at__lte=m_end)
                     .values_list("category__name").annotate(t=Sum("amount")))
        cats |= set(spent)
        expenses = sum(spent.values(), ZERO)
        months.append({"start": m_start, "end": m_end, "revenue": gp["revenue"],
                       "cost": gp["cost"], "gross": gp["profit"], "expenses": expenses,
                       "by_category": spent, "net": gp["profit"] - expenses})
    total = {key: sum((m[key] for m in months), ZERO)
             for key in ("revenue", "cost", "gross", "expenses", "net")}
    total["by_category"] = {c: sum((m["by_category"].get(c, ZERO) for m in months), ZERO)
                            for c in cats}
    total["margin"] = (total["net"] / total["revenue"] * 100) if total["revenue"] else ZERO
    for m in months:
        m["rows"] = [(c, m["by_category"].get(c, ZERO)) for c in sorted(cats, key=str)]
    return {"months": months, "total": total,
            "categories": sorted(cats, key=lambda c: (c is None, str(c)))}


# --------------------------------------------------------------------------
# 6. Balance sheet
# --------------------------------------------------------------------------

def balance_sheet(branches, whole_shop=True) -> dict:
    """
    What the business has and what it owes, today, from what this system
    records: stock at cost, money customers owe, cash in the drawers now
    open; less what is owed to suppliers and what customers have paid in
    advance. Bank balances, equipment, loans and the owner's capital are not
    recorded here, so they are not in it -- the page says so.
    """
    from apps.inventory.models import StockItem
    from apps.pos.models import Shift, ShiftStatus

    stock = StockItem.objects.filter(branch__in=branches, qty_on_hand__gt=0).aggregate(
        v=Coalesce(Sum(F("qty_on_hand") * F("avg_cost"), output_field=DecimalField()),
                   ZERO))["v"]
    drawers = sum((s.compute_expected_cash() for s in
                   Shift.objects.filter(branch__in=branches, status=ShiftStatus.OPEN)), ZERO)
    owed_to_you = owed_to_suppliers = prepaid = ZERO
    if whole_shop:
        owed = receivables()
        owed_to_you = owed["total"]
        prepaid = owed["in_credit_total"]
        owed_to_suppliers = payables()["total"]
    assets = [("Stock, at what it cost", stock), ("Owed to you by customers", owed_to_you),
              ("Cash in the drawers now open", drawers)]
    liabilities = [("Owed to suppliers", owed_to_suppliers),
                   ("Paid in advance by customers", prepaid)]
    total_assets = sum((v for _, v in assets), ZERO)
    total_liabilities = sum((v for _, v in liabilities), ZERO)
    return {"assets": assets, "liabilities": liabilities, "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "net_worth": total_assets - total_liabilities, "whole_shop": whole_shop}


# --------------------------------------------------------------------------
# 7. VAT
# --------------------------------------------------------------------------

def vat_summary(branches, start, end) -> dict:
    """
    VAT charged on what stayed sold, by rate: the figures a VAT return asks
    for on the sales side. Prices include VAT, so it is the VAT inside them.
    """
    from apps.reports.services import _kept_lines

    rows = (_kept_lines(branches, start, end).values("tax_rate")
            .annotate(
                gross=Sum(F("line_total") * F("kept") / F("qty"), output_field=DecimalField()),
                vat=Sum(F("tax_amount") * F("kept") / F("qty"), output_field=DecimalField()),
                lines=Count("id"))
            .order_by("-tax_rate"))
    out = []
    for row in rows:
        gross, vat = row["gross"] or ZERO, row["vat"] or ZERO
        out.append({"rate": row["tax_rate"], "gross": gross, "vat": vat,
                    "net": gross - vat, "lines": row["lines"]})
    return {"rows": out,
            "gross": sum((r["gross"] for r in out), ZERO),
            "vat": sum((r["vat"] for r in out), ZERO),
            "net": sum((r["net"] for r in out), ZERO)}


# --------------------------------------------------------------------------
# 8. Best sellers and purchases
# --------------------------------------------------------------------------

def best_sellers(branches, start, end, by="money", limit=25) -> list:
    """What sold most, ranked the way asked -- the dashboard's list, longer."""
    from apps.reports.services import top_products

    return top_products(branches, start, end, limit=limit, by=by)


def purchases(branches, start, end) -> dict:
    """Goods received from each supplier in the period, at what they cost."""
    from apps.purchasing.models import GoodsReceiptLine

    lines = GoodsReceiptLine.objects.filter(receipt__branch__in=branches,
                                            receipt__received_at__date__gte=start,
                                            receipt__received_at__date__lte=end)
    rows = list(lines.values("receipt__supplier__name")
                .annotate(receipts=Count("receipt", distinct=True), items=Count("id"),
                          value=Sum(F("qty") * F("unit_cost"), output_field=DecimalField()))
                .order_by("-value"))
    return {"rows": rows, "total": sum((r["value"] or ZERO for r in rows), ZERO)}

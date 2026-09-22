"""
What a refund does to everything downstream.

A refund is the one operation that touches money, stock, the day's takings,
the profit report and the customer's balance at once. If any of those
disagree afterwards, the shop finds out weeks later.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.inventory.models import StockItem
from apps.pos.models import SaleStatus
from apps.reports import services as figures

pytestmark = pytest.mark.django_db


def _sell(shop, branch, who, variant, qty):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=branch, user=who):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}])


def test_a_part_refund_moves_money_stock_and_every_figure(shop, main_branch, stocked, owner):
    from apps.pos.services import create_return

    variant = stocked["Sukari 1kg"]          # 3,000 each
    sale = _sell(shop, main_branch, owner, variant, qty=3)   # 9,000
    today = timezone.localdate()

    with tenant_context(shop, branch=main_branch):
        before_stock = StockItem.objects.get(variant=variant, branch=main_branch).qty_on_hand
        before = figures.takings([main_branch], today, today)
    assert before["net"] == Decimal("9000")

    with tenant_context(shop, branch=main_branch, user=owner):
        line = sale.lines.first()
        doc = create_return(sale, {line.pk: 1}, reason="one bag back")

    with tenant_context(shop, branch=main_branch):
        sale.refresh_from_db()
        after = figures.takings([main_branch], today, today)
        after_stock = StockItem.objects.get(variant=variant, branch=main_branch).qty_on_hand
        profit = figures.gross_profit([main_branch], today, today)
        top = figures.top_products([main_branch], today, today)

    # The refund document carries what went back.
    assert doc.total == Decimal("3000")
    # The sale itself is untouched -- the refund stands beside it.
    assert sale.total == Decimal("9000")
    assert sale.status == SaleStatus.PART_REFUNDED
    # Takings are net of it, and the count still counts one sale.
    assert after["net"] == Decimal("6000") and after["refunded"] == Decimal("3000")
    assert after["count"] == before["count"]
    # The bag is back on the shelf.
    assert after_stock == before_stock + 1
    # Profit counts only what stayed sold: two bags, not three.
    assert profit["cost"] == Decimal("4800")          # two bags at 2,400
    # And the best-seller list counts two.
    assert top and top[0]["qty_sold"] == Decimal("2")


def test_refunding_everything_leaves_nothing_counted(shop, main_branch, stocked, owner):
    from apps.pos.services import create_return

    sale = _sell(shop, main_branch, owner, stocked["Mkate"], qty=2)
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(sale, {sale.lines.first().pk: 2}, reason="all back")
    with tenant_context(shop, branch=main_branch):
        after = figures.takings([main_branch], today, today)
        profit = figures.gross_profit([main_branch], today, today)
        sale.refresh_from_db()

    assert sale.status == SaleStatus.REFUNDED
    assert after["net"] == Decimal("0")
    # Nothing stayed sold, so nothing was earned on it.
    assert profit["profit"] == Decimal("0") and profit["cost"] == Decimal("0")


def test_the_dashboard_and_the_report_agree_after_a_refund(client, shop, main_branch,
                                                           stocked, owner):
    from apps.pos.services import create_return

    sale = _sell(shop, main_branch, owner, stocked["Soda 500ml"], qty=4)
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(sale, {sale.lines.first().pk: 1}, reason="one back")

    client.force_login(owner)
    dashboard = client.get(reverse("core:dashboard") + "?range=today")
    report = client.get(reverse("reports:index") + "?preset=today")

    takings = next(c for c in dashboard.context["cards"] if c["label"] == "Takings")
    assert takings["value"] == report.context["totals"]["net"]
    assert dashboard.context["today"]["refunded"] == report.context["totals"]["refunded"]

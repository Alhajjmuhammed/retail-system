"""
The rebuilt dashboard: the period it describes, what it compares against,
and the charts it draws from.

The figures themselves live in one place so that the dashboard and the
reports cannot drift apart; these tests pin the behaviour that matters when
somebody reads the front page and then opens the report behind it.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core import charts
from apps.core.context import tenant_context
from apps.reports import services as figures

pytestmark = pytest.mark.django_db


def _sell(shop, branch, who, variant, qty=1, when=None):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=branch, user=who):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}],
                             sold_at=when)


# -- the little charts -----------------------------------------------------


def test_a_flat_series_is_drawn_through_the_middle_not_along_the_floor():
    """Three identical days is not a collapse to zero."""
    line = charts.spark([500, 500, 500])
    heights = {point.split(",")[1] for point in line["line"].split()}
    assert len(heights) == 1 and float(heights.pop()) == pytest.approx(14)


def test_nothing_at_all_draws_nothing():
    assert charts.spark([0, 0, 0]) is None
    assert charts.curve([{"label": "x", "value": 0}]) is None


def test_the_big_chart_is_anchored_at_zero():
    """A line starting halfway up an axis marked zero is a lie."""
    c = charts.curve([{"label": "a", "value": 0}, {"label": "b", "value": 100}])
    first, second = c["line"].split()
    assert float(first.split(",")[1]) > float(second.split(",")[1])
    assert c["grid"][-1]["value"] == 0


def test_there_is_nothing_to_compare_a_first_week_against():
    assert charts.change(1000, 0) is None
    assert charts.change(110, 100) == 10.0
    assert charts.change(90, 100) == -10.0


# -- the figures behind them ----------------------------------------------


def test_takings_are_net_of_refunds(shop, main_branch, stocked, owner):
    from apps.pos.services import create_return

    sale = _sell(shop, main_branch, owner, stocked["Mkate"], qty=2)  # 3,000
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(sale, {sale.lines.first().pk: 1}, reason="x")
        today = timezone.localdate()
        took = figures.takings([main_branch], today, today)
    assert took["net"] == Decimal("1500") and took["refunded"] == Decimal("1500")


def test_a_quiet_day_still_gets_a_point_on_the_line(shop, main_branch, stocked, owner):
    """Otherwise the line joins Monday to Friday and draws a week that never happened."""
    today = timezone.localdate()
    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop, branch=main_branch):
        rows = figures.by_day([main_branch], today - timedelta(days=3), today)
    assert len(rows) == 4
    assert [bool(r["count"]) for r in rows] == [False, False, False, True]


def test_profit_uses_the_cost_stored_on_the_line(shop, main_branch, stocked, owner):
    """Sukari sells at 3,000 and cost 2,400, whatever it costs today."""
    _sell(shop, main_branch, owner, stocked["Sukari 1kg"])
    with tenant_context(shop, branch=main_branch):
        today = timezone.localdate()
        made = figures.gross_profit([main_branch], today, today)
    # Revenue here is net of the VAT already inside the shelf price.
    assert made["cost"] == Decimal("2400")
    assert made["revenue"] < Decimal("3000")
    assert made["profit"] == made["revenue"] - made["cost"]


def test_best_sellers_are_ranked_by_money_not_by_count(shop, main_branch, stocked, owner):
    _sell(shop, main_branch, owner, stocked["Soda 500ml"], qty=20)   # 20,000
    _sell(shop, main_branch, owner, stocked["Sukari 1kg"], qty=10)   # 30,000
    with tenant_context(shop, branch=main_branch):
        today = timezone.localdate()
        top = figures.top_products([main_branch], today, today)
    assert [row["item"] for row in top] == ["Sukari 1kg", "Soda 500ml"]
    assert top[0]["pct"] == 100


# -- the page ---------------------------------------------------------------


def test_the_period_asked_for_is_the_period_shown(client, shop, main_branch, stocked, owner):
    client.force_login(owner)
    r = client.get(reverse("core:dashboard") + "?range=30d")
    assert r.context["period"]["key"] == "30d" and r.context["period"]["days"] == 30
    assert r.context["period"]["previous_end"] == r.context["period"]["start"] - timedelta(days=1)


def test_a_typo_in_the_address_bar_is_not_a_500(client, shop, owner):
    client.force_login(owner)
    r = client.get(reverse("core:dashboard") + "?range=fortnight")
    assert r.status_code == 200 and r.context["period"]["key"] == "7d"


def test_a_single_day_is_plotted_hour_by_hour(client, shop, main_branch, stocked, owner):
    """One point is not a chart."""
    _sell(shop, main_branch, owner, stocked["Mkate"])
    client.force_login(owner)
    rows = client.get(reverse("core:dashboard") + "?range=today").context["chart_rows"]
    assert len(rows) > 1 and rows[0]["label"].endswith(":00")


def test_the_week_before_is_what_the_arrows_compare_against(
        client, shop, main_branch, stocked, owner):
    now = timezone.now()
    _sell(shop, main_branch, owner, stocked["Mkate"], qty=2, when=now - timedelta(days=9))
    _sell(shop, main_branch, owner, stocked["Mkate"], qty=1)
    client.force_login(owner)
    takings = client.get(reverse("core:dashboard")).context["cards"][0]
    # 1,500 this week against 3,000 the week before.
    assert takings["label"] == "Takings" and takings["change"] == -50.0


def test_a_cashier_gets_no_takings_no_chart_and_no_feed(
        client, shop, main_branch, stocked, owner, cashier):
    from apps.accounts.models import Membership, Role

    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier,
                                  role=Role.objects.get(name="Cashier"))
    client.force_login(cashier)
    context = client.get(reverse("core:dashboard")).context
    assert "cards" not in context and "chart" not in context and "feed" not in context


def test_profit_is_only_for_those_who_may_see_margins(
        client, shop, main_branch, stocked, owner, cashier):
    from apps.accounts.models import Membership, Role

    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier,
                                  role=Role.objects.get(name="Manager"))
    client.force_login(owner)
    assert any(c["label"] == "Gross profit"
               for c in client.get(reverse("core:dashboard")).context["cards"])


def test_an_action_code_is_turned_into_words(shop, main_branch, owner):
    from apps.core.audit import describe, record

    with tenant_context(shop, branch=main_branch, user=owner):
        row = record("stock.adjusted")
    said = describe(row)
    assert said["text"] == "Stock adjusted" and said["icon"] == "layers"
    assert said["who"] == "Salma"


# -- the platform's own overview -------------------------------------------


def test_the_platform_overview_compares_what_it_can_and_says_so(client, shop, owner):
    """
    Money collected and shops joined are stamped with a day and can be
    compared. Recurring revenue and what is owed are true right now and have
    no yesterday, so they are marked as standing figures instead of being
    given an invented trend.
    """
    from decimal import Decimal

    from apps.tenancy.models import Invoice, InvoiceStatus

    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    now = timezone.now()
    Invoice.objects.create(
        tenant=shop, number="INV-1", period_start=now.date(), period_end=now.date(),
        amount=Decimal("100000"), total=Decimal("100000"),
        status=InvoiceStatus.PAID, paid_at=now - timedelta(days=1),
    )

    client.force_login(owner)
    cards = {c["label"]: c for c in
             client.get(reverse("platform:dashboard")).context["cards"]}

    assert cards["Collected"]["value"] == Decimal("100000")
    assert cards["Collected"]["spark"] is not None
    assert cards["Monthly recurring"]["standing"] is True
    assert cards["Monthly recurring"]["change"] is None
    assert cards["Owed to you"]["standing"] is True


def test_the_platform_overview_answers_for_the_period_asked_for(client, shop, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    client.force_login(owner)
    r = client.get(reverse("platform:dashboard") + "?range=today")
    assert r.context["period"]["key"] == "today" and r.context["period"]["days"] == 1

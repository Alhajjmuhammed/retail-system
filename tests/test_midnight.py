"""
The day a sale belongs to.

Every daily figure in this system groups by date in the shop's own timezone.
A sale at 23:58 belongs to that day; one at 00:02 belongs to the next. If
any screen disagrees, a shopkeeper counting their drawer at closing time is
told a number that does not match the cash in front of them.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.reports import services as figures

pytestmark = pytest.mark.django_db

DAR = ZoneInfo("Africa/Dar_es_Salaam")


def _sell_at(shop, branch, who, variant, local_dt, qty=1):
    """A sale rung up at a wall-clock time in the shop's own timezone."""
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    when = local_dt.replace(tzinfo=DAR)
    with tenant_context(shop, branch=branch, user=who):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}],
                             sold_at=when)


def test_a_sale_just_before_midnight_belongs_to_that_day(shop, main_branch, stocked, owner):
    day = timezone.localdate() - timedelta(days=2)
    late = datetime.combine(day, time(23, 58))
    early = datetime.combine(day + timedelta(days=1), time(0, 2))

    _sell_at(shop, main_branch, owner, stocked["Mkate"], late)        # 1,500
    _sell_at(shop, main_branch, owner, stocked["Mkate"], early, qty=2)  # 3,000

    with tenant_context(shop, branch=main_branch):
        first = figures.takings([main_branch], day, day)
        second = figures.takings([main_branch], day + timedelta(days=1),
                                 day + timedelta(days=1))
    assert first["net"] == Decimal("1500"), "the 23:58 sale fell out of its own day"
    assert second["net"] == Decimal("3000"), "the 00:02 sale fell out of its own day"


def test_the_daily_line_puts_each_sale_on_the_right_point(shop, main_branch, stocked, owner):
    day = timezone.localdate() - timedelta(days=2)
    _sell_at(shop, main_branch, owner, stocked["Mkate"], datetime.combine(day, time(23, 58)))

    with tenant_context(shop, branch=main_branch):
        rows = figures.by_day([main_branch], day, day + timedelta(days=1))
    by_date = {row["day"]: row["value"] for row in rows}
    assert by_date[day] == Decimal("1500")
    assert by_date[day + timedelta(days=1)] == Decimal("0")


def test_the_hourly_view_uses_the_shops_clock(shop, main_branch, stocked, owner):
    """A sale at 20:30 local should appear at 20:00, not at 17:00 UTC."""
    today = timezone.localdate()
    _sell_at(shop, main_branch, owner, stocked["Mkate"], datetime.combine(today, time(20, 30)))
    with tenant_context(shop, branch=main_branch):
        rows = figures.by_hour([main_branch], today)
    busy = [row for row in rows if row["value"]]
    assert busy and busy[0]["label"] == "20:00", [r["label"] for r in busy]


def test_the_dashboard_and_the_report_agree_across_midnight(client, shop, main_branch,
                                                            stocked, owner):
    day = timezone.localdate() - timedelta(days=1)
    _sell_at(shop, main_branch, owner, stocked["Mkate"], datetime.combine(day, time(23, 59)))

    client.force_login(owner)
    dash = client.get(reverse("core:dashboard") + "?range=7d")
    report = client.get(reverse("reports:index") + "?preset=7d")
    takings = next(c for c in dash.context["cards"] if c["label"] == "Takings")
    assert takings["value"] == report.context["totals"]["net"]

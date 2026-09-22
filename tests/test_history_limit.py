"""
A shop that has been trading longer than its plan lets it look back.

"Thirty days of history" is a plan limit, and a shop on the Free plan after
a year has eleven months of sales it may not see. Nothing is deleted: the
question is whether every screen clamps to the same day, or whether one of
them quietly shows a figure the plan does not include.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.core.features import LIMIT_HISTORY_DAYS

pytestmark = pytest.mark.django_db


def _sell_on(shop, branch, who, variant, day, qty=1):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    when = timezone.make_aware(
        timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=12)))
    with tenant_context(shop, branch=branch, user=who):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}],
                             sold_at=when)


@pytest.fixture
def short_memory(shop, main_branch, stocked, owner, free_plan):
    """A shop on a plan that keeps thirty days, with older sales than that."""
    today = timezone.localdate()
    _sell_on(shop, main_branch, owner, stocked["Mkate"], today - timedelta(days=200), qty=10)
    _sell_on(shop, main_branch, owner, stocked["Mkate"], today - timedelta(days=5))
    shop.subscription.plan = free_plan
    shop.subscription.save(update_fields=["plan"])
    return shop


def test_the_plan_limit_is_a_real_date(short_memory):
    floor = short_memory.history_start()
    assert floor is not None
    assert floor == timezone.localdate() - timedelta(days=29)
    assert short_memory.limit_for(LIMIT_HISTORY_DAYS) == 30


def test_the_dashboard_cannot_be_asked_past_the_limit(client, short_memory, owner):
    client.force_login(owner)
    page = client.get(reverse("core:dashboard") + "?range=30d")
    period = page.context["period"]
    assert period["start"] >= short_memory.history_start()


def test_the_report_clamps_too_and_the_two_agree(client, short_memory, owner):
    client.force_login(owner)
    dash = client.get(reverse("core:dashboard") + "?range=30d")
    report = client.get(reverse("reports:index") + "?preset=30d")
    assert report.context["start"] >= short_memory.history_start()
    takings = next(c for c in dash.context["cards"] if c["label"] == "Takings")
    assert takings["value"] == report.context["totals"]["net"]


def test_a_hand_typed_date_before_the_limit_is_refused(client, short_memory, owner):
    """The address bar is not a way around the plan."""
    client.force_login(owner)
    long_ago = (timezone.localdate() - timedelta(days=300)).isoformat()
    report = client.get(reverse("reports:index") + f"?from={long_ago}&to={timezone.localdate()}")
    assert report.context["start"] >= short_memory.history_start()
    # The old sale's money is not in the figures.
    assert report.context["totals"]["net"] == Decimal("1500")


def test_upgrading_shows_it_all_again(client, short_memory, owner, business_plan):
    """Nothing was deleted -- it was only out of view."""
    short_memory.subscription.plan = business_plan
    short_memory.subscription.save(update_fields=["plan"])
    short_memory.refresh_from_db()
    assert short_memory.history_start() is None

    client.force_login(owner)
    long_ago = (timezone.localdate() - timedelta(days=300)).isoformat()
    report = client.get(reverse("reports:index") + f"?from={long_ago}&to={timezone.localdate()}")
    assert report.context["totals"]["net"] == Decimal("16500")   # 10 loaves + 1

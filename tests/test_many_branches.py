"""
A chain, not a duka.

Everything else here has one or two branches. The plan allows five and the
Enterprise plan more, so the scoping, the figures and the cost of a page all
have to hold when there are fifteen.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role
from apps.core.context import tenant_context
from apps.reports import services as figures

pytestmark = pytest.mark.django_db


@pytest.fixture
def chain(shop, main_branch, stocked, owner):
    """
    Fifteen branches, each with a sale of its own.

    On the Enterprise plan: the Business plan allows five and enforces it,
    which is the system working -- a chain this size is a plan decision.
    """
    from apps.tenancy.models import Plan

    shop.subscription.plan = Plan.objects.get(code="enterprise")
    shop.subscription.save(update_fields=["plan"])
    shop.refresh_from_db()

    from apps.catalog.models import Price, PriceList
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement
    from apps.org.models import Branch
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    branches = [main_branch]
    variant = stocked["Mkate"]          # 1,500
    with tenant_context(shop):
        for n in range(2, 16):
            branches.append(Branch.objects.create(name=f"Branch {n}"))
        assert Price.objects.filter(
            price_list=PriceList.objects.get(is_default=True), variant=variant).exists()

    for index, branch in enumerate(branches, start=1):
        with tenant_context(shop, branch=branch, user=owner):
            record_movement(variant=variant, qty_delta=Decimal("50"),
                            reason=MovementReason.PURCHASE, branch=branch,
                            unit_cost=Decimal("1100"))
            cart = new_cart(branch=branch)
            add_to_cart(cart, variant, qty=index)      # branch n sells n loaves
            complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}])
    return branches


def test_each_branch_keeps_its_own_stock(shop, chain, stocked):
    from apps.inventory.models import StockItem

    with tenant_context(shop):
        rows = {item.branch.name: item.qty_on_hand
                for item in StockItem.objects.select_related("branch")
                .filter(variant=stocked["Mkate"])}
    assert len(rows) == 15
    # Main already held 100 from the fixture, then 50 in and 1 sold.
    assert rows["Main"] == Decimal("149")
    assert rows["Branch 15"] == Decimal("35")     # 50 in, 15 sold


def test_a_manager_sees_only_the_branches_they_work_in(shop, chain, cashier):
    with tenant_context(shop):
        person = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Manager"))
        for branch in chain[:3]:
            person.branch_links.create(branch=branch)
        mine = list(person.branches())

    assert len(mine) == 3
    today = timezone.localdate()
    with tenant_context(shop):
        took = figures.takings(mine, today, today)
    # Branches 1, 2 and 3 sold 1 + 2 + 3 loaves at 1,500.
    assert took["net"] == Decimal("9000")


def test_the_owner_sees_the_whole_chain(shop, chain, owner):
    today = timezone.localdate()
    with tenant_context(shop):
        everything = list(Role.objects.none()) or chain
        took = figures.takings(everything, today, today)
    # 1 + 2 + ... + 15 = 120 loaves.
    assert took["net"] == Decimal("180000")


def test_a_page_does_not_cost_more_because_the_chain_is_bigger(client, shop, chain, owner):
    """
    Fifteen branches must not mean fifteen more queries. The sidebar, the
    branch picker and the figures all take a list of branches; any of them
    could have asked per branch instead.
    """
    client.force_login(owner)
    # Warm the caches first: the first request of a session fills the
    # permission and plan caches, and comparing a cold request with a warm
    # one measures the cache, not the branches.
    client.get(reverse("core:dashboard"), HTTP_HOST="testserver")

    with CaptureQueriesContext(connection) as many:
        client.get(reverse("core:dashboard"), HTTP_HOST="testserver")
    with tenant_context(shop):
        from apps.org.models import Branch
        Branch.objects.exclude(name__in=["Main", "Branch 2"]).update(is_active=False)
    client.get(reverse("core:dashboard"), HTTP_HOST="testserver")
    with CaptureQueriesContext(connection) as few:
        client.get(reverse("core:dashboard"), HTTP_HOST="testserver")

    grew = len(many.captured_queries) - len(few.captured_queries)
    assert grew <= 3, f"{grew} more queries for 13 more branches"


def test_the_reports_can_be_narrowed_to_one_of_many(client, shop, chain, owner):
    client.force_login(owner)
    one = chain[4]                                  # Branch 5, sold 5 loaves
    page = client.get(reverse("reports:index") + f"?branch={one.pk}&preset=today")
    assert page.context["totals"]["net"] == Decimal("7500")
    assert len(page.context["all_branches"]) == 15

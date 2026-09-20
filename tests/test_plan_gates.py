"""
What a plan sells has to be what a plan withholds.

The Business plan sells "a second price list for bulk buyers". The check only
looked at the list's *kind*, so a shop without the feature created a second
list called "retail", attached it to its bulk customers and charged wholesale
prices off it. The feature was sold and never actually withheld.
"""

import pytest
from django.urls import reverse

from apps.catalog.models import PriceList
from apps.core.context import tenant_context
from apps.core.features import WHOLESALE_PRICING

pytestmark = pytest.mark.django_db


def _plan_without(shop, feature):
    """Strip one feature from this shop's plan."""
    shop.subscription.plan.features.filter(feature_key=feature).delete()
    shop.subscription.plan.save()
    return shop


def test_a_shop_without_the_feature_gets_one_price_list(client, shop, owner):
    _plan_without(shop, WHOLESALE_PRICING)
    client.force_login(owner)
    with tenant_context(shop):
        before = PriceList.objects.count()

    for kind in ("wholesale", "retail"):
        client.post(reverse("catalog:price_lists"),
                    {"action": "create", "name": f"Bulk {kind}", "kind": kind})

    with tenant_context(shop):
        assert PriceList.objects.count() == before, (
            "Neither a wholesale list nor a second retail list may be created "
            "without the feature the plan charges for."
        )


def test_the_shop_is_told_why(client, shop, owner):
    _plan_without(shop, WHOLESALE_PRICING)
    client.force_login(owner)
    client.post(reverse("catalog:price_lists"),
                {"action": "create", "name": "Bulk", "kind": "retail"})
    page = client.get(reverse("catalog:price_lists"))
    said = " ".join(str(m) for m in page.context["messages"])
    assert "not included in your plan" in said


def test_a_shop_that_pays_for_it_can_have_both(client, shop, owner):
    client.force_login(owner)          # the fixture's plan includes the feature
    with tenant_context(shop):
        before = PriceList.objects.count()
    client.post(reverse("catalog:price_lists"),
                {"action": "create", "name": "Wholesale", "kind": "wholesale"})
    client.post(reverse("catalog:price_lists"),
                {"action": "create", "name": "Staff prices", "kind": "retail"})
    with tenant_context(shop):
        assert PriceList.objects.count() == before + 2

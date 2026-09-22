"""
A till that has been offline for days.

The queue is meant to survive a week without a line. What arrives afterwards
has to land on the day it was actually sold, be countable in that day's
takings, and still be refused if it is impossibly old -- a device left in a
drawer for a month is not a sale, it is a story.
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.pos.models import Sale
from apps.pos.services import open_shift
from apps.reports import services as figures

pytestmark = pytest.mark.django_db


@pytest.fixture
def till(shop, main_branch, register):
    with tenant_context(shop, branch=main_branch):
        person = User.objects.create_user("offline@shop.test", "pw", name="Offline")
        membership = Membership.objects.create(
            tenant=shop, user=person, role=Role.objects.get(name="Cashier"))
        membership.branch_links.create(branch=main_branch)
    with tenant_context(shop, branch=main_branch, user=person):
        shift = open_shift(register=register, opening_float=0)
    return person, shift


def _push(client, shift_id, *sales):
    return client.post(
        reverse("sync:push_sales"),
        data=json.dumps({"device_id": "till-offline", "shift_id": shift_id,
                         "sales": list(sales)}),
        content_type="application/json",
    ).json()


def _sale(variant, when, qty=1, price=3000):
    return {"client_uuid": str(uuid.uuid4()), "sold_at": when.isoformat(),
            "lines": [{"variant_id": variant.pk, "qty": qty, "unit_price": price}],
            "payments": [{"method": "cash", "amount": price * qty}]}


def test_three_days_of_queued_sales_land_on_their_own_days(client, shop, main_branch,
                                                           stocked, till):
    person, shift = till
    client.force_login(person)
    variant = stocked["Sukari 1kg"]
    today = timezone.localdate()

    queued = [_sale(variant, timezone.now() - timedelta(days=n, hours=2)) for n in (3, 2, 1)]
    result = _push(client, shift.pk, *queued)
    assert len(result["accepted"]) == 3, result

    with tenant_context(shop, branch=main_branch):
        for n in (3, 2, 1):
            day = today - timedelta(days=n)
            assert figures.takings([main_branch], day, day)["net"] == Decimal("3000"), day


def test_a_sale_from_a_month_ago_is_refused_with_a_reason(client, shop, stocked, till):
    person, shift = till
    client.force_login(person)
    result = _push(client, shift.pk,
                   _sale(stocked["Sukari 1kg"], timezone.now() - timedelta(days=30)))
    assert not result["accepted"]
    assert result["rejected"], result
    assert "old" in json.dumps(result["rejected"]).lower()
    with tenant_context(shop):
        assert Sale.objects.count() == 0


def test_a_till_with_a_wrong_clock_is_recorded_now_and_flagged(client, shop, main_branch,
                                                               stocked, till):
    person, shift = till
    client.force_login(person)
    result = _push(client, shift.pk,
                   _sale(stocked["Sukari 1kg"], timezone.now() + timedelta(days=2)))
    assert len(result["accepted"]) == 1, result
    with tenant_context(shop, branch=main_branch):
        sale = Sale.objects.get()
    assert sale.needs_review, "a sale from the future should be checked by somebody"
    assert sale.sold_at <= timezone.now() + timedelta(minutes=5)


def test_a_queued_sale_still_syncs_after_the_product_was_archived(client, shop, main_branch,
                                                                  stocked, till):
    """The money was taken days ago; the catalogue changing does not undo it."""
    from apps.catalog.models import Product

    person, shift = till
    variant = stocked["Soda 500ml"]
    with tenant_context(shop):
        Product.objects.filter(pk=variant.product_id).update(is_active=False)

    client.force_login(person)
    result = _push(client, shift.pk,
                   _sale(variant, timezone.now() - timedelta(days=1), price=1000))
    assert len(result["accepted"]) == 1, result


def test_the_same_queued_sale_sent_twice_is_stored_once(client, shop, stocked, till):
    """A till that loses the reply retries; it must not sell twice."""
    person, shift = till
    client.force_login(person)
    entry = _sale(stocked["Sukari 1kg"], timezone.now() - timedelta(days=1))
    first = _push(client, shift.pk, entry)
    second = _push(client, shift.pk, entry)
    assert len(first["accepted"]) == 1 and len(second["accepted"]) == 1
    with tenant_context(shop):
        assert Sale.objects.count() == 1

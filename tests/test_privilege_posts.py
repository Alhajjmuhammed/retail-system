"""
A refused page must also be a refused action.

Hiding a link is a courtesy and checking on GET is half a guard: the request
that matters is the POST somebody can write by hand. Every one of these is a
privileged action attempted by somebody who holds the till and nothing else.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def till_only(shop, main_branch):
    """Somebody with the cashier's role: sells, and nothing more."""
    person = User.objects.create_user("juma@till.test", "pw", name="Juma")
    with tenant_context(shop):
        membership = Membership.objects.create(
            tenant=shop, user=person, role=Role.objects.get(name="Cashier"))
        membership.branch_links.create(branch=main_branch)
    return person


def _refused(response):
    """Either turned away, or sent to sign in / somewhere safe."""
    return response.status_code in (403, 404) or (
        (response.status_code == 302 and "login" in response.url) or
        (response.status_code == 302 and response.url in ("/", reverse("core:dashboard")))
    )


def test_a_cashier_cannot_rewrite_a_role(client, shop, till_only):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(till_only)
    response = client.post(reverse("accounts:role_edit", args=[role.pk]), {
        "name": "Cashier", "description": "", "mode": "simple", "job:admin": "on",
    })
    assert _refused(response), response.status_code
    with tenant_context(shop):
        held = {rp.permission.code for rp in Role.objects.get(pk=role.pk).permissions.all()}
    assert "user.manage" not in held and "settings.edit" not in held


def test_a_cashier_cannot_change_the_business_settings(client, shop, till_only):
    client.force_login(till_only)
    response = client.post(reverse("org:business"), {
        "name": "Not their shop", "cost_method": "last_cost",
    })
    assert _refused(response), response.status_code
    shop.refresh_from_db()
    assert shop.name != "Not their shop"


def test_a_cashier_cannot_adjust_stock(client, shop, main_branch, stocked, till_only):
    from apps.inventory.models import StockItem

    with tenant_context(shop, branch=main_branch):
        item = StockItem.objects.filter(variant=stocked["Sukari 1kg"]).first()
        before = item.qty_on_hand
    client.force_login(till_only)
    response = client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                           {"qty": "999", "reason": "adjustment", "note": "mine now"})
    assert _refused(response), response.status_code
    with tenant_context(shop, branch=main_branch):
        assert StockItem.objects.get(pk=item.pk).qty_on_hand == before


def test_a_cashier_cannot_invite_staff(client, shop, till_only):
    client.force_login(till_only)
    response = client.post(reverse("accounts:staff_invite"),
                           {"email": "friend@x.test", "role": ""})
    assert _refused(response), response.status_code
    with tenant_context(shop):
        assert not User.objects.filter(email="friend@x.test").exists()


def test_a_cashier_cannot_switch_off_a_till(client, shop, main_branch, till_only):
    from apps.org.models import Device

    with tenant_context(shop):
        device = Device.objects.create(branch=main_branch, device_id="abc-123",
                                       kind="till", label="Till 1")
    client.force_login(till_only)
    response = client.post(reverse("org:device_update", args=[device.pk]),
                           {"action": "block"})
    assert _refused(response), response.status_code
    with tenant_context(shop):
        assert Device.objects.get(pk=device.pk).is_active


def test_a_cashier_cannot_reach_the_platform(client, shop, till_only):
    client.force_login(till_only)
    for name in ("platform:dashboard", "platform:tenant_list", "platform:invoices"):
        response = client.get(reverse(name))
        assert response.status_code in (403, 404, 302), f"{name} -> {response.status_code}"


def test_a_cashier_cannot_approve_their_own_short_drawer(client, shop, main_branch,
                                                         register, till_only, owner):
    from decimal import Decimal

    from apps.pos.services import close_shift, open_shift

    with tenant_context(shop, branch=main_branch, user=till_only):
        shift = open_shift(register=register, opening_float=Decimal("1000"))
        close_shift(shift, counted_cash=Decimal("500"), note="short")
    client.force_login(till_only)
    response = client.post(reverse("finance:cashups"),
                           {"action": "approve", "shift": shift.pk})
    assert _refused(response) or response.status_code == 200
    with tenant_context(shop):
        from apps.pos.models import Shift
        assert Shift.objects.get(pk=shift.pk).approved_by_id is None

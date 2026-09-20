"""
Moving yourself between the branches you work in.

``Membership.active_branch`` has always preferred a branch held in the session,
but nothing ever put one there, so whoever covered two shops was stuck in the
default one. That stranded stock: a transfer is accepted by the branch it
arrives at, and nobody could stand in that branch to accept it.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.org.models import Branch

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_branches(shop, main_branch):
    with tenant_context(shop):
        return main_branch, Branch.objects.create(tenant=shop, name="Nungwi")


def test_somebody_who_covers_two_shops_can_stand_in_either(client, shop, owner, two_branches):
    here, there = two_branches
    client.force_login(owner)

    assert client.get(reverse("core:dashboard")).context["branch"] == here

    moved = client.post(reverse("accounts:switch_branch"), {"branch_id": there.pk})
    assert moved.status_code == 302
    assert client.session["branch_id"] == there.pk
    assert client.get(reverse("core:dashboard")).context["branch"] == there


def test_you_cannot_move_into_a_branch_you_do_not_work_in(client, shop, main_branch, two_branches):
    here, there = two_branches
    with tenant_context(shop):
        clerk = User.objects.create_user("clerk@x.test", "pw", name="Clerk")
        m = Membership.objects.create(tenant=shop, user=clerk,
                                      role=Role.objects.get(name="Cashier"))
        m.set_branches([here], all_branches=False)
    client.force_login(clerk)

    client.post(reverse("accounts:switch_branch"), {"branch_id": there.pk})
    assert "branch_id" not in client.session
    assert client.get(reverse("core:dashboard")).context["branch"] == here


def test_a_made_up_branch_id_is_refused(client, shop, owner, two_branches):
    client.force_login(owner)
    client.post(reverse("accounts:switch_branch"), {"branch_id": 999999})
    assert "branch_id" not in client.session


def test_the_switch_only_returns_to_our_own_pages(client, shop, owner, two_branches):
    """`next` is user input, so it cannot be used to bounce somebody off-site."""
    here, there = two_branches
    client.force_login(owner)
    away = client.post(reverse("accounts:switch_branch"),
                       {"branch_id": there.pk, "next": "https://evil.example/steal"})
    assert away.status_code == 302
    assert "evil.example" not in away["Location"]

    back = client.post(reverse("accounts:switch_branch"),
                       {"branch_id": here.pk, "next": reverse("inventory:transfer_list")})
    assert back["Location"] == reverse("inventory:transfer_list")


def test_switching_shop_forgets_the_branch(client, shop, owner, two_branches):
    """A branch id from one business must not leak into another."""
    _, there = two_branches
    client.force_login(owner)
    client.post(reverse("accounts:switch_branch"), {"branch_id": there.pk})
    assert client.session["branch_id"] == there.pk
    client.post(reverse("accounts:switch"), {"tenant_id": shop.pk})
    assert "branch_id" not in client.session


def test_stock_sent_to_another_branch_can_now_be_accepted_there(
        client, shop, owner, two_branches, stocked):
    """
    The whole point of the switcher: without it, stock sent between branches
    left the sending shelf and could never be accepted, because only the
    receiving branch may accept and nobody could stand there.
    """
    from apps.inventory.models import StockItem, Transfer, TransferStatus
    from apps.inventory.services import get_stock_item, receive_transfer, send_transfer

    here, there = two_branches
    sugar = stocked["Sukari 1kg"]
    with tenant_context(shop, branch=here, user=owner):
        get_stock_item(sugar, here)
        transfer = Transfer.objects.create(tenant=shop, reference="TR-TEST-1",
                                           from_branch=here, to_branch=there)
        transfer.lines.create(tenant=shop, variant=sugar, qty_sent=5)
        send_transfer(transfer)
    client.force_login(owner)

    # Standing in the sending branch, the page offers no way to accept.
    page = client.get(reverse("inventory:transfer_detail", args=[transfer.pk]))
    assert page.context["can_receive"] is False

    client.post(reverse("accounts:switch_branch"), {"branch_id": there.pk})
    page = client.get(reverse("inventory:transfer_detail", args=[transfer.pk]))
    assert page.context["can_receive"] is True

    with tenant_context(shop, branch=there, user=owner):
        receive_transfer(transfer, {transfer.lines.first().pk: 5})
        transfer.refresh_from_db()
        assert transfer.status == TransferStatus.RECEIVED
        assert StockItem.objects.get(branch=there, variant=sugar).qty_on_hand == 5

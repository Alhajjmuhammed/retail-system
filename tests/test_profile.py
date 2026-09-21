"""
Your own account page.

Everybody reaches it, whatever their role holds: a cashier who cannot open a
single settings page still has a name that might be spelled wrong and a phone
number that changes. What nobody may change here is anything that would be a
way around their own permissions.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier(shop, main_branch):
    with tenant_context(shop):
        user = User.objects.create_user("juma@x.test", "pw", name="Juma Ally")
        m = Membership.objects.create(tenant=shop, user=user,
                                      role=Role.objects.get(name="Cashier"))
        m.set_branches([main_branch], all_branches=False)
    return user


def test_somebody_with_almost_no_permissions_still_has_an_account_page(client, shop, cashier):
    client.force_login(cashier)
    assert client.get(reverse("accounts:profile")).status_code == 200
    assert client.get(reverse("accounts:my_permissions")).status_code == 200


def test_a_role_that_holds_nothing_still_has_one(client, shop, main_branch):
    with tenant_context(shop):
        empty = Role.objects.create(tenant=shop, name="New role")
        user = User.objects.create_user("new@x.test", "pw", name="Baraka")
        Membership.objects.create(tenant=shop, user=user, role=empty)
    client.force_login(user)
    page = client.get(reverse("accounts:my_permissions"))
    assert page.status_code == 200
    assert page.context["held"] == 0


def test_you_may_fix_your_own_name_and_number(client, shop, cashier):
    client.force_login(cashier)
    client.post(reverse("accounts:profile"),
                {"action": "details", "name": "Juma Ally Mwinyi", "phone": "0766554433"})
    cashier.refresh_from_db()
    assert cashier.name == "Juma Ally Mwinyi"
    assert cashier.phone == "0766554433"


def test_a_name_cannot_be_emptied(client, shop, cashier):
    """It goes on every sale they make."""
    client.force_login(cashier)
    client.post(reverse("accounts:profile"), {"action": "details", "name": "  ", "phone": ""})
    cashier.refresh_from_db()
    assert cashier.name == "Juma Ally"


def test_you_cannot_change_your_own_email_or_role(client, shop, cashier):
    """Either would be a way around who the shop thinks you are."""
    client.force_login(cashier)
    with tenant_context(shop):
        manager = Role.objects.get(name="Manager")
    client.post(reverse("accounts:profile"), {
        "action": "details", "name": "Juma Ally", "phone": "",
        "email": "owner@x.test", "role": manager.pk,
    })
    cashier.refresh_from_db()
    assert cashier.email == "juma@x.test"
    with tenant_context(shop):
        assert Membership.objects.get(user=cashier).role.name == "Cashier"


def test_you_set_your_own_approval_pin(client, shop, cashier):
    """A PIN somebody else typed in for you is not yours."""
    client.force_login(cashier)
    client.post(reverse("accounts:profile"),
                {"action": "pin", "pin": "4321", "pin_again": "4321"})
    with tenant_context(shop):
        assert Membership.objects.get(user=cashier).check_pin("4321")


def test_a_pin_that_does_not_match_itself_is_refused(client, shop, cashier):
    client.force_login(cashier)
    client.post(reverse("accounts:profile"),
                {"action": "pin", "pin": "4321", "pin_again": "1234"})
    with tenant_context(shop):
        assert not Membership.objects.get(user=cashier).pin_hash


@pytest.mark.parametrize("pin", ["12", "abcd", "123456789", ""])
def test_a_pin_has_to_look_like_a_pin(client, shop, cashier, pin):
    client.force_login(cashier)
    client.post(reverse("accounts:profile"),
                {"action": "pin", "pin": pin, "pin_again": pin})
    with tenant_context(shop):
        assert not Membership.objects.get(user=cashier).pin_hash


def test_the_page_lists_what_the_role_actually_allows(client, shop, cashier):
    client.force_login(cashier)
    page = client.get(reverse("accounts:my_permissions"))
    held = {item["permission"].code
            for items in page.context["groups"].values() for item in items}
    assert "pos.sell" in held
    assert "user.manage" not in held, "a cashier does not manage staff"


def test_a_personal_exception_is_shown_as_one(client, shop, cashier):
    """The part somebody will want to argue about is the part worth naming."""
    from apps.accounts.models import Permission, UserPermission

    with tenant_context(shop):
        membership = Membership.objects.get(user=cashier)
        UserPermission.objects.create(
            membership=membership,
            permission=Permission.objects.get(code="pos.refund"),
            effect="allow",
        )
    client.force_login(cashier)
    page = client.get(reverse("accounts:my_permissions"))
    personal = [item for items in page.context["groups"].values()
                for item in items if item["personal"]]
    assert [item["permission"].code for item in personal] == ["pos.refund"]


def test_signed_out_nobody_sees_anybody(client):
    assert client.get(reverse("accounts:profile")).status_code == 302
    assert client.get(reverse("accounts:my_permissions")).status_code == 302

"""
Running the team must not mean owning the shop.

`user.manage` lets a Manager add and edit staff. It used to also let them
make themselves Owner, demote the real owner, and grant themselves or
anyone else permissions they never held -- every permission there is, in
three clicks. These pin each route shut.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Invitation, Membership, Role, User, UserPermission
from apps.core.context import tenant_context, unscoped

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(shop, owner):
    with tenant_context(shop):
        roles = {r.name: r for r in Role.objects.all()}
        manager = User.objects.create_user("manager@shop.test", "pw", name="Manager")
        cashier = User.objects.create_user("till@shop.test", "pw", name="Till")
        m_manager = Membership.objects.create(tenant=shop, user=manager, role=roles["Manager"])
        m_cashier = Membership.objects.create(tenant=shop, user=cashier, role=roles["Cashier"])
        m_owner = Membership.objects.get(user=owner)
    return {"roles": roles, "manager": manager, "m_manager": m_manager,
            "m_cashier": m_cashier, "m_owner": m_owner, "owner": owner}


def _edit(client, membership, **fields):
    data = {"name": membership.user.name, "email": membership.user.email,
            "role": membership.role_id, "all_branches": "on"}
    data.update(fields)
    return client.post(reverse("accounts:staff_edit", args=[membership.pk]), data)


def _role(membership):
    with unscoped():
        return Membership.objects_all.get(pk=membership.pk).role.name


def test_a_manager_cannot_make_themselves_owner(client, team):
    client.force_login(team["manager"])
    _edit(client, team["m_manager"], role=team["roles"]["Owner"].pk)
    assert _role(team["m_manager"]) == "Manager"


def test_a_manager_cannot_demote_the_owner(client, team):
    client.force_login(team["manager"])
    _edit(client, team["m_owner"], role=team["roles"]["Cashier"].pk)
    assert _role(team["m_owner"]) == "Owner"


def test_a_manager_cannot_make_somebody_else_owner(client, team):
    client.force_login(team["manager"])
    _edit(client, team["m_cashier"], role=team["roles"]["Owner"].pk)
    assert _role(team["m_cashier"]) == "Cashier"


def test_nobody_grants_themselves_exceptions(client, team):
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_edit", args=[team["m_manager"].pk]),
                {"action": "overrides", "override:role.manage": "grant"})
    with unscoped():
        assert not UserPermission.objects.filter(membership=team["m_manager"]).exists()


def test_a_manager_only_grants_what_they_hold(client, team):
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_edit", args=[team["m_cashier"].pk]), {
        "action": "overrides",
        "override:billing.manage": "grant",                              # not held
        "override:pos.discount": "grant", "override_limit:pos.discount": "50",  # over their 20
        "override:pos.void": "grant", "override_limit:pos.void": "1000",       # within 500,000
    })
    with unscoped():
        granted = set(UserPermission.objects.filter(membership=team["m_cashier"])
                      .values_list("permission__code", flat=True))
    assert granted == {"pos.void"}


def test_a_manager_cannot_invite_an_owner(client, team):
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_invite"),
                {"email": "boss2@shop.test", "role": team["roles"]["Owner"].pk})
    with unscoped():
        assert not Invitation.objects_all.filter(email="boss2@shop.test").exists()


def test_a_manager_cannot_remove_the_owner(client, team):
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_remove", args=[team["m_owner"].pk]))
    with unscoped():
        assert Membership.objects_all.filter(pk=team["m_owner"].pk).exists()


def test_the_owner_can_still_appoint_another_owner(client, team):
    client.force_login(team["owner"])
    _edit(client, team["m_manager"], role=team["roles"]["Owner"].pk)
    assert _role(team["m_manager"]) == "Owner"


def test_the_only_owner_cannot_be_demoted_even_by_themselves(client, team):
    client.force_login(team["owner"])
    _edit(client, team["m_owner"], role=team["roles"]["Manager"].pk)
    assert _role(team["m_owner"]) == "Owner"


def test_a_role_editor_cannot_widen_their_own_role(client, team):
    with tenant_context(team["m_manager"].tenant):
        from apps.accounts.models import Permission, RolePermission

        RolePermission.objects.create(
            role=team["roles"]["Manager"], permission=Permission.objects.get(code="role.manage"),
            granted=True,
        )
        team["m_manager"].invalidate_permissions()
    client.force_login(team["manager"])
    client.post(reverse("accounts:role_edit", args=[team["roles"]["Manager"].pk]),
                {"name": "Manager", "grant:billing.manage": "on"})
    with unscoped():
        from apps.accounts.models import RolePermission

        codes = set(RolePermission.objects.filter(role=team["roles"]["Manager"])
                    .values_list("permission__code", flat=True))
    assert "billing.manage" not in codes


def test_role_names_are_saved(client, team):
    client.force_login(team["owner"])
    client.post(reverse("accounts:role_edit", args=[team["roles"]["Cashier"].pk]),
                {"name": "Till operator", "description": "Front counter",
                 "grant:pos.operate": "on", "grant:pos.sell": "on"})
    team["roles"]["Cashier"].refresh_from_db()
    assert team["roles"]["Cashier"].name == "Till operator"

"""
The screens, end to end.

Signup is the first thing a shop does and the role builder is the screen that
sells the system, so both are exercised through the real request cycle --
middleware, permissions, templates and all.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.core.context import tenant_context
from apps.tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_signup_creates_a_working_shop(client):
    response = client.post(
        reverse("signup"),
        {
            "business_name": "Duka la Salma",
            "name": "Salma Juma",
            "email": "salma@example.com",
            "phone": "0777000000",
            "password": "correct-horse-battery",
        },
        follow=True,
    )
    assert response.status_code == 200

    tenant = Tenant.objects.get(name="Duka la Salma")
    with tenant_context(tenant):
        # A shop is usable the moment it is created: a trial, an owner, a
        # branch, a till, roles, units, tax rates and a price list.
        assert tenant.subscription.status == "trialing"
        assert Role.objects.count() == 4
        assert tenant.org_branch_set.count() == 1
        assert tenant.catalog_taxrate_set.count() == 3

        membership = Membership.objects.get(user__email="salma@example.com")
        assert membership.role.is_owner_role


def test_login_lands_on_the_dashboard(client, shop, owner):
    client.force_login(owner)
    response = client.get(reverse("core:dashboard"))
    assert response.status_code == 200
    assert b"Duka la Salma" in response.content


def test_cashier_cannot_open_the_role_builder(client, shop, cashier):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        Membership.objects.create(tenant=shop, user=cashier, role=role)

    client.force_login(cashier)
    response = client.get(reverse("accounts:roles"))
    assert response.status_code == 403


def test_owner_can_open_the_role_builder(client, shop, owner):
    client.force_login(owner)
    response = client.get(reverse("accounts:roles"))
    assert response.status_code == 200
    assert b"Cashier" in response.content


def test_role_builder_saves_permissions_and_limits(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")

    client.force_login(owner)
    response = client.post(
        reverse("accounts:role_edit", args=[role.pk]),
        {
            "name": "Cashier",
            "description": "Sells at the till",
            "grant:pos.sell": "on",
            "grant:pos.discount": "on",
            "limit:pos.discount": "12.5",
            "grant:product.view": "on",
        },
        follow=True,
    )
    assert response.status_code == 200

    with tenant_context(shop):
        role.refresh_from_db()
        codes = set(role.permissions.values_list("permission__code", flat=True))
        assert codes == {"pos.sell", "pos.discount", "product.view"}

        discount = role.permissions.get(permission__code="pos.discount")
        assert float(discount.limit_value) == 12.5

        # Un-ticked permissions are gone, not merely un-granted.
        assert not role.permissions.filter(permission__code="pos.operate").exists()


def test_owner_role_cannot_be_edited(client, shop, owner):
    with tenant_context(shop):
        owner_role = Role.objects.get(is_owner_role=True)

    client.force_login(owner)
    response = client.get(reverse("accounts:role_edit", args=[owner_role.pk]))
    assert response.status_code == 302
    assert response.url == reverse("accounts:roles")


def test_per_user_override_is_saved_and_applied(client, shop, owner, cashier):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        membership = Membership.objects.create(tenant=shop, user=cashier, role=role)
        assert not membership.can("stock.adjust", value=1000)

    client.force_login(owner)
    response = client.post(
        reverse("accounts:staff_edit", args=[membership.pk]),
        {"action": "overrides", "override:stock.adjust": "grant",
         "override_limit:stock.adjust": "75000"},
        follow=True,
    )
    assert response.status_code == 200

    with tenant_context(shop):
        membership.refresh_from_db()
        assert membership.can("stock.adjust", value=1000)
        assert not membership.can("stock.adjust", value=100000)


def test_signed_out_users_are_sent_to_login(client):
    response = client.get(reverse("core:dashboard"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response.url


def test_switching_business_changes_the_tenant(client, db, owner, business_plan):
    from apps.tenancy.services import create_tenant

    first, _ = create_tenant(name="Shop One", owner=owner, plan=business_plan)
    second, _ = create_tenant(name="Shop Two", owner=owner, plan=business_plan)

    client.force_login(owner)
    client.post(reverse("accounts:switch"), {"tenant_id": first.pk})
    assert client.session["tenant_id"] == first.pk

    client.post(reverse("accounts:switch"), {"tenant_id": second.pk})
    assert client.session["tenant_id"] == second.pk

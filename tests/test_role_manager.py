"""The Manager role, as a manager meets it."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, RolePermission, User
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(shop, main_branch):
    with tenant_context(shop):
        u = User.objects.create_user("mgr@x.test", "pw", name="Mgr")
        Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Manager"))
    return u


def test_new_managers_can_manage_tills_and_devices(client, manager):
    client.force_login(manager)
    assert client.get(reverse("org:devices")).status_code == 200
    assert client.get(reverse("org:branches")).status_code == 200


def test_staff_list_offers_no_actions_on_the_owner(client, shop, owner, manager):
    with tenant_context(shop):
        own = Membership.objects.get(user=owner)
        cashier = User.objects.create_user("c@x.test", "pw", name="C")
        cm = Membership.objects.create(tenant=shop, user=cashier,
                                       role=Role.objects.get(name="Cashier"))
    client.force_login(manager)
    rows = {m.pk: m for m in client.get(reverse("accounts:staff")).context["memberships"]}
    assert not rows[own.pk].may_manage
    assert rows[cm.pk].may_manage
    assert client.get(reverse("accounts:staff_edit", args=[own.pk])).status_code == 403
    assert client.get(reverse("accounts:staff_edit", args=[cm.pk])).status_code == 200


def test_manager_cannot_reach_roles_billing_or_platform(client, manager):
    client.force_login(manager)
    assert client.get(reverse("accounts:roles")).status_code == 403
    assert client.get(reverse("tenancy:billing")).status_code == 403
    assert client.get("/platform/").status_code in (302, 403, 404)


def test_older_shops_managers_get_tills_too(shop):
    """The data migration's rule: whoever can change settings can manage tills."""
    import importlib

    from django.apps import apps as django_apps

    with tenant_context(shop):
        manager_role = Role.objects.get(name="Manager")
        RolePermission.objects.filter(role=manager_role,
                                      permission__code="register.manage").delete()
    migration = importlib.import_module(
        "apps.accounts.migrations.0010_grant_register_manage_to_managers")
    migration.forwards(django_apps, None)
    assert RolePermission.objects.filter(role=manager_role,
                                         permission__code="register.manage").exists()

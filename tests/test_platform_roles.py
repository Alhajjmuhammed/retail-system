"""
What each member of the platform team may do.

Not everybody who can see every shop should be able to delete one, issue
invoices or open a till. Each area is gated in the view, not only hidden in
the template, and nobody can hand out -- or write themselves -- more than
they already hold.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import PlatformRole, User

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


def _admin(email, role_name):
    return User.objects.create_user(
        email, "pw", name=email.split("@")[0].title(), is_platform_staff=True,
        platform_role=PlatformRole.objects.get(name=role_name),
    )


@pytest.fixture
def boss(db):
    return _admin("boss@platform.test", "Super admin")


@pytest.fixture
def support(db):
    return _admin("support@platform.test", "Support")


@pytest.fixture
def reader(db):
    return _admin("reader@platform.test", "Read only")


def test_the_default_roles_exist():
    names = set(PlatformRole.objects.values_list("name", flat=True))
    assert {"Super admin", "Support", "Billing", "Read only"} <= names
    assert PlatformRole.objects.get(name="Super admin").is_super


def test_a_role_only_reaches_its_own_pages(client, support):
    client.force_login(support)
    assert client.get(reverse("platform:tenant_list")).status_code == 200
    assert client.get(reverse("platform:user_list")).status_code == 200
    assert client.get(reverse("platform:invoices")).status_code == 403
    assert client.get(reverse("platform:plans")).status_code == 403
    assert client.get(reverse("platform:roles")).status_code == 403


def test_the_sidebar_shows_only_what_the_role_can_open(client, support):
    client.force_login(support)
    labels = [label for label, *_ in client.get(reverse("platform:tenant_list")).context["nav_items"]]
    assert "Shops" in labels and "People" in labels
    assert "Invoices" not in labels and "Admin roles" not in labels


def test_read_only_cannot_change_anything(client, reader, shop):
    client.force_login(reader)
    assert client.post(reverse("platform:tenant_suspend", args=[shop.pk])).status_code == 403
    assert client.post(reverse("platform:tenant_delete", args=[shop.pk])).status_code == 403
    assert client.post(reverse("platform:impersonate", args=[shop.pk])).status_code == 403
    shop.refresh_from_db()
    assert shop.status == "active"


def test_buttons_they_cannot_use_are_not_shown(client, reader, shop):
    client.force_login(reader)
    page = client.get(reverse("platform:tenant_list")).content
    assert b"New shop" not in page
    assert b"Open their account for support" not in page
    page = client.get(reverse("platform:tenant_detail", args=[shop.pk])).content
    assert b"Delete this shop" not in page and b"Add person" not in page


def test_a_role_without_the_overview_lands_on_what_it_can_see(client):
    role = PlatformRole.objects.create(name="Devices only", permissions=["devices.view"])
    person = User.objects.create_user("dev@platform.test", "pw", name="Dev",
                                      is_platform_staff=True, platform_role=role)
    client.force_login(person)
    response = client.get(reverse("platform:dashboard"))
    assert response.status_code == 302 and response["Location"] == reverse("platform:devices")


def test_support_cannot_take_over_another_admin(client, support, boss):
    """Resetting an admin's password is a way to become them."""
    client.force_login(support)
    client.post(reverse("platform:user_password", args=[boss.pk]), {"password": "taken-over-1"})
    boss.refresh_from_db()
    assert boss.check_password("pw")


def test_support_can_still_help_shop_staff(client, support, cashier):
    client.force_login(support)
    client.post(reverse("platform:user_password", args=[cashier.pk]), {"password": "fresh-pass-1"})
    cashier.refresh_from_db()
    assert cashier.check_password("fresh-pass-1")


def test_nobody_hands_out_more_than_they_hold(client, cashier):
    manager = PlatformRole.objects.create(
        name="Team lead", permissions=["people.view", "admins.manage"]
    )
    lead = User.objects.create_user("lead@platform.test", "pw", name="Lead",
                                    is_platform_staff=True, platform_role=manager)
    client.force_login(lead)
    client.post(reverse("platform:user_platform_access", args=[cashier.pk]),
                {"role": PlatformRole.objects.get(name="Super admin").pk})
    cashier.refresh_from_db()
    assert not cashier.is_platform_staff


def test_nobody_writes_themselves_a_bigger_role(client):
    lead_role = PlatformRole.objects.create(
        name="Team lead", permissions=["people.view", "admins.manage"]
    )
    lead = User.objects.create_user("lead@platform.test", "pw", name="Lead",
                                    is_platform_staff=True, platform_role=lead_role)
    client.force_login(lead)
    client.post(reverse("platform:role_edit", args=[lead_role.pk]),
                {"name": "Team lead", "perms": ["people.view", "admins.manage", "shops.delete"]})
    lead_role.refresh_from_db()
    assert "shops.delete" not in lead_role.permissions


def test_permissions_you_lack_are_kept_not_dropped(client):
    billing = PlatformRole.objects.get(name="Billing")
    lead_role = PlatformRole.objects.create(
        name="Team lead", permissions=["people.view", "admins.manage", "dashboard.view"]
    )
    lead = User.objects.create_user("lead@platform.test", "pw", name="Lead",
                                    is_platform_staff=True, platform_role=lead_role)
    client.force_login(lead)
    client.post(reverse("platform:role_edit", args=[billing.pk]),
                {"name": "Billing", "perms": []})
    billing.refresh_from_db()
    assert "invoices.manage" in billing.permissions


def test_nobody_changes_their_own_access(client, boss):
    client.force_login(boss)
    client.post(reverse("platform:user_platform_access", args=[boss.pk]),
                {"role": PlatformRole.objects.get(name="Read only").pk})
    boss.refresh_from_db()
    assert boss.platform_role.is_super


def test_a_role_can_be_created_and_given_out(client, boss, cashier):
    client.force_login(boss)
    response = client.post(reverse("platform:role_create"),
                           {"name": "Auditor", "perms": ["audit.view", "shops.view"]}, **HX)
    assert response.status_code == 204
    role = PlatformRole.objects.get(name="Auditor")
    assert sorted(role.permissions) == ["audit.view", "shops.view"]

    client.post(reverse("platform:user_platform_access", args=[cashier.pk]), {"role": role.pk})
    cashier.refresh_from_db()
    assert cashier.has_platform_perm("audit.view")
    assert not cashier.has_platform_perm("shops.delete")


def test_super_admin_cannot_be_edited_or_removed(client, boss):
    role = PlatformRole.objects.get(name="Super admin")
    client.force_login(boss)
    client.post(reverse("platform:role_edit", args=[role.pk]), {"name": "Tiny", "perms": ["audit.view"]})
    client.post(reverse("platform:role_delete", args=[role.pk]))
    role.refresh_from_db()
    assert role.name == "Super admin" and role.is_super


def test_a_role_in_use_is_not_removed(client, boss, support):
    client.force_login(boss)
    client.post(reverse("platform:role_delete", args=[support.platform_role.pk]))
    assert PlatformRole.objects.filter(name="Support").exists()


def test_an_unused_role_can_be_removed(client, boss):
    role = PlatformRole.objects.create(name="Temp", permissions=["audit.view"])
    client.force_login(boss)
    client.post(reverse("platform:role_delete", args=[role.pk]))
    assert not PlatformRole.objects.filter(pk=role.pk).exists()


def test_a_deactivated_admin_holds_nothing(boss):
    boss.is_active = False
    assert boss.platform_permissions == frozenset()


def test_shop_staff_hold_no_platform_permissions(cashier):
    assert cashier.platform_permissions == frozenset()

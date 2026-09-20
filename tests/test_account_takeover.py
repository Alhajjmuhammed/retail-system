"""
An account is one identity across every shop and the platform.

A shop may choose the password of somebody who works only for it. It must
never be able to choose -- or reset -- the password of somebody who also
works elsewhere, or who runs the platform: that would let any shop owner
sign in as them there. Each of these used to be possible.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, Membership, Role, User
from apps.core.context import tenant_context, unscoped

pytestmark = pytest.mark.django_db


@pytest.fixture
def platform_admin(db):
    return User.objects.create_user("admin@platform.test", "the-real-one-1",
                                    name="Admin", is_platform_staff=True)


def test_adding_staff_never_rewrites_an_existing_password(client, shop, owner, platform_admin):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(owner)
    client.post(reverse("accounts:staff_create"), {"all_branches": "on", 
        "name": "Mallory", "email": platform_admin.email, "role": role.pk,
        "password": "owner-chose-this-1",
    })
    platform_admin.refresh_from_db()
    assert platform_admin.check_password("the-real-one-1")
    assert platform_admin.name == "Admin"


def test_an_existing_account_is_invited_not_attached(client, shop, owner, cashier):
    """
    Attaching directly let one shop plant an account whose password it knew
    into another shop's team. The person has to accept with their own.
    """
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(owner)
    client.post(reverse("accounts:staff_create"),
                {"all_branches": "on", "name": "Juma", "email": cashier.email, "role": role.pk})
    with unscoped():
        assert not Membership.objects_all.filter(tenant=shop, user=cashier).exists()
        assert Invitation.objects_all.filter(tenant=shop, email=cashier.email).exists()


def test_a_new_person_still_needs_a_password(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(owner)
    client.post(reverse("accounts:staff_create"),
                {"all_branches": "on", "name": "New", "email": "brand-new@shop.test", "role": role.pk})
    assert not User.objects.filter(email="brand-new@shop.test").exists()


def test_editing_staff_cannot_reset_someone_who_works_elsewhere(
    client, shop, owner, cashier, business_plan
):
    from apps.tenancy.services import create_tenant

    create_tenant(name="Other Duka", owner=cashier, plan=business_plan)
    cashier.set_password("cashiers-own-1")
    cashier.save()
    with tenant_context(shop):
        m = Membership.objects.create(tenant=shop, user=cashier,
                                      role=Role.objects.get(name="Cashier"))
        role = m.role
    client.force_login(owner)
    client.post(reverse("accounts:staff_edit", args=[m.pk]), {"all_branches": "on", 
        "name": "Renamed", "email": cashier.email, "role": role.pk,
        "password": "owner-chose-this-1",
    })
    cashier.refresh_from_db()
    assert cashier.check_password("cashiers-own-1")
    assert cashier.name != "Renamed"


def test_editing_staff_still_resets_someone_who_works_only_here(client, shop, owner, cashier):
    with tenant_context(shop):
        m = Membership.objects.create(tenant=shop, user=cashier,
                                      role=Role.objects.get(name="Cashier"))
    client.force_login(owner)
    client.post(reverse("accounts:staff_edit", args=[m.pk]), {"all_branches": "on", 
        "name": "Juma Ally", "email": cashier.email, "role": m.role.pk,
        "password": "fresh-from-owner-1",
    })
    cashier.refresh_from_db()
    assert cashier.check_password("fresh-from-owner-1")


def test_an_invite_link_cannot_choose_an_existing_accounts_password(client, shop, platform_admin):
    """The owner is shown the link. It must not be a way into anyone's account."""
    with tenant_context(shop):
        invitation = Invitation.objects.create(
            tenant=shop, email=platform_admin.email, role=Role.objects.get(name="Cashier"),
            expires_at=timezone.now() + timedelta(days=7),
        )
    accept = reverse("accounts:accept_invitation", args=[invitation.token])
    client.post(accept, {"name": "Mallory", "password": "owner-chose-this-1"})
    platform_admin.refresh_from_db()
    assert platform_admin.check_password("the-real-one-1")
    with unscoped():
        assert not Membership.objects_all.filter(user=platform_admin).exists()


def test_an_existing_person_joins_with_their_own_password(client, shop, cashier):
    cashier.set_password("cashiers-own-1")
    cashier.save()
    with tenant_context(shop):
        invitation = Invitation.objects.create(
            tenant=shop, email=cashier.email, role=Role.objects.get(name="Cashier"),
            expires_at=timezone.now() + timedelta(days=7),
        )
    accept = reverse("accounts:accept_invitation", args=[invitation.token])
    assert b"current password" in client.get(accept).content
    client.post(accept, {"password": "cashiers-own-1"})
    with unscoped():
        assert Membership.objects_all.filter(tenant=shop, user=cashier).exists()
    cashier.refresh_from_db()
    assert cashier.check_password("cashiers-own-1")


def test_platform_shop_staff_form_protects_admins(client, shop, platform_admin):
    from apps.accounts.models import PlatformRole

    support = User.objects.create_user(
        "support@platform.test", "pw", name="Sup", is_platform_staff=True,
        platform_role=PlatformRole.objects.get(name="Support"),
    )
    with tenant_context(shop):
        m = Membership.objects.create(tenant=shop, user=platform_admin,
                                      role=Role.objects.get(name="Cashier"))
    client.force_login(support)
    client.post(reverse("platform:tenant_member_edit", args=[shop.pk, m.pk]), {
        "name": "Admin", "email": platform_admin.email, "role": m.role.pk,
        "password": "support-chose-1",
    })
    platform_admin.refresh_from_db()
    assert platform_admin.check_password("the-real-one-1")


def test_platform_add_person_keeps_an_existing_password(client, shop, platform_admin, cashier):
    cashier.set_password("cashiers-own-1")
    cashier.save()
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(platform_admin)
    client.post(reverse("platform:tenant_member_create", args=[shop.pk]), {
        "name": "Juma", "email": cashier.email, "role": role.pk, "password": "someone-else-1",
    })
    cashier.refresh_from_db()
    assert cashier.check_password("cashiers-own-1")
    with unscoped():
        assert Membership.objects_all.filter(tenant=shop, user=cashier).exists()


def test_losing_support_access_ends_a_support_session(client, shop):
    from apps.accounts.models import PlatformRole

    person = User.objects.create_user(
        "sup@platform.test", "pw", name="Sup", is_platform_staff=True,
        platform_role=PlatformRole.objects.get(name="Support"),
    )
    client.force_login(person)
    client.post(reverse("platform:impersonate", args=[shop.pk]))
    person.platform_role = PlatformRole.objects.get(name="Read only")
    person.save()
    client.get(reverse("core:dashboard"))
    assert not client.session.get("impersonating")

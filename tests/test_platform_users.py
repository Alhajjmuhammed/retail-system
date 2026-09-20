"""
Platform-level user management.

Support work starts here: somebody rings saying they cannot sign in, and this
is the only place that can find them without already knowing their shop.

Every action is guarded against the two ways this screen could lock people
out: removing the last platform administrator, and removing a shop's only
owner.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context, unscoped

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(db, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    return owner


def test_the_people_list_crosses_every_shop(client, db, staff, cashier, business_plan):
    from apps.tenancy.services import create_tenant

    create_tenant(name="Shop One", owner=staff, plan=business_plan)
    create_tenant(name="Shop Two", owner=cashier, plan=business_plan)

    client.force_login(staff)
    response = client.get(reverse("platform:user_list"))
    assert response.status_code == 200

    emails = {u.email for u in response.context["users"]}
    assert {staff.email, cashier.email} <= emails


def test_people_in_no_shop_can_be_found(client, db, staff):
    """They sign in and reach a dead end, so they have to be findable."""
    stray = User.objects.create_user("stray@example.com", "pw", name="Stray")

    client.force_login(staff)
    response = client.get(reverse("platform:user_list"), {"view": "orphaned"})
    assert stray.email in {u.email for u in response.context["users"]}


def test_a_password_can_be_reset_for_support(client, shop, staff, cashier):
    with tenant_context(shop):
        Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )

    client.force_login(staff)
    client.post(
        reverse("platform:user_password", args=[cashier.pk]),
        {"password": "brand-new-password"},
        follow=True,
    )

    client.logout()
    assert client.login(email=cashier.email, password="brand-new-password")


def test_a_short_password_is_refused(client, db, staff, cashier):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_password", args=[cashier.pk]),
        {"password": "short"},
        follow=True,
    )
    assert b"at least 8" in response.content
    client.logout()
    assert not client.login(email=cashier.email, password="short")


def test_an_account_can_be_switched_off_and_back_on(client, db, staff, cashier):
    client.force_login(staff)
    url = reverse("platform:user_toggle_active", args=[cashier.pk])

    client.post(url, {}, follow=True)
    cashier.refresh_from_db()
    assert cashier.is_active is False

    client.post(url, {}, follow=True)
    cashier.refresh_from_db()
    assert cashier.is_active is True


def test_you_cannot_deactivate_yourself(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_toggle_active", args=[staff.pk]),
        {}, follow=True,
    )
    assert b"cannot deactivate your own" in response.content
    staff.refresh_from_db()
    assert staff.is_active is True


def test_the_last_platform_administrator_cannot_be_demoted(client, db, staff):
    """Demote the last one and nobody can administer the platform at all."""
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_platform_access", args=[staff.pk]),
        {"role": ""}, follow=True,
    )
    assert b"your own platform access" in response.content
    staff.refresh_from_db()
    assert staff.is_platform_staff is True


def test_somebody_else_can_be_promoted_and_demoted(client, db, staff, cashier):
    client.force_login(staff)
    from apps.accounts.models import PlatformRole

    url = reverse("platform:user_platform_access", args=[cashier.pk])

    client.post(url, {"role": PlatformRole.objects.get(name="Support").pk}, follow=True)
    cashier.refresh_from_db()
    assert cashier.is_platform_staff is True
    # And it takes effect: they can now reach the platform.
    client.force_login(cashier)
    assert client.get(reverse("platform:dashboard")).status_code == 200

    client.force_login(staff)
    client.post(url, {"role": ""}, follow=True)
    cashier.refresh_from_db()
    assert cashier.is_platform_staff is False


def test_a_membership_can_be_removed(client, shop, staff, cashier):
    with tenant_context(shop):
        membership = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )

    client.force_login(staff)
    client.post(
        reverse("platform:user_detail", args=[cashier.pk]),
        {"action": "remove_membership", "membership": membership.pk},
        follow=True,
    )
    with unscoped():
        assert not Membership.objects_all.filter(pk=membership.pk).exists()


def test_a_shops_only_owner_cannot_be_removed(client, shop, staff, cashier, business_plan):
    """Remove them and the shop has nobody who can administer it."""
    from apps.tenancy.services import create_tenant

    _tenant, membership = create_tenant(
        name="Duka la Juma", owner=cashier, plan=business_plan
    )

    client.force_login(staff)
    response = client.post(
        reverse("platform:user_detail", args=[cashier.pk]),
        {"action": "remove_membership", "membership": membership.pk},
        follow=True,
    )
    assert b"only owner" in response.content
    with unscoped():
        assert Membership.objects_all.filter(pk=membership.pk).exists()


def test_shop_staff_cannot_reach_any_of_this(client, shop, owner):
    client.force_login(owner)
    assert client.get(reverse("platform:user_list")).status_code == 403
    assert client.get(reverse("platform:user_detail", args=[owner.pk])).status_code == 403


def test_changing_your_own_password_keeps_you_signed_in(client, db, staff):
    """It used to sign you straight out: the session hash was not carried over."""
    client.force_login(staff)
    client.post(reverse("platform:user_password", args=[staff.pk]),
                {"password": "my-new-password"})
    assert client.get(reverse("platform:user_list")).status_code == 200


def test_a_persons_details_can_be_corrected(client, db, staff, cashier):
    client.force_login(staff)
    client.post(
        reverse("platform:user_edit", args=[cashier.pk]),
        {"name": "Asha Juma", "email": "Asha@Example.com", "phone": "0712 000 111"},
    )
    cashier.refresh_from_db()
    assert (cashier.name, cashier.email, cashier.phone) == (
        "Asha Juma", "asha@example.com", "0712 000 111"
    )


def test_an_email_already_in_use_is_refused_not_crashed(client, db, staff, cashier):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_edit", args=[cashier.pk]),
        {"name": "X", "email": staff.email},
    )
    assert response.status_code == 200
    assert b"already belongs to another account" in response.content
    cashier.refresh_from_db()
    assert cashier.email != staff.email


def test_never_signed_in_is_its_own_view(client, db, staff, cashier):
    client.force_login(staff)
    response = client.get(reverse("platform:user_list"), {"view": "never"})
    emails = [u.email for u in response.context["users"]]
    assert cashier.email in emails
    assert staff.email not in emails  # force_login stamps last_login


def test_search_finds_a_phone_number(client, db, staff, cashier):
    cashier.phone = "0755123456"
    cashier.save(update_fields=["phone"])
    client.force_login(staff)
    response = client.get(reverse("platform:user_list"), {"q": "755123"})
    assert [u.email for u in response.context["users"]] == [cashier.email]


def test_the_list_pages_rather_than_silently_capping(client, db, staff):
    with unscoped():
        User.objects.bulk_create(
            [User(email=f"bulk{i}@x.test", name=f"Bulk {i:03}") for i in range(120)]
        )
    client.force_login(staff)
    response = client.get(reverse("platform:user_list"), {"page": 3})
    page = response.context["page"]
    assert page.paginator.count >= 121
    assert page.number == 3
    assert len(page.object_list) == page.paginator.count - 100


def test_you_cannot_deactivate_yourself_from_the_list(client, db, staff):
    client.force_login(staff)
    client.post(reverse("platform:user_toggle_active", args=[staff.pk]))
    staff.refresh_from_db()
    assert staff.is_active is True


def test_an_inactive_account_cannot_be_made_an_admin(client, db, staff, cashier):
    cashier.is_active = False
    cashier.save(update_fields=["is_active"])
    client.force_login(staff)
    from apps.accounts.models import PlatformRole

    client.post(reverse("platform:user_platform_access", args=[cashier.pk]),
                {"role": PlatformRole.objects.get(name="Support").pk})
    cashier.refresh_from_db()
    assert cashier.is_platform_staff is False


def test_account_actions_are_audited_in_each_shop(client, db, staff, cashier, shop):
    from apps.accounts.models import AuditLog

    with tenant_context(shop):
        Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )
    client.force_login(staff)
    client.post(reverse("platform:user_toggle_active", args=[cashier.pk]))
    with tenant_context(shop):
        assert AuditLog.objects.filter(action="platform.user_toggle_active").exists()


def test_actions_return_to_the_page_they_were_started_from(client, db, staff, cashier):
    client.force_login(staff)
    back = "/platform/users/?view=never"
    response = client.post(
        reverse("platform:user_toggle_active", args=[cashier.pk]) + "?next=" + back
    )
    assert response["Location"] == back


def test_the_quick_view_opens(client, db, staff, cashier):
    client.force_login(staff)
    response = client.get(reverse("platform:user_quick_view", args=[cashier.pk]),
                          HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert cashier.email.encode() in response.content


def test_relative_times_read_as_one_unit():
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.templatetags.perms import ago

    now = timezone.now()
    assert ago(now) == "just now"
    assert ago(now - timedelta(hours=1, minutes=6)) == "1\xa0hour ago"
    assert ago(None) == ""

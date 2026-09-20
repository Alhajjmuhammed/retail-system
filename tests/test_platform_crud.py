"""
Every platform screen's add, edit and delete — through the modal and without.

The forms load into a modal via HTMX, but each one also renders as its own
page: a form that exists only inside a modal cannot be opened in a new tab or
reached when scripting fails.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.context import tenant_context, unscoped
from apps.org.models import Device
from apps.tenancy.models import Invoice, InvoiceStatus, Plan, Tenant

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def staff(db, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    return owner


# --------------------------------------------------------------------------
# The forms open both ways
# --------------------------------------------------------------------------

MODAL_FORMS = [
    "platform:tenant_create",
    "platform:user_create",
    "platform:invoice_create",
    "platform:plan_create",
]


@pytest.mark.parametrize("name", MODAL_FORMS)
def test_each_form_opens_in_a_modal(client, db, staff, name):
    client.force_login(staff)
    response = client.get(reverse(name), **HX)
    assert response.status_code == 200
    assert b"fixed inset-0" in response.content, "not rendered as a modal"


@pytest.mark.parametrize("name", MODAL_FORMS)
def test_each_form_also_works_as_a_plain_page(client, db, staff, name):
    """Opened in a new tab, or with scripting blocked."""
    client.force_login(staff)
    response = client.get(reverse(name))
    assert response.status_code == 200
    assert b"Platform" in response.content, "not wrapped in the admin shell"


# --------------------------------------------------------------------------
# Shops
# --------------------------------------------------------------------------

def test_a_shop_can_be_created_from_the_platform(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:tenant_create"),
        {"business_name": "Duka Jipya", "branch_name": "Main",
         "name": "Neema", "email": "neema@duka.test",
         "password": "correct-horse-99"},
        **HX,
    )
    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("platform:tenant_list")

    with unscoped():
        tenant = Tenant.objects.get(name="Duka Jipya")
        assert tenant.subscription.status == "trialing"

    # And the owner can sign in immediately.
    assert client.login(email="neema@duka.test", password="correct-horse-99")


def test_creating_a_shop_shows_its_errors_in_the_modal(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:tenant_create"),
        {"business_name": "Duka", "name": "X", "email": "x@x.test",
         "password": "short"},
        **HX,
    )
    assert response.status_code == 200
    assert b"at least 8 characters" in response.content


def test_a_shop_that_never_traded_can_be_deleted(client, db, staff, cashier, business_plan):
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Never Traded", owner=cashier, plan=business_plan)

    client.force_login(staff)
    client.post(reverse("platform:tenant_delete", args=[tenant.pk]),
                {"confirm": "Never Traded"}, follow=True)

    with unscoped():
        assert not Tenant.objects.filter(pk=tenant.pk).exists()


def test_a_shop_with_sales_cannot_be_deleted(client, shop, main_branch, stocked, staff):
    """It holds records somebody may still be answerable for."""
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=staff):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])

    client.force_login(staff)
    response = client.post(reverse("platform:tenant_delete", args=[shop.pk]),
                           {"confirm": shop.name}, follow=True)

    assert b"cannot be deleted" in response.content
    with unscoped():
        assert Tenant.objects.filter(pk=shop.pk).exists()


def test_deleting_a_shop_needs_its_name_typed(client, db, staff, cashier, business_plan):
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Never Traded", owner=cashier, plan=business_plan)

    client.force_login(staff)
    response = client.post(reverse("platform:tenant_delete", args=[tenant.pk]),
                           {"confirm": "wrong"}, follow=True)
    assert b"Type the shop" in response.content
    with unscoped():
        assert Tenant.objects.filter(pk=tenant.pk).exists()


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

def test_an_account_can_be_created_from_the_platform(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_create"),
        {"name": "Support Person", "email": "support@platform.test",
         "password": "correct-horse-99", "role": _role("Support").pk},
        **HX,
    )
    assert response.status_code == 204

    with unscoped():
        user = User.objects.get(email="support@platform.test")
        assert user.is_platform_staff

    assert client.login(email="support@platform.test", password="correct-horse-99")


def test_an_account_made_here_needs_a_role(client, db, staff):
    """Without one it used to create an account that could do nothing."""
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_create"),
        {"name": "No Role", "email": "norole@platform.test", "password": "correct-horse-99"},
        **HX,
    )
    assert b"Choose what they may do" in response.content
    with unscoped():
        assert not User.objects.filter(email="norole@platform.test").exists()


def _role(name):
    from apps.accounts.models import PlatformRole

    return PlatformRole.objects.get(name=name)


def test_a_duplicate_email_is_refused(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_create"),
        {"name": "Clash", "email": staff.email, "password": "correct-horse-99"},
        **HX,
    )
    assert response.status_code == 200
    assert b"already has an account" in response.content


def test_an_unused_account_can_be_deleted(client, db, staff):
    with unscoped():
        stray = User.objects.create_user("stray@x.test", "pw", name="Stray")

    client.force_login(staff)
    client.post(reverse("platform:user_delete", args=[stray.pk]), follow=True)

    with unscoped():
        assert not User.objects.filter(pk=stray.pk).exists()


def test_an_account_that_has_been_used_is_deactivated_instead(client, shop, staff, cashier):
    """Their name is on sales and audit rows that have to keep meaning something."""
    from apps.accounts.models import Role

    with tenant_context(shop):
        Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )

    client.force_login(staff)
    response = client.post(
        reverse("platform:user_delete", args=[cashier.pk]), follow=True
    )
    assert b"deactivated rather than deleted" in response.content

    cashier.refresh_from_db()
    assert cashier.is_active is False


def test_you_cannot_delete_your_own_account(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:user_delete", args=[staff.pk]), follow=True
    )
    assert b"cannot delete your own" in response.content
    with unscoped():
        assert User.objects.filter(pk=staff.pk).exists()


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

def _raise_invoice(client, shop):
    from django.utils import timezone

    today = timezone.localdate()
    client.post(
        reverse("platform:invoice_create"),
        {"tenant": shop.pk, "period_start": today, "period_end": today,
         "amount": "60000"},
        **HX,
    )
    with unscoped():
        return Invoice.objects.latest("id")


def test_an_invoice_can_be_raised_and_corrected(client, shop, staff):
    from django.utils import timezone

    client.force_login(staff)
    invoice = _raise_invoice(client, shop)
    assert invoice.total == 60000

    today = timezone.localdate()
    response = client.post(
        reverse("platform:invoice_edit", args=[invoice.pk]),
        {"amount": "45000", "period_start": today, "period_end": today},
        **HX,
    )
    assert response.status_code == 204

    invoice.refresh_from_db()
    assert invoice.total == 45000


def test_an_invoice_raised_in_error_is_voided_not_deleted(client, shop, staff):
    """A number that simply disappears is a gap an accountant asks about."""
    client.force_login(staff)
    invoice = _raise_invoice(client, shop)

    client.post(reverse("platform:invoice_void", args=[invoice.pk]), follow=True)

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.VOID
    with unscoped():
        assert Invoice.objects.filter(pk=invoice.pk).exists()


def test_a_paid_invoice_cannot_be_voided(client, shop, staff):
    client.force_login(staff)
    invoice = _raise_invoice(client, shop)
    client.post(reverse("platform:invoice_pay", args=[invoice.pk]),
                {"amount": "60000", "reference": "MPESA1", "method": "mpesa"}, follow=True)

    response = client.post(
        reverse("platform:invoice_void", args=[invoice.pk]), follow=True
    )
    assert b"has payments against it" in response.content

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

def test_a_device_can_be_named_and_removed(client, shop, main_branch, staff):
    with tenant_context(shop):
        device = Device.objects.create(
            branch=main_branch, device_id="abc123", kind="till"
        )

    client.force_login(staff)
    response = client.post(
        reverse("platform:device_edit", args=[device.pk]),
        {"label": "Front counter", "kind": "till", "is_active": "on"},
        **HX,
    )
    assert response.status_code == 204

    device.refresh_from_db()
    assert device.label == "Front counter"

    client.post(reverse("platform:device_delete", args=[device.pk]), follow=True)
    with unscoped():
        assert not Device.objects.filter(pk=device.pk).exists()


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def test_a_plan_can_be_created_and_deleted_from_the_modal(client, db, staff):
    client.force_login(staff)
    response = client.post(
        reverse("platform:plan_create"),
        {"name": "Trial Plan", "price_monthly": "1000", "trial_days": "7",
         "limit:branches": "2", "features": []},
        **HX,
    )
    assert response.status_code == 204

    with unscoped():
        plan = Plan.objects.get(name="Trial Plan")

    response = client.post(reverse("platform:plan_delete", args=[plan.pk]), **HX)
    assert response.status_code == 204
    with unscoped():
        assert not Plan.objects.filter(pk=plan.pk).exists()


def test_shop_staff_cannot_reach_any_platform_form(client, shop, owner):
    client.force_login(owner)
    for name in MODAL_FORMS:
        assert client.get(reverse(name)).status_code == 403


# --------------------------------------------------------------------------
# The shops list
# --------------------------------------------------------------------------

def test_the_shops_list_carries_enough_to_act_on(client, shop, main_branch, stocked, staff):
    """
    A name and a status is not enough. The questions are always how big they
    are, whether they are paying, and when anyone last heard from them.
    """
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=staff):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])

    client.force_login(staff)
    response = client.get(reverse("platform:tenant_list"))
    assert response.status_code == 200

    row = next(t for t in response.context["tenants"] if t.pk == shop.pk)
    assert row.branch_count >= 1
    assert row.user_count >= 1
    assert row.last_sale is not None

    assert response.context["summary"]["total"] >= 1


def test_the_shops_list_uses_icon_actions_not_a_wall_of_buttons(client, shop, staff):
    client.force_login(staff)
    content = client.get(reverse("platform:tenant_list")).content.decode()

    # Edit opens the modal, not a page.
    assert f'hx-get="{reverse("platform:tenant_edit", args=[shop.pk])}?next=' in content
    assert 'hx-target="#modal"' in content
    # Each action is an icon with a title, so it is discoverable on hover.
    assert content.count("btn-icon") >= 4
    assert 'title="Edit details"' in content
    assert 'title="Open their account for support"' in content


def test_shops_can_be_searched_and_filtered(client, db, staff, cashier, business_plan, free_plan):
    from apps.tenancy.services import create_tenant

    create_tenant(name="Nungwi Traders", owner=staff, plan=business_plan)
    create_tenant(name="Paje Supplies", owner=cashier, plan=free_plan)

    client.force_login(staff)

    found = client.get(reverse("platform:tenant_list"), {"q": "Nungwi"})
    names = [t.name for t in found.context["tenants"]]
    assert names == ["Nungwi Traders"]

    # Filtering by status narrows it without losing the summary counts.
    trialing = client.get(reverse("platform:tenant_list"), {"status": "trialing"})
    assert all(
        t.subscription.status == "trialing" for t in trialing.context["tenants"]
    )
    assert trialing.context["summary"]["total"] >= 2


def test_a_shop_that_has_never_been_used_is_called_out(client, db, staff, cashier, business_plan):
    """The ones worth phoning before the trial runs out."""
    from apps.tenancy.services import create_tenant

    create_tenant(name="Silent Duka", owner=cashier, plan=business_plan)

    client.force_login(staff)
    content = client.get(reverse("platform:tenant_list")).content.decode()
    assert "never used it" in content


# --------------------------------------------------------------------------
# Dialogs and table numbering
# --------------------------------------------------------------------------

def test_no_page_uses_the_browser_confirm_dialog():
    """
    The browser's confirm() cannot be styled, cannot say which shop it means
    in anything but plain text, and on some phones appears as an unbranded
    system sheet that looks like the page has been hijacked.
    """
    import pathlib

    offenders = []
    for path in pathlib.Path("templates").rglob("*.html"):
        source = path.read_text()
        for marker in ("return confirm(", "window.confirm", "hx-confirm"):
            if marker in source:
                offenders.append(f"{path} ({marker})")

    assert not offenders, "browser dialogs remain: " + ", ".join(offenders)


def test_destructive_actions_ask_through_the_shared_dialog(client, shop, staff):
    client.force_login(staff)
    content = client.get(reverse("platform:tenant_list")).content.decode()

    assert "$store.confirm.ask($el" in content
    # And the dialog itself is on the page to answer.
    assert 'x-show="$store.confirm.open"' in content


PLATFORM_TABLES = [
    "platform:tenant_list",
    "platform:user_list",
    "platform:invoices",
    "platform:devices",
    "platform:platform_audit",
    "platform:permissions",
]


@pytest.mark.parametrize("name", PLATFORM_TABLES)
def test_every_platform_table_is_numbered(client, shop, staff, name):
    """A row number, so somebody can say "row 14" on a call."""
    client.force_login(staff)
    content = client.get(reverse(name)).content.decode()
    assert ">SN</th>" in content, f"{name} has no SN column"


def test_opening_a_shop_shows_it_in_a_modal(client, shop, main_branch, stocked, staff):
    """
    Most of the time the question is who they are and whether they are
    alright, which does not deserve a page load and a trip back.
    """
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=staff):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=2)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 6000}])

    client.force_login(staff)

    listing = client.get(reverse("platform:tenant_list")).content.decode()
    assert reverse("platform:tenant_quick_view", args=[shop.pk]) in listing

    quick = client.get(
        reverse("platform:tenant_quick_view", args=[shop.pk]), **HX
    )
    assert quick.status_code == 200
    assert b"fixed inset-0" in quick.content
    assert quick.context["sales_count"] == 1
    assert quick.context["sales_value"] == Decimal("6000.00")
    # With a way through to the full page rather than replacing it.
    assert reverse("platform:tenant_detail", args=[shop.pk]).encode() in quick.content


def test_the_quick_view_also_works_as_a_page(client, shop, staff):
    client.force_login(staff)
    response = client.get(reverse("platform:tenant_quick_view", args=[shop.pk]))
    assert response.status_code == 200
    assert b"Platform" in response.content


# --------------------------------------------------------------------------
# Managing a shop's people from the platform
# --------------------------------------------------------------------------

def test_a_person_can_be_added_to_a_shop_from_the_platform(client, shop, main_branch, staff):
    """
    Support work: an owner rings because their manager left and nobody else
    can approve a void.
    """
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        role = Role.objects.get(name="Manager")

    client.force_login(staff)
    response = client.post(
        reverse("platform:tenant_member_create", args=[shop.pk]),
        {"name": "Asha Replacement", "email": "asha2@shop.test",
         "role": role.pk, "branches": [main_branch.pk],
         "password": "correct-horse-99", "pin": "4321"},
        **HX,
    )
    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("platform:tenant_detail", args=[shop.pk])

    with tenant_context(shop):
        membership = Membership.objects.get(user__email="asha2@shop.test")
        assert membership.role == role
        assert membership.branch_links.count() == 1
        assert membership.check_pin("4321")

    # And they can sign in straight away.
    assert client.login(email="asha2@shop.test", password="correct-horse-99")


def test_a_persons_role_and_branches_can_be_changed(client, shop, main_branch, staff, cashier):
    from apps.accounts.models import Membership, Role
    from apps.org.models import Branch

    with tenant_context(shop):
        cashier_role = Role.objects.get(name="Cashier")
        manager_role = Role.objects.get(name="Manager")
        nungwi = Branch.objects.create(name="Nungwi")
        membership = Membership.objects.create(
            tenant=shop, user=cashier, role=cashier_role
        )

    client.force_login(staff)
    client.post(
        reverse("platform:tenant_member_edit", args=[shop.pk, membership.pk]),
        {"name": cashier.name, "email": cashier.email, "role": manager_role.pk,
         "branches": [main_branch.pk, nungwi.pk], "password": ""},
        **HX,
    )

    with tenant_context(shop):
        membership.refresh_from_db()
        assert membership.role == manager_role
        assert membership.branch_links.count() == 2


def test_a_password_left_blank_when_editing_is_kept(client, shop, staff, cashier):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        membership = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )

    client.force_login(staff)
    client.post(
        reverse("platform:tenant_member_edit", args=[shop.pk, membership.pk]),
        {"name": "Juma Renamed", "email": cashier.email,
         "role": membership.role_id, "password": ""},
        **HX,
    )

    client.logout()
    assert client.login(email=cashier.email, password="pw")


def test_somebody_already_in_the_shop_is_refused(client, shop, staff, owner):
    from apps.accounts.models import Role

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")

    client.force_login(staff)
    response = client.post(
        reverse("platform:tenant_member_create", args=[shop.pk]),
        {"name": "Again", "email": owner.email, "role": role.pk,
         "password": "correct-horse-99"},
        **HX,
    )
    assert response.status_code == 200
    assert b"already in this shop" in response.content


def test_a_person_can_be_removed_and_their_records_stay(
    client, shop, main_branch, stocked, staff, cashier
):
    from apps.accounts.models import Membership, Role
    from apps.pos.models import PaymentMethod, Sale
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=cashier):
        membership = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )
        cart = new_cart(branch=main_branch, user=cashier)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(
            cart, [{"method": PaymentMethod.CASH, "amount": 1500}], user=cashier
        )

    client.force_login(staff)
    client.post(
        reverse("platform:tenant_member_remove", args=[shop.pk, membership.pk]),
        follow=True,
    )

    with tenant_context(shop):
        assert not Membership.objects.filter(pk=membership.pk).exists()
        assert Sale.objects.filter(pk=sale.pk).exists()


def test_the_only_owner_of_a_shop_cannot_be_removed(client, shop, staff, owner):
    """The shop would have nobody who could administer it."""
    from apps.accounts.models import Membership

    with tenant_context(shop):
        membership = Membership.objects.get(user=owner)

    client.force_login(staff)
    response = client.post(
        reverse("platform:tenant_member_remove", args=[shop.pk, membership.pk]),
        follow=True,
    )
    assert b"only owner" in response.content
    with unscoped():
        assert Membership.objects_all.filter(pk=membership.pk).exists()


def test_the_shop_page_lists_its_people_with_actions(client, shop, staff):
    client.force_login(staff)
    content = client.get(reverse("platform:tenant_detail", args=[shop.pk])).content.decode()

    assert reverse("platform:tenant_member_create", args=[shop.pk]) in content
    assert ">SN</th>" in content
    # Every person has an edit and a remove.
    for member in client.get(
        reverse("platform:tenant_detail", args=[shop.pk])
    ).context["members"]:
        assert reverse("platform:tenant_member_edit", args=[shop.pk, member.pk]) in content
        assert reverse("platform:tenant_member_remove", args=[shop.pk, member.pk]) in content


def test_deleting_a_shop_takes_accounts_that_only_belonged_to_it(
    client, db, staff, cashier, business_plan
):
    """
    Left behind, they sat in no shop at all, and setting the same shop up
    again for the same owner failed with "already has an account".
    """
    from apps.accounts.models import User
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Short Lived", owner=cashier, plan=business_plan)

    client.force_login(staff)
    client.post(reverse("platform:tenant_delete", args=[tenant.pk]),
                {"confirm": "Short Lived"}, follow=True)

    with unscoped():
        assert not User.objects.filter(pk=cashier.pk).exists()

    # And the same owner can be set up again straight away.
    response = client.post(
        reverse("platform:tenant_create"),
        {"business_name": "Short Lived", "name": "Juma",
         "email": cashier.email, "password": "correct-horse-99"},
        **HX,
    )
    assert response.status_code == 204


def test_deleting_a_shop_keeps_people_who_belong_to_another(
    client, db, staff, owner, cashier, business_plan
):
    from apps.accounts.models import Membership, Role, User
    from apps.tenancy.services import create_tenant

    first, _ = create_tenant(name="Keeps Going", owner=owner, plan=business_plan)
    second, _ = create_tenant(name="Closing Down", owner=cashier, plan=business_plan)
    with tenant_context(second):
        Membership.objects.create(
            tenant=second, user=owner, role=Role.objects.get(name="Manager")
        )

    client.force_login(staff)
    client.post(reverse("platform:tenant_delete", args=[second.pk]),
                {"confirm": "Closing Down"}, follow=True)

    with unscoped():
        assert User.objects.filter(pk=owner.pk).exists()
        assert Membership.objects_all.filter(user=owner, tenant=first).exists()


def test_support_session_resolves_the_shop_and_can_be_ended(client, shop, staff):
    """
    Platform staff belong to no shop, so "open for support" used to bounce
    straight back to the platform -- and had no way out either.
    """
    from apps.accounts.models import Membership

    with unscoped():
        # A pure platform account, belonging to no shop.
        Membership.objects_all.filter(user=staff).delete()
    client.force_login(staff)

    response = client.post(reverse("platform:impersonate", args=[shop.pk]), follow=True)
    assert response.status_code == 200
    assert b"Support session in" in response.content
    assert response.context["membership"].is_support
    assert client.get(reverse("catalog:product_list")).status_code == 200

    ended = client.post(reverse("platform:end_support"))
    assert ended.url == reverse("platform:tenant_detail", args=[shop.pk])
    assert "impersonating" not in client.session


def test_support_access_is_a_post_not_a_link(client, shop, staff):
    client.force_login(staff)
    assert client.get(reverse("platform:impersonate", args=[shop.pk])).status_code == 405


def test_actions_return_to_where_they_started(client, shop, staff):
    client.force_login(staff)
    detail = reverse("platform:tenant_detail", args=[shop.pk])
    listing = reverse("platform:tenant_list")

    edited = client.post(
        reverse("platform:tenant_edit", args=[shop.pk]) + f"?next={detail}",
        {"name": shop.name, "next": detail}, **HX,
    )
    assert edited["HX-Redirect"] == detail

    suspended = client.post(
        reverse("platform:tenant_suspend", args=[shop.pk]) + f"?next={listing}"
    )
    assert suspended.url == listing

    # And nothing outside the platform admin is accepted as a destination.
    bounced = client.post(
        reverse("platform:tenant_suspend", args=[shop.pk]) + "?next=https://evil.test/"
    )
    assert bounced.url == detail


def test_the_invoice_form_preselects_the_shop_it_came_from(client, shop, staff):
    client.force_login(staff)
    response = client.get(
        reverse("platform:invoice_create") + f"?tenant={shop.pk}", **HX
    )
    assert f'value="{shop.pk}" selected'.encode() in response.content


def test_the_at_risk_filter_matches_the_at_risk_count(client, shop, staff):
    from apps.tenancy.models import SubscriptionStatus

    with unscoped():
        s = shop.subscription
        s.status = SubscriptionStatus.GRACE
        s.save(update_fields=["status"])

    client.force_login(staff)
    response = client.get(reverse("platform:tenant_list"), {"status": "at_risk"})
    assert shop.pk in [t.pk for t in response.context["tenants"]]
    assert len(response.context["tenants"]) == response.context["summary"]["at_risk"]


def test_modal_errors_replace_the_modal_rather_than_nest_in_it():
    import pathlib

    for path in pathlib.Path("templates/platform").glob("_*.html"):
        source = path.read_text()
        if "hx-post" in source:
            assert 'hx-swap="outerHTML"' not in source, path
            assert 'hx-target="#modal"' in source, path


def test_there_is_a_favicon(client, db):
    response = client.get("/favicon.ico")
    assert response.status_code == 301



def test_support_opens_the_right_shop_even_for_a_staff_member_with_their_own(
    client, db, staff, cashier, business_plan
):
    """
    The normal lookup used to win, so a platform admin who also owns a shop
    opened somebody else's for support and landed back in their own.
    """
    from apps.tenancy.services import create_tenant

    create_tenant(name="Staff Own Shop", owner=staff, plan=business_plan)
    theirs, _ = create_tenant(name="Customer Shop", owner=cashier, plan=business_plan)

    client.force_login(staff)
    response = client.post(reverse("platform:impersonate", args=[theirs.pk]), follow=True)
    assert response.context["tenant"].pk == theirs.pk
    assert b"Support session in Customer Shop" in response.content


@pytest.mark.parametrize("situation,expected", [
    ("owes", "past_due"),
    ("trial", "trialing"),
    ("paid_up", "active"),
])
def test_reactivating_restores_the_status_the_shop_really_has(
    client, shop, staff, situation, expected
):
    """
    Always "active" was wrong twice over: a shop suspended for not paying came
    back counted as paying, and a suspended trial lost its trial.
    """
    from datetime import timedelta

    from django.utils import timezone

    with unscoped():
        s = shop.subscription
        s.trial_ends_at = (
            timezone.now() + timedelta(days=5) if situation == "trial"
            else timezone.now() - timedelta(days=1)
        )
        s.save(update_fields=["trial_ends_at"])
        if situation == "owes":
            Invoice.objects.create(
                tenant=shop, number="INV-OWES-1",
                period_start=timezone.localdate(), period_end=timezone.localdate(),
                amount=1000, total=1000, status=InvoiceStatus.OPEN,
            )

    client.force_login(staff)
    client.post(reverse("platform:tenant_suspend", args=[shop.pk]))   # suspend
    client.post(reverse("platform:tenant_suspend", args=[shop.pk]))   # reactivate

    shop.subscription.refresh_from_db()
    assert shop.subscription.status == expected

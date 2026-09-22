"""Settings: branches and tills, staff status, devices, subscription, activity."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.org.models import Branch, Register

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


def _person(shop, email, role="Cashier"):
    with tenant_context(shop):
        u = User.objects.create_user(email, "pw", name=email.split("@")[0])
        m = Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name=role))
    return u, m


def test_branch_errors_stay_in_the_modal(client, shop, owner, main_branch):
    client.force_login(owner)
    r = client.get(reverse("org:branch_create"), **HX)
    assert r.status_code == 200
    r = client.post(reverse("org:branch_create"), {"name": "main", "is_active": "on"}, **HX)
    assert r.status_code == 200 and "already a branch" in r.content.decode()
    r = client.post(reverse("org:branch_create"), {"name": "Kiosk", "is_active": "on"}, **HX)
    assert r.status_code == 204


def test_till_added_to_the_chosen_branch_and_duplicates_refused(client, shop, owner, main_branch,
                                                                 register):
    client.force_login(owner)
    r = client.get(reverse("org:register_create"), {"branch": main_branch.pk}, **HX)
    assert r.context["form"].initial["branch"] == main_branch.pk
    r = client.post(reverse("org:register_create"),
                    {"branch": main_branch.pk, "name": register.name, "is_active": "on"}, **HX)
    assert r.status_code == 200
    r = client.post(reverse("org:register_create"),
                    {"branch": main_branch.pk, "name": "Till 2", "is_active": "on"}, **HX)
    assert r.status_code == 204
    with tenant_context(shop):
        assert Register.objects.filter(branch=main_branch).count() == 2


def test_suspend_and_reactivate_staff(client, shop, owner):
    _u, m = _person(shop, "juma@x.test")
    client.force_login(owner)
    client.post(reverse("accounts:staff_toggle", args=[m.pk]))
    with tenant_context(shop):
        m.refresh_from_db()
        assert not m.is_active
    r = client.get(reverse("accounts:staff"), {"status": "suspended"})
    assert [x.pk for x in r.context["memberships"]] == [m.pk]
    client.post(reverse("accounts:staff_toggle", args=[m.pk]))
    with tenant_context(shop):
        m.refresh_from_db()
        assert m.is_active


def test_cannot_suspend_yourself_or_a_stronger_person(client, shop, owner):
    mgr, _ = _person(shop, "mgr@x.test", "Manager")
    with tenant_context(shop):
        own = Membership.objects.get(user=owner)
    client.force_login(owner)
    client.post(reverse("accounts:staff_toggle", args=[own.pk]))
    client.force_login(mgr)
    client.post(reverse("accounts:staff_toggle", args=[own.pk]))
    with tenant_context(shop):
        own.refresh_from_db()
        assert own.is_active


def test_subscription_page_lists_invoices(client, shop, owner):
    client.force_login(owner)
    r = client.get(reverse("tenancy:billing"))
    assert r.status_code == 200 and "Invoices" in r.content.decode()


def test_activity_log_ignores_garbage(client, shop, owner):
    client.force_login(owner)
    r = client.get(reverse("accounts:audit_log"), {"who": "abc", "days": "x", "page": "z"})
    assert r.status_code == 200


def test_device_forget(client, shop, owner, main_branch):
    from apps.org.models import Device

    with tenant_context(shop):
        d = Device.objects.create(tenant=shop, branch=main_branch, label="Old phone",
                                  device_id="stolen-1", is_active=False)
    client.force_login(owner)
    client.post(reverse("org:device_update", args=[d.pk]), {"action": "forget"})
    with tenant_context(shop):
        d.refresh_from_db()
        assert d.hidden and not d.is_active   # kept, so it stays blocked
    r = client.get(reverse("org:devices"))
    assert d.pk not in [x.pk for x in r.context["devices"]]
    # The stolen phone trying to register again is still refused.
    r = client.post(reverse("sync:device_register"),
                    data='{"device_id": "stolen-1", "kind": "phone"}',
                    content_type="application/json")
    with tenant_context(shop):
        assert not Device.objects.get(device_id="stolen-1").is_active


def test_a_tills_only_role_can_still_reach_tills_and_devices(client, shop):
    """
    The ten settings pages sit behind one door now, so the sidebar no longer
    links to each. What matters is unchanged: somebody who may manage tills
    can get to them, and is offered nothing they may not open.
    """
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Branch admin")
        role.grant("register.manage")
        u = User.objects.create_user("ba@x.test", "pw", name="BA")
        Membership.objects.create(tenant=shop, user=u, role=role)
        Branch.objects.first()
    client.force_login(u)

    sidebar = client.get(reverse("org:branches"))
    assert sidebar.context["navigation"]["settings"] is not None

    door = client.get(reverse("org:settings_home"))
    offered = {item["url"] for item in door.context["sections"]}
    assert reverse("org:devices") in offered and reverse("org:branches") in offered
    # ...and nothing this role may not open.
    assert reverse("tenancy:billing") not in offered
    assert reverse("accounts:staff") not in offered


def test_starter_owner_can_manage_tills_and_devices(client, db):
    from apps.tenancy.models import Plan
    from apps.tenancy.services import create_tenant

    owner = User.objects.create_user("small@x.test", "pw", name="Small")
    create_tenant(name="Kibanda", owner=owner, plan=Plan.objects.get(code="starter"))
    client.force_login(owner)
    assert client.get(reverse("org:devices")).status_code == 200
    assert client.get(reverse("org:branches")).status_code == 200
    assert client.get(reverse("org:register_create"), HTTP_HX_REQUEST="true").status_code == 200
    # but opening a second branch is still a Business-plan feature
    assert client.get(reverse("org:branch_create")).status_code == 403


def test_branch_confined_manager_cannot_touch_another_branch(client, shop, main_branch):
    with tenant_context(shop):
        other = Branch.objects.create(tenant=shop, name="Kiosk")
        till = Register.objects.create(tenant=shop, branch=other, name="K1")
        u, m = _person(shop, "bm@x.test", "Manager")
        m.set_branches([main_branch], all_branches=False)
    client.force_login(u)
    r = client.post(reverse("org:branch_edit", args=[other.pk]), {"name": "Pwned", "is_active": "on"})
    assert r.status_code == 403
    r = client.post(reverse("org:register_delete", args=[till.pk]))
    assert r.status_code == 403
    with tenant_context(shop):
        other.refresh_from_db()
        assert other.name == "Kiosk" and Register.objects.filter(pk=till.pk).exists()


def test_every_business_setting_reaches_the_page(client, shop, owner):
    """
    The page lays the settings out in two groups and the receipt text on its
    own. A field left out of all three would still exist on the form, still
    save, and simply never be seen -- which is how a setting goes missing
    without anybody noticing.
    """
    client.force_login(owner)
    page = client.get(reverse("org:business"))
    laid_out = {field.name for field in page.context["plain_fields"]}
    laid_out |= {field.name for field in page.context["switch_fields"]}
    laid_out |= {"receipt_header", "receipt_footer"}
    assert set(page.context["form"].fields) == laid_out


def test_the_settings_are_said_in_plain_words(client, shop, owner):
    """"Weighted average cost method" is correct and unusable."""
    client.force_login(owner)
    body = client.get(reverse("org:business")).content.decode()
    assert "What an item costs you" in body
    assert "average of what you have paid" in body
    assert "Weighted average" not in body

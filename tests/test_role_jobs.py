"""
Roles said in plain words.

The permission catalogue is forty-six entries in eight modules, each able to
carry a ceiling, a percentage or a list of methods. That is the right
machinery and the wrong first question for somebody who employs three
people. These tests hold the second way of saying it honest: it must
describe the roles a shop really has, it must not quietly take permissions
away, and everything it grants must go through the same guards as the
matrix.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.role_jobs import JOBS, SPOKEN_FOR, expand, read
from apps.core.context import tenant_context
from apps.core.permissions import registry

pytestmark = pytest.mark.django_db


def _granted(role):
    return {rp.permission.code: rp.limit_value
            for rp in role.permissions.select_related("permission")}


# -- the vocabulary itself -------------------------------------------------


def test_every_permission_belongs_to_a_job():
    """
    A permission added next year must be placed in a job, or the simple view
    would silently stop being able to describe a role that holds it.
    """
    import apps.accounts.permissions  # noqa: F401  (registers the catalogue)

    assert {spec.code for spec in registry.all()} - SPOKEN_FOR == set()


def test_no_two_jobs_are_known_by_the_same_permission():
    """
    Jobs share the harmless ones -- four of them need "see the product list"
    -- but what identifies a job must be its own, or a cashier reads as half
    a buyer.
    """
    seen = {}
    for job in JOBS:
        for code in job.core:
            assert code not in seen, f"{code} identifies both {seen.get(code)} and {job.key}"
            seen[code] = job.key


def test_the_roles_a_shop_really_has_are_describable(client, shop, owner):
    """The seeded roles are the ones a duka actually builds."""
    with tenant_context(shop):
        cashier = read(_granted(Role.objects.get(name="Cashier")))
        manager = read(_granted(Role.objects.get(name="Manager")))

    on = {key for key, row in cashier.items() if row["state"] == "on"}
    assert on == {"sell", "discount"}
    assert not [key for key, row in cashier.items() if row["state"] == "some"]
    assert all(row["state"] == "on" for row in manager.values())


# -- turning switches into permissions -------------------------------------


def test_a_switch_grants_what_it_says_it_grants():
    out = expand({"job:sell": "on"})
    assert set(out) == set(dict.fromkeys(JOBS[0].grants))
    assert out["pos.operate"] == {"limit": "", "options": []}


def test_a_ceiling_lands_on_the_permission_that_carries_it():
    out = expand({"job:discount": "on", "joblimit:discount": "7.5"})
    assert out["pos.discount"]["limit"] == "7.5"
    # The companion comes with it, without a ceiling of its own.
    assert out["pos.price_override"]["limit"] == ""


def test_a_list_of_methods_is_never_granted_empty():
    """
    A phone seller who could fill a basket and then take no payment for it
    was exactly this: the permission held, its list of methods empty.
    """
    import apps.accounts.permissions  # noqa: F401

    out = expand({"job:phone": "on"})
    assert out["pos.mobile_methods"]["options"], "granted with no method is no grant at all"


def test_what_the_plan_does_not_include_is_not_claimed():
    out = expand({"job:stock": "on"}, available={"stock.receive", "stock.count"})
    assert set(out) == {"stock.receive", "stock.count"}


# -- the page --------------------------------------------------------------


def test_saving_the_simple_view_sets_exactly_those_jobs(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Evening cashier")

    client.force_login(owner)
    response = client.post(reverse("accounts:role_edit", args=[role.pk]), {
        "name": "Evening cashier", "description": "", "mode": "simple",
        "job:sell": "on", "job:undo": "on", "joblimit:undo": "20000",
    })
    assert response.status_code == 302

    with tenant_context(shop):
        held = _granted(Role.objects.get(pk=role.pk))
    assert "pos.operate" in held and "pos.refund" in held
    assert held["pos.refund"] == Decimal("20000")
    # And nothing from the jobs that were left off.
    assert "stock.receive" not in held and "report.sales" not in held


def test_turning_a_job_off_takes_it_away(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Temp")
    client.force_login(owner)
    url = reverse("accounts:role_edit", args=[role.pk])
    client.post(url, {"name": "Temp", "description": "", "mode": "simple",
                      "job:sell": "on", "job:stock": "on"})
    client.post(url, {"name": "Temp", "description": "", "mode": "simple",
                      "job:sell": "on"})

    with tenant_context(shop):
        held = _granted(Role.objects.get(pk=role.pk))
    assert "pos.operate" in held and "stock.receive" not in held


def test_a_role_built_by_hand_opens_in_the_matrix_and_says_why(client, shop, owner):
    """Flattening half a job into a switch would take permissions away."""
    from apps.accounts.models import Permission, RolePermission

    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Odd one")
        # Half of "sells at the till", and nothing else.
        RolePermission.objects.create(
            role=role, permission=Permission.objects.get(code="pos.operate"), granted=True)

    client.force_login(owner)
    page = client.get(reverse("accounts:role_edit", args=[role.pk]))
    assert page.context["set_by_hand"] is True
    assert b"put together by hand" in page.content


def test_the_matrix_still_saves_the_way_it_always_did(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="By hand")
    client.force_login(owner)
    client.post(reverse("accounts:role_edit", args=[role.pk]), {
        "name": "By hand", "description": "", "mode": "advanced",
        "grant:pos.operate": "on", "grant:pos.discount": "on", "limit:pos.discount": "3",
    })
    with tenant_context(shop):
        held = _granted(Role.objects.get(pk=role.pk))
    assert set(held) == {"pos.operate", "pos.discount"}
    assert held["pos.discount"] == Decimal("3")


def test_you_cannot_hand_out_more_than_you_hold_through_the_simple_view(
        client, shop, main_branch, cashier):
    """
    The plain view is a second door to the same room, so it meets the same
    guard: a manager cannot give away what they do not have themselves.
    """
    from apps.accounts.models import Membership

    with tenant_context(shop):
        limited = Role.objects.create(tenant=shop, name="Shift lead")
        person = Membership.objects.create(tenant=shop, user=cashier,
                                           role=Role.objects.get(name="Manager"))
        person.branch_links.create(branch=main_branch)
    client.force_login(cashier)
    client.post(reverse("accounts:role_edit", args=[limited.pk]), {
        "name": "Shift lead", "description": "", "mode": "simple", "job:admin": "on",
    })
    with tenant_context(shop):
        held = _granted(Role.objects.get(pk=limited.pk))
    # A manager holds neither of these, so neither may be handed on.
    assert "role.manage" not in held and "billing.manage" not in held

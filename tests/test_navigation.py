"""
The sidebar: what it shows, and where it says you are.

Two failures this file exists to stop. One, the wrong entry lighting up --
the fiscal receipts page used to light "Goods received", because the check
was a substring test and "fiscal_receipts" contains "receipt". Two, a shop
carrying words it has never used: a duka that has never moved stock between
branches should not read "Move stock" every day.
"""

import re

import pytest
from django.urls import reverse

from apps.core import nav
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

# Every page the sidebar can reach, and the entry that should be lit there.
PAGES = [
    ("core:dashboard", "Dashboard"),
    ("pos:sale_list", "Sales"),
    ("catalog:product_list", "Products"),
    ("inventory:stock_list", "Stock"),
    ("inventory:transfer_list", "Move stock between branches"),
    ("inventory:count_list", "Count the shelves"),
    ("inventory:batch_list", "What is going off"),
    ("purchasing:receipt_list", "Deliveries that have arrived"),
    ("purchasing:order_list", "Orders to suppliers"),
    ("purchasing:supplier_list", "Who you buy from"),
    ("customers:customer_list", "Customers"),
    ("finance:expense_list", "Expenses"),
    ("pos:fiscal_receipts", "Tax copies sent to TRA"),
    ("finance:cashups", "Counting the drawer at the end of a shift"),
    ("reports:index", "Sales report"),
    ("reports:margin", "Profit"),
    ("reports:stock_value", "Stock value"),
    ("reports:staff", "Who sold what, and who was short"),
    ("accounts:audit_log", "Activity"),
]

# The ten settings pages live behind one door, so all of them light it.
SETTINGS_PAGES = [
    "org:settings_home", "accounts:staff", "accounts:roles", "org:branches",
    "org:devices", "catalog:price_lists", "notifications:message_log",
    "catalog:tiles", "catalog:taxonomy", "org:business", "tenancy:billing",
]


def _lit(body):
    """The titles of every entry drawn as the page you are on."""
    return set(re.findall(r'title="([^"]+)" class="nav-link nav-link-active"', body)) | set(
        re.findall(r'title="([^"]+)"\s+class="nav-link nav-link-active"', body)
    )


@pytest.fixture
def ready(shop, main_branch, owner):
    from apps.org.models import Register

    with tenant_context(shop):
        Register.objects.get_or_create(branch=main_branch, name="Till 1")
    nav.forget(shop)
    return shop


@pytest.mark.parametrize("name,label", PAGES)
def test_exactly_one_thing_is_lit_and_it_is_the_right_one(client, ready, owner, name, label):
    client.force_login(owner)
    response = client.get(reverse(name))
    assert response.status_code == 200, name
    assert _lit(response.content.decode()) == {label}, name


@pytest.mark.parametrize("name", SETTINGS_PAGES)
def test_every_settings_page_lights_settings(client, ready, owner, name):
    """Ten words in the sidebar became one door; it should say you are behind it."""
    client.force_login(owner)
    response = client.get(reverse(name))
    assert response.status_code == 200, name
    assert _lit(response.content.decode()) == {"Settings"}, name


def test_the_statements_page_belongs_to_customers(client, ready, owner):
    """
    "Who owes" is only its own entry for somebody who may collect but may not
    manage customers. For everyone else the page sits under Customers.
    """
    client.force_login(owner)
    assert _lit(client.get(reverse("customers:statements")).content.decode()) == {"Customers"}


# -- what the shop has never used ------------------------------------------


def _shown(response):
    """The entries drawn in the sidebar itself, not the ones folded away."""
    return [item["label"] for section in response.context["navigation"]["sections"]
            for item in section["entries"]]


def _folded(response):
    return [item["label"] for section in response.context["navigation"]["sections"]
            for item in section["more"]]


def test_a_new_shop_is_not_shown_what_it_has_never_done(client, ready, owner):
    client.force_login(owner)
    page = client.get(reverse("core:dashboard"))
    shown, folded = _shown(page), _folded(page)

    # One branch, no deliveries, no orders, no expenses, no cash-ups.
    for waiting in ("Move stock", "Deliveries", "Supplier orders", "Expenses", "End of day"):
        assert waiting in folded and waiting not in shown, waiting
    # The things it does every day are there.
    for daily in ("Dashboard", "Till", "Sales", "Products", "Stock"):
        assert daily in shown, daily
    assert page.context["navigation"]["settings"]["label"] == "Settings"


def test_using_something_brings_it_into_the_sidebar(client, ready, main_branch, owner):
    """And it stays: the shop has done it now."""
    from decimal import Decimal

    from apps.finance.models import Expense, ExpenseCategory

    client.force_login(owner)
    assert "Expenses" not in _shown(client.get(reverse("core:dashboard")))

    with tenant_context(ready, branch=main_branch, user=owner):
        category = ExpenseCategory.objects.create(name="Transport")
        Expense.objects.create(tenant=ready, branch=main_branch, category=category,
                               amount=Decimal("5000"), spent_at="2026-09-01")
    nav.forget(ready)

    assert "Expenses" in _shown(client.get(reverse("core:dashboard")))


def test_a_second_branch_is_reason_enough_to_move_stock(client, ready, owner):
    """Two shops make "which branch" a real question, before any transfer exists."""
    from apps.org.models import Branch

    client.force_login(owner)
    assert "Move stock" not in _shown(client.get(reverse("core:dashboard")))

    with tenant_context(ready):
        Branch.objects.create(name="Nungwi")
    nav.forget(ready)

    assert "Move stock" in _shown(client.get(reverse("core:dashboard")))


def test_you_are_never_on_a_page_whose_entry_is_folded_away(client, ready, owner):
    """Opening a folded page brings its entry out, or the sidebar would lie."""
    client.force_login(owner)
    page = client.get(reverse("finance:expense_list"))
    assert "Expenses" in _shown(page) and "Expenses" not in _folded(page)


def test_a_cashier_sees_a_cashier_sized_sidebar(client, shop, main_branch, cashier):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        person = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
        person.branch_links.create(branch=main_branch)
    nav.forget(shop)

    client.force_login(cashier)
    page = client.get(reverse("core:dashboard"))
    assert "Till" in _shown(page)
    everything = _shown(page) + _folded(page)
    for owners_only in ("Profit", "Who sold what", "Suppliers"):
        assert owners_only not in everything, owners_only
    assert page.context["navigation"]["settings"] is None

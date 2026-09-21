"""
The sidebar says where you are.

Two links lit at once, or the wrong one lit, is a small thing that makes a
system feel untrustworthy: the fiscal receipts page used to light up "Goods
received", because the check was a substring test and "fiscal_receipts"
contains "receipt".
"""

import re

import pytest
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

# Every destination in the sidebar, with the label that should be lit there.
PAGES = [
    ("core:dashboard", "Dashboard"),
    ("pos:sale_list", "Sales"),
    ("catalog:product_list", "Products"),
    ("inventory:stock_list", "Stock"),
    ("inventory:transfer_list", "Transfers"),
    ("inventory:count_list", "Counts"),
    ("inventory:batch_list", "Expiry"),
    ("purchasing:receipt_list", "Goods received"),
    ("purchasing:order_list", "Orders"),
    ("purchasing:supplier_list", "Suppliers"),
    ("customers:customer_list", "Customers"),
    ("finance:expense_list", "Expenses"),
    ("pos:fiscal_receipts", "Fiscal receipts"),
    ("finance:cashups", "Cash ups"),
    ("reports:index", "Sales report"),
    ("reports:margin", "Profit"),
    ("reports:stock_value", "Stock value"),
    ("reports:staff", "Staff"),
    ("accounts:audit_log", "Activity"),
    ("accounts:staff", "Staff"),
    ("accounts:roles", "Roles and permissions"),
    ("org:branches", "Branches and tills"),
    ("org:devices", "Tills and phones in use"),
    ("catalog:price_lists", "Price lists"),
    ("notifications:message_log", "Messages"),
    ("catalog:tiles", "Till tiles"),
    ("catalog:taxonomy", "Categories and VAT"),
    ("org:business", "Business settings"),
    ("tenancy:billing", "Subscription"),
]


def _lit(body):
    """The titles of every link drawn as the current page."""
    return {
        title for title in re.findall(r'title="([^"]+)" class="nav-link nav-link-active', body)
    } | {
        title for title in
        re.findall(r'title="([^"]+)" class="nav-link [^"]*nav-link-active', body)
    }


def test_the_statements_page_belongs_to_customers(client, shop, owner):
    """
    "Who owes" is only a nav entry for somebody who may collect but may not
    manage customers. For everyone else the page sits under Customers, and
    the sidebar should say so rather than going blank.
    """
    client.force_login(owner)
    body = client.get(reverse("customers:statements")).content.decode()
    assert _lit(body) == {"Customers"}


@pytest.mark.parametrize("name,label", PAGES)
def test_exactly_one_thing_is_lit_and_it_is_the_right_one(
        client, shop, main_branch, owner, name, label):
    from apps.org.models import Register

    with tenant_context(shop):
        Register.objects.get_or_create(branch=main_branch, name="Till 1")
    client.force_login(owner)
    response = client.get(reverse(name))
    assert response.status_code == 200, name
    assert _lit(response.content.decode()) == {label}, name

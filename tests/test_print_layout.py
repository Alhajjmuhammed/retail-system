"""
What the printed quotation, proforma and invoice show, and in what order.

The owner ticks the columns under Settings, Business and numbers them; the
paper follows. And a discounted document says what it would have cost, what
came off, and what is left.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def rules(shop):
    from apps.org.models import TenantSettings

    with tenant_context(shop):
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)
        return row


@pytest.fixture
def proforma(shop, main_branch, stocked):
    from apps.customers.models import Customer
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        buyer = Customer.objects.create(name="HANY SEIF")
        inv = Invoice.objects.create(
            reference="PRO260001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        inv.lines.create(variant=stocked["Mkate"], description="Biskuti Paketi",
                         qty=Decimal("2"), unit_price=Decimal("2000"),
                         discount_percent=Decimal("80"), tax_rate=Decimal("18"), position=10)
        inv.lines.create(description="Kahawa 200g", qty=Decimal("1"),
                         unit_price=Decimal("7003"), tax_rate=Decimal("18"), position=20)
        return inv


def _headers(html):
    head = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    cells = re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)
    return [re.sub(r"<[^>]+>", "", c).strip() for c in cells]


def _print(client, owner, proforma):
    client.force_login(owner)
    return client.get(reverse("selling:invoice_print", args=[proforma.pk])).content.decode()


def test_the_usual_columns_by_default(client, owner, proforma):
    assert _headers(_print(client, owner, proforma)) == [
        "Item", "Description", "Qty", "Rate", "Amount", "VAT"]


def test_the_owner_chooses_columns_and_their_order(client, shop, owner, rules, proforma):
    rules.print_columns = ["picture", "description", "qty", "amount"]
    rules.save()
    assert _headers(_print(client, owner, proforma)) == [
        "Picture", "Description", "Qty", "Amount"]


def test_description_and_amount_always_print(client, shop, owner, rules, proforma):
    rules.print_columns = ["qty"]
    rules.save()
    assert _headers(_print(client, owner, proforma)) == ["Qty", "Description", "Amount"]


def test_a_picture_prints_when_chosen(client, shop, owner, rules, proforma, stocked):
    from django.core.files.base import ContentFile

    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.image.save("mkate.webp", ContentFile(b"RIFF0000WEBP"), save=True)
    rules.print_columns = ["picture", "description", "amount"]
    rules.save()
    html = _print(client, owner, proforma)
    assert product.image.url in html


def test_a_discount_column_takes_the_discount_out_of_the_amount(
        client, shop, owner, rules, proforma):
    rules.print_columns = ["description", "qty", "rate", "discount", "amount"]
    rules.save()
    html = _print(client, owner, proforma)
    assert _headers(html)[3] == "Discount"
    assert "(less 80%)" not in html
    assert "line-through" not in html


def test_totals_show_before_and_after_the_discount(client, owner, proforma):
    html = _print(client, owner, proforma)
    # 2 x 2,000 + 7,003 = 11,003 before; 3,200 off; 7,803 to pay.
    assert "Total before discount" in html
    assert "11,003.00" in html
    assert "3,200.00" in html
    assert "7,803.00" in html


def test_no_discount_rows_without_a_discount(client, shop, owner, proforma):
    with tenant_context(shop):
        proforma.lines.update(discount_percent=0)
    assert "Total before discount" not in _print(client, owner, proforma)


def test_the_quotation_print_follows_the_same_layout(
        client, shop, main_branch, owner, rules, stocked):
    from apps.customers.models import Customer
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        q = Quotation.objects.create(
            reference="QUO260001", customer=Customer.objects.create(name="X"),
            branch=main_branch, issued_on=today, valid_until=today + timedelta(days=14))
        q.lines.create(description="Mincer", qty=Decimal("2"),
                       unit_price=Decimal("2170.60"), discount_percent=Decimal("10"),
                       position=10)
    rules.print_columns = ["qty", "description", "rate", "amount"]
    rules.save()
    client.force_login(owner)
    html = client.get(reverse("selling:quotation_print", args=[q.pk])).content.decode()
    assert _headers(html) == ["Qty", "Description", "Rate", "Amount"]
    assert "Total before discount" in html
    assert "4,341.00" in html  # 2 x 2,170.60, to the shilling, before the 10%


def test_settings_save_the_columns_in_the_numbered_order(rules):
    from django.http import QueryDict

    from apps.org.views import _print_columns_from

    post = QueryDict(mutable=True)
    post.setlist("print_show", ["picture", "qty", "vat"])
    post.update({"print_pos_picture": "1", "print_pos_qty": "4", "print_pos_vat": "2",
                 "print_pos_description": "3", "print_pos_amount": "5"})
    assert _print_columns_from(post) == ["picture", "vat", "description", "qty", "amount"]


def test_the_settings_page_lists_every_column(client, shop, owner, rules):
    client.force_login(owner)
    page = client.get(reverse("org:business")).content.decode()
    assert "Printed quotations and invoices" in page
    for label in ["Picture", "Item code", "Discount", "VAT"]:
        assert label in page

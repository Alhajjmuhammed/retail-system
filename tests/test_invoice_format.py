"""
The invoice as a Zanzibar business expects it.

Who is billing with their VRN and TIN, who is billed with their ZRA number,
the shop's own code against each line, and at the foot the account the money
should go to. An invoice quoting no bank account, or one that cannot take
the money it is asking for, does not get paid.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling.models import Invoice

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(
            name="Tulia Zanzibar Resort", phone="0788112233",
            tin="111-101-694", zra_number="2035300846-04F",
            credit_limit=Decimal("5000000"))


@pytest.fixture
def invoice(shop, main_branch, buyer, stocked):
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        inv = Invoice.objects.create(
            reference="INV260001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        inv.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                         qty=Decimal("4"), unit_price=Decimal("1500"),
                         tax_rate=Decimal("18"), position=10)
        return inv


def test_it_carries_both_sides_tax_numbers(client, shop, main_branch, owner, invoice):
    with tenant_context(shop):
        shop.vrn = "37-011186-Y"
        shop.tin = "111-101-694"
        shop.save(update_fields=["vrn", "tin"])

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "Tax Invoice" in body or "TAX INVOICE" in body.upper()
    assert "VRN NO: 37-011186-Y" in body
    assert "TIN NO: 111-101-694" in body
    assert "Invoice To" in body
    assert "ZRA NO: 2035300846-04F" in body


def test_the_item_column_shows_the_shops_own_code(
        client, shop, main_branch, owner, invoice, stocked):
    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.sku = "CC-050"
        product.save(update_fields=["sku"])

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "CC-050" in body


def test_the_factory_code_fills_in_when_there_is_no_own_code(
        client, shop, main_branch, owner, invoice, stocked):
    """The column should not be blank on half the lines."""
    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.sku = ""
        product.factory_code = "TPCC-50-A"
        product.save(update_fields=["sku", "factory_code"])

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "TPCC-50-A" in body


def test_the_bank_account_that_takes_the_money_is_the_one_printed(
        client, shop, main_branch, owner, invoice):
    """
    A shop with a shilling account and a dollar account must show the one
    that can receive what the invoice asks for.
    """
    from apps.org.models import BankAccount

    with tenant_context(shop):
        BankAccount.objects.create(
            bank_name="ABSA Bank (T) Ltd", branch="Zanzibar",
            account_name="Duka la Salma", account_number="003 6005289",
            swift_code="BARCTZTZ", currency="TZS", position=1)
        BankAccount.objects.create(
            bank_name="ABSA Bank (T) Ltd", branch="Zanzibar",
            account_name="Duka la Salma", account_number="003 6009314",
            swift_code="BARCTZTZ", currency="USD", position=2)

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "BANK NAME: ABSA Bank (T) Ltd" in body
    assert "003 6005289" in body, "the shilling account should be shown"
    assert "003 6009314" not in body, "the dollar account is for dollars"
    assert "SWIFT CODE: BARCTZTZ" in body


def test_with_no_account_for_that_money_it_shows_what_there_is(
        client, shop, main_branch, owner, invoice):
    """Better an account they can ask about than a blank space."""
    from apps.org.models import BankAccount

    with tenant_context(shop):
        BankAccount.objects.create(
            bank_name="CRDB", account_name="Duka la Salma",
            account_number="015 2233445", currency="USD")

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "015 2233445" in body


def test_a_shop_with_no_bank_account_still_prints(client, shop, main_branch, owner, invoice):
    client.force_login(owner)
    page = client.get(reverse("selling:invoice_print", args=[invoice.pk]))
    assert page.status_code == 200
    assert "BANK NAME" not in page.content.decode()


def test_the_footer_carries_the_way_to_reach_the_shop(
        client, shop, main_branch, owner, invoice):
    with tenant_context(shop):
        shop.phone = "+255 777 411887"
        shop.email = "chau@hotel-solutions.co.tz"
        shop.website = "www.hotel-solutions.co.tz"
        shop.save(update_fields=["phone", "email", "website"])

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    for detail in ("+255 777 411887", "chau@hotel-solutions.co.tz",
                   "www.hotel-solutions.co.tz"):
        assert detail in body


def test_the_totals_read_the_way_the_trade_expects(
        client, shop, main_branch, owner, invoice):
    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    for label in ("Subtotal", "VAT Total", "Total", "Payments/Credits", "Bal. Due"):
        assert label in body, label


def test_bank_accounts_are_kept_in_settings(client, shop, main_branch, owner):
    from apps.org.models import BankAccount

    client.force_login(owner)
    client.post(reverse("org:bank_account_create"), {
        "bank_name": "ABSA Bank (T) Ltd", "branch": "Zanzibar",
        "account_name": "Duka la Salma", "account_number": "003 6005289",
        "swift_code": "BARCTZTZ", "currency": "TZS", "is_active": "on",
    }, follow=True)
    with tenant_context(shop):
        account = BankAccount.objects.get()
        assert account.account_number == "003 6005289"

    page = client.get(reverse("org:bank_accounts"))
    assert "ABSA Bank (T) Ltd" in page.content.decode()


def test_a_cashier_may_not_change_where_the_money_goes(client, shop, main_branch, cashier):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier,
                                  role=Role.objects.get(name="Cashier"))
    client.force_login(cashier)
    assert client.get(reverse("org:bank_accounts")).status_code == 403
    assert client.post(reverse("org:bank_account_create"), {
        "bank_name": "Mine", "account_name": "Mine", "account_number": "1",
        "currency": "TZS",
    }).status_code == 403


# The smallest valid PNG: one transparent pixel.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_a_discounted_line_shows_what_it_was_and_what_it_is(
        client, shop, main_branch, owner, invoice):
    """
    A customer promised ten per cent wants to see the ten per cent. Showing
    only the figure after it leaves them working out whether they got it.
    """
    with tenant_context(shop, branch=main_branch):
        line = invoice.lines.get()
        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        assert line.gross_total == Decimal("6000")     # 4 x 1,500
        assert line.line_total == Decimal("5400")

    client.force_login(owner)
    for name in ("selling:invoice_print", "selling:invoice_detail"):
        body = client.get(reverse(name, args=[invoice.pk])).content.decode()
        assert "6,000" in body, f"{name}: the price before the discount is missing"
        assert "5,400" in body, f"{name}: the price after the discount is missing"


def test_an_undiscounted_line_shows_one_figure(client, shop, main_branch, owner, invoice):
    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "line-through" not in body


def test_the_product_picture_is_on_the_line(client, shop, main_branch, owner, invoice, stocked):
    from django.core.files.uploadedfile import SimpleUploadedFile

    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.image = SimpleUploadedFile("mkate.png", PIXEL, content_type="image/png")
        product.save(update_fields=["image"])

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_detail", args=[invoice.pk])).content.decode()
    assert product.image.url in body, "the product picture is not shown against the line"


def test_the_picker_carries_the_picture_and_the_code(
        client, shop, main_branch, owner, buyer, stocked):
    from django.core.files.uploadedfile import SimpleUploadedFile

    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.sku = "CC-050"
        product.image = SimpleUploadedFile("mkate2.png", PIXEL, content_type="image/png")
        product.save(update_fields=["sku", "image"])

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        draft = Invoice.objects.create(
            reference="PRO260009", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_detail", args=[draft.pk])).content.decode()
    assert "data-img=" in body
    assert 'data-code="CC-050"' in body
    assert "data-desc=" in body


def test_pictures_on_the_lines_are_the_shops_choice(
        client, shop, main_branch, owner, invoice, stocked):
    """
    A shop selling things that look alike wants them; a shop that wants
    plain paperwork does not. Choosing a product always shows one, because
    that is about picking the right product, not about the document.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.org.models import TenantSettings

    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.image = SimpleUploadedFile("mk.png", PIXEL, content_type="image/png")
        product.save(update_fields=["image"])
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)

    # The thumbnail drawn against a line, as opposed to the one the picker
    # carries in a data attribute for choosing with.
    THUMB = 'class="h-9 w-9 shrink-0 rounded object-cover'

    client.force_login(owner)
    url = reverse("selling:invoice_detail", args=[invoice.pk])
    assert THUMB in client.get(url).content.decode()

    with tenant_context(shop):
        row.show_product_images = False
        row.save(update_fields=["show_product_images"])

    body = client.get(url).content.decode()
    assert THUMB not in body, "switched off, the line should carry no picture"
    # The picker still offers one, whatever the setting says.
    assert "data-img=" in body
    assert product.image.url in body


def test_the_totals_block_adds_up_on_the_page(client, shop, main_branch, owner, invoice):
    """
    Subtotal + VAT = Total, as anybody reading an invoice expects.

    Prices here include VAT, so printing the total as the subtotal and the
    VAT beneath it made the tax look added on top and the sum wrong. Only
    looking at the page catches this; every figure was individually right.
    """
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.net_of_tax + invoice.tax == invoice.total
        assert invoice.net_of_tax < invoice.total, "the net must be less than the gross"

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    from django.contrib.humanize.templatetags.humanize import intcomma
    assert intcomma(f"{invoice.net_of_tax:.2f}") in body
    assert intcomma(f"{invoice.total:.2f}") in body

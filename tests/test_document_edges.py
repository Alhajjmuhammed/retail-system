"""
The awkward shapes a document can be in.

A template that only works with tidy data fails on the first shop that has
not filled everything in -- and the shop finds out in front of a customer.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling.models import Invoice, Quotation, QuotationStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def bare(shop):
    """A shop with nothing filled in beyond its name."""
    from apps.customers.models import Customer

    with tenant_context(shop):
        shop.tin = shop.vrn = shop.address = shop.phone = ""
        shop.email = shop.website = ""
        shop.save()
        return Customer.objects.create(name="Walk-in Company")


def test_an_empty_invoice_still_prints(client, shop, main_branch, owner, bare):
    """No lines, no tax numbers, no bank account, nothing filled in."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.create(
            reference="PRO260001", customer=bare, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))

    client.force_login(owner)
    page = client.get(reverse("selling:invoice_print", args=[invoice.pk]))
    assert page.status_code == 200
    body = page.content.decode()
    assert bare.name in body
    # Nothing rendered as the word None or a broken total.
    assert "None" not in body


def test_an_empty_quotation_still_prints(client, shop, main_branch, owner, bare):
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=bare, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.DRAFT)

    client.force_login(owner)
    page = client.get(reverse("selling:quotation_print", args=[quotation.pk]))
    assert page.status_code == 200
    assert "None" not in page.content.decode()


def test_a_typed_line_has_no_code_and_no_picture(client, shop, main_branch, owner, bare):
    """Delivery and labour are not in the product list and never will be."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.create(
            reference="PRO260002", customer=bare, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        line = invoice.lines.create(
            description="Delivery to Nungwi and back, two lorries",
            qty=Decimal("1"), unit_price=Decimal("25000"), tax_rate=Decimal("18"))
        assert line.item_code == ""

    client.force_login(owner)
    for name in ("selling:invoice_print", "selling:invoice_detail"):
        page = client.get(reverse(name, args=[invoice.pk]))
        assert page.status_code == 200
        assert "Delivery to Nungwi" in page.content.decode()


def test_a_hundred_per_cent_discount_does_not_break_the_money(shop, main_branch, bare):
    """Goods given away still have to add up, and carry no VAT."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.create(
            reference="PRO260003", customer=bare, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        invoice.lines.create(description="Sample", qty=Decimal("1"),
                             unit_price=Decimal("10000"), tax_rate=Decimal("18"),
                             discount_percent=Decimal("100"))
        assert invoice.lines_total == Decimal("0")
        assert invoice.total == Decimal("0")
        assert invoice.tax == Decimal("0")
        assert invoice.balance == Decimal("0")


def test_an_invoice_of_nothing_can_still_be_read(client, shop, main_branch, owner, bare):
    """Zero lines means a zero total, not a crash on dividing by it."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.create(
            reference="PRO260004", customer=bare, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14),
            discount_percent=Decimal("10"))
        assert invoice.lines_total == Decimal("0")
        assert invoice.order_discount == Decimal("0")
        assert invoice.tax == Decimal("0")

    client.force_login(owner)
    assert client.get(
        reverse("selling:invoice_print", args=[invoice.pk])).status_code == 200


def test_a_switched_off_bank_account_does_not_print(
        client, shop, main_branch, owner, bare):
    from apps.org.models import BankAccount

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        BankAccount.objects.create(
            bank_name="Closed Bank", account_name="Old", account_number="999",
            currency=shop.currency, is_active=False)
        invoice = Invoice.objects.create(
            reference="PRO260005", customer=bare, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))

    client.force_login(owner)
    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "Closed Bank" not in body


def test_one_shop_never_sees_another_shops_bank_account(shop, django_user_model):
    from apps.core.context import unscoped
    from apps.org.models import BankAccount
    from apps.tenancy.services import create_tenant

    with tenant_context(shop):
        BankAccount.objects.create(bank_name="Ours", account_name="Ours",
                                   account_number="111", currency="TZS")

    asha = django_user_model.objects.create_user("asha2@duka.test", "pw", name="Asha")
    other, _ = create_tenant(name="Duka la Asha 2", owner=asha,
                             plan=shop.subscription.plan)
    with tenant_context(other):
        assert not BankAccount.objects.exists()
    with unscoped():
        assert BankAccount.objects.count() == 1

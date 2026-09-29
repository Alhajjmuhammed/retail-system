"""
Discounts, the other codes a product goes by, and whole-number money.

The arithmetic is the point. A discount that leaves VAT declared on money
the customer never paid is worse than no discount at all, so every figure
here was worked out on paper first.
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
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini", phone="0788112233",
                                       credit_limit=Decimal("5000000"))


@pytest.fixture
def invoice(shop, main_branch, buyer, stocked):
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        inv = Invoice.objects.create(reference="INV260001", customer=buyer,
                                     branch=main_branch, issued_on=today,
                                     due_on=today + timedelta(days=14))
        inv.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                         qty=Decimal("10"), unit_price=Decimal("18000"),
                         tax_rate=Decimal("18"), position=10)
        return inv


# -- the arithmetic --------------------------------------------------------

def test_a_discount_on_a_line_lowers_the_vat_inside_it(shop, main_branch, invoice):
    """
    180,000 less 10% is 162,000, and the VAT inside 162,000 at 18% inclusive
    is 24,711.86 -- not the 27,457.63 that was inside the undiscounted price.
    Declaring the larger figure is declaring tax on money nobody paid.
    """
    with tenant_context(shop, branch=main_branch):
        line = invoice.lines.get()
        assert line.line_total == Decimal("180000")

        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        line.refresh_from_db()

        assert line.discount == Decimal("18000")
        assert line.line_total == Decimal("162000")
        assert line.tax_amount == Decimal("24711.86")


def test_a_discount_on_the_whole_invoice(shop, main_branch, invoice):
    with tenant_context(shop, branch=main_branch):
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        invoice.refresh_from_db()

        assert invoice.lines_total == Decimal("180000")
        assert invoice.order_discount == Decimal("9000")
        assert invoice.total == Decimal("171000")
        # VAT scaled to what is actually charged: 171,000 x 18/118.
        assert invoice.tax == Decimal("26084.75")


def test_both_discounts_stack(shop, main_branch, invoice):
    """Ten off the line, then five off the lot."""
    with tenant_context(shop, branch=main_branch):
        line = invoice.lines.get()
        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        invoice.refresh_from_db()

        assert invoice.lines_total == Decimal("162000")
        assert invoice.order_discount == Decimal("8100")
        assert invoice.total == Decimal("153900")


def test_no_discount_changes_nothing(shop, main_branch, invoice):
    """Every invoice already written must still mean what it meant."""
    with tenant_context(shop, branch=main_branch):
        assert invoice.discount_percent == Decimal("0")
        assert invoice.total == Decimal("180000")
        assert invoice.tax == Decimal("27457.63")
        assert invoice.order_discount == Decimal("0")


def test_the_customer_owes_the_discounted_figure(shop, main_branch, buyer, invoice, owner):
    """What is billed is what was agreed, not the figure before the discount."""
    from apps.selling import services

    with tenant_context(shop, branch=main_branch, user=owner):
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        services.issue(invoice)
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("171000")


def test_more_than_everything_off_is_refused(client, shop, main_branch, owner, invoice):
    client.force_login(owner)
    response = client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {
        "action": "header", "due_on": invoice.due_on.isoformat(),
        "discount_percent": "120", "note": "",
    }, follow=True)
    assert "cannot be more than 100" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.discount_percent == Decimal("0")


# -- picking a product fills the line in -----------------------------------

def test_the_price_is_offered_when_a_product_is_picked(
        client, shop, main_branch, owner, buyer, stocked):
    """
    The page carries each product's price, so choosing one fills the box in
    rather than leaving somebody to type a shelf price from memory.
    """
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.DRAFT)

    client.force_login(owner)
    body = client.get(
        reverse("selling:quotation_detail", args=[quotation.pk])).content.decode()
    assert "data-price=" in body
    assert "line-entry.js" in body


def test_a_discount_can_be_typed_when_the_line_is_added(
        client, shop, main_branch, owner, buyer, stocked):
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260002", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.DRAFT)

    client.force_login(owner)
    client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "variant": stocked["Mkate"].pk,
        "qty": "10", "unit_price": "18000", "discount_percent": "10",
    }, follow=True)

    with tenant_context(shop, branch=main_branch):
        line = Quotation.objects.get(pk=quotation.pk).lines.get()
        assert line.discount_percent == Decimal("10")
        assert line.line_total == Decimal("162000")


# -- the other codes a product goes by -------------------------------------

def test_a_product_can_be_found_by_its_old_code(client, shop, main_branch, owner):
    """A customer rings quoting the number off a price list from last year."""
    from apps.catalog.models import Product, TaxRate, Unit

    with tenant_context(shop):
        product = Product.objects.create(
            name="Simenti Twiga 50kg", sku="CEM-50",
            factory_code="TPCC-50-A", old_code="OLD-991",
            base_unit=Unit.objects.first(), tax_rate=TaxRate.objects.first())

    client.force_login(owner)
    for term in ("OLD-991", "TPCC-50-A"):
        body = client.get(reverse("catalog:product_list"), {"q": term}).content.decode()
        assert product.name in body, f"{term} did not find it"


def test_money_boxes_step_in_whole_shillings(client, shop, main_branch, owner, invoice):
    """Nobody hands across a cent, and the arrows used to move by 0.01."""
    client.force_login(owner)
    body = client.get(reverse("selling:invoice_detail", args=[invoice.pk])).content.decode()
    assert 'name="unit_price"' in body
    price_box = body[body.index('name="unit_price"') - 120:body.index('name="unit_price"') + 40]
    assert 'step="1"' in price_box
    assert 'step="0.01"' not in price_box

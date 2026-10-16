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
    """
    The arrows never move by 0.01. A price may still be typed with cents --
    a shop that buys at 2,170.60 means it, and a step of 1 made the browser
    refuse it -- so a price box takes step="any". An amount handed across is
    whole shillings.
    """
    client.force_login(owner)
    body = client.get(reverse("selling:invoice_detail", args=[invoice.pk])).content.decode()
    assert 'name="unit_price"' in body
    price_box = body[body.index('name="unit_price"') - 120:body.index('name="unit_price"') + 40]
    assert 'step="any"' in price_box
    assert 'step="0.01"' not in price_box


# -- the discount has to survive all the way to the sale -------------------

def test_a_delivery_records_the_discounted_sale(
        shop, main_branch, buyer, invoice, stocked, owner):
    """
    The invoice charges 153,900; the sale the till records must say the
    same. Posted at the full 180,000 it would put 26,100 into the day's
    takings that nobody paid, and declare VAT on it.
    """
    from apps.selling import services
    from apps.selling.models import DeliveryNote

    with tenant_context(shop, branch=main_branch, user=owner):
        line = invoice.lines.get()
        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        invoice.refresh_from_db()
        assert invoice.total == Decimal("153900")

        services.issue(invoice)
        note = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate())
        note.lines.create(invoice_line=line, qty=line.qty)
        services.deliver(note, user=owner)

        note.refresh_from_db()
        assert note.sale is not None
        assert note.sale.total == Decimal("153900")
        # And the customer still owes exactly the invoice, charged once.
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("153900")


def test_a_customers_own_price_list_beats_the_default(shop, main_branch, buyer, stocked):
    """
    A hotel on a wholesale list must be quoted the wholesale price. Sorted
    the wrong way round, the default list overwrote theirs and they were
    quietly given the shelf price.
    """
    from apps.catalog.models import Price, PriceList
    from apps.selling import services

    with tenant_context(shop, branch=main_branch):
        variant = stocked["Mkate"]
        default = PriceList.objects.get(is_default=True)
        Price.objects.update_or_create(
            variant=variant, price_list=default, min_qty=1,
            defaults={"amount": Decimal("1500")})

        wholesale = PriceList.objects.create(name="Wholesale", is_default=False)
        Price.objects.create(variant=variant, price_list=wholesale, min_qty=1,
                             amount=Decimal("1200"))
        buyer.price_list = wholesale
        buyer.save(update_fields=["price_list"])

        assert services.prices_for(buyer)[variant.pk] == Decimal("1200")
        # Somebody with no list of their own still gets the shelf price.
        buyer.price_list = None
        buyer.save(update_fields=["price_list"])
        assert services.prices_for(buyer)[variant.pk] == Decimal("1500")


def test_the_invoice_bills_the_discount_that_was_agreed(
        shop, main_branch, buyer, stocked, owner):
    """
    They accepted an offer with 10% off the line and 5% off the lot. An
    invoice at the full price charges more than was agreed, which is the
    argument a shop least wants to have.
    """
    from apps.selling import services

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        quotation = Quotation.objects.create(
            reference="QUO260009", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.ACCEPTED, discount_percent=Decimal("5"))
        quotation.lines.create(
            variant=stocked["Mkate"], description="Mkate Mkubwa",
            qty=Decimal("10"), unit_price=Decimal("18000"),
            tax_rate=Decimal("18"), discount_percent=Decimal("10"), position=10)
        assert quotation.total == Decimal("153900")

        invoice = services.invoice_from_quotation(quotation)
        assert invoice.discount_percent == Decimal("5")
        assert invoice.lines.get().discount_percent == Decimal("10")
        assert invoice.total == quotation.total == Decimal("153900")


def test_the_same_offer_again_is_the_same_money(shop, main_branch, buyer, stocked, owner):
    from apps.selling import services

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        quotation = Quotation.objects.create(
            reference="QUO260010", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.SENT, discount_percent=Decimal("5"))
        quotation.lines.create(
            variant=stocked["Mkate"], description="Mkate Mkubwa",
            qty=Decimal("10"), unit_price=Decimal("18000"),
            tax_rate=Decimal("18"), discount_percent=Decimal("10"), position=10)

        copy = services.copy_for_new(quotation)
        assert copy.total == quotation.total == Decimal("153900")


def test_the_discount_cannot_move_once_the_invoice_is_out(
        client, shop, main_branch, buyer, invoice, owner):
    """
    The debt was written the day it was issued. A discount changed after
    that would leave the invoice saying one figure and the customer's
    account another.
    """
    from apps.selling import services

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        buyer.refresh_from_db()
        owed = buyer.balance

    client.force_login(owner)
    client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {
        "action": "header", "due_on": invoice.due_on.isoformat(),
        "discount_percent": "50", "note": "",
    }, follow=True)

    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        buyer.refresh_from_db()
        assert invoice.discount_percent == Decimal("0")
        assert invoice.total == Decimal("180000")
        assert buyer.balance == owed


def test_two_trips_add_up_to_the_one_invoice(
        shop, main_branch, buyer, invoice, stocked, owner):
    """Half today, half on Friday: the two sales together are the invoice."""
    from apps.selling import services
    from apps.selling.models import DeliveryNote

    with tenant_context(shop, branch=main_branch, user=owner):
        line = invoice.lines.get()
        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        invoice.refresh_from_db()
        services.issue(invoice)

        totals = []
        for ref, qty in (("DN260001", Decimal("3")), ("DN260002", Decimal("7"))):
            note = DeliveryNote.objects.create(
                reference=ref, invoice=invoice, branch=main_branch,
                delivered_on=timezone.localdate())
            note.lines.create(invoice_line=line, qty=qty)
            services.deliver(note, user=owner)
            note.refresh_from_db()
            totals.append(note.sale.total)

        assert sum(totals) == invoice.total == Decimal("153900")


def test_money_on_forms_steps_in_whole_shillings_too(db):
    """
    Django sets step="0.01" itself on any two-place decimal, so the boxes
    drawn by a form moved in cents even after the hand-written ones were
    fixed. A rate is a percentage and keeps its decimals.
    """
    from apps.catalog.forms import ProductForm, TaxRateForm

    # A price keeps its cents and is never stepped in them.
    assert 'step="any"' in str(ProductForm()["min_price"])
    assert 'step="0.01"' not in str(ProductForm()["min_price"])
    # 7.5% VAT is a real rate.
    assert 'step="0.01"' in str(TaxRateForm()["rate"])


def test_the_vat_on_a_discounted_delivery_is_declared_on_what_was_paid(
        shop, main_branch, buyer, invoice, stocked, owner):
    """
    `Sale.tax_total` is what the VAT figures and the profit report are built
    from. Left at the undiscounted amount it would declare tax on money the
    customer never handed over, and nothing else in the suite looks at it.

    153,900 at 18% inclusive carries 23,476.27 of VAT.
    """
    from apps.selling import services
    from apps.selling.models import DeliveryNote

    with tenant_context(shop, branch=main_branch, user=owner):
        line = invoice.lines.get()
        line.discount_percent = Decimal("10")
        line.save(update_fields=["discount_percent"])
        invoice.discount_percent = Decimal("5")
        invoice.save(update_fields=["discount_percent"])
        invoice.refresh_from_db()
        services.issue(invoice)

        note = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate())
        note.lines.create(invoice_line=line, qty=line.qty)
        services.deliver(note, user=owner)
        note.refresh_from_db()

        sale = note.sale
        assert sale.total == Decimal("153900")
        assert sale.tax_total == Decimal("23476.27")
        # And the sale says plainly what was taken off.
        assert sale.discount_total == Decimal("26100")

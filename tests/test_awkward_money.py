"""
Money that does not divide evenly.

Every sale I have checked so far used round numbers. Shops do not: 1.5 kg of
something at 3,333 a kilo, VAT inside the shelf price, and then half of it
brought back. This is where a cent goes missing and nobody notices for a
year.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.context import tenant_context
from apps.reports import services as figures

pytestmark = pytest.mark.django_db

CENT = Decimal("0.01")


@pytest.fixture
def odd_priced(shop, main_branch):
    """Products priced so that nothing divides evenly."""
    from apps.catalog.models import Price, PriceList, Product, TaxRate, Unit
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement

    items = {}
    with tenant_context(shop, branch=main_branch):
        vat = TaxRate.objects.get(is_default=True)
        retail = PriceList.objects.get(is_default=True)
        for name, unit, price, cost in [
            ("Unga (kg)", "kg", Decimal("3333.33"), Decimal("2777.77")),
            ("Sukari (kg)", "kg", Decimal("1249.99"), Decimal("999.99")),
            ("Mafuta (l)", "l", Decimal("7777.77"), Decimal("6666.66")),
        ]:
            product = Product.objects.create(
                name=name, base_unit=Unit.objects.get(code=unit), tax_rate=vat)
            variant = product.default_variant
            Price.objects.create(price_list=retail, variant=variant, amount=price)
            record_movement(variant=variant, qty_delta=Decimal("100"),
                            reason=MovementReason.PURCHASE, unit_cost=cost)
            items[name] = variant
    return items


def _sell(shop, branch, who, lines):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=branch, user=who):
        cart = new_cart(branch=branch)
        for variant, qty in lines:
            add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}])


def test_a_sale_of_fractions_still_adds_up(shop, main_branch, odd_priced, owner):
    sale = _sell(shop, main_branch, owner, [
        (odd_priced["Unga (kg)"], Decimal("1.5")),
        (odd_priced["Sukari (kg)"], Decimal("0.25")),
        (odd_priced["Mafuta (l)"], Decimal("0.75")),
    ])
    with tenant_context(shop, branch=main_branch):
        lines = list(sale.lines.all())

    # Every payable amount is a whole shilling -- the smallest thing the
    # drawer holds -- and the lines add up to the total exactly.
    for line in lines:
        assert line.line_total == line.line_total.to_integral_value(), line.line_total
    assert sum((line.line_total for line in lines), Decimal("0")) == sale.subtotal
    assert sale.subtotal - sale.discount_total == sale.total
    assert sale.total == sale.total.to_integral_value()

    # VAT is worked out of those prices and kept to the cent: it is declared,
    # not handed over.
    rate = Decimal("18")
    expected_tax = sum(
        (line.line_total * rate / (100 + rate) for line in lines), Decimal("0"))
    assert abs(sale.tax_total - expected_tax) <= Decimal("0.05"), (
        f"VAT drifted: {sale.tax_total} vs {expected_tax}")


def test_every_line_is_paid_for_exactly(shop, main_branch, odd_priced, owner):
    sale = _sell(shop, main_branch, owner, [(odd_priced["Unga (kg)"], Decimal("1.5"))])
    with tenant_context(shop, branch=main_branch):
        paid = sum((p.amount for p in sale.payments.all()), Decimal("0"))
    assert paid == sale.total


def test_half_a_line_brought_back_is_half_the_money(shop, main_branch, odd_priced, owner):
    from apps.pos.services import create_return

    sale = _sell(shop, main_branch, owner, [(odd_priced["Unga (kg)"], Decimal("1.5"))])
    with tenant_context(shop, branch=main_branch, user=owner):
        line = sale.lines.first()
        doc = create_return(sale, {line.pk: Decimal("0.75")}, reason="half back")

    # Half of the line, to the nearest shilling the drawer can give back.
    assert abs(doc.total - (line.line_total / 2)) <= Decimal("0.5"), (
        f"{doc.total} vs {line.line_total / 2}")
    assert doc.total == doc.total.to_integral_value(), doc.total


def test_the_figures_do_not_drift_on_fractions(shop, main_branch, odd_priced, owner):
    from apps.pos.services import create_return

    sale = _sell(shop, main_branch, owner, [
        (odd_priced["Unga (kg)"], Decimal("1.5")),
        (odd_priced["Mafuta (l)"], Decimal("0.75")),
    ])
    with tenant_context(shop, branch=main_branch, user=owner):
        line = sale.lines.first()
        back = create_return(sale, {line.pk: Decimal("0.5")}, reason="some back")

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        took = figures.takings([main_branch], today, today)
        profit = figures.gross_profit([main_branch], today, today)

    assert took["net"] == sale.total - back.total
    # Profit is revenue less cost on what stayed sold; both sides must be
    # scaled by the same fraction, or the margin quietly wanders.
    assert profit["revenue"] > 0 and profit["cost"] > 0
    assert profit["profit"] == profit["revenue"] - profit["cost"]


def test_a_receipt_shows_what_was_actually_charged(client, shop, main_branch,
                                                   odd_priced, owner):
    from django.urls import reverse

    sale = _sell(shop, main_branch, owner, [(odd_priced["Sukari (kg)"], Decimal("0.25"))])
    client.force_login(owner)
    body = client.get(reverse("pos:receipt", args=[sale.pk])).content.decode()
    # What is printed is what was charged and what the drawer holds. The
    # receipt used to say 313 for a sale the system recorded as 312.50.
    assert f"{sale.total:.0f}" in body.replace(",", ""), body[:400]
    assert sale.total == sale.total.to_integral_value()

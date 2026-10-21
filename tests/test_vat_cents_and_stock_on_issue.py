"""
What a hotel supplier's client found on a proforma:

- 10 at 3.65 printed as 37.00: shilling paperwork was rounded to the coin.
- Prices were taken to include VAT, and this shop quotes before VAT.
- Issuing an invoice did not take the goods out of stock, and the Sales
  report stayed empty because only a delivery note recorded the sale.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services
from apps.selling.models import Invoice, InvoiceStatus

pytestmark = pytest.mark.django_db


def _settings(shop, **values):
    from apps.org.models import TenantSettings

    with tenant_context(shop):
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)
        for key, value in values.items():
            setattr(row, key, value)
        row.save()


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Nasir A Hassan")


def _invoice(shop, branch, buyer, lines, reference="PRO260001"):
    today = timezone.localdate()
    with tenant_context(shop, branch=branch):
        inv = Invoice.objects.create(reference=reference, customer=buyer, branch=branch,
                                     issued_on=today, due_on=today + timedelta(days=14))
        for i, (variant, qty, price, extra) in enumerate(lines):
            inv.lines.create(variant=variant, description=str(variant or "Typed line"),
                             qty=Decimal(qty), unit_price=Decimal(price),
                             tax_rate=Decimal("18"), position=i, **extra)
        return Invoice.objects.get(pk=inv.pk)


# -- cents -------------------------------------------------------------------

def test_shilling_paperwork_keeps_its_cents(shop, main_branch, buyer, stocked):
    inv = _invoice(shop, main_branch, buyer, [
        (stocked["Mkate"], "10", "3.65", {}),
        (stocked["Mkate"], "2", "12.18", {}),
        (stocked["Mkate"], "1", "5.00", {"discount_percent": Decimal("1")}),
    ])
    with tenant_context(shop):
        totals = [line.line_total for line in inv.lines.all()]
        assert totals == [Decimal("36.50"), Decimal("24.36"), Decimal("4.95")]
        assert inv.total == Decimal("65.81")
        assert inv.money_places == 2


# -- VAT on top --------------------------------------------------------------

def test_vat_added_on_top_when_the_shop_quotes_before_vat(shop, main_branch, buyer, stocked):
    _settings(shop, prices_include_tax=False)
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "1", "100", {})])
    with tenant_context(shop):
        assert inv.adds_vat
        assert inv.tax == Decimal("18.00")
        assert inv.total == Decimal("118.00")
        assert inv.net_of_tax == Decimal("100.00")


def test_vat_inside_the_price_stays_the_default(shop, main_branch, buyer, stocked):
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "1", "118", {})])
    with tenant_context(shop):
        assert not inv.adds_vat
        assert inv.tax == Decimal("18.00") and inv.total == Decimal("118.00")


def test_switching_the_setting_does_not_rewrite_paperwork(shop, main_branch, buyer, stocked):
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "1", "118", {})])
    _settings(shop, prices_include_tax=False)
    with tenant_context(shop):
        inv = Invoice.objects.get(pk=inv.pk)
        inv.save()
        assert not inv.adds_vat and inv.total == Decimal("118.00")


def test_the_printout_says_how_vat_is_charged(client, shop, owner, main_branch, buyer, stocked):
    _settings(shop, prices_include_tax=False)
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "1", "100", {})])
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_print", args=[inv.pk])).content.decode()
    assert "VAT is added on top of the prices." in html
    assert "Prices include VAT." not in html


def test_the_settings_page_offers_both_switches(client, shop, owner):
    client.force_login(owner)
    html = client.get(reverse("org:business")).content.decode()
    assert 'name="prices_include_tax"' in html and 'name="stock_out_on_invoice"' in html


# -- stock and the sale follow the invoice -----------------------------------

def _on_hand(shop, branch, variant):
    from apps.inventory.services import get_stock_item

    with tenant_context(shop, branch=branch):
        return get_stock_item(variant, branch).qty_on_hand


def test_issuing_takes_the_goods_out_and_records_the_sale(client, shop, owner, main_branch,
                                                         buyer, stocked):
    before = _on_hand(shop, main_branch, stocked["Mkate"])
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "4", "1500", {}),
                                              (None, "1", "2000", {})])
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.OPEN
        assert inv.deliveries.count() == 1
    assert _on_hand(shop, main_branch, stocked["Mkate"]) == before - 4

    client.force_login(owner)
    totals = client.get(reverse("reports:index")).context["totals"]
    assert totals["count"] == 1 and totals["net"] == Decimal("8000")
    # The debt is made once, by the invoice; the sale settles nothing twice.
    with tenant_context(shop):
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("8000")


def test_with_vat_on_top_the_sale_is_what_the_customer_pays(client, shop, owner, main_branch,
                                                           buyer, stocked):
    _settings(shop, prices_include_tax=False)
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "2", "100", {})])
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        sale = inv.deliveries.get().sale
        assert sale.total == Decimal("236")
        assert buyer.balance == Decimal("236.00")


def test_a_proforma_never_touches_stock(shop, main_branch, buyer, stocked):
    before = _on_hand(shop, main_branch, stocked["Mkate"])
    _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "4", "1500", {})])
    assert _on_hand(shop, main_branch, stocked["Mkate"]) == before


def test_cancelling_the_invoice_puts_the_goods_back(shop, main_branch, owner, buyer, stocked):
    before = _on_hand(shop, main_branch, stocked["Mkate"])
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "4", "1500", {})])
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        services.void_invoice(Invoice.objects.get(pk=inv.pk), user=owner)
        buyer.refresh_from_db()
        assert buyer.balance == 0
    assert _on_hand(shop, main_branch, stocked["Mkate"]) == before


def test_short_of_stock_refuses_the_issue_when_the_shop_says_so(shop, main_branch, owner,
                                                               buyer, stocked):
    _settings(shop, negative_stock_allowed=False)
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "100000", "1500", {})])
    with tenant_context(shop, branch=main_branch, user=owner):
        with pytest.raises(ValueError):
            services.issue(inv)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.DRAFT
        buyer.refresh_from_db()
        assert buyer.balance == 0


def test_switched_off_the_goods_wait_for_a_delivery_note(shop, main_branch, owner, buyer,
                                                        stocked):
    _settings(shop, stock_out_on_invoice=False)
    before = _on_hand(shop, main_branch, stocked["Mkate"])
    inv = _invoice(shop, main_branch, buyer, [(stocked["Mkate"], "4", "1500", {})])
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        assert inv.deliveries.count() == 0
    assert _on_hand(shop, main_branch, stocked["Mkate"]) == before


def test_an_invoice_from_a_quotation_charges_vat_as_quoted(shop, main_branch, buyer, stocked):
    from apps.selling.models import Quotation

    _settings(shop, prices_include_tax=False)
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        q = Quotation.objects.create(reference="QUO260001", customer=buyer, branch=main_branch,
                                     issued_on=today, valid_until=today + timedelta(days=7))
        q.lines.create(variant=stocked["Mkate"], description="Mkate", qty=Decimal("1"),
                       unit_price=Decimal("100"), tax_rate=Decimal("18"), position=1)
    _settings(shop, prices_include_tax=True)     # changed after the quote went out
    with tenant_context(shop, branch=main_branch):
        q = Quotation.objects.get(pk=q.pk)
        q.status = "accepted"
        q.save()
        inv = services.invoice_from_quotation(q)
        assert inv.adds_vat and inv.total == Decimal("118.00")

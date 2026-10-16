"""
The owner's say over prices and discounts on quotations and invoices.

Two settings under Settings > Business: lock product prices so staff cannot
type over them, and cap how much a document may take off -- line and whole
document together, because 40% and 40% is 64% off.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini")


@pytest.fixture
def rules(shop):
    from apps.org.models import TenantSettings

    with tenant_context(shop):
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)
        return row


@pytest.fixture
def quotation(shop, main_branch, buyer):
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        return Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14))


@pytest.fixture
def proforma(shop, main_branch, buyer):
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        return Invoice.objects.create(
            reference="PRO260001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))


def _add(client, doc, url, **data):
    return client.post(reverse(url, args=[doc.pk]), {"action": "add_line", "qty": "1", **data},
                       follow=True)


# -- prices ------------------------------------------------------------------

def test_unlocked_a_product_price_can_be_typed_over(
        client, shop, main_branch, owner, quotation, stocked):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail",
         variant=stocked["Mkate"].pk, unit_price="999")
    with tenant_context(shop, branch=main_branch):
        assert quotation.lines.get().unit_price == Decimal("999")


@pytest.mark.parametrize("url,doc_fixture", [("selling:quotation_detail", "quotation"),
                                             ("selling:invoice_detail", "proforma")])
def test_locked_a_product_goes_in_at_its_list_price(
        client, request, shop, main_branch, owner, rules, stocked, url, doc_fixture):
    doc = request.getfixturevalue(doc_fixture)
    rules.lock_document_prices = True
    rules.save()
    client.force_login(owner)
    # Somebody edits the read-only box in the browser and posts 1.
    _add(client, doc, url, variant=stocked["Mkate"].pk, unit_price="1")
    with tenant_context(shop, branch=main_branch):
        assert doc.lines.get().unit_price == Decimal("1500")


def test_locked_a_typed_line_is_still_priced_by_hand(
        client, shop, main_branch, owner, rules, quotation):
    rules.lock_document_prices = True
    rules.save()
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail",
         description="Delivery to Nungwi", unit_price="25000")
    with tenant_context(shop, branch=main_branch):
        assert quotation.lines.get().unit_price == Decimal("25000")


def test_locked_a_product_with_no_price_is_refused(
        client, shop, main_branch, owner, rules, quotation):
    from apps.catalog.models import Product, TaxRate, Unit

    rules.lock_document_prices = True
    rules.save()
    from apps.catalog.models import Variant

    with tenant_context(shop, branch=main_branch):
        product = Product.objects.create(name="Mincer", base_unit=Unit.objects.get(code="pc"),
                                         tax_rate=TaxRate.objects.get(is_default=True))
        variant = product.default_variant or Variant.objects.create(
            product=product, is_default=True)
    client.force_login(owner)
    page = _add(client, quotation, "selling:quotation_detail",
                variant=variant.pk, unit_price="2170.60")
    assert "has no price yet" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not quotation.lines.exists()


def test_the_page_tells_the_browser_prices_are_locked(
        client, shop, owner, rules, quotation):
    rules.lock_document_prices = True
    rules.save()
    client.force_login(owner)
    page = client.get(reverse("selling:quotation_detail", args=[quotation.pk])).content.decode()
    assert 'x-data="lineEntry(true)"' in page
    assert ':readonly="fixed"' in page


# -- discounts ---------------------------------------------------------------

def test_combined_discount_compounds():
    assert services.combined_discount(40, 40) == Decimal("64.00")
    assert services.combined_discount(0, 25) == Decimal("25.00")


@pytest.mark.parametrize("url,doc_fixture", [("selling:quotation_detail", "quotation"),
                                             ("selling:invoice_detail", "proforma")])
def test_a_line_discount_over_the_limit_is_refused(
        client, request, shop, main_branch, owner, rules, url, doc_fixture):
    doc = request.getfixturevalue(doc_fixture)
    rules.max_document_discount = Decimal("20")
    rules.save()
    client.force_login(owner)
    page = _add(client, doc, url, description="Mincer", unit_price="2170.60",
                discount_percent="80")
    assert "The most this shop allows off is 20%" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not doc.lines.exists()


def test_within_the_limit_it_is_taken(client, shop, main_branch, owner, rules, quotation):
    rules.max_document_discount = Decimal("20")
    rules.save()
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", description="Mincer",
         unit_price="2170.60", discount_percent="20")
    with tenant_context(shop, branch=main_branch):
        assert quotation.lines.get().discount_percent == Decimal("20")


def test_line_and_document_discounts_count_together(
        client, shop, main_branch, owner, rules, quotation):
    rules.max_document_discount = Decimal("30")
    rules.save()
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", description="Mincer",
         unit_price="1000", discount_percent="20")
    # 20% on the line and 20% on the whole is 36% off: over 30.
    page = client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "header", "discount_percent": "20",
        "valid_until": (timezone.localdate() + timedelta(days=14)).isoformat(),
    }, follow=True)
    assert "36% off" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        quotation.refresh_from_db()
        assert quotation.discount_percent == Decimal("0")


def test_no_limit_by_default(client, shop, main_branch, owner, quotation):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", description="Mincer",
         unit_price="1000", discount_percent="80")
    with tenant_context(shop, branch=main_branch):
        assert quotation.lines.get().discount_percent == Decimal("80")


# -- where the owner sets it -------------------------------------------------

def test_the_settings_are_on_the_business_page(client, shop, owner, rules):
    client.force_login(owner)
    page = client.get(reverse("org:business")).content.decode()
    assert "Lock product prices on quotations and invoices" in page
    assert "Largest discount on a quotation or invoice" in page
    assert "Show product pictures on quotations and invoices" in page


def test_saving_them(shop, rules):
    from django.forms.models import model_to_dict

    from apps.org.forms import BusinessForm

    with tenant_context(shop):
        data = {k: v for k, v in model_to_dict(rules).items() if v not in (None, False)}
        data.update({"lock_document_prices": "on", "max_document_discount": "15"})
        form = BusinessForm(data, instance=rules)
        assert form.is_valid(), form.errors
        form.save()
        rules.refresh_from_db()
        assert rules.lock_document_prices is True
        assert rules.max_document_discount == Decimal("15")
        # And the limit cannot be set outside 0-100.
        data["max_document_discount"] = "120"
        assert not BusinessForm(data, instance=rules).is_valid()


def test_the_button_says_proforma(client, shop, owner):
    client.force_login(owner)
    page = client.get(reverse("selling:invoice_list")).content.decode()
    assert "New proforma" in page
    assert "Issue it" in page


# -- documents written before the limit --------------------------------------

def _old_quotation(shop, main_branch, buyer, stocked, status, off="80"):
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        q = Quotation.objects.create(
            reference="QUO260009", customer=buyer, branch=main_branch, status=status,
            issued_on=today, valid_until=today + timedelta(days=14))
        q.lines.create(variant=stocked["Mkate"], description="Mkate", qty=Decimal("10"),
                       unit_price=Decimal("999"), discount_percent=Decimal(off), position=10)
        return q


@pytest.mark.parametrize("step", ["send", "copy", "invoice"])
def test_an_old_discount_cannot_slip_past_a_new_limit(
        shop, main_branch, owner, buyer, stocked, rules, step):
    from apps.selling.models import QuotationStatus

    status = {"send": QuotationStatus.DRAFT, "copy": QuotationStatus.SENT,
              "invoice": QuotationStatus.ACCEPTED}[step]
    q = _old_quotation(shop, main_branch, buyer, stocked, status)
    rules.max_document_discount = Decimal("20")
    rules.save()
    action = {"send": services.send, "copy": services.copy_for_new,
              "invoice": services.invoice_from_quotation}[step]
    with tenant_context(shop, branch=main_branch, user=owner), \
            pytest.raises(ValueError, match="allows off is 20%"):
        action(q)


def test_an_old_proforma_cannot_be_issued_over_the_limit(
        shop, main_branch, owner, rules, proforma):
    with tenant_context(shop, branch=main_branch, user=owner):
        proforma.lines.create(description="Mincer", qty=Decimal("1"),
                              unit_price=Decimal("2170.60"),
                              discount_percent=Decimal("80"), position=10)
        rules.max_document_discount = Decimal("20")
        rules.save()
        with pytest.raises(ValueError, match="allows off is 20%"):
            services.issue(proforma)
        proforma.refresh_from_db()
        assert proforma.status == "draft"


def test_with_prices_locked_a_copy_takes_todays_price(
        shop, main_branch, owner, buyer, stocked, rules):
    from apps.selling.models import QuotationStatus

    q = _old_quotation(shop, main_branch, buyer, stocked, QuotationStatus.SENT, off="0")
    rules.lock_document_prices = True
    rules.save()
    with tenant_context(shop, branch=main_branch, user=owner):
        fresh = services.copy_for_new(q)
        assert fresh.lines.get().unit_price == Decimal("1500")

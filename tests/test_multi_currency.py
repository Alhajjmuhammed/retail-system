"""
Quotations and invoices in another currency, with the books in the shop's own.

A hotel is quoted and billed in dollars at the rate kept under Settings. The
document keeps that rate frozen; its total is shown in shillings beside it;
and everything that reaches the books -- the customer's account, the sale,
stock -- is in shillings at that same rate.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _goods_leave_on_a_delivery_note(shop):
    """These tests are about delivery notes, so issuing must not send the goods."""
    from apps.org.models import TenantSettings

    with tenant_context(shop):
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)
        row.stock_out_on_invoice = False
        row.save()

RATE = Decimal("2500")   # 1 USD = 2,500 TZS: easy arithmetic.


@pytest.fixture
def usd(shop):
    from apps.org.models import ExchangeRate

    with tenant_context(shop):
        return ExchangeRate.objects.create(code="USD", rate=RATE)


@pytest.fixture
def hotel(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini")


def _new(client, url_name, hotel, currency="USD", **extra):
    client.post(reverse(url_name), {"customer": hotel.pk, "currency": currency, **extra})


def _usd_invoice(shop, main_branch, hotel, stocked, qty="10", price="2.40"):
    """10 loaves at USD 2.40 = USD 24.00 = TZS 60,000."""
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        inv = Invoice.objects.create(
            reference="PRO260001", customer=hotel, branch=main_branch, issued_on=today,
            due_on=today + timedelta(days=14), currency="USD", exchange_rate=RATE)
        inv.lines.create(variant=stocked["Mkate"], description="Mkate", qty=Decimal(qty),
                         unit_price=Decimal(price), tax_rate=Decimal("18"), position=10)
        return inv


# -- settings ----------------------------------------------------------------

def test_the_owner_adds_updates_and_removes_a_currency(client, shop, owner):
    from apps.org.models import ExchangeRate

    client.force_login(owner)
    url = reverse("org:currencies")
    client.post(url, {"action": "save_rate", "code": "USD", "rate": "2650"})
    with tenant_context(shop):
        assert ExchangeRate.objects.get(code="USD").rate == Decimal("2650")
    client.post(url, {"action": "save_rate", "code": "USD", "rate": "2700"})
    with tenant_context(shop):
        assert ExchangeRate.objects.get(code="USD").rate == Decimal("2700")
    page = client.get(url).content.decode()
    assert "2700" in page and "2.7E" not in page
    client.post(url, {"action": "remove", "code": "USD"})
    with tenant_context(shop):
        assert not ExchangeRate.objects.get(code="USD").is_active


def test_the_main_currency_can_change_until_money_moves(client, shop, owner, hotel, main_branch):
    client.force_login(owner)
    url = reverse("org:currencies")
    client.post(url, {"action": "main", "code": "USD"})
    shop.refresh_from_db()
    assert shop.currency == "USD"
    client.post(url, {"action": "main", "code": "TZS"})
    shop.refresh_from_db()
    assert shop.currency == "TZS"
    # Once something is quoted, it is fixed.
    _new(client, "selling:quotation_create", hotel, currency="TZS")
    page = client.post(url, {"action": "main", "code": "USD"}, follow=True).content.decode()
    assert "fixed" in page
    shop.refresh_from_db()
    assert shop.currency == "TZS"


def test_a_currency_with_no_rate_cannot_be_chosen(client, shop, owner, hotel):
    from apps.selling.models import Quotation

    client.force_login(owner)
    page = client.post(reverse("selling:quotation_create"),
                       {"customer": hotel.pk, "currency": "EUR"}, follow=True)
    assert "no rate for EUR" in page.content.decode()
    with tenant_context(shop):
        assert not Quotation.objects.exists()


def test_a_shop_with_one_currency_is_not_asked(client, shop, owner):
    client.force_login(owner)
    html = client.get(reverse("selling:quotation_create")).content.decode()
    assert 'name="currency"' not in html


# -- writing in dollars ------------------------------------------------------

def test_a_quotation_in_dollars_freezes_the_rate_and_converts_prices(
        client, shop, main_branch, owner, usd, hotel, stocked):
    from apps.selling.models import Quotation

    client.force_login(owner)
    assert 'name="currency"' in client.get(reverse("selling:quotation_create")).content.decode()
    _new(client, "selling:quotation_create", hotel)
    with tenant_context(shop, branch=main_branch):
        q = Quotation.objects.get()
        assert q.currency == "USD" and q.exchange_rate == RATE
    html = client.get(reverse("selling:quotation_detail", args=[q.pk])).content.decode()
    # Mkate is TZS 1,500: USD 0.60 at 2,500.
    assert 'data-price="0.6"' in html or 'data-price="0.60"' in html
    # The rate changes tomorrow; the quotation keeps its own.
    usd.rate = Decimal("3000")
    usd.save()
    with tenant_context(shop):
        q.refresh_from_db()
        assert q.exchange_rate == RATE


def test_dollar_amounts_keep_their_cents_and_show_shillings_beside(
        client, shop, main_branch, owner, usd, hotel, stocked):
    inv = _usd_invoice(shop, main_branch, hotel, stocked, qty="3", price="2.45")
    with tenant_context(shop, branch=main_branch):
        assert inv.total == Decimal("7.35")           # not rounded to a dollar
        assert inv.total_main == Decimal("18375")      # 7.35 x 2,500
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_detail", args=[inv.pk])).content.decode()
    assert "7.35" in html
    assert "TZS 18,375" in html
    assert "1 USD = 2,500 TZS" in html


def test_a_locked_price_goes_in_converted(client, shop, main_branch, owner, usd, hotel, stocked):
    from apps.org.models import TenantSettings

    with tenant_context(shop):
        row, _ = TenantSettings.objects.get_or_create(tenant=shop)
        row.lock_document_prices = True
        row.save()
    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    client.force_login(owner)
    client.post(reverse("selling:invoice_detail", args=[inv.pk]), {
        "action": "add_line", "variant": stocked["Sukari 1kg"].pk, "qty": "1",
        "unit_price": "999"})
    with tenant_context(shop):
        # Sukari is TZS 3,000: USD 1.20.
        assert inv.lines.get(variant=stocked["Sukari 1kg"]).unit_price == Decimal("1.20")


def test_the_floor_holds_at_the_documents_rate(client, shop, main_branch, owner, usd, hotel,
                                               stocked):
    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.min_price = Decimal("1400")      # = USD 0.56
        product.save()
    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    client.force_login(owner)
    page = client.post(reverse("selling:invoice_detail", args=[inv.pk]), {
        "action": "add_line", "variant": stocked["Mkate"].pk, "qty": "1",
        "unit_price": "0.40", "description": "Mkate cheap"}, follow=True)
    assert "never sold below" in page.content.decode()


# -- into the books ----------------------------------------------------------

def test_issuing_puts_the_shilling_equivalent_on_the_account(shop, main_branch, owner, usd,
                                                            hotel, stocked):
    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        inv.refresh_from_db()
        assert hotel.balance == Decimal("60000")
        assert inv.balance == Decimal("24.00")
        charge = inv.credit_entries.get(kind="charge")
        assert charge.doc_amount == Decimal("24.00")


def test_paying_in_dollars_in_parts_clears_exactly(shop, main_branch, owner, usd, hotel, stocked):
    inv = _usd_invoice(shop, main_branch, hotel, stocked, qty="3", price="3.33")  # 9.99
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        services.take_payment(inv, amount=Decimal("3.33"), method="bank")
        inv.refresh_from_db()
        assert inv.status == "part_paid"
        assert inv.balance == Decimal("6.66")
        services.take_payment(inv, amount=Decimal("6.66"), method="bank")
        inv.refresh_from_db()
        assert inv.status == "paid"
        assert inv.balance == 0
        # Not a shilling left behind on the account by rounding.
        assert hotel.balance == 0


def test_dollar_cash_does_not_go_in_the_shilling_drawer(shop, main_branch, register, owner, usd,
                                                        hotel, stocked):
    from apps.pos.services import open_shift

    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        shift = open_shift(register=register, user=owner, opening_float=10000)
        moved = services.take_payment(inv, amount=Decimal("24.00"), method="cash", shift=shift)
        assert moved is None
        assert shift.compute_expected_cash() == Decimal("10000")


def test_a_shilling_payment_on_the_account_against_a_dollar_invoice(
        client, shop, main_branch, owner, usd, hotel, stocked):
    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
    client.force_login(owner)
    client.post(reverse("customers:customer_payment", args=[hotel.pk]), {
        "amount": "25000", "method": "bank", "invoice": inv.pk})
    with tenant_context(shop):
        inv.refresh_from_db()
        assert inv.paid == Decimal("10.00")       # 25,000 / 2,500
        assert inv.status == "part_paid"
        payment = inv.credit_entries.get(kind="payment")
    client.post(reverse("customers:customer_payment_reverse", args=[payment.pk]))
    with tenant_context(shop):
        inv.refresh_from_db()
        assert inv.paid == 0
        assert inv.status == "open"


def test_cancelling_takes_back_exactly_what_was_charged(shop, main_branch, owner, usd, hotel,
                                                        stocked):
    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        usd.rate = Decimal("3000")      # the rate moves; the charge does not
        usd.save()
        services.void_invoice(inv, user=owner)
        assert hotel.balance == 0


def test_the_delivery_sale_is_in_shillings(shop, main_branch, owner, usd, hotel, stocked):
    from apps.inventory.services import quantity_of
    from apps.selling.models import DeliveryNote

    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(inv)
        before = quantity_of(stocked["Mkate"], main_branch)
        note = DeliveryNote.objects.create(reference="DN260001", invoice=inv, branch=main_branch,
                                           delivered_on=timezone.localdate())
        note.lines.create(invoice_line=inv.lines.get(), qty=Decimal("10"))
        services.deliver(note, user=owner)
        note.refresh_from_db()
        assert note.sale.total == Decimal("60000")
        assert quantity_of(stocked["Mkate"], main_branch) == before - 10
        assert hotel.balance == Decimal("60000")    # charged once, by the invoice


def test_an_accepted_dollar_quotation_bills_in_dollars(shop, main_branch, owner, usd, hotel,
                                                      stocked):
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        q = Quotation.objects.create(
            reference="QUO260001", customer=hotel, branch=main_branch, status="accepted",
            issued_on=today, valid_until=today + timedelta(days=14),
            currency="USD", exchange_rate=RATE)
        q.lines.create(description="Mincer", qty=Decimal("2"), unit_price=Decimal("82"),
                       position=10)
        usd.rate = Decimal("3000")
        usd.save()
        inv = services.invoice_from_quotation(q)
        assert inv.currency == "USD"
        assert inv.exchange_rate == RATE          # the rate the customer accepted
        assert inv.total == Decimal("164.00")


def test_the_printed_invoice_says_its_currency_and_the_equivalent(
        client, shop, main_branch, owner, usd, hotel, stocked):
    from apps.org.models import BankAccount

    inv = _usd_invoice(shop, main_branch, hotel, stocked)
    with tenant_context(shop):
        BankAccount.objects.create(bank_name="CRDB", account_name="Duka", account_number="TZS-1",
                                   currency="TZS")
        BankAccount.objects.create(bank_name="NMB", account_name="Duka", account_number="USD-9",
                                   currency="USD")
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_print", args=[inv.pk])).content.decode()
    assert "USD 24.00" in html
    assert "Equivalent in TZS" in html
    assert "60,000.00" in html
    assert "USD-9" in html and "TZS-1" not in html     # the account that takes dollars
    assert "dollars" in html.lower()                   # the total in words


def test_shilling_documents_are_unchanged(client, shop, main_branch, owner, hotel, stocked):
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        inv = Invoice.objects.create(reference="PRO260001", customer=hotel, branch=main_branch,
                                     issued_on=today, due_on=today, currency="TZS")
        inv.lines.create(description="Mincer", qty=Decimal("2"), unit_price=Decimal("2170.60"),
                         position=10)
        assert inv.total == Decimal("4341.20")       # to the cent, like every document
        assert not inv.is_foreign
        services.issue(inv)
        assert hotel.balance == Decimal("4341.20")
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_detail", args=[inv.pk])).content.decode()
    assert inv.reference in html
    assert "1 TZS" not in html and "&asymp;" not in html


def test_one_shop_never_sees_another_shops_rates(shop, business_plan, usd):
    """The rates table is a shop's own, kept apart by row-level security too."""
    from django.db import connection

    from apps.accounts.models import User
    from apps.core.currency import rates
    from apps.org.models import ExchangeRate
    from apps.tenancy.services import create_tenant

    other_owner = User.objects.create_user("other@shop.test", "pw", name="Other")
    other, _ = create_tenant(name="Duka la Pili", owner=other_owner, plan=business_plan)
    with tenant_context(other):
        assert rates() == {}
        assert not ExchangeRate.objects.exists()
        # Below the application: the database itself hides the row.
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM org_exchangerate")
            assert cur.fetchone()[0] == 0
    with tenant_context(shop):
        assert rates() == {"USD": RATE}

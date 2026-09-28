"""
Quotations, invoices and delivery notes: the holes a read-through found.

Each test is one way the paperwork and the money used to disagree -- cash
from an invoice missing from the drawer, a limit nobody checked, goods back
in the store and still on the bill, a paid invoice still showing as owed.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Permission, Role, RolePermission, User
from apps.core.context import tenant_context
from apps.selling import services
from apps.selling.models import (
    DeliveryNote,
    Invoice,
    InvoiceStatus,
    Quotation,
    QuotationStatus,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini", phone="0788112233")


@pytest.fixture
def invoice(shop, main_branch, buyer, stocked):
    """40 loaves and a delivery charge: 85,000."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        inv = Invoice.objects.create(
            reference="INV260001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14),
        )
        inv.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                         qty=Decimal("40"), unit_price=Decimal("1500"),
                         tax_rate=Decimal("18"), position=10)
        inv.lines.create(description="Delivery to Nungwi", qty=Decimal("1"),
                         unit_price=Decimal("25000"), tax_rate=Decimal("18"),
                         position=20)
        return inv


def _deliver(invoice, qty_by_line, owner, reference="DN260001"):
    note = DeliveryNote.objects.create(
        reference=reference, invoice=invoice, branch=invoice.branch,
        delivered_on=timezone.localdate())
    for line, qty in qty_by_line:
        note.lines.create(invoice_line=line, qty=qty)
    services.deliver(note, user=owner)
    note.refresh_from_db()
    return note


def _biller(shop, limit):
    """Somebody who may invoice, up to `limit`."""
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Biller")
        for code, value in [("invoice.view", None), ("invoice.manage", limit),
                            ("invoice.payment", None)]:
            RolePermission.objects.create(
                role=role, permission=Permission.objects.get(code=code), limit_value=value)
        person = User.objects.create_user("biller@shop.test", "pw", name="Biller")
        Membership.objects.create(tenant=shop, user=person, role=role)
    return person


# -- cash from an invoice lands in the drawer --------------------------------

def test_cash_against_an_invoice_goes_in_the_open_drawer(
        client, shop, main_branch, register, owner, invoice):
    from apps.pos.services import open_shift

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        shift = open_shift(register=register, user=owner, opening_float=10000)

    client.force_login(owner)
    page = client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {
        "action": "payment", "amount": "30000", "method": "cash",
    }, follow=True)
    assert "Added to your POS drawer" in page.content.decode()

    with tenant_context(shop, branch=main_branch):
        shift.refresh_from_db()
        # The evening count expects the hotel's cash as well as the float.
        assert shift.compute_expected_cash() == Decimal("40000")
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PART_PAID


def test_mobile_money_against_an_invoice_stays_out_of_the_drawer(
        client, shop, main_branch, register, owner, invoice):
    from apps.pos.services import open_shift

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        shift = open_shift(register=register, user=owner, opening_float=10000)

    client.force_login(owner)
    client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {
        "action": "payment", "amount": "30000", "method": "mpesa",
    })
    with tenant_context(shop, branch=main_branch):
        assert shift.compute_expected_cash() == Decimal("10000")


def test_a_payment_cannot_be_more_than_is_owed(shop, main_branch, owner, invoice):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        with pytest.raises(ValueError, match="more than"):
            services.take_payment(invoice, amount=Decimal("85001"), method="bank")


# -- limits -----------------------------------------------------------------

def test_the_biggest_invoice_a_role_may_issue_is_enforced(
        client, shop, main_branch, invoice):
    person = _biller(shop, limit=50000)
    client.force_login(person)

    page = client.post(reverse("selling:invoice_detail", args=[invoice.pk]),
                       {"action": "issue"}, follow=True)
    assert "up to 50,000" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.DRAFT


def test_within_the_limit_it_issues(client, shop, main_branch, invoice):
    person = _biller(shop, limit=100000)
    client.force_login(person)
    client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {"action": "issue"})
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN


def test_a_customers_credit_limit_holds_for_invoices(shop, main_branch, owner, buyer, invoice):
    with tenant_context(shop, branch=main_branch, user=owner):
        buyer.credit_limit = Decimal("50000")
        buyer.save()
        with pytest.raises(ValueError, match="take them over"):
            services.issue(invoice)
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.DRAFT
        assert buyer.balance == Decimal("0")


def test_no_credit_limit_set_means_invoices_are_not_held(
        shop, main_branch, owner, buyer, invoice):
    with tenant_context(shop, branch=main_branch, user=owner):
        assert buyer.credit_limit == 0
        services.issue(invoice)
        assert buyer.balance == Decimal("85000")


# -- goods that come back ----------------------------------------------------

def test_a_cancelled_delivery_puts_the_goods_back_and_owes_them_again(
        shop, main_branch, owner, buyer, invoice, stocked):
    from apps.inventory.services import quantity_of
    from apps.pos.models import SaleStatus

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        before = quantity_of(stocked["Mkate"], main_branch)
        note = _deliver(invoice, [(bread, Decimal("40"))], owner)
        assert quantity_of(stocked["Mkate"], main_branch) == before - 40

        services.cancel_delivery(note, reason="Wrong bread", user=owner)
        note.refresh_from_db()
        bread.refresh_from_db()
        invoice.refresh_from_db()

        assert note.voided_at is not None
        assert note.sale.status == SaleStatus.VOIDED
        assert quantity_of(stocked["Mkate"], main_branch) == before
        assert bread.qty_outstanding == Decimal("40")
        assert invoice.can_deliver
        # The debt is the invoice's, and the invoice still stands.
        assert buyer.balance == Decimal("85000")

        # With nothing out and nothing paid, the invoice itself can now go.
        services.void_invoice(invoice, user=owner)
        assert buyer.balance == Decimal("0")


def test_the_till_will_not_void_or_refund_a_delivery_on_its_own(
        shop, main_branch, owner, invoice):
    from apps.pos.services import create_return, void_sale

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        note = _deliver(invoice, [(bread, Decimal("10"))], owner)

        with pytest.raises(ValueError, match="Cancel the delivery note"):
            void_sale(note.sale, reason="oops", user=owner)
        line = note.sale.lines.first()
        with pytest.raises(ValueError, match="Cancel the delivery note"):
            create_return(note.sale, {line.pk: Decimal("1")}, reason="back",
                          method="credit", user=owner)


def test_cancelling_a_delivery_from_the_invoice_page(
        client, shop, main_branch, owner, invoice):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        note = _deliver(invoice, [(bread, Decimal("10"))], owner)

    client.force_login(owner)
    client.post(reverse("selling:invoice_detail", args=[invoice.pk]),
                {"action": "cancel_delivery", "note": note.pk})
    with tenant_context(shop, branch=main_branch):
        note.refresh_from_db()
        assert note.voided_at is not None
        # A second tap finds it already cancelled and changes nothing.
        with pytest.raises(ValueError, match="already cancelled"):
            services.cancel_delivery(note, user=owner)


# -- one ledger, one answer --------------------------------------------------

def test_paying_the_account_against_an_invoice_marks_the_invoice(
        client, shop, main_branch, owner, buyer, invoice):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)

    client.force_login(owner)
    client.post(reverse("customers:customer_payment", args=[buyer.pk]), {
        "amount": "85000", "method": "bank", "invoice": invoice.pk,
    })
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.balance == Decimal("0")
        assert buyer.balance == Decimal("0")


def test_undoing_an_invoice_payment_shows_it_owed_again(
        client, shop, main_branch, owner, buyer, invoice):
    from apps.customers.models import CreditKind

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        services.take_payment(invoice, amount=Decimal("85000"), method="bank")
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        payment = invoice.credit_entries.get(kind=CreditKind.PAYMENT)

    client.force_login(owner)
    client.post(reverse("customers:customer_payment_reverse", args=[payment.pk]))
    with tenant_context(shop, branch=main_branch):
        invoice.refresh_from_db()
        assert invoice.paid == Decimal("0")
        assert invoice.status == InvoiceStatus.OPEN
        assert buyer.balance == Decimal("85000")


# -- deliveries --------------------------------------------------------------

def test_no_negative_stock_means_no_delivery_of_goods_not_there(
        client, shop, main_branch, owner, invoice, stocked):
    from apps.inventory.services import quantity_of
    from apps.org.models import TenantSettings

    with tenant_context(shop, branch=main_branch, user=owner):
        settings_row = TenantSettings.objects.first()
        settings_row.negative_stock_allowed = False
        settings_row.save()
        bread = invoice.lines.get(description="Mkate Mkubwa")
        bread.qty = Decimal("150")      # 100 in the store
        bread.save()
        services.issue(invoice)
        before = quantity_of(stocked["Mkate"], main_branch)

    client.force_login(owner)
    page = client.post(reverse("selling:delivery_create", args=[invoice.pk]), {
        f"qty:{bread.pk}": "150", "delivered_on": timezone.localdate().isoformat(),
    }, follow=True)
    assert "Not enough" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        # The whole trip went back: no note, no sale, no stock moved.
        assert not DeliveryNote.objects.exists()
        assert quantity_of(stocked["Mkate"], main_branch) == before


def test_the_sale_keeps_the_vat_on_the_invoice(shop, main_branch, owner, invoice, stocked):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        # The product's rate changes after the invoice was written.
        from apps.catalog.models import TaxRate

        zero = TaxRate.objects.create(name="Changed after invoicing", rate=Decimal("0"))
        product = stocked["Mkate"].product
        product.tax_rate = zero
        product.save()

        bread = invoice.lines.get(description="Mkate Mkubwa")
        note = _deliver(invoice, [(bread, Decimal("40"))], owner)
        line = note.sale.lines.get()
        assert line.tax_rate == Decimal("18")
        assert line.tax_amount == bread.tax_amount


def test_one_note_cannot_send_more_than_is_left(shop, main_branch, owner, invoice):
    """The service checks again under its lock, whatever the form said."""
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        _deliver(invoice, [(bread, Decimal("30"))], owner)
        with pytest.raises(ValueError, match="only 10"):
            _deliver(invoice, [(bread, Decimal("20"))], owner, reference="DN260002")


# -- quotations --------------------------------------------------------------

def _quotation(shop, main_branch, buyer, stocked, status):
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14), status=status)
        quotation.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                               qty=Decimal("10"), unit_price=Decimal("1500"), position=10)
        return quotation


@pytest.mark.parametrize("status", [QuotationStatus.EXPIRED, QuotationStatus.DECLINED,
                                    QuotationStatus.DRAFT])
def test_only_a_sent_quotation_can_be_answered(
        shop, main_branch, owner, buyer, stocked, status):
    quotation = _quotation(shop, main_branch, buyer, stocked, status)
    with (tenant_context(shop, branch=main_branch, user=owner),
          pytest.raises(ValueError, match="Only a quotation that has been sent")):
        services.decide(quotation, accepted=True, user=owner)


def test_only_an_accepted_quotation_becomes_an_invoice(
        shop, main_branch, owner, buyer, stocked):
    quotation = _quotation(shop, main_branch, buyer, stocked, QuotationStatus.SENT)
    with (tenant_context(shop, branch=main_branch, user=owner),
          pytest.raises(ValueError, match="Only an accepted quotation")):
        services.invoice_from_quotation(quotation)


def test_a_quotation_is_invoiced_once(shop, main_branch, owner, buyer, stocked):
    quotation = _quotation(shop, main_branch, buyer, stocked, QuotationStatus.ACCEPTED)
    with tenant_context(shop, branch=main_branch, user=owner):
        first = services.invoice_from_quotation(quotation)
        with pytest.raises(ValueError, match=f"already invoiced as {first.reference}"):
            services.invoice_from_quotation(quotation)
        # Once that invoice is cancelled, the offer can be billed again.
        services.void_invoice(first, user=owner)
        second = services.invoice_from_quotation(quotation)
        assert second.reference != first.reference

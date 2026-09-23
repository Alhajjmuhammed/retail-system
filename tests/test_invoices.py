"""
Invoices and delivery notes: a debt, then goods, in that order.

The whole design rests on one decision -- the invoice creates the debt, the
delivery moves the stock and lands the sale -- so most of this file is about
the seam between them. The failure that matters is charging a customer twice
for one delivery, and it is the first test here.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services
from apps.selling.models import DeliveryNote, Invoice, InvoiceStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(
            name="Hoteli ya Baharini", phone="0788112233", tin="123-456-789",
            address="Nungwi", credit_limit=Decimal("5000000"),
        )


@pytest.fixture
def invoice(shop, main_branch, buyer, stocked):
    """One invoice, two lines: 40 loaves and a delivery charge."""
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


def test_issuing_creates_the_debt_once(shop, main_branch, buyer, invoice):
    with tenant_context(shop, branch=main_branch):
        assert buyer.balance == Decimal("0")

        services.issue(invoice)
        buyer.refresh_from_db()
        invoice.refresh_from_db()

        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.total == Decimal("85000")        # 60,000 + 25,000
        assert buyer.balance == Decimal("85000")
        # Issuing twice must not double it: a page reloaded is not a second sale.
        services.issue(invoice)
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("85000")


def test_delivery_moves_the_stock_and_lands_the_sale(
        shop, main_branch, buyer, invoice, stocked, owner):
    """And does not charge the customer again -- the invoice already did."""
    from apps.inventory.services import quantity_of
    from apps.pos.models import Sale

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        before = quantity_of(stocked["Mkate"], main_branch)
        balance_after_invoice = buyer.balance

        note = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate(), received_by="Juma",
        )
        for line in invoice.lines.all():
            note.lines.create(invoice_line=line, qty=line.qty)
        services.deliver(note, user=owner)

        buyer.refresh_from_db()
        note.refresh_from_db()

        # Stock left the store.
        assert quantity_of(stocked["Mkate"], main_branch) == before - Decimal("40")
        # The sale is recorded, against this invoice, on today's date.
        assert note.sale is not None
        assert note.sale.invoice_id == invoice.pk
        assert note.sale.total == Decimal("85000")
        assert Sale.objects.filter(invoice=invoice).count() == 1
        # And the customer owes exactly what the invoice said. Once.
        assert buyer.balance == balance_after_invoice == Decimal("85000")


def test_a_lorry_that_goes_twice(shop, main_branch, buyer, invoice, stocked, owner):
    """Half today, half on Friday, against one invoice."""
    from apps.inventory.services import quantity_of

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        before = quantity_of(stocked["Mkate"], main_branch)

        first = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate())
        first.lines.create(invoice_line=bread, qty=Decimal("15"))
        services.deliver(first, user=owner)

        bread.refresh_from_db()
        assert bread.qty_delivered == Decimal("15")
        assert bread.qty_outstanding == Decimal("25")
        assert not invoice.delivered_everything
        assert quantity_of(stocked["Mkate"], main_branch) == before - Decimal("15")

        second = DeliveryNote.objects.create(
            reference="DN260002", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate())
        second.lines.create(invoice_line=bread, qty=Decimal("25"))
        services.deliver(second, user=owner)

        bread.refresh_from_db()
        assert bread.qty_outstanding == Decimal("0")
        assert quantity_of(stocked["Mkate"], main_branch) == before - Decimal("40")
        # Two deliveries, two sales, one debt.
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("85000")


def test_paying_in_pieces(shop, main_branch, buyer, invoice, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)

        services.take_payment(invoice, amount=Decimal("30000"), method="mpesa",
                              reference="QGR7XK2P", user=owner)
        invoice.refresh_from_db()
        buyer.refresh_from_db()
        assert invoice.status == InvoiceStatus.PART_PAID
        assert invoice.paid == Decimal("30000")
        assert invoice.balance == Decimal("55000")
        assert buyer.balance == Decimal("55000")

        services.take_payment(invoice, amount=Decimal("55000"), method="cash", user=owner)
        invoice.refresh_from_db()
        buyer.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.balance == Decimal("0")
        assert buyer.balance == Decimal("0")


def test_the_statement_tells_the_whole_story(shop, main_branch, buyer, invoice, owner):
    """One ledger. An invoice and a payment are two lines on the account the
    customer already had, not a second set of books."""
    from apps.customers.models import CreditKind

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        services.take_payment(invoice, amount=Decimal("20000"), method="bank", user=owner)

        rows = list(buyer.credit_transactions.order_by("created_at"))
        assert [row.kind for row in rows] == [CreditKind.CHARGE, CreditKind.PAYMENT]
        assert [row.reference for row in rows] == ["INV260001", "INV260001"]
        assert all(row.invoice_id == invoice.pk for row in rows)


def test_an_invoice_with_goods_gone_cannot_be_cancelled(
        shop, main_branch, buyer, invoice, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        note = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate())
        note.lines.create(invoice_line=invoice.lines.first(), qty=Decimal("1"))
        services.deliver(note, user=owner)

        with pytest.raises(ValueError, match="already been delivered"):
            services.void_invoice(invoice, user=owner)


def test_cancelling_takes_the_debt_back_off(shop, main_branch, buyer, invoice, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        buyer.refresh_from_db()
        assert buyer.balance == Decimal("85000")

        services.void_invoice(invoice, user=owner)
        buyer.refresh_from_db()
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.VOID
        assert buyer.balance == Decimal("0")


def test_nothing_is_sent_against_a_draft(client, shop, main_branch, owner, invoice):
    client.force_login(owner)
    response = client.get(
        reverse("selling:delivery_create", args=[invoice.pk]), follow=True)
    assert "Issue the invoice before sending goods" in response.content.decode()


def test_more_cannot_be_sent_than_was_invoiced(
        client, shop, main_branch, owner, invoice, stocked):
    client.force_login(owner)
    with tenant_context(shop, branch=main_branch):
        services.issue(invoice)
        bread = invoice.lines.get(description="Mkate Mkubwa")
        other = invoice.lines.get(description="Delivery to Nungwi")

    response = client.post(reverse("selling:delivery_create", args=[invoice.pk]), {
        f"qty:{bread.pk}": "60", f"qty:{other.pk}": "0",
        "delivered_on": timezone.localdate().isoformat(),
    }, follow=True)
    assert "only 40" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not DeliveryNote.objects.exists()


def test_an_invoice_can_be_made_from_an_accepted_quotation(
        shop, main_branch, buyer, stocked, owner):
    from apps.selling.models import Quotation, QuotationStatus

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.ACCEPTED, note="Half now, half on delivery",
        )
        quotation.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                               qty=Decimal("10"), unit_price=Decimal("1500"),
                               tax_rate=Decimal("18"), position=10)

        invoice = services.invoice_from_quotation(quotation, terms_days=30)

        assert invoice.quotation_id == quotation.pk
        assert invoice.total == Decimal("15000")
        assert invoice.due_on == today + timedelta(days=30)
        assert invoice.note == quotation.note
        # Copied, not shared: editing the quotation afterwards changes nothing.
        quotation.lines.update(unit_price=Decimal("9999"))
        assert invoice.lines.get().unit_price == Decimal("1500")


def test_a_cashier_may_not_issue_or_deliver(client, shop, main_branch, cashier, invoice):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier,
                                  role=Role.objects.get(name="Cashier"))
    client.force_login(cashier)

    assert client.post(reverse("selling:invoice_detail", args=[invoice.pk]),
                       {"action": "issue"}).status_code == 403
    assert client.get(
        reverse("selling:delivery_create", args=[invoice.pk])).status_code == 403
    with tenant_context(shop, main_branch):
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.DRAFT


def test_the_delivery_note_is_about_goods_not_money(
        client, shop, main_branch, owner, invoice, stocked):
    """It lists quantities and a signature line. A customer who is handed a
    price list at the door starts a conversation nobody wanted."""
    client.force_login(owner)
    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(invoice)
        note = DeliveryNote.objects.create(
            reference="DN260001", invoice=invoice, branch=main_branch,
            delivered_on=timezone.localdate(), received_by="Juma Ally")
        note.lines.create(invoice_line=invoice.lines.first(), qty=Decimal("40"))
        services.deliver(note, user=owner)

    page = client.get(reverse("selling:delivery_print", args=[note.pk]))
    body = page.content.decode()
    assert "Delivery note" in body
    assert "DN260001" in body
    assert "Received by" in body
    assert "not a demand for payment" in body
    assert "85,000" not in body          # no money on it

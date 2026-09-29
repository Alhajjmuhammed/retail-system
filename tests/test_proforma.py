"""
The proforma invoice: what it would come to, owed by nobody.

A draft used to print as "Invoice" with an invoice number already spent on
it. A customer could reasonably treat that as a tax invoice, and a draft
that was never issued left a hole in the invoice sequence -- the first
thing anybody auditing the books asks about.
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


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini", phone="0788112233",
                                       credit_limit=Decimal("5000000"))


def _draft(client, buyer):
    return client.post(reverse("selling:invoice_create"),
                       {"customer": buyer.pk, "terms_days": 14}, follow=True)


def test_a_new_invoice_starts_as_a_proforma(client, shop, main_branch, owner, buyer):
    client.force_login(owner)
    _draft(client, buyer)
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.get()
        assert invoice.reference.startswith("PRO")
        assert invoice.status == InvoiceStatus.DRAFT


def test_it_prints_as_a_proforma_and_says_nothing_is_owed(
        client, shop, main_branch, owner, buyer, stocked):
    client.force_login(owner)
    _draft(client, buyer)
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.get()
        invoice.lines.create(variant=stocked["Mkate"], description="Mkate Mkubwa",
                             qty=Decimal("10"), unit_price=Decimal("1500"),
                             tax_rate=Decimal("18"), position=10)

    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "Proforma invoice" in body
    assert "not a demand for payment" in body
    assert invoice.reference in body


def test_issuing_spends_the_invoice_number(client, shop, main_branch, owner, buyer, stocked):
    """The PRO number is replaced by an INV one the moment it becomes real."""
    client.force_login(owner)
    _draft(client, buyer)
    with tenant_context(shop, branch=main_branch, user=owner):
        invoice = Invoice.objects.get()
        invoice.lines.create(description="Delivery", qty=Decimal("1"),
                             unit_price=Decimal("25000"), tax_rate=Decimal("18"))
        proforma_ref = invoice.reference
        services.issue(invoice)
        invoice.refresh_from_db()

        assert proforma_ref.startswith("PRO")
        assert invoice.reference.startswith("INV")
        assert invoice.status == InvoiceStatus.OPEN
        # The charge on the account carries the invoice number, not the old one.
        row = buyer.credit_transactions.get()
        assert row.reference == invoice.reference

    body = client.get(reverse("selling:invoice_print", args=[invoice.pk])).content.decode()
    assert "Proforma" not in body
    assert "not a demand for payment" not in body


def test_a_proforma_that_dies_leaves_no_gap_in_the_numbering(
        shop, main_branch, owner, buyer):
    """
    Three proformas, only the middle one issued: the invoice sequence still
    starts at one. Gaps in an invoice run are what an auditor asks about.
    """
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        made = []
        for _ in range(3):
            inv = Invoice.objects.create(
                reference=services.next_reference(Invoice, "PRO"),
                customer=buyer, branch=main_branch, issued_on=today,
                due_on=today + timedelta(days=14))
            inv.lines.create(description="Labour", qty=Decimal("1"),
                             unit_price=Decimal("10000"), tax_rate=Decimal("0"))
            made.append(inv)

        services.issue(made[1])
        made[1].refresh_from_db()
        year = today.year % 100
        assert made[1].reference == f"INV{year:02d}0001"

        services.issue(made[2])
        made[2].refresh_from_db()
        assert made[2].reference == f"INV{year:02d}0002"


def test_an_invoice_numbered_by_hand_is_left_alone(shop, main_branch, owner, buyer):
    """Paperwork already written keeps the number it went out with."""
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        invoice = Invoice.objects.create(
            reference="INV260099", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        invoice.lines.create(description="Labour", qty=Decimal("1"),
                             unit_price=Decimal("5000"), tax_rate=Decimal("0"))
        services.issue(invoice)
        invoice.refresh_from_db()
        assert invoice.reference == "INV260099"


def test_a_proforma_number_is_never_handed_out_twice(shop, main_branch, owner, buyer):
    """
    Issuing PRO260001 turns it into INV260001. If the sequence counted from
    the reference column, PRO260001 would be free again and the next
    customer would be sent a number another customer is already holding.
    """
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch, user=owner):
        first = services.save_as_proforma(Invoice(
            customer=buyer, branch=main_branch, issued_on=today,
            due_on=today + timedelta(days=14)))
        first.lines.create(description="Labour", qty=Decimal("1"),
                           unit_price=Decimal("10000"), tax_rate=Decimal("0"))
        year = today.year % 100
        assert first.reference == f"PRO{year:02d}0001"

        services.issue(first)
        first.refresh_from_db()
        assert first.reference == f"INV{year:02d}0001"
        assert first.proforma_reference == f"PRO{year:02d}0001"

        second = services.save_as_proforma(Invoice(
            customer=buyer, branch=main_branch, issued_on=today,
            due_on=today + timedelta(days=14)))
        assert second.reference == f"PRO{year:02d}0002"


def test_two_people_issuing_at_once_do_not_collide(shop, main_branch, owner, buyer, monkeypatch):
    """
    Two proformas issued in the same moment both work out the same next
    invoice number. Without a retry the second one fails on the unique
    index and the person sees an error instead of an invoice.

    The collision is forced here: the number generator hands out a number
    that is already taken, once.
    """
    from apps.selling import services as svc

    today = timezone.localdate()
    year = today.year % 100
    with tenant_context(shop, branch=main_branch, user=owner):
        taken = Invoice.objects.create(
            reference=f"INV{year:02d}0001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))
        taken.lines.create(description="Already out", qty=Decimal("1"),
                           unit_price=Decimal("1000"), tax_rate=Decimal("0"))

        proforma = svc.save_as_proforma(Invoice(
            customer=buyer, branch=main_branch, issued_on=today,
            due_on=today + timedelta(days=14)))
        proforma.lines.create(description="Labour", qty=Decimal("1"),
                              unit_price=Decimal("10000"), tax_rate=Decimal("0"))

        real = svc.next_reference
        handed = {"n": 0}

        def clumsy(model, prefix, field="reference"):
            # First ask gets a number somebody else already holds.
            if prefix == "INV" and handed["n"] == 0:
                handed["n"] += 1
                return f"INV{year:02d}0001"
            return real(model, prefix, field=field)

        monkeypatch.setattr(svc, "next_reference", clumsy)
        svc.issue(proforma)
        proforma.refresh_from_db()

        assert proforma.reference == f"INV{year:02d}0002"
        assert handed["n"] == 1, "the collision was not actually forced"
        # And the charge carries the number that stuck, not the one that failed.
        assert buyer.credit_transactions.get().reference == proforma.reference

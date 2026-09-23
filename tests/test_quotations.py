"""
Quotations: an offer, and nothing more than an offer.

The rule this file exists to hold is that a quotation is inert. A shop that
quotes ten jobs and wins two must see two sales, no stock movement from the
other eight, and nothing on any customer's balance. Everything else here --
numbering, expiry, who may write one -- is ordinary paperwork discipline.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services
from apps.selling.models import Quotation, QuotationStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(
            name="Hoteli ya Baharini", phone="0788112233", tin="123-456-789",
            address="Nungwi", credit_limit=Decimal("2000000"),
        )


def _start(client, customer, days=14):
    return client.post(reverse("selling:quotation_create"),
                       {"customer": customer.pk, "valid_days": days}, follow=True)


def test_a_quotation_touches_nothing(client, shop, main_branch, owner, buyer, stocked):
    """No stock, no balance, no takings -- until somebody says yes and it
    becomes an invoice, which is a different document entirely."""
    from apps.inventory.services import quantity_of
    from apps.pos.models import Sale

    with tenant_context(shop, branch=main_branch):
        before_stock = quantity_of(stocked["Mkate"], main_branch)
        before_balance = buyer.balance
        before_sales = Sale.objects.count()

    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
    client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "variant": stocked["Mkate"].pk,
        "qty": "40", "unit_price": "1500",
    }, follow=True)

    with tenant_context(shop, branch=main_branch):
        buyer.refresh_from_db()
        assert quantity_of(stocked["Mkate"], main_branch) == before_stock
        assert buyer.balance == before_balance
        assert Sale.objects.count() == before_sales
        assert Quotation.objects.get().total == Decimal("60000")


def test_it_can_quote_for_something_that_is_not_a_product(
        client, shop, main_branch, owner, buyer):
    """
    Half of what a shop quotes for is delivery, fitting or labour, and none
    of that is in the product list. The picker used to be a required field,
    which made a typed line impossible to submit at all.
    """
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()

    client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "variant": "", "description": "Delivery to Nungwi",
        "qty": "1", "unit_price": "25000",
    }, follow=True)

    with tenant_context(shop, branch=main_branch):
        line = Quotation.objects.get().lines.get()
        assert line.variant_id is None
        assert line.description == "Delivery to Nungwi"
        assert line.line_total == Decimal("25000")


def test_a_line_needs_a_product_or_a_description(client, shop, main_branch, owner, buyer):
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()

    response = client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "variant": "", "description": "",
        "qty": "1", "unit_price": "1000",
    }, follow=True)
    assert "type what you are quoting for" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not Quotation.objects.get().lines.exists()


def test_a_sent_quotation_is_not_edited_behind_the_customer(
        client, shop, main_branch, owner, buyer, stocked):
    """They are holding a piece of paper. Changing what it says is how a
    shop ends up in an argument it cannot win."""
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
    url = reverse("selling:quotation_detail", args=[quotation.pk])
    client.post(url, {"action": "add_line", "variant": stocked["Mkate"].pk,
                      "qty": "1", "unit_price": "1500"}, follow=True)
    client.post(url, {"action": "send"}, follow=True)

    response = client.post(url, {"action": "add_line", "variant": stocked["Sukari 1kg"].pk,
                                 "qty": "1", "unit_price": "3000"}, follow=True)
    assert "cannot be changed" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert Quotation.objects.get().lines.count() == 1


def test_an_empty_quotation_cannot_be_sent(client, shop, main_branch, owner, buyer):
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
    response = client.post(reverse("selling:quotation_detail", args=[quotation.pk]),
                           {"action": "send"}, follow=True)
    assert "nothing on this quotation" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert Quotation.objects.get().status == QuotationStatus.DRAFT


def test_the_answer_is_recorded_against_a_name_and_a_time(
        client, shop, main_branch, owner, buyer, stocked):
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
    url = reverse("selling:quotation_detail", args=[quotation.pk])
    client.post(url, {"action": "add_line", "variant": stocked["Mkate"].pk,
                      "qty": "2", "unit_price": "1500"}, follow=True)
    client.post(url, {"action": "send"}, follow=True)
    client.post(url, {"action": "accept"}, follow=True)

    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
        assert quotation.status == QuotationStatus.ACCEPTED
        assert quotation.decided_by_id == owner.pk
        assert quotation.decided_at is not None


def test_a_lapsed_quotation_stops_being_an_offer(shop, main_branch, buyer):
    """
    "Valid until" meant nothing until something enforced it: a customer who
    rang in March about a January price was quietly given January's price.
    """
    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        stale = Quotation.objects.create(
            customer=buyer, branch=main_branch, reference="QUO260001",
            issued_on=today - timedelta(days=40), valid_until=today - timedelta(days=10),
            status=QuotationStatus.SENT,
        )
        live = Quotation.objects.create(
            customer=buyer, branch=main_branch, reference="QUO260002",
            issued_on=today, valid_until=today + timedelta(days=10),
            status=QuotationStatus.SENT,
        )
        accepted = Quotation.objects.create(
            customer=buyer, branch=main_branch, reference="QUO260003",
            issued_on=today - timedelta(days=40), valid_until=today - timedelta(days=10),
            status=QuotationStatus.ACCEPTED,
        )

        services.expire_overdue()
        stale.refresh_from_db()
        live.refresh_from_db()
        accepted.refresh_from_db()
        assert stale.status == QuotationStatus.EXPIRED
        assert live.status == QuotationStatus.SENT
        # Already answered: the date passing does not un-accept it.
        assert accepted.status == QuotationStatus.ACCEPTED


def test_the_same_offer_again_without_retyping_it(
        client, shop, main_branch, owner, buyer, stocked):
    client.force_login(owner)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        first = Quotation.objects.get()
    url = reverse("selling:quotation_detail", args=[first.pk])
    client.post(url, {"action": "add_line", "variant": stocked["Mkate"].pk,
                      "qty": "3", "unit_price": "1500"}, follow=True)
    client.post(url, {"action": "add_line", "variant": "", "description": "Labour",
                      "qty": "1", "unit_price": "20000"}, follow=True)
    client.post(url, {"action": "send"}, follow=True)
    client.post(url, {"action": "copy"}, follow=True)

    with tenant_context(shop, branch=main_branch):
        copy = Quotation.objects.exclude(pk=first.pk).get()
        assert copy.status == QuotationStatus.DRAFT
        assert copy.reference != first.reference
        assert copy.issued_on == timezone.localdate()
        assert [(line.description, line.qty) for line in copy.lines.all()] == \
               [(line.description, line.qty) for line in first.lines.all()]


def test_the_numbers_run_in_order(client, shop, main_branch, owner, buyer):
    client.force_login(owner)
    _start(client, buyer)
    _start(client, buyer)
    with tenant_context(shop, branch=main_branch):
        references = sorted(Quotation.objects.values_list("reference", flat=True))
    year = timezone.localdate().year % 100
    assert references == [f"QUO{year:02d}0001", f"QUO{year:02d}0002"]


def test_a_cashier_may_not_write_one(client, shop, main_branch, cashier, buyer):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier,
                                  role=Role.objects.get(name="Cashier"))
    client.force_login(cashier)
    response = client.post(reverse("selling:quotation_create"),
                           {"customer": buyer.pk, "valid_days": 14})
    assert response.status_code == 403
    with tenant_context(shop, branch=main_branch):
        assert not Quotation.objects.exists()


def test_one_shop_never_sees_another_shop_s_offers(
        client, shop, main_branch, owner, buyer, django_user_model):
    from apps.core.context import unscoped
    from apps.tenancy.services import create_tenant

    client.force_login(owner)
    _start(client, buyer)

    asha = django_user_model.objects.create_user("asha@duka.test", "pw", name="Asha")
    other, _ = create_tenant(name="Duka la Asha", owner=asha,
                             plan=shop.subscription.plan)
    with tenant_context(other):
        assert not Quotation.objects.exists()
    with unscoped():
        assert Quotation.objects.count() == 1


def test_the_document_carries_what_a_business_needs(
        client, shop, main_branch, owner, buyer, stocked):
    """A TIN on both sides, a number, and the date the price runs out."""
    client.force_login(owner)
    _start(client, buyer, days=30)
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.get()
    client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "variant": stocked["Mkate"].pk,
        "qty": "10", "unit_price": "1500",
    }, follow=True)

    page = client.get(reverse("selling:quotation_print", args=[quotation.pk]))
    body = page.content.decode()
    assert quotation.reference in body
    assert "Hoteli ya Baharini" in body
    assert "123-456-789" in body                      # the customer's TIN
    assert "not an invoice" in body

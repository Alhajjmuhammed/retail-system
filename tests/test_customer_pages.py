"""Customers: form, account payments, removal, statements."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.context import tenant_context
from apps.customers.models import CreditKind, CreditTransaction, Customer
from apps.pos.models import CashMovement, Sale
from apps.pos.services import add_to_cart, complete_sale, new_cart, open_shift

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


def _owing(shop, name="Mama Neema", amount=5000):
    with tenant_context(shop):
        c = Customer.objects.create(name=name, credit_limit=100000)
        CreditTransaction.objects.create(customer=c, kind=CreditKind.CHARGE, amount=amount,
                                         balance_after=amount)
    return c


def test_form_in_modal_with_errors_and_price_list(client, shop, owner):
    from apps.catalog.models import PriceList

    with tenant_context(shop):
        wholesale = PriceList.objects.create(name="Wholesale")
        Customer.objects.create(name="A", phone="0777")
    client.force_login(owner)
    r = client.post(reverse("customers:customer_create"),
                    {"name": "B", "phone": "0777", "credit_limit": "0"}, **HX)
    assert r.status_code == 200 and "already has the number" in r.content.decode()
    r = client.post(reverse("customers:customer_create"),
                    {"name": "B", "phone": "0778", "email": "bad", "credit_limit": "0"}, **HX)
    assert "does not look right" in r.content.decode()
    r = client.post(reverse("customers:customer_create"),
                    {"name": "B", "phone": "0778", "credit_limit": "0",
                     "price_list": wholesale.pk, "tin": "9" * 60}, **HX)
    assert r.status_code == 204
    with tenant_context(shop):
        b = Customer.objects.get(name="B")
        assert b.price_list == wholesale and len(b.tin) == 30


def test_cash_payment_goes_into_the_open_drawer(client, shop, owner, main_branch, register):
    c = _owing(shop)
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
    client.force_login(owner)
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "2000", "method": "cash"})
    with tenant_context(shop):
        assert c.balance == Decimal("3000")
        m = CashMovement.objects.get(shift=shift)
        assert m.amount == Decimal("2000") and "Mama Neema" in m.reason
        assert CreditTransaction.objects.get(kind=CreditKind.PAYMENT).method == "cash"


def test_overpayment_refused_and_undo_is_append_only(client, shop, owner):
    c = _owing(shop)
    client.force_login(owner)
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "9000", "method": "mobile"})
    with tenant_context(shop):
        assert c.balance == Decimal("5000")
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "1000", "method": "mobile"})
    with tenant_context(shop):
        pay = CreditTransaction.objects.get(kind=CreditKind.PAYMENT)
    client.post(reverse("customers:customer_payment_reverse", args=[pay.pk]))
    client.post(reverse("customers:customer_payment_reverse", args=[pay.pk]))  # twice
    with tenant_context(shop):
        assert c.balance == Decimal("5000")
        assert CreditTransaction.objects.count() == 3


def test_removing_a_customer_with_sales_keeps_the_history(
        client, shop, owner, main_branch, stocked):
    with tenant_context(shop, branch=main_branch, user=owner):
        c = Customer.objects.create(name="Regular")
        cart = new_cart(branch=main_branch, customer=c)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(owner)
    client.post(reverse("customers:customer_delete", args=[c.pk]))
    with tenant_context(shop):
        c.refresh_from_db()
        assert not c.is_active
        assert Sale.objects.get(pk=sale.pk).customer_id == c.pk
    client.post(reverse("customers:customer_restore", args=[c.pk]))
    with tenant_context(shop):
        c.refresh_from_db()
        assert c.is_active


def test_list_views_and_statement(client, shop, owner):
    c = _owing(shop)
    client.force_login(owner)
    r = client.get(reverse("customers:customer_list"), {"view": "owing"})
    assert r.context["summary"]["total_owed"] == Decimal("5000")
    assert [x.pk for x in r.context["page"]] == [c.pk]
    r = client.get(reverse("customers:customer_statement", args=[c.pk]))
    assert r.status_code == 200 and "Balance due" in r.content.decode()
    r = client.get(reverse("customers:statements"))
    assert r.status_code == 200

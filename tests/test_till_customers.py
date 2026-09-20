"""Choosing a customer at the till: their price list and selling on account."""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Price, PriceList
from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.pos.models import Sale
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db


@pytest.fixture
def wholesale(shop, stocked):
    with tenant_context(shop):
        pl = PriceList.objects.create(name="Wholesale", kind="wholesale")
        Price.objects.create(price_list=pl, variant=stocked["Sukari 1kg"], amount=2700)
    return pl


def _push(client, sale):
    return client.post(reverse("sync:push_sales"),
                       data=json.dumps({"sales": [sale], "device_id": "till-test-1"}),
                       content_type="application/json").json()


def _sale(lines, payments, **extra):
    return {"client_uuid": str(uuid.uuid4()), "sold_at": timezone.now().isoformat(),
            "lines": lines, "payments": payments, **extra}


def test_catalogue_carries_every_list_price(client, owner, stocked, wholesale):
    client.force_login(owner)
    data = client.get(reverse("sync:catalog")).json()
    sugar = next(v for v in data["variants"] if v["id"] == stocked["Sukari 1kg"].pk)
    assert sugar["price"] == "3000.00"
    assert sugar["prices"] == {str(wholesale.pk): "2700.00"}
    bread = next(v for v in data["variants"] if v["id"] == stocked["Mkate"].pk)
    assert bread["prices"] == {}


def test_customers_feed_has_list_and_credit_left(client, shop, owner, wholesale):
    from apps.customers.models import CreditKind, CreditTransaction

    with tenant_context(shop):
        c = Customer.objects.create(name="Hoteli", phone="0777", credit_limit=50000,
                                    price_list=wholesale)
        CreditTransaction.objects.create(customer=c, kind=CreditKind.CHARGE, amount=20000,
                                         balance_after=20000)
        Customer.objects.create(name="Gone", is_active=False)
    client.force_login(owner)
    data = client.get(reverse("sync:customers")).json()
    assert [row["name"] for row in data["customers"]] == ["Hoteli"]
    row = data["customers"][0]
    assert row["price_list"] == wholesale.pk and row["credit_left"] == "30000.00"
    assert data["price_lists"][str(wholesale.pk)] == "Wholesale"


def test_wholesale_price_is_not_flagged_for_a_wholesale_customer(
        client, shop, owner, main_branch, register, stocked, wholesale):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
        c = Customer.objects.create(name="Hoteli", price_list=wholesale)
    client.force_login(owner)
    v = stocked["Sukari 1kg"]
    line = [{"variant_id": v.pk, "qty": 1, "unit_price": 2700}]
    res = _push(client, _sale(line, [{"method": "cash", "amount": 2700}], customer_id=c.pk))
    assert res["accepted"] and not res["accepted"][0]["needs_review"]


def test_cashier_without_price_override_is_flagged_for_retail_price_to_nobody(
        client, shop, main_branch, register, stocked, wholesale):
    """A cashier cannot pick a wholesale customer's price and sell it to anyone."""
    from apps.accounts.models import Membership, Role, User

    with tenant_context(shop, branch=main_branch):
        u = User.objects.create_user("c@x.test", "pw", name="C")
        Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Cashier"))
        other = Customer.objects.create(name="Walk-in regular")  # no list
    with tenant_context(shop, branch=main_branch, user=u):
        open_shift(register=register)
    client.force_login(u)
    v = stocked["Sukari 1kg"]
    res = _push(client, _sale([{"variant_id": v.pk, "qty": 1, "unit_price": 2700}],
                              [{"method": "cash", "amount": 2700}], customer_id=other.pk))
    assert res["accepted"][0]["needs_review"]


def test_sale_on_account_from_the_till(client, shop, owner, main_branch, register, stocked):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
        c = Customer.objects.create(name="Mama Neema", credit_limit=10000)
    client.force_login(owner)
    v = stocked["Mkate"]
    res = _push(client, _sale([{"variant_id": v.pk, "qty": 2, "unit_price": 1500}],
                              [{"method": "credit", "amount": 3000}], customer_id=c.pk))
    assert res["accepted"]
    with tenant_context(shop):
        assert c.balance == Decimal("3000")
        assert Sale.objects.get(number=res["accepted"][0]["number"]).customer_id == c.pk


def test_till_config_offers_the_customers_feed(client, shop, owner, main_branch, register):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    body = client.get(reverse("pos:till")).content.decode()
    assert "/api/v1/sync/customers/" in body

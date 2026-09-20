"""Choosing a customer when selling on a phone."""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.catalog.models import Price, PriceList
from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.pos.models import Sale

pytestmark = pytest.mark.django_db


@pytest.fixture
def hotel(shop, stocked):
    with tenant_context(shop):
        pl = PriceList.objects.create(name="Wholesale", kind="wholesale")
        Price.objects.create(price_list=pl, variant=stocked["Sukari 1kg"], amount=2700)
        return Customer.objects.create(name="Hoteli", phone="0788", price_list=pl,
                                       credit_limit=100000)


def _checkout(client, **body):
    body.setdefault("client_uuid", str(uuid.uuid4()))
    body.setdefault("device_id", "phone-test-2")
    return client.post(reverse("pos:phone_checkout"), data=json.dumps(body),
                       content_type="application/json")


def test_customer_search_and_their_prices(client, owner, stocked, hotel):
    client.force_login(owner)
    found = client.get(reverse("pos:phone_customers"), {"q": "078"}).json()["results"]
    assert found[0]["id"] == hotel.pk and found[0]["price_list"] == "Wholesale"
    assert found[0]["credit_left"] == "100000.00"

    usual = client.get(reverse("pos:phone_lookup"), {"q": "Sukari"}).json()["results"][0]
    theirs = client.get(reverse("pos:phone_lookup"),
                        {"q": "Sukari", "customer": hotel.pk}).json()["results"][0]
    assert usual["price"] == "3000.00" and theirs["price"] == "2700.00"
    # A product not on their list keeps the usual price.
    bread = client.get(reverse("pos:phone_lookup"),
                       {"q": "Mkate", "customer": hotel.pk}).json()["results"][0]
    assert bread["price"] == "1500.00"


def test_phone_sale_on_account_at_their_prices(client, shop, owner, stocked, hotel):
    client.force_login(owner)
    r = _checkout(client, customer_id=hotel.pk, method="credit",
                  lines=[{"variant_id": stocked["Sukari 1kg"].pk, "qty": 2}])
    assert r.status_code == 200, r.content
    with tenant_context(shop):
        sale = Sale.objects.get(number=r.json()["number"])
        assert sale.customer_id == hotel.pk and sale.total == Decimal("5400.00")
        assert not sale.needs_review
        assert hotel.balance == Decimal("5400.00")


def test_on_account_needs_a_customer_and_credit_rights(client, shop, main_branch, stocked, hotel):
    with tenant_context(shop):
        seller = User.objects.create_user("floor@x.test", "pw", name="Floor")
        role = Role.objects.create(tenant=shop, name="Floor seller")
        for code in ("pos.mobile_cart", "pos.mobile_payment"):
            role.grant(code)
        role.grant("pos.mobile_methods", options=["mpesa"])
        Membership.objects.create(tenant=shop, user=seller, role=role)
    client.force_login(seller)
    line = [{"variant_id": stocked["Mkate"].pk, "qty": 1}]
    assert _checkout(client, method="credit", lines=line).status_code == 400
    assert _checkout(client, customer_id=hotel.pk, method="credit", lines=line).status_code == 403


def test_basket_sent_to_the_till_carries_the_customer(client, shop, owner, stocked, hotel):
    client.force_login(owner)
    r = client.post(reverse("sync:cart_push"), data=json.dumps({
        "customer_id": hotel.pk,
        "lines": [{"variant_id": stocked["Sukari 1kg"].pk, "qty": 1},
                  {"variant_id": stocked["Mkate"].pk, "qty": 1}]}),
        content_type="application/json")
    assert r.status_code == 200
    assert Decimal(r.json()["total"]) == Decimal("4200")  # 2,700 wholesale + 1,500 usual
    pulled = client.post(reverse("sync:cart_pull", args=[r.json()["code"]])).json()
    assert pulled["customer_id"] == hotel.pk and pulled["customer"] == "Hoteli"


def test_unpriced_product_in_a_basket_is_refused_not_free(client, shop, owner):
    from apps.catalog.models import Product, TaxRate, Unit

    with tenant_context(shop):
        p = Product.objects.create(name="No price", base_unit=Unit.objects.get(code="pc"),
                                   tax_rate=TaxRate.objects.get(is_default=True))
        vid = p.default_variant.pk
    client.force_login(owner)
    r = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"variant_id": vid, "qty": 1}]}),
        content_type="application/json")
    assert r.status_code == 400 and "no price" in r.json()["error"]

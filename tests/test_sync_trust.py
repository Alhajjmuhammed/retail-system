"""
The till's sync is the only way a sale is made, so the server has to check it.

Each of these was accepted as sent: a 99.9% discount from a cashier allowed
5%, a negative line paid with negative cash (an unauthorised refund), credit
with no customer, and sales poured into somebody else's drawer.
"""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.pos.models import Sale
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier_on_shift(shop, main_branch, register):
    with tenant_context(shop, branch=main_branch):
        person = User.objects.create_user("till@shop.test", "pw", name="Till")
        Membership.objects.create(tenant=shop, user=person, role=Role.objects.get(name="Cashier"))
    with tenant_context(shop, branch=main_branch, user=person):
        shift = open_shift(register=register, opening_float=0)
    return person, shift


def _push(client, *sales, shift_id=None):
    response = client.post(
        reverse("sync:push_sales"),
        data=json.dumps({"device_id": "till-test", "shift_id": shift_id, "sales": list(sales)}),
        content_type="application/json",
    )
    return response.json()


def _sale(lines, payments, **extra):
    return {"client_uuid": str(uuid.uuid4()), "lines": lines, "payments": payments, **extra}


def test_an_honest_sale_is_accepted(client, shop, stocked, cashier_on_shift):
    person, _shift = cashier_on_shift
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    result = _push(client, _sale([{"variant_id": v.pk, "qty": 2, "unit_price": 3000}],
                                 [{"method": "cash", "amount": 6000}]))
    assert len(result["accepted"]) == 1, result
    assert not result["accepted"][0]["needs_review"]


def test_a_discount_above_the_cashiers_limit_is_refused(client, stocked, cashier_on_shift):
    person, _ = cashier_on_shift
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    result = _push(client, _sale(
        [{"variant_id": v.pk, "qty": 1, "unit_price": 3000, "discount": 2999}],
        [{"method": "cash", "amount": 1}],
    ))
    assert not result["accepted"] and "discount" in result["rejected"][0]["error"]


def test_a_negative_line_is_not_a_refund(client, stocked, cashier_on_shift):
    person, shift = cashier_on_shift
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    result = _push(client, _sale([{"variant_id": v.pk, "qty": -5, "unit_price": 3000}],
                                 [{"method": "cash", "amount": -15000}]))
    assert not result["accepted"]
    shift.refresh_from_db()
    assert shift.compute_expected_cash() == Decimal("0")


def test_an_open_item_needs_permission(client, cashier_on_shift):
    person, _ = cashier_on_shift
    client.force_login(person)
    result = _push(client, _sale([{"description": "Anything", "qty": 1, "unit_price": 50000}],
                                 [{"method": "cash", "amount": 50000}]))
    assert not result["accepted"] and "Open items" in result["rejected"][0]["error"]


def test_credit_needs_a_customer(client, stocked, owner, shop, main_branch, register):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register, opening_float=0)
    client.force_login(owner)
    v = stocked["Mkate"]
    result = _push(client, _sale([{"variant_id": v.pk, "qty": 1, "unit_price": 1500}],
                                 [{"method": "credit", "amount": 1500}]))
    assert not result["accepted"] and "customer" in result["rejected"][0]["error"]


def test_payments_must_cover_the_sale(client, stocked, cashier_on_shift):
    person, _ = cashier_on_shift
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    result = _push(client, _sale([{"variant_id": v.pk, "qty": 3, "unit_price": 3000}],
                                 [{"method": "cash", "amount": 100}]))
    assert not result["accepted"]


def test_a_cheaper_price_is_kept_but_flagged(client, shop, stocked, cashier_on_shift):
    """Money already taken is recorded -- and put in front of the owner."""
    person, _ = cashier_on_shift
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    result = _push(client, _sale([{"variant_id": v.pk, "qty": 1, "unit_price": 100}],
                                 [{"method": "cash", "amount": 100}]))
    assert result["accepted"][0]["needs_review"]
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert "today's price" in sale.review_notes


def test_sales_go_only_into_your_own_drawer(client, shop, main_branch, stocked, owner,
                                            cashier_on_shift):
    _person, their_shift = cashier_on_shift
    client.force_login(owner)  # owner has no shift of their own
    v = stocked["Mkate"]
    _push(client, _sale([{"variant_id": v.pk, "qty": 1, "unit_price": 1500}],
                        [{"method": "cash", "amount": 1500}]), shift_id=their_shift.pk)
    with tenant_context(shop, branch=main_branch):
        their_shift.refresh_from_db()
        assert their_shift.sales.count() == 0


def test_a_held_basket_is_collected_only_once(client, shop, main_branch, stocked, owner):
    client.force_login(owner)
    v = stocked["Mkate"]
    code = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"variant_id": v.pk, "qty": 2}]}), content_type="application/json").json()["code"]
    assert client.post(reverse("sync:cart_pull", args=[code])).status_code == 200
    assert client.post(reverse("sync:cart_pull", args=[code])).status_code == 404


def test_a_phone_basket_uses_the_catalogue_price(client, shop, stocked, cashier_on_shift):
    person, _ = cashier_on_shift
    with tenant_context(shop):
        from apps.accounts.models import Permission, RolePermission

        RolePermission.objects.create(role=Role.objects.get(name="Cashier"),
                                      permission=Permission.objects.get(code="pos.mobile_cart"),
                                      granted=True)
        Membership.objects.get(user=person).invalidate_permissions()
    client.force_login(person)
    v = stocked["Sukari 1kg"]
    data = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"variant_id": v.pk, "qty": 1, "unit_price": 1}]}),
        content_type="application/json").json()
    assert Decimal(data["total"]) == Decimal("3000")


def test_bad_input_is_a_400_not_a_crash(client, owner, shop):
    client.force_login(owner)
    assert client.post(reverse("sync:cart_push"), data="{nope",
                       content_type="application/json").status_code == 400
    assert client.get(reverse("sync:catalog"), {"since": "yesterday"}).status_code == 200

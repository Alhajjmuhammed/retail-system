"""
Selling from a phone: build a basket, hand it to a till or take payment,
within exactly what the owner allowed.
"""

import json
import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Permission, Role, RolePermission, User
from apps.core.context import tenant_context
from apps.pos.models import Sale

pytestmark = pytest.mark.django_db


@pytest.fixture
def floor(shop, main_branch):
    """A floor assistant: may build baskets and take M-Pesa up to 10,000."""
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Floor")
        for code, limit, options in [("pos.mobile_cart", None, []),
                                     ("pos.mobile_payment", 10000, []),
                                     ("pos.mobile_methods", None, ["mpesa"])]:
            RolePermission.objects.create(role=role, permission=Permission.objects.get(code=code),
                                          limit_value=limit, set_value=options)
        person = User.objects.create_user("floor@shop.test", "pw", name="Floor")
        Membership.objects.create(tenant=shop, user=person, role=role)
    return person


def _checkout(client, lines, method="mpesa", reference="QGR7XK2P", sale_id=None):
    return client.post(reverse("pos:phone_checkout"), data=json.dumps({
        "client_uuid": sale_id or str(uuid.uuid4()), "method": method, "reference": reference,
        "device_id": "phone-test-1", "lines": lines,
    }), content_type="application/json")


def test_the_phone_screen_opens_with_the_owners_rules(client, floor):
    client.force_login(floor)
    r = client.get(reverse("pos:phone"))
    config = json.loads(r.context["config"])
    assert r.status_code == 200 and config["may_pay"] and config["pay_limit"] == 10000
    assert [m["value"] for m in config["methods"]] == ["mpesa"]


def test_lookup_by_name_and_barcode_prices_from_the_server(client, shop, floor, stocked):
    from apps.catalog.services import attach_barcode
    with tenant_context(shop):
        attach_barcode(stocked["Mkate"], "6001234567890")
    client.force_login(floor)
    by_name = client.get(reverse("pos:phone_lookup"), {"q": "mka"}).json()["results"]
    by_code = client.get(reverse("pos:phone_lookup"), {"code": "6001234567890"}).json()["results"]
    assert by_name[0]["price"] == "1500.00" and by_code[0]["id"] == stocked["Mkate"].pk


def test_a_phone_sale_within_the_limit_is_recorded_at_the_servers_price(client, shop, floor, stocked):
    client.force_login(floor)
    r = _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 2}])
    assert r.status_code == 200
    with tenant_context(shop):
        sale = Sale.objects.get(number=r.json()["number"])
        assert sale.total == 3000 and sale.user == floor


def test_pressing_pay_twice_makes_one_sale(client, shop, floor, stocked):
    client.force_login(floor)
    sale_id = str(uuid.uuid4())
    _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 1}], sale_id=sale_id)
    _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 1}], sale_id=sale_id)
    with tenant_context(shop):
        assert Sale.objects.filter(client_uuid=sale_id).count() == 1


def test_over_the_phone_limit_is_refused(client, floor, stocked):
    client.force_login(floor)
    r = _checkout(client, [{"variant_id": stocked["Sukari 1kg"].pk, "qty": 4}])  # 12,000
    assert r.status_code == 403 and "till" in r.json()["error"]


def test_a_method_the_owner_did_not_allow_is_refused(client, floor, stocked):
    client.force_login(floor)
    r = _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 1}], method="card")
    assert r.status_code == 403


def test_mobile_money_needs_its_reference(client, floor, stocked):
    client.force_login(floor)
    r = _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 1}], reference="")
    assert r.status_code == 400


def test_cash_on_a_phone_needs_a_drawer(client, shop, owner, stocked):
    client.force_login(owner)  # owners may use any method
    r = _checkout(client, [{"variant_id": stocked["Mkate"].pk, "qty": 1}], method="cash", reference="")
    assert r.status_code == 400 and "drawer" in r.json()["error"]


def test_somebody_without_phone_rights_cannot_use_it(client, shop, cashier):
    with tenant_context(shop):
        Membership.objects.create(tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
    client.force_login(cashier)
    assert client.get(reverse("pos:phone")).status_code == 403


def test_a_basket_can_be_handed_to_a_till(client, shop, floor, stocked):
    client.force_login(floor)
    r = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"variant_id": stocked["Mkate"].pk, "qty": 2}]}), content_type="application/json")
    assert r.status_code == 200 and len(r.json()["code"]) == 4

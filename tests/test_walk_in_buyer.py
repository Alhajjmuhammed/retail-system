"""
A name and a phone for somebody who is not on the books.

Most sales over a counter are to a person the shop will never see again. A few
need a name on them anyway: a delivery, something put aside, a thing that may
come back. Neither field is required, and filling one does not add anybody to
the customer list -- a till that quietly created a customer record every time
a cashier typed a name would fill the list with "John" inside a week.
"""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.pos.models import Sale
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db


def _push(client, stocked, **extra):
    sale = {
        "client_uuid": str(uuid.uuid4()),
        "sold_at": timezone.now().isoformat(),
        "lines": [{"variant_id": stocked["Mkate"].pk, "qty": 1, "unit_price": 1500}],
        "payments": [{"method": "cash", "amount": 1500}],
        **extra,
    }
    return client.post(reverse("sync:push_sales"),
                       data=json.dumps({"sales": [sale], "device_id": "till-1"}),
                       content_type="application/json").json()


@pytest.fixture
def selling(client, shop, main_branch, register, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register, opening_float=Decimal("5000"))
    client.force_login(owner)
    return client


def test_a_sale_needs_neither_of_them(selling, shop, stocked):
    result = _push(selling, stocked)
    assert result["accepted"]
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert sale.buyer_name == "" and sale.buyer_phone == ""


def test_a_name_on_its_own_is_enough(selling, shop, stocked):
    result = _push(selling, stocked, buyer_name="Mzee Hamisi")
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert sale.buyer_name == "Mzee Hamisi"
        assert sale.buyer_phone == ""


def test_a_phone_on_its_own_is_enough(selling, shop, stocked):
    result = _push(selling, stocked, buyer_phone="0712345678")
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert sale.buyer_phone == "0712345678"
        assert sale.buyer_name == ""


def test_typing_a_name_does_not_add_anybody_to_the_customer_list(selling, shop, stocked):
    with tenant_context(shop):
        before = Customer.objects.count()
    _push(selling, stocked, buyer_name="Mzee Hamisi", buyer_phone="0712345678")
    with tenant_context(shop):
        assert Customer.objects.count() == before


def test_something_far_too_long_is_cut_rather_than_refused(selling, shop, stocked):
    """This comes from a device the shop controls, but so does everything."""
    result = _push(selling, stocked, buyer_name="x" * 500, buyer_phone="9" * 200)
    assert result["accepted"], result
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert len(sale.buyer_name) == 80
        assert len(sale.buyer_phone) == 30


def test_the_receipt_says_who_it_was_for(selling, shop, stocked):
    result = _push(selling, stocked, buyer_name="Mzee Hamisi", buyer_phone="0712345678")
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
    page = selling.get(reverse("pos:receipt", args=[sale.pk]))
    body = page.content.decode()
    assert "Mzee Hamisi" in body and "0712345678" in body


def test_a_real_customer_still_wins(selling, shop, stocked):
    """Their account already carries their name; the typed one is for nobody."""
    with tenant_context(shop):
        hotel = Customer.objects.create(name="Hoteli", credit_limit=Decimal("50000"))
    result = _push(selling, stocked, customer_id=hotel.pk, buyer_name="Somebody else")
    with tenant_context(shop):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert sale.customer_id == hotel.pk
    page = selling.get(reverse("pos:receipt", args=[sale.pk]))
    assert "Hoteli" in page.content.decode()

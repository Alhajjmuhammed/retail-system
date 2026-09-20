"""The Stock clerk role, as a clerk meets it."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.purchasing.models import Supplier, SupplierInvoice

pytestmark = pytest.mark.django_db


@pytest.fixture
def clerk(shop, main_branch):
    with tenant_context(shop):
        u = User.objects.create_user("clerk@x.test", "pw", name="Clerk")
        m = Membership.objects.create(tenant=shop, user=u,
                                      role=Role.objects.get(name="Stock clerk"))
        m.set_branches([main_branch], all_branches=False)
    return u


def test_stock_report_shows_quantities_but_no_costs(client, clerk, stocked):
    client.force_login(clerk)
    r = client.get(reverse("reports:stock_value"))
    assert r.status_code == 200 and not r.context["can_see_cost"]
    body = r.content.decode()
    assert "Stock on hand" in body and "Unit cost" not in body and "110,000" not in body


def test_owner_still_sees_the_stock_value(client, owner, stocked):
    client.force_login(owner)
    r = client.get(reverse("reports:stock_value"))
    assert r.context["can_see_cost"] and "Unit cost" in r.content.decode()


def test_clerk_cannot_record_a_supplier_bill(client, shop, clerk):
    with tenant_context(shop):
        s = Supplier.objects.create(name="Azam")
    client.force_login(clerk)
    r = client.post(reverse("purchasing:supplier_bill", args=[s.pk]),
                    {"number": "X1", "amount": "50000"})
    assert r.status_code == 403
    with tenant_context(shop):
        assert not SupplierInvoice.objects.exists()
    assert "Record a bill" not in client.get(
        reverse("purchasing:supplier_detail", args=[s.pk])).content.decode()


def test_clerk_receives_goods_into_stock(client, shop, main_branch, clerk, stocked):
    from apps.inventory.models import StockItem
    from apps.purchasing.models import GoodsReceipt

    with tenant_context(shop):
        s = Supplier.objects.create(name="Bakhresa")
    client.force_login(clerk)
    client.post(reverse("purchasing:receipt_create"), {"supplier": s.pk})
    with tenant_context(shop):
        receipt = GoodsReceipt.objects.get()
    url = reverse("purchasing:receipt_detail", args=[receipt.pk])
    client.post(url, {"action": "add_line", "variant": stocked["Mkate"].pk, "qty": "10",
                      "unit_cost": "1000"})
    client.post(url, {"action": "post"})
    with tenant_context(shop):
        assert StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"]) \
            .qty_on_hand == 110

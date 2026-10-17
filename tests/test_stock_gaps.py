"""
Where stock is seen and changed.

A product that had never had stock at a branch was missing from that
branch's stock page, so it had no Adjust button and no way in -- every
product at a second branch, and every product added without an opening
quantity.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def nungwi(shop):
    from apps.org.models import Branch

    with tenant_context(shop):
        return Branch.objects.create(name="Nungwi", code="NGW")


@pytest.fixture
def bare_product(shop):
    """Added with no opening stock: no stock line anywhere."""
    from apps.catalog.models import Product, TaxRate, Unit, Variant

    with tenant_context(shop):
        product = Product.objects.create(
            name="Mincer", base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True))
        variant = product.default_variant or Variant.objects.create(product=product,
                                                                    is_default=True)
    return variant


def _stock_page(client, owner, branch=None):
    client.force_login(owner)
    if branch is not None:
        client.post(reverse("accounts:switch_branch"), {"branch_id": branch.pk})
    return client.get(reverse("inventory:stock_list")).content.decode()


def test_a_product_with_no_stock_yet_is_on_the_stock_page(client, shop, main_branch, owner,
                                                          bare_product):
    from apps.inventory.models import StockItem

    html = _stock_page(client, owner)
    assert "Mincer" in html
    with tenant_context(shop):
        item = StockItem.objects.get(variant=bare_product, branch=main_branch)
        assert item.qty_on_hand == 0
    assert reverse("inventory:stock_adjust", args=[item.pk]) in html


def test_every_product_shows_at_a_second_branch(client, shop, owner, stocked, nungwi):
    html = _stock_page(client, owner, nungwi)
    for name in ("Sukari 1kg", "Mkate", "Soda 500ml"):
        assert name in html


def test_a_product_that_keeps_no_count_is_left_off(client, shop, owner, bare_product):
    with tenant_context(shop):
        product = bare_product.product
        product.track_stock = False
        product.save()
    assert "Mincer" not in _stock_page(client, owner)


def test_adjust_from_the_product_page(client, shop, main_branch, owner, bare_product):
    client.force_login(owner)
    page = client.get(reverse("catalog:product_detail", args=[bare_product.product_id]))
    url = reverse("inventory:stock_adjust_product", args=[bare_product.pk])
    assert url in page.content.decode()
    landed = client.get(url)
    assert landed.status_code == 302
    assert "/adjust/" in landed["Location"]


def test_a_cashier_gets_no_adjust_button(client, shop, cashier, bare_product):
    from apps.accounts.models import Membership, Permission, Role, RolePermission

    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Looker")
        RolePermission.objects.create(role=role,
                                      permission=Permission.objects.get(code="product.view"))
        Membership.objects.create(tenant=shop, user=cashier, role=role)
    client.force_login(cashier)
    html = client.get(reverse("catalog:product_detail",
                              args=[bare_product.product_id])).content.decode()
    assert reverse("inventory:stock_adjust_product", args=[bare_product.pk]) not in html
    assert client.get(reverse("inventory:stock_adjust_product",
                              args=[bare_product.pk])).status_code == 403


# -- the adjust form ---------------------------------------------------------

def _item(shop, branch, variant):
    from apps.inventory.services import get_stock_item

    with tenant_context(shop, branch=branch):
        return get_stock_item(variant, branch)


def test_adding_some(client, shop, main_branch, owner, stocked):
    item = _item(shop, main_branch, stocked["Mkate"])          # 100 on hand
    client.force_login(owner)
    client.post(reverse("inventory:stock_adjust", args=[item.pk]), {
        "mode": "change", "change_by": "24", "reason": "24 more came",
        "seen_qty": str(item.qty_on_hand)})
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == Decimal("124")


def test_taking_some_away(client, shop, main_branch, owner, stocked):
    item = _item(shop, main_branch, stocked["Mkate"])
    client.force_login(owner)
    client.post(reverse("inventory:stock_adjust", args=[item.pk]), {
        "mode": "change", "change_by": "-3", "reason": "3 broke",
        "seen_qty": str(item.qty_on_hand)})
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == Decimal("97")


def test_taking_more_than_there_is_is_refused(client, shop, main_branch, owner, stocked):
    item = _item(shop, main_branch, stocked["Mkate"])
    client.force_login(owner)
    page = client.post(reverse("inventory:stock_adjust", args=[item.pk]), {
        "mode": "change", "change_by": "-500", "reason": "oops",
        "seen_qty": str(item.qty_on_hand)})
    assert "less than nothing" in page.content.decode()
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == Decimal("100")


def test_counting_still_works(client, shop, main_branch, owner, stocked):
    item = _item(shop, main_branch, stocked["Mkate"])
    client.force_login(owner)
    client.post(reverse("inventory:stock_adjust", args=[item.pk]), {
        "mode": "count", "new_qty": "40", "reason": "Counted the shelf",
        "seen_qty": str(item.qty_on_hand)})
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == Decimal("40")


def test_a_sale_made_while_typing_is_not_overwritten(client, shop, main_branch, owner, stocked):
    """The form says what it saw; if the shelf moved meanwhile, it is told."""
    item = _item(shop, main_branch, stocked["Mkate"])
    client.force_login(owner)
    page = client.post(reverse("inventory:stock_adjust", args=[item.pk]), {
        "mode": "count", "new_qty": "40", "reason": "Counted",
        "seen_qty": "90"})            # the page showed 90; it is really 100
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == Decimal("100")
    assert page.status_code == 200    # shown again, not saved


# -- names -------------------------------------------------------------------

def test_goods_from_suppliers_are_called_goods_received(client, shop, owner):
    client.force_login(owner)
    html = client.get(reverse("purchasing:receipt_list")).content.decode()
    assert "Goods received" in html

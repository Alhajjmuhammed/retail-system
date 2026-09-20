"""Products, barcodes, price lists, categories/VAT and till tiles."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.catalog.models import (
    Barcode,
    Brand,
    Category,
    Price,
    PriceList,
    Product,
    QuickKey,
    TaxRate,
    Unit,
)
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}


def _base(shop):
    with tenant_context(shop):
        return {
            "base_unit": Unit.objects.get(code="pc").pk,
            "tax_rate": TaxRate.objects.get(is_default=True).pk,
            "track_stock": "on", "sellable_at_pos": "on", "discount_allowed": "on",
            "is_active": "on",
        }


def test_create_in_modal_with_wholesale_price(client, shop, owner):
    with tenant_context(shop):
        wholesale = PriceList.objects.create(name="Wholesale")
    client.force_login(owner)
    r = client.get(reverse("catalog:product_create"), **HX)
    assert r.status_code == 200 and f"list_price:{wholesale.pk}" in r.content.decode()

    r = client.post(reverse("catalog:product_create"), {
        **_base(shop), "name": "Unga 2kg", "price": "5000", f"list_price:{wholesale.pk}": "4500",
    }, **HX)
    assert r.status_code == 204 and r["HX-Redirect"] == reverse("catalog:product_list")
    with tenant_context(shop):
        v = Product.objects.get(name="Unga 2kg").default_variant
        assert Price.objects.get(variant=v, price_list=wholesale).amount == Decimal("4500")


def test_bad_values_stay_in_the_modal(client, shop, owner):
    client.force_login(owner)
    r = client.post(reverse("catalog:product_create"), {
        **_base(shop), "name": "X", "price": "-5", "min_price": "10",
    }, **HX)
    assert r.status_code == 200
    assert "form" in r.context and r.context["form"].errors
    with tenant_context(shop):
        assert not Product.objects.filter(name="X").exists()


def test_floor_above_price_refused(client, shop, owner):
    client.force_login(owner)
    r = client.post(reverse("catalog:product_create"), {
        **_base(shop), "name": "Y", "price": "100", "min_price": "200",
    }, **HX)
    assert "min_price" in r.context["form"].errors


def test_edit_shows_reorder_level_and_has_no_cost_field(client, shop, owner, main_branch, stocked):
    from apps.inventory.services import get_stock_item

    v = stocked["Mkate"]
    with tenant_context(shop, branch=main_branch):
        item = get_stock_item(v, main_branch)
        item.reorder_level = 7
        item.save()
    client.force_login(owner)
    r = client.get(reverse("catalog:product_edit", args=[v.product_id]), **HX)
    form = r.context["form"]
    assert "cost" not in form.fields
    assert form.initial["reorder_level"] == Decimal("7")


def test_barcode_add_in_place_and_validation(client, shop, owner, stocked):
    v = stocked["Soda 500ml"]
    client.force_login(owner)
    url = reverse("catalog:barcode_add", args=[v.pk])
    r = client.post(url, {"code": "6001", "pack_quantity": "abc"}, **HX)
    assert r.status_code == 200 and "above zero" in r.content.decode()
    r = client.post(url, {"code": "6001", "pack_quantity": "0"}, **HX)
    assert "above zero" in r.content.decode()
    r = client.post(url, {"code": "6001", "pack_quantity": "24"}, **HX)
    assert "6001" in r.content.decode()
    r = client.post(reverse("catalog:barcode_add", args=[stocked["Mkate"].pk]),
                    {"code": "6001"}, **HX)
    assert "already belongs" in r.content.decode()


def test_removing_the_main_barcode_promotes_another(client, shop, owner, stocked):
    from apps.catalog.services import attach_barcode

    v = stocked["Soda 500ml"]
    with tenant_context(shop):
        first = attach_barcode(v, "111")
        attach_barcode(v, "222")
        assert first.is_primary
    client.force_login(owner)
    client.post(reverse("catalog:barcode_delete", args=[first.pk]), **HX)
    with tenant_context(shop):
        assert Barcode.objects.get(code="222").is_primary


def test_price_list_rename_and_restore(client, shop, owner):
    with tenant_context(shop):
        pl = PriceList.objects.create(name="Bulk", is_active=False)
    client.force_login(owner)
    client.post(reverse("catalog:price_lists"), {"action": "restore", "price_list": pl.pk})
    client.post(reverse("catalog:price_lists"),
                {"action": "rename", "price_list": pl.pk, "name": "Wholesale"})
    with tenant_context(shop):
        pl.refresh_from_db()
        assert pl.is_active and pl.name == "Wholesale"


def test_taxonomy_add_existing_says_so_and_restores_retired(client, shop, owner):
    with tenant_context(shop):
        Brand.objects.create(name="Azam", is_active=False)
        Category.objects.create(name="Drinks")
    client.force_login(owner)
    r = client.post(reverse("catalog:taxonomy"), {"kind": "category", "name": "drinks"}, follow=True)
    assert "already on the list" in r.content.decode()
    client.post(reverse("catalog:taxonomy"), {"kind": "brand", "name": "Azam"})
    with tenant_context(shop):
        assert Brand.objects.get(name="Azam").is_active
        assert Category.objects.filter(name__iexact="drinks").count() == 1


def test_taxonomy_edit_error_stays_in_modal(client, shop, owner):
    with tenant_context(shop):
        tax = TaxRate.objects.get(is_default=True)
    client.force_login(owner)
    r = client.post(reverse("catalog:taxonomy_edit", args=["tax", tax.pk]),
                    {"name": tax.name, "rate": "150"}, **HX)
    assert r.status_code == 200 and "between 0 and 100" in r.content.decode()
    r = client.post(reverse("catalog:taxonomy_edit", args=["tax", tax.pk]),
                    {"name": tax.name, "rate": "16"}, **HX)
    assert r.status_code == 204


def test_tiles_bad_input_and_duplicate_code(client, shop, owner, stocked):
    client.force_login(owner)
    r = client.post(reverse("catalog:tiles"), {"action": "add", "variant": "abc"})
    assert r.status_code == 404
    client.post(reverse("catalog:tiles"),
                {"action": "add", "variant": stocked["Mkate"].pk, "plu_code": "11"})
    client.post(reverse("catalog:tiles"),
                {"action": "add", "variant": stocked["Soda 500ml"].pk, "plu_code": "11"})
    with tenant_context(shop):
        assert QuickKey.objects.count() == 1
    r = client.get(reverse("catalog:tiles"), {"q": "soda"})
    assert [v.pk for v in r.context["candidates"]] == [stocked["Soda 500ml"].pk]

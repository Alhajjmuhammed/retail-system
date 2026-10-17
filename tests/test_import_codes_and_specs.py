"""
Importing a shop's item list from another system.

Two items may share a name; the code is what tells them apart. And a
machine's specification, typed into the name on the old system, comes over
whole without breaking the import.
"""

import io

import pytest

from apps.catalog.imports import import_products
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def _run(shop, branch, text):
    with tenant_context(shop, branch=branch):
        return import_products(io.BytesIO(text.encode("utf-8")), branch=branch)


def test_two_items_with_one_name_stay_two(shop, main_branch):
    from apps.catalog.models import Product
    from apps.inventory.services import quantity_of

    result = _run(shop, main_branch, "name,sku,qty,cost\n"
                                     "SOAP - 20g,C-119,700,111.33\n"
                                     "SOAP - 20g,C-120,2887,111.33\n")
    assert result.created == 2 and not result.errors
    with tenant_context(shop, branch=main_branch):
        a, b = Product.objects.get(sku="C-119"), Product.objects.get(sku="C-120")
        assert quantity_of(a.default_variant, main_branch) == 700
        assert quantity_of(b.default_variant, main_branch) == 2887


def test_without_a_code_the_name_still_finds_the_product(shop, main_branch):
    _run(shop, main_branch, "name,price\nMkate,1500\n")
    result = _run(shop, main_branch, "name,price\nMkate,1600\n")
    assert result.created == 0 and result.updated == 1


def test_old_code_and_description_come_over(shop, main_branch):
    from apps.catalog.models import Product

    _run(shop, main_branch, 'name,sku,old_code,description\n'
                            'WATER DISPENSER - White,HY-1,HY-LDR-A07T,"MODEL HY-LDR-A07T\nVOLTAGE 220-240V"\n')
    with tenant_context(shop):
        p = Product.objects.get(sku="HY-1")
        assert p.old_code == "HY-LDR-A07T"
        assert "VOLTAGE 220-240V" in p.description


def test_a_name_too_long_is_shortened_and_kept_whole(shop, main_branch):
    from apps.catalog.models import Product

    spec = "DOUGH KNEADING MACHINE 50 KG 220V " + "- Stainless steel bowl " * 12
    result = _run(shop, main_branch, f'name,sku\n"{spec}",HY.05.K\n')
    assert not result.errors
    with tenant_context(shop):
        p = Product.objects.get(sku="HY.05.K")
        assert len(p.name) <= 160
        assert p.description.strip() == spec.strip()


def test_a_nought_price_column_left_empty_sets_no_price(shop, main_branch):
    from apps.catalog.models import Product

    _run(shop, main_branch, "name,sku,price,qty,cost\nAshtray,KAR70,,5,1500\n")
    with tenant_context(shop):
        variant = Product.objects.get(sku="KAR70").default_variant
        assert variant.price_for(None) is None


def test_the_export_carries_the_new_columns(shop, main_branch):
    from apps.catalog.imports import export_products

    _run(shop, main_branch, "name,sku,old_code,description\nTray,T-1,OLD-9,Round 36cm\n")
    with tenant_context(shop, branch=main_branch):
        text = export_products(main_branch)
    header = text.splitlines()[0]
    assert "old_code" in header and "description" in header
    assert "OLD-9" in text and "Round 36cm" in text

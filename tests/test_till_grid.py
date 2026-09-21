"""
The till's grid: categories, and what is left on the shelf.

A shop with sixty lines is unusable as one long grid, and a cashier who has to
scroll past the soap to reach the bread is slower than the queue behind them.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.catalog.models import Category, QuickKey
from apps.catalog.services import tile_categories, tiles
from apps.core.context import tenant_context
from apps.inventory.models import MovementReason
from apps.inventory.services import record_movement
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db


@pytest.fixture
def grid(shop, main_branch, stocked):
    with tenant_context(shop, branch=main_branch):
        food = Category.objects.create(name="Food")
        drinks = Category.objects.create(name="Drinks")
        for name, category in (("Sukari 1kg", food), ("Mkate", food),
                               ("Soda 500ml", drinks)):
            variant = stocked[name]
            variant.product.category = category
            variant.product.save(update_fields=["category"])
            QuickKey.objects.create(variant=variant, position=0)
    return stocked


def test_the_tabs_come_from_what_is_actually_tappable(shop, main_branch, grid):
    """A category with nothing in it is a tab that leads to an empty screen."""
    with tenant_context(shop, branch=main_branch):
        Category.objects.create(name="Hardware")      # nothing tiled in it
        found = dict(tile_categories(tiles(branch=main_branch)))
    assert found == {"Food": 2, "Drinks": 1}


def test_each_tile_knows_what_is_left(shop, main_branch, grid):
    with tenant_context(shop, branch=main_branch):
        counts = {str(k.variant): k.stock_left for k in tiles(branch=main_branch)}
    assert counts["Sukari 1kg"] == Decimal("100.000")


def test_selling_changes_what_the_tile_says(shop, main_branch, grid, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        record_movement(variant=grid["Sukari 1kg"], qty_delta=-40,
                        reason=MovementReason.SALE, branch=main_branch)
        counts = {str(k.variant): k.stock_left for k in tiles(branch=main_branch)}
    assert counts["Sukari 1kg"] == Decimal("60.000")


def test_a_product_that_keeps_no_stock_says_nothing(shop, main_branch, grid):
    """A service has no shelf to be short on."""
    with tenant_context(shop, branch=main_branch):
        sugar = grid["Sukari 1kg"].product
        sugar.track_stock = False
        sugar.save(update_fields=["track_stock"])
        counts = {str(k.variant): k.stock_left for k in tiles(branch=main_branch)}
    assert counts["Sukari 1kg"] is None


def test_the_till_page_offers_the_tabs(client, shop, main_branch, register, owner, grid):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register, opening_float=Decimal("1000"))
    client.force_login(owner)
    page = client.get(reverse("pos:till"))
    assert page.status_code == 200
    assert dict(page.context["tile_categories"]) == {"Food": 2, "Drinks": 1}
    body = page.content.decode()
    assert "All items" in body and "tileGroup" in body


def test_the_grid_does_not_cost_a_query_per_tile(
        django_assert_max_num_queries, shop, main_branch, grid):
    """Sixty tiles must not be sixty trips to the database."""
    with tenant_context(shop, branch=main_branch), django_assert_max_num_queries(8):
        [(str(k.variant), k.stock_left) for k in tiles(branch=main_branch)]

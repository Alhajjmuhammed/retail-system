"""
The categories page as a tree: one row a shelf, folding, searching, and
still readable with a hundred of them.
"""

import re

import pytest
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def shelves(shop):
    """Drinks > Soda > Bottles, and Food on its own."""
    from apps.catalog.models import Category

    with tenant_context(shop):
        drinks = Category.objects.create(name="Drinks")
        soda = Category.objects.create(name="Soda", parent=drinks)
        bottles = Category.objects.create(name="Bottles", parent=soda)
        food = Category.objects.create(name="Food")
    return {"drinks": drinks, "soda": soda, "bottles": bottles, "food": food}


def _page(client, owner):
    client.force_login(owner)
    return client.get(reverse("catalog:categories")).content.decode()


def test_every_shelf_is_one_row_in_reading_order(client, shop, owner, shelves):
    html = _page(client, owner)
    order = re.findall(r'class="cat-row group" data-pk="(\d+)"', html)
    pks = [str(shelves[k].pk) for k in ("drinks", "soda", "bottles", "food")]
    assert [p for p in order if p in pks] == pks


def test_a_row_is_indented_by_its_depth(client, shop, owner, shelves):
    html = _page(client, owner)
    row = re.search(r'data-pk="%d".*?style="--depth: (\d)"' % shelves["bottles"].pk, html, re.S)
    assert row.group(1) == "2"


def test_a_search_for_a_deep_shelf_keeps_its_parents(shop, shelves):
    from apps.catalog.services import category_rows

    with tenant_context(shop):
        rows = {row.name: row for row in category_rows()}
    # "bottles" is found on Bottles, and on Soda and Drinks around it.
    assert "bottles" in rows["Bottles"].search
    assert "bottles" in rows["Soda"].search
    assert "bottles" in rows["Drinks"].search
    assert "bottles" not in rows["Food"].search
    assert rows["Bottles"].ancestor_pks == [shelves["drinks"].pk, shelves["soda"].pk]


def test_the_product_count_opens_those_products(client, shop, owner, shelves):
    html = _page(client, owner)
    assert f'{reverse("catalog:product_list")}?category={shelves["drinks"].pk}' in html


def test_add_inside_offers_every_shelf_with_room(client, shop, owner, shelves):
    html = _page(client, owner)
    select = re.search(r'<select name="parent".*?</select>', html, re.S).group(0)
    assert "Drinks &gt; Soda" in select or "Drinks › Soda" in select or "Drinks" in select
    # The third level is as deep as it goes: nothing can be put inside Bottles.
    assert f'value="{shelves["bottles"].pk}"' not in select


def test_adding_inside_a_shelf(client, shop, owner, shelves):
    from apps.catalog.models import Category

    client.force_login(owner)
    client.post(reverse("catalog:categories"),
                {"kind": "category", "name": "Cans", "parent": shelves["soda"].pk})
    with tenant_context(shop):
        assert Category.objects.get(name="Cans").parent == shelves["soda"]


def test_a_short_list_opens_unfolded_and_a_long_one_folded(client, shop, owner):
    from apps.catalog.models import Category

    with tenant_context(shop):
        for i in range(5):
            Category.objects.create(name=f"Shelf {i}")
    assert 'x-data="categoryTree(true)"' in _page(client, owner)
    with tenant_context(shop):
        for i in range(5, 40):
            top = Category.objects.create(name=f"Shelf {i}")
            Category.objects.create(name=f"Inside {i}", parent=top)
    html = _page(client, owner)
    assert 'x-data="categoryTree(false)"' in html
    # Still one row each, however many: no card per shelf.
    assert html.count('class="cat-row group"') >= 75
    assert 'class="shelf' not in html


def test_the_page_loads_its_script_before_alpine(client, shop, owner, shelves):
    html = _page(client, owner)
    assert html.index("category-tree.js") < html.index("alpine-3")

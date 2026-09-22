"""
Shelves inside shelves: Drinks > Soda > Bottles.

Three levels, because that is how a duka files stock and because the till
has room for two rows of tabs. The rules worth holding: nothing goes four
deep, nothing goes inside itself, and picking a shelf finds what is under it.
"""

import io

import pytest
from django.urls import reverse

from apps.catalog.imports import import_products
from apps.catalog.models import CATEGORY_DEPTH, Category, Product
from apps.catalog.services import (
    category_family,
    category_parents,
    category_tree,
)
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def shelves(shop):
    """Drinks > Soda > Bottles, and a Food shelf beside it."""
    with tenant_context(shop):
        drinks = Category.objects.create(name="Drinks")
        soda = Category.objects.create(name="Soda", parent=drinks)
        bottles = Category.objects.create(name="Bottles", parent=soda)
        cans = Category.objects.create(name="Cans", parent=soda)
        food = Category.objects.create(name="Food")
    return {"drinks": drinks, "soda": soda, "bottles": bottles,
            "cans": cans, "food": food}


def test_a_category_knows_where_it_sits(shop, shelves):
    with tenant_context(shop):
        bottles = Category.objects.get(pk=shelves["bottles"].pk)
        assert bottles.level == 3
        assert bottles.path_label == Category.SEPARATOR.join(["Drinks", "Soda", "Bottles"])
        assert bottles.top.name == "Drinks"
        assert shelves["food"].level == 1


def test_the_tree_puts_a_parent_before_what_is_inside_it(shop, shelves):
    with tenant_context(shop):
        rows = category_tree()
    assert [(row.name, row.level) for row in rows] == [
        ("Drinks", 1), ("Soda", 2), ("Bottles", 3), ("Cans", 3), ("Food", 1),
    ]


def test_choosing_a_shelf_finds_everything_under_it(shop, shelves):
    """Picking Drinks has to find the soda and the bottles, or filing
    things properly makes the list look empty."""
    with tenant_context(shop):
        family = category_family(shelves["drinks"].pk)
    assert family == {shelves[k].pk for k in ("drinks", "soda", "bottles", "cans")}


def test_nothing_may_be_put_four_deep(shop, shelves, owner, client):
    client.force_login(owner)
    response = client.post(
        reverse("catalog:taxonomy"),
        {"kind": "category", "name": "Half litre", "parent": shelves["bottles"].pk},
        follow=True,
    )
    assert "three deep" in response.content.decode()
    with tenant_context(shop):
        assert not Category.objects.filter(name="Half litre").exists()


def test_the_places_a_category_may_move_to(shop, shelves):
    """Soda has Bottles inside it, so it needs two levels of room: a top
    shelf will take it, one that is already two deep will not."""
    with tenant_context(shop):
        Category.objects.create(name="Flour", parent=shelves["food"])
        soda = Category.objects.get(pk=shelves["soda"].pk)
        offered = {row.name for row in category_parents(exclude=soda, height=2)}

    # Drinks is where it already is, Food is the other top shelf. Not Flour:
    # Soda inside it would put Bottles four deep. Not Soda, Bottles or Cans:
    # those are itself and its own.
    assert offered == {"Drinks", "Food"}


def test_a_category_cannot_be_moved_inside_itself(shop, shelves, owner, client):
    client.force_login(owner)
    response = client.post(
        reverse("catalog:taxonomy_edit", args=["category", shelves["drinks"].pk]),
        {"name": "Drinks", "parent": shelves["bottles"].pk},
    )
    assert "inside itself" in response.content.decode()
    with tenant_context(shop):
        assert Category.objects.get(pk=shelves["drinks"].pk).parent_id is None


def test_a_shelf_with_shelves_inside_it_is_not_removed_silently(
        shop, shelves, owner, client):
    """`parent` is SET_NULL: removing Drinks would tip Soda out onto the top
    level and tell nobody."""
    client.force_login(owner)
    response = client.post(
        reverse("catalog:taxonomy_delete", args=["category", shelves["drinks"].pk]),
        follow=True,
    )
    assert "Remove what is inside" in response.content.decode()
    with tenant_context(shop):
        assert Category.objects.filter(pk=shelves["drinks"].pk).exists()
        assert Category.objects.get(pk=shelves["soda"].pk).parent_id == shelves["drinks"].pk


def test_the_product_list_filters_down_the_whole_shelf(
        shop, main_branch, shelves, owner, client, stocked):
    with tenant_context(shop, branch=main_branch):
        soda = Product.objects.filter(name="Soda 500ml").first()
        soda.category = shelves["bottles"]
        soda.save(update_fields=["category"])

    client.force_login(owner)
    page = client.get(reverse("catalog:product_list"), {"category": shelves["drinks"].pk})
    assert "Soda 500ml" in page.content.decode()


def test_a_path_in_a_spreadsheet_becomes_three_categories(shop, main_branch):
    csv = ("name,sku,barcode,category,unit,price,cost,qty\n"
           "Coca-Cola 500ml,,,Drinks > Soda > Bottles,pc,1000,,0\n")
    with tenant_context(shop, branch=main_branch):
        result = import_products(io.BytesIO(csv.encode()), branch=main_branch)
        assert result.created == 1
        product = Product.objects.get(name="Coca-Cola 500ml")
        assert product.category.path_label == Category.SEPARATOR.join(
            ["Drinks", "Soda", "Bottles"])
        # And not a fourth level, whatever the spreadsheet says.
        import_products(
            io.BytesIO(b"name,sku,barcode,category,unit,price,cost,qty\n"
                       b"Fanta,,,A > B > C > D,pc,1000,,0\n"),
            branch=main_branch)
        assert Product.objects.get(name="Fanta").category.level == CATEGORY_DEPTH


def test_the_export_writes_the_path_back_out(shop, main_branch, shelves, stocked):
    from apps.catalog.imports import export_products

    with tenant_context(shop, branch=main_branch):
        soda = Product.objects.filter(name="Soda 500ml").first()
        soda.category = shelves["bottles"]
        soda.save(update_fields=["category"])
        csv = export_products(branch=main_branch)
    assert "Drinks > Soda > Bottles" in csv


def test_the_demo_shop_has_shelves_inside_shelves(db):
    """The demo is where most people first see the second row of tabs."""
    from django.core.management import call_command

    from apps.core.context import unscoped
    from apps.tenancy.models import Tenant

    call_command("seed_demo", "--force", verbosity=0)
    with unscoped():
        demo = Tenant.objects.filter(name__icontains="Salma").first()
    with tenant_context(demo):
        rows = {row.path_label: row.level for row in category_tree()}
        assert rows[Category.SEPARATOR.join(["Vinywaji", "Soda", "Chupa"])] == 3
        soda = Category.objects.get(name="Soda")
        assert Product.objects.filter(category__in=category_family(soda.pk)).exists()


def test_a_loop_in_the_data_does_not_hang_a_page(shop, shelves, owner, client):
    """
    The forms refuse to make one, but a bad import or a hand-edited database
    can: Drinks inside Soda inside Drinks. Every page that walks the shelves
    has to come back rather than spin.
    """
    from django.db import connection

    with tenant_context(shop):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catalog_category SET parent_id = %s WHERE id = %s",
                [shelves["bottles"].pk, shelves["drinks"].pk],
            )
        drinks = Category.objects.get(pk=shelves["drinks"].pk)
        # Each of these walks parents, and each has to stop.
        assert drinks.level <= CATEGORY_DEPTH + 1
        assert drinks.path_label
        assert category_family(shelves["drinks"].pk)
        # The loop is unreachable from the top, so it is simply not listed.
        assert all(row.level <= CATEGORY_DEPTH for row in category_tree())

    client.force_login(owner)
    assert client.get(reverse("catalog:taxonomy")).status_code == 200
    assert client.get(reverse("catalog:product_list")).status_code == 200


def test_one_shop_never_sees_another_shop_s_shelves(shop, shelves, django_user_model):
    from apps.tenancy.services import create_tenant

    asha = django_user_model.objects.create_user(
        "asha@example.test", "pw", name="Asha")
    other, _ = create_tenant(name="Duka la Asha", owner=asha,
                             plan=shop.subscription.plan)
    with tenant_context(other):
        Category.objects.create(name="Mahitaji")
        mine = {row.name for row in category_tree()}
    assert mine == {"Mahitaji"}

    with tenant_context(shop):
        assert "Mahitaji" not in {row.name for row in category_tree()}
        # And the family of my own shelf cannot reach across either.
        assert Category.objects.filter(
            pk__in=category_family(shelves["drinks"].pk)).count() == 4


def test_the_products_page_does_not_cost_a_query_per_shelf(
        django_capture_on_commit_callbacks, shop, main_branch, shelves, owner,
        client, stocked):
    """
    The filter lists every shelf and the query walks down one. What matters
    is not the number itself -- a page has a sidebar and a session to load
    too -- but that it does not grow as a shop adds shelves.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.force_login(owner)
    url = reverse("catalog:product_list")
    filtered = {"category": shelves["drinks"].pk}

    client.get(url, filtered)                       # warm caches and the nav
    with CaptureQueriesContext(connection) as few:
        client.get(url, filtered)

    with tenant_context(shop):
        for i in range(12):
            Category.objects.create(name=f"Shelf {i}", parent=shelves["food"])

    with CaptureQueriesContext(connection) as many:
        assert client.get(url, filtered).status_code == 200

    assert len(many) == len(few), f"{len(few)} -> {len(many)} with 12 more shelves"


def test_a_shelf_can_be_made_while_adding_the_product(shop, main_branch, owner, client):
    """
    Sending an owner to Settings to invent "Food" before they can save sugar
    is the errand that ends with every product in one category. Typed beside
    a chosen shelf, the new one goes inside it.
    """
    from apps.catalog.models import TaxRate, Unit

    client.force_login(owner)
    with tenant_context(shop, branch=main_branch):
        piece = Unit.objects.get(code="pc")
        vat = TaxRate.objects.filter(is_default=True).first()
        drinks = Category.objects.create(name="Vinywaji")

    def add(name, category, fresh):
        return client.post(reverse("catalog:product_create"), {
            "name": name, "sku": "", "barcode": "", "category": category or "",
            "new_category": fresh, "brand": "", "base_unit": piece.pk,
            "tax_rate": vat.pk, "description": "", "price": "1000", "cost": "",
            "opening_qty": "", "reorder_level": "", "min_price": "",
            "track_stock": "on", "sellable_at_pos": "on", "is_active": "on",
        }, follow=True)

    add("Soda 350ml", drinks.pk, "Soda")
    with tenant_context(shop, branch=main_branch):
        soda = Category.objects.get(name="Soda")
        assert soda.parent_id == drinks.pk           # inside the one chosen
        assert Product.objects.get(name="Soda 350ml").category_id == soda.pk

    # Nothing chosen above: a new top shelf.
    add("Sabuni", "", "Nyumbani")
    with tenant_context(shop, branch=main_branch):
        assert Category.objects.get(name="Nyumbani").parent_id is None

    # And it still refuses a fourth level.
    with tenant_context(shop, branch=main_branch):
        bottles = Category.objects.create(
            name="Chupa", parent=Category.objects.get(name="Soda"))
    response = add("Nusu lita", bottles.pk, "Ndogo")
    assert "as deep as a category goes" in response.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not Category.objects.filter(name="Ndogo").exists()

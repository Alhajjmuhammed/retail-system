"""
A product can be read on its own page, and the brand and supplier lists on
the product form say where they come from -- with a brand made in place.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def test_the_product_list_has_a_view_action(client, owner, stocked):
    client.force_login(owner)
    html = client.get(reverse("catalog:product_list")).content.decode()
    assert reverse("catalog:product_detail", args=[stocked["Mkate"].product.pk]) in html


def test_the_view_page_shows_the_product(client, shop, owner, stocked):
    product = stocked["Mkate"].product
    with tenant_context(shop):
        product.min_price = Decimal("1400")
        product.factory_code = "23083007"
        product.save()
    client.force_login(owner)
    html = client.get(reverse("catalog:product_detail", args=[product.pk])).content.decode()
    assert product.name in html
    assert "23083007" in html
    assert "1,400" in html           # never sell below
    assert "1,500" in html           # its price
    assert "On the shelf" in html
    assert "Last stock movements" in html
    assert reverse("catalog:product_edit", args=[product.pk]) in html


def test_cost_only_for_those_who_may_see_it(client, shop, main_branch, stocked):
    from apps.accounts.models import Membership, Permission, Role, RolePermission, User

    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Shelf")
        RolePermission.objects.create(role=role,
                                      permission=Permission.objects.get(code="product.view"))
        person = User.objects.create_user("shelf@shop.test", "pw", name="Shelf")
        Membership.objects.create(tenant=shop, user=person, role=role)
    client.force_login(person)
    product = stocked["Mkate"].product
    response = client.get(reverse("catalog:product_detail", args=[product.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert "@ 1,100" not in html     # the cost
    assert reverse("catalog:product_edit", args=[product.pk]) not in html


def test_a_new_brand_is_made_and_chosen_from_the_product_form(shop):
    from apps.catalog.forms import ProductForm
    from apps.catalog.models import Brand, TaxRate, Unit

    with tenant_context(shop):
        data = {
            "name": "Butcher knife", "base_unit": Unit.objects.get(code="pc").pk,
            "tax_rate": TaxRate.objects.get(is_default=True).pk, "new_brand": "Athus",
            "is_active": "on", "track_stock": "on", "sellable_at_pos": "on",
            "discount_allowed": "on",
        }
        form = ProductForm(data)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["brand"].name == "Athus"
        # The same name in other capitals is the same brand.
        again = ProductForm({**data, "name": "Paring knife", "new_brand": "ATHUS"})
        assert again.is_valid(), again.errors
        assert again.cleaned_data["brand"] == form.cleaned_data["brand"]
        assert Brand.objects.filter(name__iexact="athus").count() == 1


def test_the_form_says_where_brands_and_suppliers_come_from(client, shop, owner):
    client.force_login(owner)
    html = client.get(reverse("catalog:product_create")).content.decode()
    assert "or add a new brand" in html
    assert reverse("purchasing:supplier_list") in html
    assert reverse("catalog:taxonomy") in html


def test_the_settings_card_names_brands(client, shop, owner):
    client.force_login(owner)
    html = client.get(reverse("org:settings_home")).content.decode()
    assert "Units, brands &amp; VAT" in html or "Units, brands & VAT" in html

"""
The demo shop the seeder builds.

It used to create only people: no products, no prices, no barcodes. Every
demo database therefore got its catalogue typed in by hand and drifted, which
is how the one on this machine ended up full of "Probe Hotel" and "Fix probe".
A demo you cannot rebuild is a demo that rots, so this pins what one contains.
"""

import pytest
from django.core.management import call_command

from apps.accounts.models import Membership, Role, User
from apps.catalog.models import Barcode, Price, PriceList, Product, QuickKey
from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.inventory.models import StockItem
from apps.org.models import Branch
from apps.purchasing.models import Supplier
from apps.tenancy.models import Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def demo(db):
    call_command("seed_demo", verbosity=0)
    return Tenant.objects.get(name="Duka la Salma")


def test_the_shop_has_two_branches_and_somebody_in_every_role(demo):
    with tenant_context(demo):
        assert Branch.objects.count() == 2
        people = {m.user.email: m.role.name for m in Membership.objects.all()}
    assert people == {
        "owner@demo.test": "Owner",
        "manager@demo.test": "Manager",
        "cashier@demo.test": "Cashier",
        "stock@demo.test": "Stock clerk",
        "phone@demo.test": "Phone seller",
        "buyer@demo.test": "Buyer",
        "books@demo.test": "Bookkeeper",
        "empty@demo.test": "New role",
    }


def test_everyone_can_sign_in_with_the_demo_password(demo):
    for email in ("owner@demo.test", "cashier@demo.test", "books@demo.test",
                  "platform@demo.test"):
        assert User.objects.get(email=email).check_password("demo12345")


def test_the_manager_can_approve_with_their_pin(demo):
    with tenant_context(demo):
        manager = Membership.objects.get(user__email="manager@demo.test")
    assert manager.check_pin("1234")


def test_a_role_that_holds_nothing_is_there_to_show(demo):
    with tenant_context(demo):
        empty = Role.objects.get(name="New role")
        assert empty.permissions.count() == 0


def test_the_shelf_is_stocked_priced_and_countable(demo):
    with tenant_context(demo):
        assert Product.objects.count() == 8
        retail = PriceList.objects.get(is_default=True)
        # Everything has a price: an unpriced product cannot be sold at all.
        for product in Product.objects.all():
            assert Price.objects.filter(
                price_list=retail, variant=product.default_variant
            ).exists(), f"{product.name} has no price"
        assert StockItem.objects.filter(qty_on_hand__gt=0).count() >= 6


def test_there_is_something_to_scan_including_a_crate(demo):
    with tenant_context(demo):
        singles = Barcode.objects.filter(pack_quantity=1)
        assert singles.count() >= 6
        crate = Barcode.objects.get(pack_quantity=24)
        assert crate.variant.product.name == "Soda 500ml"


def test_the_things_with_no_barcode_can_still_be_sold(demo):
    """Tomatoes and charcoal have no barcode; without a tile they are unsellable."""
    with tenant_context(demo):
        no_code = [p for p in Product.objects.all()
                   if not Barcode.objects.filter(variant=p.default_variant).exists()]
        assert no_code, "the demo should show products that have no barcode"
        tiled = {q.variant.product_id for q in QuickKey.objects.all()}
        for product in no_code:
            assert product.pk in tiled, f"{product.name} has no barcode and no tile"


def test_a_customer_buys_on_account_at_wholesale_prices(demo):
    with tenant_context(demo):
        hotel = Customer.objects.get(name="Hoteli ya Baharini")
        assert hotel.credit_limit > 0
        assert hotel.price_list is not None
        sugar = Product.objects.get(name="Sukari 1kg").default_variant
        assert sugar.price_for(hotel.price_list) < sugar.price_for()


def test_there_is_a_supplier_to_buy_from(demo):
    with tenant_context(demo):
        assert Supplier.objects.count() == 1

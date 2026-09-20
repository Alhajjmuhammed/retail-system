"""
Isolation and plan limits.

If these break, one shop sees another's data, or a shop gets capacity it has
not paid for. Nothing else in the system matters more.
"""

import pytest

from apps.catalog.models import Product, TaxRate, Unit
from apps.core.context import tenant_context, unscoped
from apps.core.features import LIMIT_BRANCHES, LimitExceeded
from apps.org.models import Branch
from apps.tenancy.services import create_tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_shops(db, owner, cashier, business_plan, free_plan):
    salma, _ = create_tenant(name="Duka la Salma", owner=owner, plan=business_plan)
    juma, _ = create_tenant(name="Duka la Juma", owner=cashier, plan=free_plan)
    return salma, juma


def _make_product(tenant, name):
    with tenant_context(tenant):
        return Product.objects.create(
            name=name,
            base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True),
        )


def test_products_do_not_leak_between_tenants(two_shops):
    salma, juma = two_shops
    _make_product(salma, "Sukari 1kg")
    _make_product(juma, "Mkate")

    with tenant_context(salma):
        names = set(Product.objects.values_list("name", flat=True))
        assert names == {"Sukari 1kg"}

    with tenant_context(juma):
        names = set(Product.objects.values_list("name", flat=True))
        assert names == {"Mkate"}


def test_no_tenant_context_returns_nothing(two_shops):
    """
    A bug becomes an empty page, never another shop's data.

    This is deliberate: the manager returns none() rather than everything when
    it cannot tell whose request this is.
    """
    _make_product(two_shops[0], "Sukari 1kg")
    assert Product.objects.count() == 0


def test_unscoped_is_the_only_way_across_tenants(two_shops):
    _make_product(two_shops[0], "Sukari 1kg")
    _make_product(two_shops[1], "Mkate")

    with unscoped():
        assert Product.objects.count() == 2


def test_branch_limit_is_enforced_at_write_time(two_shops):
    """The Free plan allows one branch. The second is refused on creation."""
    _, juma = two_shops
    with tenant_context(juma):
        assert Branch.objects.count() == 1
        with pytest.raises(LimitExceeded) as excinfo:
            Branch.objects.create(name="Second shop")
        assert excinfo.value.limit_key == LIMIT_BRANCHES


def test_business_plan_allows_more_branches(two_shops):
    salma, _ = two_shops
    with tenant_context(salma):
        Branch.objects.create(name="Nungwi")
        Branch.objects.create(name="Paje")
        assert Branch.objects.count() == 3


def test_every_product_gets_a_default_variant(two_shops):
    product = _make_product(two_shops[0], "Sukari 1kg")
    with tenant_context(two_shops[0]):
        assert product.variants.count() == 1
        assert product.default_variant is not None


def test_tenant_slugs_do_not_collide(db, owner, cashier, business_plan):
    first, _ = create_tenant(name="Duka", owner=owner, plan=business_plan)
    second, _ = create_tenant(name="Duka", owner=cashier, plan=business_plan)
    assert first.slug != second.slug

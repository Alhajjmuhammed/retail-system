import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.core.context import tenant_context
from apps.tenancy.models import Plan
from apps.tenancy.services import create_tenant


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts with nothing cached: no lockouts, no stale permissions."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _seed(db):
    call_command("sync_permissions", verbosity=0)
    call_command("seed_plans", verbosity=0)
    from apps.accounts.models import PlatformRole

    PlatformRole.ensure_defaults()


@pytest.fixture
def owner(db):
    return User.objects.create_user("salma@example.com", "pw", name="Salma")


@pytest.fixture
def cashier(db):
    return User.objects.create_user("juma@example.com", "pw", name="Juma")


@pytest.fixture
def business_plan(db):
    return Plan.objects.get(code="business")


@pytest.fixture
def free_plan(db):
    return Plan.objects.get(code="free")


@pytest.fixture
def shop(db, owner, business_plan):
    tenant, _membership = create_tenant(
        name="Duka la Salma", owner=owner, plan=business_plan
    )
    return tenant


@pytest.fixture
def in_shop(shop, owner):
    with tenant_context(shop, user=owner):
        yield shop


@pytest.fixture
def main_branch(shop):
    from apps.core.context import tenant_context
    from apps.org.models import Branch

    with tenant_context(shop):
        return Branch.objects.get(name="Main")


@pytest.fixture
def register(shop, main_branch):
    from apps.core.context import tenant_context
    from apps.org.models import Register

    with tenant_context(shop):
        return Register.objects.get(branch=main_branch)


@pytest.fixture
def stocked(shop, main_branch):
    """Three products with stock and prices, ready to sell."""

    from apps.catalog.models import Price, PriceList, Product, TaxRate, Unit
    from apps.core.context import tenant_context
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement

    items = {}
    with tenant_context(shop, branch=main_branch):
        unit = Unit.objects.get(code="pc")
        vat = TaxRate.objects.get(is_default=True)
        retail = PriceList.objects.get(is_default=True)

        for name, price, cost in [
            ("Sukari 1kg", 3000, 2400),
            ("Mkate", 1500, 1100),
            ("Soda 500ml", 1000, 700),
        ]:
            product = Product.objects.create(name=name, base_unit=unit, tax_rate=vat)
            variant = product.default_variant
            Price.objects.create(price_list=retail, variant=variant, amount=price)
            record_movement(
                variant=variant, qty_delta=100,
                reason=MovementReason.PURCHASE, unit_cost=cost,
            )
            items[name] = variant
    return items

"""
The stock ledger.

Quantities, costing and the append-only guarantee. If costing drifts, every
margin figure in the system is wrong and nobody notices for months.
"""

from decimal import Decimal

import pytest

from apps.catalog.models import Product, TaxRate, Unit
from apps.core.context import tenant_context
from apps.inventory.models import MovementReason, StockItem, StockMovement
from apps.inventory.services import (
    InsufficientStock,
    adjust,
    quantity_of,
    rebuild_stock_items,
    receive_transfer,
    record_movement,
    send_transfer,
)
from apps.org.models import Branch, CostMethod, TenantSettings

pytestmark = pytest.mark.django_db


@pytest.fixture
def sugar(shop):
    with tenant_context(shop):
        product = Product.objects.create(
            name="Sukari 1kg",
            base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True),
        )
        return product.default_variant


@pytest.fixture
def main_branch(shop):
    with tenant_context(shop):
        return Branch.objects.get(name="Main")


def test_receiving_stock_raises_the_quantity(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        record_movement(
            variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=1800
        )
        assert quantity_of(sugar) == 100


def test_weighted_average_moves_only_on_the_way_in(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        record_movement(variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=1000)
        record_movement(variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=2000)

        item = StockItem.objects.get(variant=sugar)
        assert item.avg_cost == Decimal("1500.00")

        # Selling must not change what the remaining stock is worth.
        record_movement(variant=sugar, qty_delta=-50, reason=MovementReason.SALE)
        item.refresh_from_db()
        assert item.avg_cost == Decimal("1500.00")
        assert item.qty_on_hand == 150


def test_last_cost_method_is_honoured(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        settings_row = TenantSettings.objects.first()
        settings_row.cost_method = CostMethod.LAST_COST
        settings_row.save()

        record_movement(variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=1000)
        record_movement(variant=sugar, qty_delta=10, reason=MovementReason.PURCHASE, unit_cost=2200)

        assert StockItem.objects.get(variant=sugar).avg_cost == Decimal("2200.00")


def test_balance_after_is_stamped_on_every_movement(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        record_movement(variant=sugar, qty_delta=50, reason=MovementReason.PURCHASE, unit_cost=1000)
        record_movement(variant=sugar, qty_delta=-20, reason=MovementReason.SALE)

        balances = list(
            StockMovement.objects.order_by("id").values_list("balance_after", flat=True)
        )
        assert balances == [Decimal("50.000"), Decimal("30.000")]


def test_movements_cannot_be_edited_or_deleted(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        movement = record_movement(
            variant=sugar, qty_delta=10, reason=MovementReason.PURCHASE, unit_cost=100
        )
        movement.qty_delta = 999
        with pytest.raises(ValueError, match="append-only"):
            movement.save()
        with pytest.raises(ValueError, match="append-only"):
            movement.delete()


def test_overselling_is_allowed_when_the_shop_permits_it(shop, sugar, main_branch):
    """
    Offline tills oversell. Refusing afterwards is worse than a negative
    number: the money is taken and the customer has already gone.
    """
    with tenant_context(shop, branch=main_branch):
        record_movement(variant=sugar, qty_delta=-5, reason=MovementReason.SALE)
        assert quantity_of(sugar) == -5


def test_overselling_is_refused_when_the_shop_forbids_it(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        settings_row = TenantSettings.objects.first()
        settings_row.negative_stock_allowed = False
        settings_row.save()

        with pytest.raises(InsufficientStock):
            record_movement(variant=sugar, qty_delta=-5, reason=MovementReason.SALE)


def test_untracked_products_never_move_stock(shop, main_branch):
    with tenant_context(shop, branch=main_branch):
        service = Product.objects.create(
            name="Delivery",
            base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True),
            track_stock=False,
        )
        assert record_movement(
            variant=service.default_variant, qty_delta=-1, reason=MovementReason.SALE
        ) is None
        assert not StockMovement.objects.filter(variant__product=service).exists()


def test_adjustment_records_the_difference(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        record_movement(variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=1000)
        adjust(variant=sugar, new_qty=93, reason_text="Damaged in store")

        assert quantity_of(sugar) == 93
        last = StockMovement.objects.order_by("-id").first()
        assert last.qty_delta == Decimal("-7.000")
        assert last.note == "Damaged in store"


def test_transfer_leaves_when_sent_and_arrives_when_accepted(shop, sugar, main_branch):
    from apps.inventory.models import Transfer, TransferLine

    with tenant_context(shop, branch=main_branch):
        nungwi = Branch.objects.create(name="Nungwi")
        record_movement(variant=sugar, qty_delta=100, reason=MovementReason.PURCHASE, unit_cost=1000)

        transfer = Transfer.objects.create(
            reference="TR0001", from_branch=main_branch, to_branch=nungwi
        )
        TransferLine.objects.create(transfer=transfer, variant=sugar, qty_sent=30)

        send_transfer(transfer)
        assert quantity_of(sugar, main_branch) == 70
        # In transit: gone from one shop, not yet at the other.
        assert quantity_of(sugar, nungwi) == 0

        receive_transfer(transfer, counted={transfer.lines.first().pk: 28})
        assert quantity_of(sugar, nungwi) == 28
        # The two missing units stay visible as a difference on the line.
        assert transfer.lines.first().shortfall == Decimal("2.000")


def test_rebuild_repairs_a_drifted_cache(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        record_movement(variant=sugar, qty_delta=40, reason=MovementReason.PURCHASE, unit_cost=1000)

        StockItem.objects.filter(variant=sugar).update(qty_on_hand=999)
        assert quantity_of(sugar) == 999

        rebuild_stock_items(main_branch)
        assert quantity_of(sugar) == 40


def test_stock_is_per_branch_not_per_tenant(shop, sugar, main_branch):
    with tenant_context(shop, branch=main_branch):
        nungwi = Branch.objects.create(name="Nungwi")
        record_movement(variant=sugar, qty_delta=10, reason=MovementReason.PURCHASE, unit_cost=1000)
        record_movement(
            variant=sugar, qty_delta=4, reason=MovementReason.PURCHASE,
            unit_cost=1000, branch=nungwi,
        )
        assert quantity_of(sugar, main_branch) == 10
        assert quantity_of(sugar, nungwi) == 4

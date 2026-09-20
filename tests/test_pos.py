"""
Checkout.

Idempotency, costing snapshots, tax, stock, credit, voids and returns. The
idempotency test is the one that matters most: without it, a till that loses
its connection mid-sync charges a customer twice.
"""

import uuid
from decimal import Decimal

import pytest

from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.inventory.services import quantity_of
from apps.pos.models import AddedVia, PaymentMethod, Sale, SaleStatus
from apps.pos.services import (
    ShiftAlreadyOpen,
    add_to_cart,
    close_shift,
    complete_sale,
    create_return,
    hold_cart,
    new_cart,
    open_shift,
    record_cash_movement,
    void_sale,
)

pytestmark = pytest.mark.django_db


def sell(shop, branch, stocked, items, payments=None, **kwargs):
    cart = new_cart(branch=branch)
    for name, qty in items:
        add_to_cart(cart, stocked[name], qty=qty)
    if payments is None:
        payments = [{"method": PaymentMethod.CASH, "amount": cart.subtotal}]
    return complete_sale(cart, payments, **kwargs)


def test_a_sale_moves_stock_and_records_money(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Sukari 1kg", 2), ("Mkate", 1)])

        assert sale.total == Decimal("7500.00")
        assert quantity_of(stocked["Sukari 1kg"]) == 98
        assert quantity_of(stocked["Mkate"]) == 99
        assert sale.paid == Decimal("7500.00")


def test_vat_is_extracted_from_a_tax_inclusive_price(shop, main_branch, stocked, owner):
    """18% inclusive on 3,000 is about 457, not 540."""
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Sukari 1kg", 1)])
        assert sale.tax_total == Decimal("457.63")
        assert sale.total == Decimal("3000.00")


def test_cost_is_snapshotted_at_the_moment_of_sale(shop, main_branch, stocked, owner):
    """
    A later purchase at a different price must not rewrite past margin.
    """
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement

    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Sukari 1kg", 1)])
        original_cost = sale.lines.first().unit_cost
        assert original_cost == Decimal("2400.00")

        record_movement(
            variant=stocked["Sukari 1kg"], qty_delta=100,
            reason=MovementReason.PURCHASE, unit_cost=5000,
        )

        sale.refresh_from_db()
        assert sale.lines.first().unit_cost == original_cost


def test_the_same_sale_synced_twice_is_stored_once(shop, main_branch, stocked, owner):
    """
    The property that makes offline selling safe. A till retrying after a
    dropped connection must not charge the customer again.
    """
    with tenant_context(shop, branch=main_branch, user=owner):
        client_uuid = uuid.uuid4()

        first = sell(
            shop, main_branch, stocked, [("Soda 500ml", 3)],
            client_uuid=client_uuid, is_offline_origin=True,
        )
        second = sell(
            shop, main_branch, stocked, [("Soda 500ml", 3)],
            client_uuid=client_uuid, is_offline_origin=True,
        )

        assert first.pk == second.pk
        assert Sale.objects.count() == 1
        # And crucially: stock moved once, not twice.
        assert quantity_of(stocked["Soda 500ml"]) == 97


def test_scanning_the_same_item_twice_stacks_the_line(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1, added_via=AddedVia.SCAN)
        add_to_cart(cart, stocked["Mkate"], qty=1, added_via=AddedVia.SCAN)

        assert cart.lines.count() == 1
        assert cart.lines.first().qty == 2


def test_an_open_item_needs_no_catalogue_entry(shop, main_branch, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, None, qty=1, unit_price=5000, description="Carrier bag")

        sale = complete_sale(
            cart, [{"method": PaymentMethod.CASH, "amount": 5000}]
        )
        line = sale.lines.first()
        assert line.variant_id is None
        assert line.added_via == AddedVia.MANUAL
        assert sale.total == Decimal("5000.00")


def test_split_payment_across_cash_and_mobile_money(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(
            shop, main_branch, stocked, [("Sukari 1kg", 2)],
            payments=[
                {"method": PaymentMethod.CASH, "amount": 2000},
                {"method": PaymentMethod.MPESA, "amount": 4000, "reference": "QGR7X"},
            ],
        )
        assert sale.paid == Decimal("6000.00")
        assert sale.payments.count() == 2


def test_selling_on_credit_charges_the_customer_account(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", credit_limit=100000)
        cart = new_cart(branch=main_branch, customer=customer)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=2)

        sale = complete_sale(
            cart, [{"method": PaymentMethod.CREDIT, "amount": 6000}]
        )
        assert customer.balance == Decimal("6000.00")
        assert sale.customer_id == customer.pk


def test_credit_limit_is_visible_before_selling(shop, main_branch, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", credit_limit=10000)
        assert customer.can_take_credit(9000)
        assert not customer.can_take_credit(11000)


def test_voiding_puts_the_stock_back_and_keeps_the_sale(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Mkate", 4)])
        assert quantity_of(stocked["Mkate"]) == 96

        void_sale(sale, reason="Wrong item scanned")

        assert quantity_of(stocked["Mkate"]) == 100
        sale.refresh_from_db()
        # Marked, never deleted.
        assert sale.status == SaleStatus.VOIDED
        assert Sale.objects.filter(pk=sale.pk).exists()


def test_a_sale_cannot_be_voided_twice(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Mkate", 1)])
        void_sale(sale, reason="Mistake")
        with pytest.raises(ValueError):
            void_sale(sale, reason="Again")


def test_partial_return_restocks_only_what_came_back(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Soda 500ml", 5)])
        line = sale.lines.first()

        doc = create_return(sale, {line.pk: 2}, reason="Customer changed mind")

        assert doc.total == Decimal("2000.00")
        assert quantity_of(stocked["Soda 500ml"]) == 97
        sale.refresh_from_db()
        assert sale.status == SaleStatus.PART_REFUNDED


def test_returning_more_than_was_sold_is_refused(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Soda 500ml", 2)])
        line = sale.lines.first()
        with pytest.raises(ValueError, match="can still be returned"):
            create_return(sale, {line.pk: 5}, reason="Too many")


def test_damaged_returns_do_not_go_back_on_the_shelf(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Mkate", 3)])
        line = sale.lines.first()

        create_return(sale, {line.pk: 3}, reason="Mouldy", restock=False)
        assert quantity_of(stocked["Mkate"]) == 97


def test_shift_variance_is_recorded_against_the_cashier(shop, main_branch, register, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=50000)
        close_shift(shift, counted_cash=48000, note="Short")

        assert shift.expected_cash == Decimal("50000.00")
        assert shift.variance == Decimal("-2000.00")
        assert shift.user_id == owner.pk


def test_expected_cash_counts_sales_and_drawer_movements(
    shop, main_branch, register, stocked, owner
):
    from apps.pos.models import CashMovementKind

    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=10000)
        sell(shop, main_branch, stocked, [("Sukari 1kg", 1)], shift=shift)
        record_cash_movement(
            shift, kind=CashMovementKind.PAY_OUT, amount=-4000, reason="Boda fare"
        )

        close_shift(shift, counted_cash=9000)
        # 10,000 float + 3,000 sale - 4,000 paid out = 9,000 expected.
        assert shift.expected_cash == Decimal("9000.00")
        assert shift.variance == Decimal("0.00")


def test_one_till_cannot_have_two_open_shifts(shop, main_branch, register, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register, opening_float=0)
        with pytest.raises(ShiftAlreadyOpen):
            open_shift(register=register, opening_float=0)


def test_held_cart_gets_a_readable_handoff_code(shop, main_branch, stocked, owner):
    """The shelf handoff: built on a phone, collected at the till."""
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1, added_via=AddedVia.CAMERA)

        code = hold_cart(cart)
        assert len(code) == 4
        assert code.isupper()
        # No characters a cashier could misread aloud.
        assert not set(code) & set("BIOS0125")


def test_sale_numbers_are_sequential_per_branch_per_day(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        first = sell(shop, main_branch, stocked, [("Mkate", 1)])
        second = sell(shop, main_branch, stocked, [("Mkate", 1)])

        assert first.number[:-4] == second.number[:-4]
        assert int(second.number[-4:]) == int(first.number[-4:]) + 1


def test_margin_uses_the_snapshot_not_current_cost(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = sell(shop, main_branch, stocked, [("Sukari 1kg", 1)])
        # 3,000 gross - 457.63 VAT - 2,400 cost
        assert sale.margin == Decimal("142.37")

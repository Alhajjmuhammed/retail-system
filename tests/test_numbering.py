"""
Document numbers under contention.

Numbers are read-max-then-increment. Two tills selling in the same second
both read the same maximum and build the same number, and the unique
constraint rejects the second. The customer has already paid, so losing that
sale is the one failure this system must never produce.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError

from apps.core.context import tenant_context
from apps.core.numbering import save_with_number
from apps.pos.models import PaymentMethod, Sale
from apps.pos.services import add_to_cart, complete_sale, new_cart

pytestmark = pytest.mark.django_db


def test_a_taken_number_is_reallocated_not_rejected(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        first = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])

        # Hand the generator the number that is already taken, exactly as a
        # second till reading the same maximum would.
        taken = first.number
        attempts = []

        def generate():
            attempts.append(len(attempts))
            return taken if len(attempts) == 1 else taken[:-4] + "9999"

        second = Sale(
            tenant=shop, branch=main_branch, user=owner,
            subtotal=Decimal("1500"), total=Decimal("1500"),
        )
        save_with_number(second, field="number", generate=generate)

        assert len(attempts) == 2, "the first number should have been rejected"
        assert second.pk is not None
        assert second.number != first.number
        assert Sale.objects.count() == 2


def test_two_sales_in_the_same_second_both_survive(shop, main_branch, stocked, owner):
    """
    The realistic version: two registers selling at once. Both sales must
    exist afterwards, with different numbers.
    """
    with tenant_context(shop, branch=main_branch, user=owner):
        sales = []
        for _ in range(4):
            cart = new_cart(branch=main_branch)
            add_to_cart(cart, stocked["Soda 500ml"], qty=1)
            sales.append(
                complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1000}])
            )

        numbers = [s.number for s in sales]
        assert len(set(numbers)) == 4, numbers
        assert Sale.objects.count() == 4


def test_a_retried_offline_sale_still_resolves_to_the_stored_one(
    shop, main_branch, stocked, owner
):
    """
    Number retry must not defeat idempotency: a duplicate client_uuid still
    returns the sale already stored instead of allocating a new number.
    """
    import uuid

    with tenant_context(shop, branch=main_branch, user=owner):
        client_uuid = uuid.uuid4()

        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=2)
        first = complete_sale(
            cart, [{"method": PaymentMethod.CASH, "amount": 3000}],
            client_uuid=client_uuid,
        )

        cart2 = new_cart(branch=main_branch)
        add_to_cart(cart2, stocked["Mkate"], qty=2)
        second = complete_sale(
            cart2, [{"method": PaymentMethod.CASH, "amount": 3000}],
            client_uuid=client_uuid,
        )

        assert first.pk == second.pk
        assert Sale.objects.count() == 1


def test_it_gives_up_rather_than_looping_forever(shop, main_branch, owner):
    """A generator that can never succeed must raise, not spin."""
    with tenant_context(shop, branch=main_branch, user=owner):
        taken = "FIXED0001"
        Sale.objects.create(
            tenant=shop, branch=main_branch, user=owner, number=taken,
            subtotal=Decimal("0"), total=Decimal("0"),
        )

        with pytest.raises(IntegrityError):
            save_with_number(
                Sale(tenant=shop, branch=main_branch, user=owner),
                field="number",
                generate=lambda: taken,
                attempts=3,
            )

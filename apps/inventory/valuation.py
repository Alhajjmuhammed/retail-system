"""
What a quantity of stock is worth, for permission limits.

At cost alone, anything with no cost price was worth nothing, so any amount
of it passed every limit: a clerk capped at 100,000 added a million units of
a 50,000 item. The higher of cost and selling price is the honest ceiling.
"""

from decimal import Decimal


def unit_value(variant, cost=None) -> Decimal:
    price = variant.price_for() if variant is not None else None
    return max(Decimal(cost or 0), Decimal(price or 0))


def typed_unit_value(variant) -> Decimal:
    """
    The same, ignoring a cost the person typed on this very form.

    A cost is only trustworthy when it comes from the shop's own records.
    Typing 0.01 on a new product with no price made a million units worth
    10,000 and slipped under an adjustment limit.
    """
    price = variant.price_for() if variant is not None else None
    return Decimal(price or 0)


def stock_value(variant, qty, cost=None) -> Decimal:
    return abs(Decimal(qty)) * unit_value(variant, cost)


def limit_value(variant, qty, cost=None, *, trust_cost=True) -> Decimal:
    """
    The figure a permission limit is checked against.

    A thing with neither a cost nor a price has no known value; it is treated
    as over any limit rather than as free. ``trust_cost=False`` is for a cost
    the same person typed on the form being checked -- it cannot also be the
    measure of what they are allowed to do.
    """
    from apps.core.parsing import LARGEST

    if not qty:
        return Decimal("0")
    unit = unit_value(variant, cost) if trust_cost else typed_unit_value(variant)
    return abs(Decimal(qty)) * unit if unit else LARGEST

"""
Loyalty.

A plan feature shops pay for that had no code behind it at all. Points are a
ledger like everything else that matters: earned on a sale, redeemed against
one, and the balance is the sum -- never a stored number that can drift.

Attached through events, so the checkout code knows nothing about it.
"""

from decimal import Decimal

from django.db import transaction

from apps.core.events import SALE_COMPLETED, SALE_RETURNED, SALE_VOIDED, events
from apps.core.features import LOYALTY
from apps.customers.models import LoyaltyKind, LoyaltyTransaction


def points_for(amount, *, per_unit=1000) -> int:
    """One point per 1,000 spent, rounded down. A shop setting later."""
    return int(Decimal(str(amount)) // per_unit)


@transaction.atomic
def award(sale):
    customer = sale.customer
    if customer is None or not sale.tenant.has_feature(LOYALTY):
        return None

    points = points_for(sale.total)
    if points <= 0:
        return None

    return LoyaltyTransaction.objects.create(
        tenant=sale.tenant, customer=customer, kind=LoyaltyKind.EARN,
        points=points, sale=sale,
        note=f"Earned on {sale.number}",
    )


@transaction.atomic
def redeem(customer, points, *, note="", sale=None):
    """Spend points. Refuses to take more than they have."""
    points = int(points)
    if points <= 0:
        raise ValueError("Nothing to redeem.")
    if points > customer.loyalty_points:
        raise ValueError(
            f"{customer.name} has {customer.loyalty_points} points, not {points}."
        )

    return LoyaltyTransaction.objects.create(
        tenant=customer.tenant, customer=customer, kind=LoyaltyKind.REDEEM,
        points=-points, sale=sale, note=note or "Redeemed",
    )


@events.on(SALE_COMPLETED)
def award_on_sale(sale=None, **kwargs):
    if sale is not None:
        award(sale)


@events.on(SALE_VOIDED)
def take_back_on_void(sale=None, **kwargs):
    """A voided sale takes its points with it."""
    if sale is None or sale.customer is None:
        return
    earned = LoyaltyTransaction.objects.filter(
        sale=sale, kind=LoyaltyKind.EARN
    ).first()
    if earned is None:
        return
    points = min(earned.points, max(sale.customer.loyalty_points, 0))
    if points <= 0:
        return
    LoyaltyTransaction.objects.create(
        tenant=sale.tenant, customer=sale.customer, kind=LoyaltyKind.ADJUST,
        points=-points, sale=sale, note=f"Void of {sale.number}",
    )


@events.on(SALE_RETURNED)
def take_back_on_return(sale=None, doc=None, **kwargs):
    """
    A refund takes back the points that part of the sale earned.

    Worked out from what is left of the sale, not refund by refund: rounding
    each refund down on its own left points behind after a full refund.
    Never below zero -- points already spent cannot be un-spent, only lost.
    """
    from django.db.models import Sum

    if sale is None or sale.customer is None or not sale.total:
        return
    earned = LoyaltyTransaction.objects.filter(sale=sale, kind=LoyaltyKind.EARN).first()
    if earned is None:
        return
    refunded = sale.returns.aggregate(total=Sum("total"))["total"] or Decimal("0")
    kept = max(sale.total - refunded, Decimal("0"))
    should_have = int(Decimal(earned.points) * kept / sale.total)
    taken = -(LoyaltyTransaction.objects.filter(sale=sale, kind=LoyaltyKind.ADJUST)
              .aggregate(total=Sum("points"))["total"] or 0)
    points = earned.points - taken - should_have
    points = min(points, max(sale.customer.loyalty_points, 0))
    if points <= 0:
        return
    LoyaltyTransaction.objects.create(
        tenant=sale.tenant, customer=sale.customer, kind=LoyaltyKind.ADJUST,
        points=-points, sale=sale,
        note=f"Refund {doc.number}" if doc is not None else f"Refund on {sale.number}",
    )

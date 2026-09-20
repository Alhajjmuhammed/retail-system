"""
What the server will accept from a till.

The sync endpoint is the only way a sale gets made, and it used to take the
device's word for everything: price, discount, quantity, payments. A cashier
with a 5% discount limit could post a 99.9% one; a negative quantity paid
with negative cash was a refund nobody authorised; a sale "on account" with
no customer was a debt nobody recorded.

Two tiers, because an offline sale is money already taken:
  * refused -- the entry cannot be a legitimate sale from our own till. It
    stays queued on the device, visible, for somebody to look at;
  * accepted for review -- plausible, but outside what the server allows
    today (a price that changed while the till was offline). Recorded, and
    flagged for the owner rather than silently rewritten.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.parsing import decimal_or_none
from apps.pos.models import PaymentMethod

MAX_QTY = Decimal("100000")
# Line and sale money columns hold 12 digits with 2 decimals. Bigger than this
# can only be a typo -- and used to fail at the database, every retry.
MAX_MONEY = Decimal("9999999999")
# Rounding between the till's floats and our Decimals: one cent, not one
# shilling -- a whole unit of slack could be taken on every sale unflagged.
TOLERANCE = Decimal("0.01")
OLDEST_SALE = timedelta(days=14)


class SaleRejected(ValueError):
    """This cannot be accepted as a sale. The message is shown on the till."""


@dataclass
class CheckedSale:
    client_uuid: str
    lines: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    customer: object = None
    sold_at: object = None
    review: list = field(default_factory=list)
    total: Decimal = Decimal("0")


def check_sale(membership, entry, *, price_list=None) -> CheckedSale:
    from apps.catalog.models import Variant
    from apps.customers.models import Customer

    if not isinstance(entry, dict):
        raise SaleRejected("Not a sale.")
    try:
        client_uuid = str(uuid.UUID(str(entry.get("client_uuid"))))
    except (TypeError, ValueError, AttributeError):
        raise SaleRejected("client_uuid is missing or malformed.") from None

    checked = CheckedSale(client_uuid=client_uuid)
    raw_lines = entry.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        raise SaleRejected("A sale needs at least one line.")

    # The customer first: their price list decides what "today's price" is
    # for every line below.
    if entry.get("customer_id"):
        from apps.core.parsing import int_or

        checked.customer = Customer.objects.select_related("price_list").filter(
            pk=int_or(entry["customer_id"])).first()
        if checked.customer is None:
            raise SaleRejected("That customer does not exist in this shop.")
        if not checked.customer.is_active:
            # A sale can reach the server after the customer was removed, so
            # it is kept -- but somebody should look at it.
            checked.review.append(f"{checked.customer.name} was removed from the customer list.")
    customer_list = None
    if checked.customer is not None and checked.customer.price_list_id and \
            checked.customer.price_list.is_active:
        customer_list = checked.customer.price_list
        if not membership.can("pos.price_override"):
            # Choosing a wholesale customer is a way of choosing their
            # prices. Somebody who may not set prices can still do it for a
            # real customer -- but the owner is told it happened.
            checked.review.append(
                f"{customer_list.name} prices used for {checked.customer.name}."
            )

    used_basket_lines = set()
    discount = membership.check_permission("pos.discount")
    may_override_price = membership.can("pos.price_override")

    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise SaleRejected("Malformed line.")
        qty = decimal_or_none(raw.get("qty", 1))
        price = decimal_or_none(raw.get("unit_price", 0))
        line_discount = decimal_or_none(raw.get("discount", 0)) or Decimal("0")
        if qty is None or qty <= 0 or qty > MAX_QTY:
            raise SaleRejected("Every quantity must be above zero. Refunds go through Returns.")
        if price is None or price < 0:
            raise SaleRejected("A price is missing or negative.")
        gross = qty * price
        if gross > MAX_MONEY:
            raise SaleRejected("A line is too large to be a real sale.")
        if line_discount < 0 or line_discount > gross:
            raise SaleRejected("A discount is negative or bigger than the line.")

        variant = None
        if raw.get("variant_id"):
            variant = (
                Variant.objects.select_related("product")
                .filter(pk=decimal_or_none(raw.get("variant_id")) or 0).first()
            )
            if variant is None:
                raise SaleRejected("A product on this sale does not exist in this shop.")
        elif raw.get("cart_line_id") not in used_basket_lines and _collected_open_item(raw, price, qty):
            used_basket_lines.add(raw.get("cart_line_id"))
            # Built on a phone by somebody allowed to, and collected at this
            # till: their permission already applied when the basket was made.
        else:
            limit = membership.check_permission("pos.open_item", value=gross)
            if not limit:
                raise SaleRejected(
                    "Open items are not allowed for this cashier"
                    + (f" above {limit.limit:,.0f}." if limit.limit else ".")
                )

        if line_discount > 0:
            if variant is not None and not variant.product.discount_allowed:
                raise SaleRejected(f"{variant} may not be discounted.")
            percent = (line_discount / gross * 100) if gross else Decimal("100")
            if not discount or (discount.limit is not None and percent > discount.limit + Decimal("0.01")):
                raise SaleRejected(
                    f"A {percent:.1f}% discount is above what this cashier may give."
                )

        if variant is not None and not variant.product.base_unit.allows_decimal \
                and qty != qty.to_integral_value():
            # Half a bottle is not a sale. The column rounds to three places,
            # so 0.0004 stored as 0.000 and the sale totalled nothing.
            raise SaleRejected(
                f"{variant} is sold in whole {variant.product.base_unit.code}."
            )

        if variant is not None:
            current = variant.price_for(customer_list, qty) if customer_list else None
            if current is None:
                current = variant.price_for(price_list, qty)
            if current is None and not may_override_price:
                checked.review.append(f"{variant} has no price on file; sold at {price:,.2f}.")
            elif current is not None and price + TOLERANCE < current and not may_override_price:
                checked.review.append(
                    f"{variant} sold at {price:,.2f}; today's price is {current:,.2f}."
                )
            floor = variant.product.min_price
            if floor and price + TOLERANCE < floor:
                checked.review.append(f"{variant} sold below its minimum price of {floor:,.2f}.")

        checked.lines.append({
            "variant": variant, "qty": qty, "unit_price": price,
            "discount": line_discount,
            "description": str(raw.get("description", ""))[:160],
            "added_via": str(raw.get("added_via", "scan"))[:12],
        })
        checked.total += gross - line_discount

    paid = Decimal("0")
    methods = set(PaymentMethod.values)
    for raw in entry.get("payments") or []:
        if not isinstance(raw, dict):
            raise SaleRejected("Malformed payment.")
        method = raw.get("method", PaymentMethod.CASH)
        amount = decimal_or_none(raw.get("amount", 0))
        change = decimal_or_none(raw.get("change_given", 0)) or Decimal("0")
        if method not in methods:
            raise SaleRejected(f"Unknown payment method: {method}.")
        if amount is None or amount < 0 or change < 0 or amount > MAX_MONEY or change > MAX_MONEY:
            raise SaleRejected("A payment is negative, not a number, or impossibly large.")
        if amount == 0:
            continue  # a free item needs no payment line
        paid += amount
        checked.payments.append({
            "method": method, "amount": amount,
            "reference": str(raw.get("reference", ""))[:60], "change_given": change,
        })
    if paid + TOLERANCE < checked.total:
        raise SaleRejected(f"Paid {paid:,.0f} of {checked.total:,.0f}.")
    # Only cash can be over-tendered (and change given). Mobile money, card or
    # credit beyond the total is not a payment -- credit of 400,000 on a
    # 1,000 sale put 400,000 of debt on a customer's account.
    not_cash = sum((pay["amount"] for pay in checked.payments
                    if pay["method"] != PaymentMethod.CASH), Decimal("0"))
    if not_cash > checked.total + TOLERANCE:
        raise SaleRejected("More was paid by card, mobile money or on account than the sale costs.")
    # A payment's amount is what the shop keeps. Cash handed over beyond the
    # total is change: stored that way, or the drawer would expect money
    # that went straight back to the customer -- and a refund could pay it
    # out a second time.
    excess = paid - checked.total
    if excess > TOLERANCE:
        for pay in checked.payments:
            if pay["method"] == PaymentMethod.CASH and excess > 0:
                moved = min(excess, pay["amount"])
                pay["amount"] -= moved
                pay["change_given"] += moved
                excess -= moved
        checked.payments = [pay for pay in checked.payments if pay["amount"] > 0]

    # Credit is judged on the whole amount put on account, not per payment
    # line: two lines of 450,000 used to pass a 500,000 ceiling.
    credit = sum((pay["amount"] for pay in checked.payments
                  if pay["method"] == PaymentMethod.CREDIT), Decimal("0"))
    if credit > 0:
        if checked.customer is None:
            raise SaleRejected("A sale on account needs a customer.")
        if not membership.check_permission("credit.grant", value=credit):
            raise SaleRejected("This cashier may not give that much credit.")
        if not checked.customer.can_take_credit(credit):
            checked.review.append(
                f"{checked.customer.name} went over their credit limit of "
                f"{checked.customer.credit_limit:,.0f} by taking {credit:,.0f} on account."
            )

    now = timezone.now()
    sold_at = parse_datetime(str(entry.get("sold_at") or "")) if entry.get("sold_at") else None
    if sold_at is None:
        sold_at = now
    if timezone.is_naive(sold_at):
        sold_at = timezone.make_aware(sold_at)
    if sold_at > now + timedelta(minutes=5):
        checked.review.append(f"The till's clock said {sold_at:%d %b %H:%M}, in the future.")
        sold_at = now
    elif sold_at < now - OLDEST_SALE:
        raise SaleRejected("This sale is more than two weeks old. Enter it by hand if it is real.")
    checked.sold_at = sold_at
    return checked


def _collected_open_item(raw, price, qty):
    """An open item that came from a phone basket this shop collected."""
    from apps.pos.models import CartLine, CartStatus

    line_id = decimal_or_none(raw.get("cart_line_id"))
    if line_id is None:
        return False
    # Only a basket that was really built on a phone and collected by code.
    # Every till sale also leaves a converted cart, so its open items were
    # vouchers too: a cashier with no open-item right could ring one again.
    line = CartLine.objects.select_for_update().filter(
        pk=line_id, variant__isnull=True, cart__status=CartStatus.CONVERTED,
        sold_in__isnull=True,
    ).exclude(cart__handoff_code="").first()
    return bool(line and line.unit_price == price and qty <= line.qty)


def mark_basket_lines_sold(entry, sale):
    """Once sold, a basket's open item cannot vouch for another sale."""
    from apps.pos.models import CartLine

    ids = [decimal_or_none(raw.get("cart_line_id")) for raw in entry.get("lines") or []
           if isinstance(raw, dict)]
    ids = [int(i) for i in ids if i is not None]
    if ids:
        CartLine.objects.filter(pk__in=ids, sold_in__isnull=True).update(sold_in=sale)

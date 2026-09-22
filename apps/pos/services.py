"""
Checkout.

``complete_sale`` is the one path from a basket to a sale. It is idempotent on
``client_uuid``, so an offline till that retries a sync five times stores it
once -- the single property that makes selling offline safe rather than
merely possible.
"""

import secrets
from decimal import ROUND_HALF_UP, Decimal

from django.db import models, transaction
from django.utils import timezone

from apps.core.context import get_current_branch, get_current_tenant, get_current_user
from apps.core.events import (
    CASH_VARIANCE,
    CREDIT_EXTENDED,
    SALE_COMPLETED,
    SALE_RETURNED,
    SALE_VOIDED,
    SHIFT_CLOSED,
    SHIFT_OPENED,
    events,
)
from apps.core.numbering import save_with_number
from apps.inventory.models import MovementReason
from apps.inventory.services import pick_batch, record_movement
from apps.pos.models import (
    AddedVia,
    Cart,
    CartLine,
    CartStatus,
    CashMovement,
    FiscalReceipt,
    FiscalStatus,
    PaymentMethod,
    Return,
    ReturnLine,
    Sale,
    SaleLine,
    SalePayment,
    SaleStatus,
    Shift,
    ShiftStatus,
)

ZERO = Decimal("0")
MONEY = Decimal("0.01")

# Currencies with no coin smaller than the unit. A shilling is the smallest
# thing a drawer can hold, so a sale of 312.50 is not a sale anybody can pay
# or give change for: the receipt said 313, the till expected 312.50, and
# every cash-up carried the difference as a variance nobody could explain.
# Selling a quarter kilo of sugar at 1,250 is how a duka trades all day.
WHOLE_UNIT_CURRENCIES = {
    "TZS", "UGX", "RWF", "BIF", "KMF", "DJF", "GNF", "MGA", "XAF", "XOF",
    "XPF", "JPY", "KRW", "VND", "CLP", "ISK", "PYG",
}


def quantum(currency=None) -> Decimal:
    """The smallest amount this shop's money comes in."""
    if currency is None:
        from apps.core.context import get_current_tenant

        tenant = get_current_tenant()
        currency = getattr(tenant, "currency", None)
    return Decimal("1") if (currency or "").upper() in WHOLE_UNIT_CURRENCIES else MONEY


def money(value, currency=None) -> Decimal:
    """
    An amount this shop can actually take, to the smallest coin it has.

    Rounded half up, so the shopkeeper is never a coin short on a sale they
    have already handed over.
    """
    return Decimal(str(value)).quantize(quantum(currency), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Shifts
# --------------------------------------------------------------------------

def open_shift(*, register, user=None, opening_float=0, branch=None):
    user = user or get_current_user()
    branch = branch or get_current_branch()

    existing = Shift.objects.filter(register=register, status=ShiftStatus.OPEN).first()
    if existing is not None:
        raise ShiftAlreadyOpen(existing)

    shift = Shift.objects.create(
        tenant=get_current_tenant(),
        branch=branch,
        register=register,
        user=user,
        opening_float=money(opening_float),
    )
    events.emit(SHIFT_OPENED, shift=shift)
    return shift


@transaction.atomic
def close_shift(shift: Shift, *, counted_cash, note="", user=None):
    """
    Count the drawer and record the difference.

    The variance is never quietly absorbed. It is stored with the cashier's
    name on it, because that number is the reason an owner trusts the system.
    """
    if not shift.is_open:
        raise ValueError("This shift is already closed.")

    expected = shift.compute_expected_cash()
    counted = money(counted_cash)

    shift.expected_cash = expected
    shift.counted_cash = counted
    shift.variance = counted - expected
    shift.variance_note = note
    shift.closed_at = timezone.now()
    shift.status = ShiftStatus.CLOSED
    shift.save(
        update_fields=[
            "expected_cash", "counted_cash", "variance", "variance_note",
            "closed_at", "status", "updated_at",
        ]
    )

    events.emit(SHIFT_CLOSED, shift=shift)
    if shift.variance != ZERO:
        events.emit(CASH_VARIANCE, shift=shift, variance=shift.variance)
    return shift


def record_cash_movement(shift, *, kind, amount, reason):
    """Amount is signed by the caller: pay-outs are negative."""
    return CashMovement.objects.create(
        tenant=get_current_tenant(), shift=shift, kind=kind,
        amount=money(amount), reason=reason,
    )


class ShiftAlreadyOpen(Exception):
    def __init__(self, shift):
        self.shift = shift
        super().__init__(f"{shift.register} already has an open shift ({shift.user}).")


# --------------------------------------------------------------------------
# Carts
# --------------------------------------------------------------------------

def new_cart(*, user=None, register=None, branch=None, customer=None, price_list=None):
    from apps.catalog.models import PriceList

    if price_list is None:
        price_list = (
            customer.price_list
            if customer and customer.price_list_id and customer.price_list.is_active
            else PriceList.objects.filter(is_default=True).first()
        )
    return Cart.objects.create(
        tenant=get_current_tenant(),
        branch=branch or get_current_branch(),
        register=register,
        user=user or get_current_user(),
        customer=customer,
        price_list=price_list,
    )


def add_to_cart(cart, variant, *, qty=1, added_via=AddedVia.SEARCH, unit_price=None,
                description="", discount=0, note=""):
    """
    Put something in the basket.

    Scanning the same thing twice adds to the existing line rather than making
    a second one -- what a cashier expects, and what keeps a receipt readable.
    """
    qty = Decimal(str(qty))

    if variant is None:
        # An open item: no catalogue entry, a typed price, permission-gated by
        # the caller because it is the biggest theft vector at any till.
        return CartLine.objects.create(
            tenant=cart.tenant, cart=cart, description=description or "Item",
            qty=qty, unit_price=money(unit_price or 0), tax_rate=ZERO,
            added_via=AddedVia.MANUAL, note=note,
        )

    if unit_price is None:
        # The cart's list (a customer's), else the usual price. Nothing on
        # either is not free: it used to go in at 0.
        unit_price = variant.price_for(cart.price_list, qty)
        if unit_price is None:
            unit_price = variant.price_for(None, qty)
        if unit_price is None:
            raise ValueError(f"{variant} has no price yet. Ask a manager to set one.")

    existing = cart.lines.filter(
        variant=variant, unit_price=money(unit_price), discount=Decimal(str(discount))
    ).first()
    if existing is not None:
        existing.qty += qty
        existing.save(update_fields=["qty"])
        return existing

    return CartLine.objects.create(
        tenant=cart.tenant,
        cart=cart,
        variant=variant,
        description=str(variant)[:160],
        qty=qty,
        unit_price=money(unit_price),
        discount=Decimal(str(discount)),
        tax_rate=variant.product.tax_rate.rate,
        added_via=added_via,
        note=note,
    )


def hold_cart(cart) -> str:
    """
    Park a basket and give it a short code.

    This is the shelf handoff: staff builds it on a phone, the cashier pulls
    it up by the code. Short because somebody reads it out loud.
    """
    if not cart.handoff_code:
        cart.handoff_code = _unique_handoff_code()
    cart.status = CartStatus.HELD
    cart.save(update_fields=["handoff_code", "status", "updated_at"])
    return cart.handoff_code


def _unique_handoff_code() -> str:
    alphabet = "ACDEFGHJKLMNPQRTUVWXY34789"  # no look-alike characters
    for _ in range(20):
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        if not Cart.objects.filter(handoff_code=code, status=CartStatus.HELD).exists():
            return code
    raise RuntimeError("Could not allocate a handoff code.")


# --------------------------------------------------------------------------
# Completing a sale
# --------------------------------------------------------------------------

@transaction.atomic
def complete_sale(
    cart,
    payments,
    *,
    user=None,
    shift=None,
    client_uuid=None,
    sold_at=None,
    authorised_by=None,
    is_offline_origin=False,
    review_notes=None,
    buyer_name="",
    buyer_phone="",
):
    """
    Turn a basket into a sale: stock out, payments recorded, receipt ready.

    ``payments`` is a list of ``{"method", "amount", "reference"}``. Splitting
    across cash and mobile money is normal, not an edge case.
    """
    if not cart.lines.exists():
        raise ValueError("Nothing to sell.")

    if client_uuid is not None:
        existing = Sale.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            # Already stored. A retried sync must never duplicate a sale.
            return existing

    user = user or get_current_user()
    branch = cart.branch
    tenant = cart.tenant

    subtotal = ZERO
    discount_total = ZERO
    tax_total = ZERO
    lines = list(cart.lines.select_related("variant", "variant__product"))

    # Taken from the sale's own shop rather than from ambient context, so a
    # sale completed by a command or a signal rounds the same way as one rung
    # up at the counter.
    currency = tenant.currency

    for line in lines:
        subtotal += money(line.qty * line.unit_price, currency)
        discount_total += money(line.discount, currency)
        tax_total += _tax_for(line, currency)

    total = money(subtotal - discount_total, currency)

    sale = Sale(
        tenant=tenant,
        branch=branch,
        register=cart.register,
        shift=shift,
        user=user,
        customer=cart.customer,
        # Somebody not on the books: a name on the receipt, nothing more.
        buyer_name=(buyer_name or "").strip()[:80],
        buyer_phone=(buyer_phone or "").strip()[:30],
        subtotal=money(subtotal),
        discount_total=money(discount_total),
        # Kept to the cent, like each line's: it is declared, not paid.
        tax_total=tax_total.quantize(MONEY, rounding=ROUND_HALF_UP),
        total=total,
        sold_at=sold_at or timezone.now(),
        authorised_by=authorised_by,
        is_offline_origin=is_offline_origin,
        synced_at=timezone.now() if is_offline_origin else None,
        needs_review=bool(review_notes),
        review_notes="\n".join(review_notes or []),
    )
    if client_uuid is not None:
        sale.client_uuid = client_uuid

    def already_stored():
        # Two devices raced on the same uuid: the one already stored wins.
        if client_uuid is None:
            return None
        return Sale.objects.filter(client_uuid=client_uuid).first()

    stored = save_with_number(
        sale,
        field="number",
        generate=lambda: next_sale_number(branch),
        recover=already_stored,
    )
    if stored is not sale:
        return stored

    for line in lines:
        _write_sale_line(sale, line, branch, user, currency)

    for payment in payments:
        SalePayment.objects.create(
            tenant=tenant,
            sale=sale,
            method=payment["method"],
            amount=money(payment["amount"], currency),
            reference=payment.get("reference", ""),
            change_given=money(payment.get("change_given", 0), currency),
        )

    _handle_credit(sale)
    _queue_fiscal_receipt(sale)

    cart.status = CartStatus.CONVERTED
    cart.save(update_fields=["status", "updated_at"])

    events.emit(SALE_COMPLETED, sale=sale)
    return sale


def _tax_for(line, currency=None) -> Decimal:
    """
    VAT out of a tax-inclusive price, which is how these shops quote.

    18% inclusive on 1,180 is 180, not 212.
    """
    # Out of the amount actually charged, not out of the raw multiplication:
    # a quarter kilo at 1,250 is charged as 313, so the VAT inside it is the
    # VAT inside 313. Taking it from 312.50 left the tax disagreeing with the
    # price it came out of.
    net = money((line.qty * line.unit_price) - line.discount, currency)
    rate = Decimal(str(line.tax_rate))
    if rate == ZERO:
        return ZERO
    # To the cent, not to the shilling: nobody hands VAT across a counter.
    # It is worked out of the price and declared, so precision costs nothing
    # here and rounding it would shift what is reported.
    return (net * rate / (Decimal("100") + rate)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _write_sale_line(sale, cart_line, branch, user, currency=None):
    variant = cart_line.variant
    unit_cost = ZERO
    batch = None

    if variant is not None:
        from apps.inventory.services import get_stock_item

        item = get_stock_item(variant, branch)
        unit_cost = item.avg_cost
        batch = pick_batch(variant, cart_line.qty, branch)

    line = SaleLine.objects.create(
        tenant=sale.tenant,
        sale=sale,
        variant=variant,
        batch=batch,
        description=cart_line.label[:160],
        qty=cart_line.qty,
        unit_price=cart_line.unit_price,
        unit_cost=unit_cost,
        discount=cart_line.discount,
        tax_rate=cart_line.tax_rate,
        tax_amount=_tax_for(cart_line, currency),
        line_total=money(cart_line.line_total, currency),
        added_via=cart_line.added_via,
    )

    if variant is not None:
        record_movement(
            variant=variant,
            qty_delta=-cart_line.qty,
            reason=MovementReason.SALE,
            branch=branch,
            batch=batch,
            source=sale,
            note=sale.number,
            user=user,
            allow_negative=True,
        )
    return line


def _handle_credit(sale):
    from apps.customers.models import CreditKind, CreditTransaction

    credit = sum(
        (p.amount for p in sale.payments.all() if p.method == PaymentMethod.CREDIT),
        ZERO,
    )
    if credit <= ZERO or sale.customer is None:
        return

    balance = sale.customer.balance + credit
    CreditTransaction.objects.create(
        tenant=sale.tenant,
        customer=sale.customer,
        kind=CreditKind.CHARGE,
        amount=credit,
        balance_after=balance,
        sale=sale,
        reference=sale.number,
    )
    events.emit(CREDIT_EXTENDED, sale=sale, customer=sale.customer, amount=credit)


def _queue_fiscal_receipt(sale):
    """
    Queued, never submitted inline.

    Submission needs the internet; selling does not. The paper receipt prints
    now and the fiscal copy follows when the line comes back.
    """
    from apps.core.features import FISCAL_RECEIPTS
    from apps.org.models import TenantSettings

    if not sale.tenant.has_feature(FISCAL_RECEIPTS):
        return None

    settings_row = TenantSettings.objects.first()
    provider = settings_row.fiscal_provider if settings_row else ""
    if not provider:
        return None

    return FiscalReceipt.objects.create(
        tenant=sale.tenant, sale=sale, provider=provider, status=FiscalStatus.PENDING
    )


def next_sale_number(branch) -> str:
    """
    Per branch, per day, sequential.

    Readable out loud over the phone, and a gap in the sequence is visible,
    which a random identifier would hide.
    """
    today = timezone.localdate()
    stem = f"{(branch.code or branch.name[:3]).upper()[:3]}{today:%y%m%d}"
    last = (
        Sale.objects_all.filter(tenant=branch.tenant, number__startswith=stem)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    counter = int(last[len(stem):]) + 1 if last else 1
    return f"{stem}{counter:04d}"


# --------------------------------------------------------------------------
# Undoing a sale
# --------------------------------------------------------------------------

@transaction.atomic
def void_sale(sale, *, reason, user=None, authorised_by=None):
    """
    Cancel a whole sale and put the stock back.

    The sale is marked voided, not deleted. An audit trail with holes in it is
    not an audit trail.
    """
    # Locked: a void racing a refund could otherwise put stock back twice.
    sale = type(sale).objects.select_for_update().get(pk=sale.pk)
    if sale.status != SaleStatus.COMPLETED:
        raise ValueError(f"{sale.number} is already {sale.get_status_display().lower()}.")

    for line in sale.lines.select_related("variant"):
        if line.variant_id:
            record_movement(
                variant=line.variant,
                qty_delta=line.qty,
                reason=MovementReason.RETURN,
                branch=sale.branch,
                batch=line.batch,
                source=sale,
                note=f"Void {sale.number}",
                user=user,
                allow_negative=True,
            )

    sale.status = SaleStatus.VOIDED
    sale.void_reason = reason
    sale.voided_at = timezone.now()
    sale.authorised_by = authorised_by or sale.authorised_by
    sale.save(
        update_fields=["status", "void_reason", "voided_at", "authorised_by", "updated_at"]
    )

    _reverse_credit(sale, reason=f"Void {sale.number}")
    events.emit(SALE_VOIDED, sale=sale, reason=reason)
    return sale


@transaction.atomic
def create_return(sale, quantities: dict, *, reason, method=PaymentMethod.CASH,
                  restock=True, user=None, shift=None, authorised_by=None, require_drawer=False):
    """
    Refund part of a sale.

    ``quantities`` maps sale line id to quantity coming back. The original
    sale is untouched; this stands beside it.
    """
    # Lock the sale for the rest of this transaction. Two returns racing each
    # other could both pass the "not already returned" check below.
    sale = type(sale).objects.select_for_update().get(pk=sale.pk)
    if sale.status == SaleStatus.VOIDED:
        raise ValueError("A voided sale cannot be returned.")

    paid_by = set(sale.payments.values_list("method", flat=True))
    if method not in paid_by:
        # A credit sale refunded "in cash" handed out money while the debt
        # stayed; a cash sale refunded "on account" paid out nothing at all.
        labels = dict(PaymentMethod.choices)
        raise ValueError(
            "Refund the way it was paid: "
            + ", ".join(labels.get(m, m) for m in sorted(paid_by)) + "."
        )

    doc = Return(
        tenant=sale.tenant,
        branch=sale.branch,
        sale=sale,
        user=user or get_current_user(),
        shift=shift,
        authorised_by=authorised_by,
        reason=reason,
        method=method,
        restock=restock,
    )
    save_with_number(
        doc, field="number", generate=lambda: next_return_number(sale.branch)
    )

    total = ZERO
    for line in sale.lines.select_related("variant"):
        qty = Decimal(str(quantities.get(line.pk, 0)))
        if qty <= ZERO:
            continue

        already = line.qty_returned
        if already + qty > line.qty:
            raise ValueError(
                f"Only {line.qty - already} of {line.description} can still be returned."
            )

        # Never more than the line was paid for, across every refund of it:
        # returning three items one at a time used to refund 200.01 of 200.00.
        already_paid_back = sum(
            (r.amount for r in line.return_lines.all()), ZERO
        )
        if already + qty >= line.qty:
            amount = money(line.line_total - already_paid_back)
        else:
            unit = money(line.line_total / line.qty) if line.qty else ZERO
            amount = min(money(unit * qty), money(line.line_total - already_paid_back))
        total += amount

        ReturnLine.objects.create(
            tenant=sale.tenant, return_doc=doc, sale_line=line, qty=qty, amount=amount
        )

        if restock and line.variant_id:
            record_movement(
                variant=line.variant,
                qty_delta=qty,
                reason=MovementReason.RETURN,
                branch=sale.branch,
                batch=line.batch,
                source=doc,
                note=f"Return {doc.number}",
                user=user,
                allow_negative=True,
            )

    if total == ZERO:
        raise ValueError("Nothing was returned.")

    doc.total = total
    doc.cash_amount, doc.credit_amount, doc.other_amount = _allocate_refund(sale, total, method)
    if require_drawer and doc.cash_amount > ZERO and shift is None:
        # Part of this comes back in cash even if another way was chosen --
        # cash is what is left once the other routes are used up.
        raise ValueError(f"{doc.cash_amount:,.0f} of this refund is paid in cash, which comes out "
                         "of a drawer. Open your till first.")
    doc.save(update_fields=["total", "cash_amount", "credit_amount", "other_amount",
                            "updated_at"])

    fully = all(line.qty_returned >= line.qty for line in sale.lines.all())
    sale.status = SaleStatus.REFUNDED if fully else SaleStatus.PART_REFUNDED
    sale.save(update_fields=["status", "updated_at"])

    if doc.credit_amount > ZERO:
        _reverse_credit(sale, amount=doc.credit_amount, reason=f"Return {doc.number}")

    events.emit(SALE_RETURNED, sale=sale, doc=doc)
    return doc


def _allocate_refund(sale, total, preferred):
    """
    Split a refund across the ways the sale was paid.

    Debt is cleared first -- handing back cash for goods still owed on
    account pays the customer twice -- then the preferred way (cash from the
    drawer, or mobile money), then whatever was paid the other way. Nothing
    comes back by a route beyond what was paid that way, less earlier refunds.
    """
    paid = {"cash": ZERO, "credit": ZERO, "other": ZERO}
    for payment in sale.payments.all():
        key = payment.method if payment.method in ("cash", "credit") else "other"
        # `amount` is what was kept -- change is stored separately, and
        # check_sale makes sure over-tendered cash arrives that way.
        paid[key] += payment.amount
    earlier = sale.returns.exclude(total=ZERO).aggregate(
        cash=models.Sum("cash_amount"), credit=models.Sum("credit_amount"),
        other=models.Sum("other_amount"),
    )
    left = {k: max(paid[k] - (earlier[k] or ZERO), ZERO) for k in paid}

    order = ["credit"]
    pref = preferred if preferred in ("cash", "credit") else "other"
    order += [pref] + [k for k in ("cash", "other") if k != pref]
    split = {"cash": ZERO, "credit": ZERO, "other": ZERO}
    remaining = total
    for key in dict.fromkeys(order):
        take = min(remaining, left[key])
        split[key] += take
        remaining -= take
    if remaining > ZERO:
        # Only reachable through rounding on old data: the drawer it is.
        split["cash" if paid["cash"] else pref] += remaining
    return money(split["cash"]), money(split["credit"]), money(split["other"])


def _reverse_credit(sale, *, amount=None, reason=""):
    from apps.customers.models import CreditKind, CreditTransaction

    if sale.customer is None:
        return
    charged = sale.credit_transactions.filter(kind=CreditKind.CHARGE).first()
    if charged is None:
        return

    # Never reverse more than was put on account, across every refund of
    # this sale -- a sale half cash, half credit refunded in full used to
    # wipe more debt than it had created.
    reversed_already = -sum(
        (t.amount for t in sale.credit_transactions.filter(kind=CreditKind.REFUND)),
        ZERO,
    )
    remaining = charged.amount - reversed_already
    value = min(amount if amount is not None else charged.amount, remaining)
    if value <= ZERO:
        return
    CreditTransaction.objects.create(
        tenant=sale.tenant,
        customer=sale.customer,
        kind=CreditKind.REFUND,
        amount=-money(value),
        balance_after=sale.customer.balance - money(value),
        sale=sale,
        note=reason,
    )


def next_return_number(branch) -> str:
    today = timezone.localdate()
    stem = f"R{(branch.code or branch.name[:2]).upper()[:2]}{today:%y%m%d}"
    last = (
        Return.objects_all.filter(tenant=branch.tenant, number__startswith=stem)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    counter = int(last[len(stem):]) + 1 if last else 1
    return f"{stem}{counter:03d}"

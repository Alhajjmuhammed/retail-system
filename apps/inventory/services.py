"""
The only way stock ever changes.

Every caller in the system -- a sale, a goods receipt, a transfer, a count --
goes through ``record_movement``. Nothing anywhere else writes to
``StockItem``, which is what keeps the cached quantity and the ledger in step.
"""

from decimal import Decimal

from django.db import models, transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core.context import get_current_branch, get_current_tenant, get_current_user
from apps.core.events import STOCK_LOW, STOCK_MOVED, STOCK_NEGATIVE, events
from apps.inventory.models import (
    Batch,
    CountStatus,
    MovementReason,
    StockCount,
    StockItem,
    StockMovement,
    Transfer,
    TransferStatus,
)

ZERO = Decimal("0")


def get_stock_item(variant, branch=None, *, create=True) -> StockItem | None:
    branch = branch or get_current_branch()
    if branch is None:
        raise ValueError("A branch is required to read stock.")
    if create:
        item, _ = StockItem.objects.get_or_create(
            branch=branch,
            variant=variant,
            defaults={"tenant": get_current_tenant()},
        )
        return item
    return StockItem.objects.filter(branch=branch, variant=variant).first()


def quantity_of(variant, branch=None) -> Decimal:
    item = get_stock_item(variant, branch, create=False)
    return item.qty_on_hand if item else ZERO


@transaction.atomic
def record_movement(
    *,
    variant,
    qty_delta,
    reason,
    branch=None,
    batch=None,
    unit_cost=None,
    source=None,
    note="",
    user=None,
    client_uuid=None,
    allow_negative=None,
):
    """
    Write one ledger entry and bring the cached quantity with it.

    ``unit_cost`` is only meaningful on inbound movements; it is what re-bases
    the weighted average. Outbound movements are valued at the average that
    already exists, so a sale never changes what stock is worth.
    """
    qty_delta = Decimal(str(qty_delta))
    if qty_delta == ZERO:
        return None

    if not variant.product.track_stock:
        # Services and one-off charges are sold without ever touching stock.
        return None

    branch = branch or get_current_branch()
    item = (
        StockItem.objects.select_for_update()
        .filter(branch=branch, variant=variant)
        .first()
    )
    if item is None:
        item = StockItem.objects.create(
            tenant=get_current_tenant(), branch=branch, variant=variant
        )
        item = StockItem.objects.select_for_update().get(pk=item.pk)

    new_qty = item.qty_on_hand + qty_delta

    if new_qty < ZERO:
        if allow_negative is None:
            allow_negative = _negative_allowed(branch)
        if not allow_negative:
            raise InsufficientStock(variant, branch, item.qty_on_hand, -qty_delta)

    # Weighted average moves only on the way in, and only when we know what
    # was paid. Selling does not change what the remaining stock is worth.
    if qty_delta > ZERO and unit_cost is not None:
        item.avg_cost = _new_average(item, qty_delta, Decimal(str(unit_cost)), branch)
    elif qty_delta > ZERO and unit_cost is None:
        unit_cost = item.avg_cost

    movement = StockMovement(
        tenant=get_current_tenant(),
        branch=branch,
        variant=variant,
        batch=batch,
        qty_delta=qty_delta,
        unit_cost=unit_cost if unit_cost is not None else item.avg_cost,
        reason=reason,
        note=note,
        balance_after=new_qty,
        created_by=user or get_current_user(),
    )
    if source is not None:
        movement.source_type = type(source).__name__
        movement.source_id = str(source.pk)
    if client_uuid is not None:
        movement.client_uuid = client_uuid
    movement.save()

    item.qty_on_hand = new_qty
    item.save(update_fields=["qty_on_hand", "avg_cost", "updated_at"])

    events.emit(STOCK_MOVED, movement=movement, item=item)
    if new_qty < ZERO:
        events.emit(STOCK_NEGATIVE, item=item, movement=movement)
    elif item.is_low:
        events.emit(STOCK_LOW, item=item)

    return movement


def _negative_allowed(branch) -> bool:
    """
    Offline tills can oversell, and refusing the sale afterwards is worse than
    a negative number: the money is taken and the customer has gone. The shop
    decides, and the default is to allow and flag.
    """
    from apps.org.models import TenantSettings

    settings_row = TenantSettings.objects.first()
    return settings_row.negative_stock_allowed if settings_row else True


def _new_average(item, qty_in: Decimal, unit_cost: Decimal, branch) -> Decimal:
    from apps.org.models import CostMethod, TenantSettings

    settings_row = TenantSettings.objects.first()
    method = settings_row.cost_method if settings_row else CostMethod.WEIGHTED_AVERAGE

    if method == CostMethod.LAST_COST:
        return unit_cost

    current_qty = max(item.qty_on_hand, ZERO)
    total_qty = current_qty + qty_in
    if total_qty <= ZERO:
        return unit_cost
    total_value = (current_qty * item.avg_cost) + (qty_in * unit_cost)
    return (total_value / total_qty).quantize(Decimal("0.01"))


class StockChanged(Exception):
    """Somebody else changed this quantity while the form was open."""

    def __init__(self, now):
        self.now = now
        super().__init__(
            f"The quantity changed to {now:g} while you were counting. Look again."
        )


class InsufficientStock(Exception):
    def __init__(self, variant, branch, available, wanted):
        self.variant = variant
        self.branch = branch
        self.available = available
        self.wanted = wanted
        super().__init__(
            f"Only {available} of {variant} left at {branch}, {wanted} wanted."
        )


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------

def pick_batch(variant, qty, branch=None):
    """
    First expiry, first out.

    Returns the batch a sale should draw on: the one closest to expiring that
    still has stock. Shops with no batch tracking simply get None.
    """
    branch = branch or get_current_branch()
    candidates = (
        Batch.objects.filter(variant=variant)
        .annotate(
            on_hand=Sum(
                "movements__qty_delta",
                filter=models.Q(movements__branch=branch),
            )
        )
        .filter(on_hand__gte=qty)
        .order_by(F("expiry_date").asc(nulls_last=True))
    )
    return candidates.first()


def expiring_soon(days=30, branch=None):
    from datetime import timedelta

    cutoff = timezone.localdate() + timedelta(days=days)
    branch = branch or get_current_branch()
    return (
        Batch.objects.filter(expiry_date__lte=cutoff)
        .annotate(
            on_hand=Sum(
                "movements__qty_delta", filter=models.Q(movements__branch=branch)
            )
        )
        .filter(on_hand__gt=0)
        .order_by("expiry_date")
    )


# --------------------------------------------------------------------------
# Adjustments, transfers, counts
# --------------------------------------------------------------------------

@transaction.atomic
def adjust(*, variant, new_qty, reason_text, branch=None, user=None, wastage=False,
           expected_qty=None):
    """
    Set a quantity to what it actually is, recording the difference.

    ``expected_qty`` is what the person saw when they decided: if a sale
    lands in between, the difference they approved is no longer the
    difference that would be written, so it is refused rather than guessed.
    """
    branch = branch or get_current_branch()
    get_stock_item(variant, branch)
    with transaction.atomic():
        # Locked before reading: a sale landing between the read and the
        # write left the shelf at something other than what was counted.
        item = StockItem.objects.select_for_update().get(branch=branch, variant=variant)
        if expected_qty is not None and item.qty_on_hand != Decimal(str(expected_qty)):
            raise StockChanged(item.qty_on_hand)
        delta = Decimal(str(new_qty)) - item.qty_on_hand
        if wastage and delta > 0:
            # Between the form and here, a sale moved the shelf: what was a
            # write-off would now add stock under the wastage permission.
            raise StockChanged(item.qty_on_hand)
        if delta == ZERO:
            return None
        return _adjust_movement(variant, delta, branch, reason_text, user, wastage)


def _adjust_movement(variant, delta, branch, reason_text, user, wastage):
    return record_movement(
        variant=variant,
        qty_delta=delta,
        reason=MovementReason.WASTAGE if wastage else MovementReason.ADJUSTMENT,
        branch=branch,
        note=reason_text,
        user=user,
        allow_negative=True,
    )


@transaction.atomic
def send_transfer(transfer: Transfer, user=None):
    """Stock leaves the sending branch now; it does not arrive until accepted."""
    transfer = type(transfer).objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != TransferStatus.DRAFT:
        raise ValueError("Only a draft transfer can be sent.")

    for line in transfer.lines.select_related("variant"):
        item = get_stock_item(line.variant, transfer.from_branch)
        line.unit_cost = item.avg_cost
        line.save(update_fields=["unit_cost"])
        record_movement(
            variant=line.variant,
            qty_delta=-line.qty_sent,
            reason=MovementReason.TRANSFER_OUT,
            branch=transfer.from_branch,
            batch=line.batch,
            source=transfer,
            note=f"To {transfer.to_branch}",
            user=user,
        )

    transfer.status = TransferStatus.SENT
    transfer.sent_by = user or get_current_user()
    transfer.sent_at = timezone.now()
    transfer.save(update_fields=["status", "sent_by", "sent_at", "updated_at"])
    return transfer


@transaction.atomic
def receive_transfer(transfer: Transfer, counted: dict, user=None):
    """
    Accept a transfer at the other end.

    ``counted`` maps line id to the quantity actually found. A shortfall is
    left as a difference on the line rather than quietly written off -- that
    difference is the whole point of counting on arrival.
    """
    transfer = type(transfer).objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != TransferStatus.SENT:
        raise ValueError("Only a sent transfer can be received.")

    for line in transfer.lines.select_related("variant"):
        qty = Decimal(str(counted.get(line.pk, line.qty_sent)))
        line.qty_received = qty
        line.save(update_fields=["qty_received"])
        if qty > ZERO:
            record_movement(
                variant=line.variant,
                qty_delta=qty,
                reason=MovementReason.TRANSFER_IN,
                branch=transfer.to_branch,
                batch=line.batch,
                unit_cost=line.unit_cost,
                source=transfer,
                note=f"From {transfer.from_branch}",
                user=user,
            )

    transfer.status = TransferStatus.RECEIVED
    transfer.received_by = user or get_current_user()
    transfer.received_at = timezone.now()
    transfer.save(
        update_fields=["status", "received_by", "received_at", "updated_at"]
    )
    return transfer


@transaction.atomic
def apply_count(count: StockCount, user=None):
    """Turn a finished count into adjustments, one movement per difference."""
    # Locked and re-read: applying two copies of the same count used to
    # apply the differences twice.
    count = type(count).objects.select_for_update().get(pk=count.pk)
    if count.status != CountStatus.OPEN:
        raise ValueError("This count has already been applied.")

    applied = 0
    for line in count.lines.select_related("variant"):
        if line.counted_qty is None:
            continue
        variance = line.variance
        if variance == ZERO:
            continue
        record_movement(
            variant=line.variant,
            qty_delta=variance,
            reason=MovementReason.COUNT,
            branch=count.branch,
            source=count,
            note=line.reason or f"Count {count.reference}",
            user=user,
            allow_negative=True,
        )
        applied += 1

    count.status = CountStatus.APPLIED
    count.applied_at = timezone.now()
    count.applied_by = user or get_current_user()
    count.save(update_fields=["status", "applied_at", "applied_by", "updated_at"])

    # Only what was actually counted: lines left blank were not looked at.
    StockItem.objects.filter(
        branch=count.branch,
        variant__in=count.lines.filter(counted_qty__isnull=False).values("variant"),
    ).update(last_counted_at=timezone.now())
    return applied


# --------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------

def rebuild_stock_items(branch=None):
    """
    Recompute every cached quantity from the ledger.

    The cache can drift after a crash mid-write or a bad import. The ledger
    cannot, so this is always safe to run.
    """
    branch = branch or get_current_branch()
    totals = (
        StockMovement.objects.filter(branch=branch)
        .values("variant")
        .annotate(total=Sum("qty_delta"))
    )
    fixed = 0
    for row in totals:
        updated = StockItem.objects.filter(
            branch=branch, variant_id=row["variant"]
        ).exclude(qty_on_hand=row["total"]).update(qty_on_hand=row["total"] or ZERO)
        fixed += updated
    return fixed

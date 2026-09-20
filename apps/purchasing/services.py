"""
Receiving goods.

This is where stock rises and cost price is set, so it is the one place that
decides whether margin reports mean anything.
"""


from django.db import transaction
from django.utils import timezone

from apps.core.context import get_current_user
from apps.core.events import GOODS_RECEIVED, events
from apps.inventory.models import Batch, MovementReason
from apps.inventory.services import record_movement
from apps.purchasing.models import GoodsReceipt, POStatus


@transaction.atomic
def post_receipt(receipt: GoodsReceipt, user=None):
    """
    Turn a goods receipt into stock.

    Idempotent by construction: a posted receipt has movements pointing at it,
    and posting again is refused rather than silently doubling the stock.
    """
    if receipt.lines.count() == 0:
        raise ValueError("Nothing to receive.")
    if receipt.lines.filter(qty__lte=0).exists():
        raise ValueError("Every line needs a quantity above zero.")

    from apps.inventory.models import StockMovement

    # Lock the receipt: two clicks on "Put it into stock" must not both pass
    # the check below and double the stock.
    type(receipt).objects.select_for_update().filter(pk=receipt.pk).first()
    already = StockMovement.objects.filter(
        source_type="GoodsReceipt", source_id=str(receipt.pk)
    ).exists()
    if already:
        raise ValueError(f"{receipt.reference} has already been received.")

    if receipt.order_id:
        order = type(receipt.order).objects.select_for_update().get(pk=receipt.order_id)
        if order.status not in {POStatus.SENT, POStatus.PARTIAL}:
            raise ValueError(f"{order.reference} is {order.get_status_display().lower()}; "
                             "remove the order from this delivery or reopen it.")
        # Never more than is still outstanding: two draft deliveries against
        # one order, or a doubled line, used to receive it twice over.
        wanted = {}
        for line in receipt.lines.exclude(order_line=None):
            wanted[line.order_line_id] = wanted.get(line.order_line_id, 0) + line.qty
        for order_line in order.lines.select_for_update().filter(pk__in=wanted):
            if wanted[order_line.pk] > order_line.qty_outstanding:
                raise ValueError(
                    f"{order_line.variant}: {wanted[order_line.pk]:g} is more than the "
                    f"{order_line.qty_outstanding:g} still outstanding on {order.reference}."
                )

    for line in receipt.lines.select_related("variant"):
        batch = None
        if line.batch_no or line.expiry_date:
            batch, _ = Batch.objects.get_or_create(
                variant=line.variant,
                batch_no=line.batch_no,
                expiry_date=line.expiry_date,
                defaults={"received_cost": line.unit_cost},
            )

        record_movement(
            variant=line.variant,
            qty_delta=line.qty,
            reason=MovementReason.PURCHASE,
            branch=receipt.branch,
            batch=batch,
            unit_cost=line.unit_cost,
            source=receipt,
            note=f"{receipt.supplier} {receipt.supplier_note}".strip(),
            user=user,
        )

        if line.order_line_id:
            line.order_line.qty_received += line.qty
            line.order_line.save(update_fields=["qty_received"])

    if receipt.order_id:
        receipt.order.refresh_status()

    _bill_for(receipt)
    apply_advances(receipt.supplier)
    events.emit(GOODS_RECEIVED, receipt=receipt)
    return receipt


def _bill_for(receipt):
    """
    What the shop now owes for these goods.

    Nothing on the shop side ever created a supplier bill, so "owed" could
    only go down -- every payment made it more negative. Goods in the door
    are a debt until paid; paying cash on delivery simply settles it.
    """
    from datetime import timedelta

    from apps.purchasing.models import SupplierInvoice

    if receipt.invoices.exists():
        return None
    today = timezone.localdate()
    number = (receipt.supplier_note or receipt.reference)[:40]
    typed = SupplierInvoice.objects.filter(
        supplier=receipt.supplier, number__iexact=number, receipt__isnull=True
    ).first()
    if typed is not None:
        # The same bill was already entered by hand: link it to these goods
        # rather than owing the supplier twice.
        typed.receipt = receipt
        typed.save(update_fields=["receipt", "updated_at"])
        return typed
    if SupplierInvoice.objects.filter(supplier=receipt.supplier, number__iexact=number).exists():
        # Bill numbers are unique per supplier; a reused delivery-note number
        # made this crash and the delivery could never be put into stock.
        number = f"{number[:28]}-{receipt.reference}"[:40]
    return SupplierInvoice.objects.create(
        supplier=receipt.supplier,
        receipt=receipt,
        number=number,
        invoice_date=today,
        due_date=today + timedelta(days=receipt.supplier.payment_terms_days or 0),
        amount=receipt.total,
        note=f"For goods received on {receipt.reference}",
    )


def next_reference(model, prefix: str) -> str:
    """
    Human-readable document numbers, per tenant and per year.

    Shops refer to these out loud on the phone, so they have to be short and
    readable -- never a UUID.
    """
    year = timezone.localdate().year
    stem = f"{prefix}{year % 100:02d}"
    from django.db.models.functions import Length

    # Longest first, then highest: as text "PO2610000" sorts below
    # "PO269999", so the 10,000th document went back to number one.
    last = (
        model.objects.filter(reference__startswith=stem)
        .order_by(Length("reference").desc(), "-reference")
        .values_list("reference", flat=True)
        .first()
    )
    try:
        counter = int(last[len(stem):]) + 1 if last else 1
    except ValueError:
        counter = model.objects.filter(reference__startswith=stem).count() + 1
    return f"{stem}{counter:04d}"


@transaction.atomic
def approve_order(order, user=None):
    order.status = POStatus.SENT
    order.approved_by = user or get_current_user()
    order.approved_at = timezone.now()
    order.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return order


def apply_advances(supplier):
    """
    Settle open bills with money already paid ahead, oldest first.

    An advance used to sit unlinked for ever: the balance said nothing was
    owed while the bill said it was unpaid, and the page invited paying it
    again. Part of an advance that covers part of a bill is split into a
    linked row and the rest, both keeping the payment's batch.
    """
    from decimal import Decimal

    from apps.purchasing.models import Supplier, SupplierPayment

    with transaction.atomic():
        Supplier.objects.select_for_update().filter(pk=supplier.pk).first()
        advances = list(supplier.payments.filter(invoice__isnull=True, amount__gt=0)
                        .order_by("paid_at", "pk"))
        if not advances:
            return
        bills = [b for b in supplier.invoices.order_by("invoice_date", "pk") if b.outstanding > 0]
        for bill in bills:
            due = bill.outstanding
            while due > 0 and advances:
                advance = advances[0]
                if advance.amount <= due:
                    advance.invoice = bill
                    advance.save(update_fields=["invoice", "updated_at"])
                    due -= advance.amount
                    advances.pop(0)
                else:
                    SupplierPayment.objects.create(
                        supplier=supplier, invoice=bill, amount=due, method=advance.method,
                        reference=advance.reference, paid_at=advance.paid_at,
                        note=advance.note, batch=advance.batch,
                    )
                    advance.amount -= due
                    advance.save(update_fields=["amount", "updated_at"])
                    due = Decimal("0")
            if not advances:
                break

"""Background work for the till."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.core.context import unscoped
from apps.core.jobs import tracked
from apps.pos.models import Cart, CartStatus, FiscalReceipt, FiscalStatus

logger = logging.getLogger(__name__)

MAX_FISCAL_ATTEMPTS = 12


@shared_task
@tracked("send-fiscal-receipts")
def send_pending_fiscal_receipts(limit=200):
    """
    Drain the fiscal queue.

    Submission needs the internet and selling does not, so receipts pile up
    whenever a shop is offline. Each provider plugs in here; until one is
    connected the rows stay pending and the platform health page shows the
    backlog rather than pretending it is done.
    """
    sent = failed = skipped = 0

    with unscoped():
        pending = FiscalReceipt.objects.select_related("sale", "tenant").filter(
            status=FiscalStatus.PENDING, attempts__lt=MAX_FISCAL_ATTEMPTS
        )[:limit]

        for receipt in pending:
            provider = _provider_for(receipt)
            if provider is None:
                skipped += 1
                continue

            receipt.attempts += 1
            try:
                result = provider(receipt)
            except Exception as exc:
                receipt.status = FiscalStatus.FAILED
                receipt.error = str(exc)[:500]
                failed += 1
                logger.exception("Fiscal submission failed for %s", receipt.sale.number)
            else:
                receipt.receipt_no = result.get("receipt_no", "")
                receipt.verification_code = result.get("verification_code", "")
                receipt.status = FiscalStatus.SENT
                receipt.sent_at = timezone.now()
                receipt.error = ""
                sent += 1

            receipt.save(update_fields=[
                "attempts", "status", "receipt_no", "verification_code",
                "sent_at", "error", "updated_at",
            ])

    return {"sent": sent, "failed": failed, "skipped": skipped}


# Providers register themselves here. Keeping the registry empty is honest:
# nothing is submitted until a real EFD/VFD integration is added, and the
# health page shows exactly how much is waiting.
FISCAL_PROVIDERS = {}


def _provider_for(receipt):
    return FISCAL_PROVIDERS.get(receipt.provider)


@shared_task
@tracked("clear-abandoned-carts")
def clear_abandoned_carts(older_than_hours=24):
    """
    Baskets nobody finished.

    Held baskets are left alone -- a shelf handoff can legitimately sit for a
    while, and deleting one loses a customer's basket in front of them.
    """
    cutoff = timezone.now() - timedelta(hours=older_than_hours)

    with unscoped():
        stale = Cart.objects.filter(status=CartStatus.OPEN, updated_at__lt=cutoff)
        count = stale.count()
        stale.update(status=CartStatus.ABANDONED)

    return count

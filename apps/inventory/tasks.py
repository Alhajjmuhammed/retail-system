"""Stock alerts, raised once a day rather than on every movement."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from apps.core.context import tenant_context, unscoped
from apps.core.events import BATCH_EXPIRING, STOCK_LOW, events
from apps.core.jobs import tracked
from apps.inventory.models import Batch, StockItem
from apps.tenancy.models import Tenant

logger = logging.getLogger(__name__)


@shared_task
@tracked("expiry-and-low-stock-alerts")
def raise_stock_alerts():
    """
    Tell shops what is running out and what is about to expire.

    Emitted as events rather than sent directly, so SMS, WhatsApp or email can
    subscribe without this task ever knowing they exist.
    """
    from apps.org.models import TenantSettings

    low_total = expiring_total = 0

    with unscoped():
        tenants = list(Tenant.objects.all())

    for tenant in tenants:
        with tenant_context(tenant):
            settings_row = TenantSettings.objects.first()
            if settings_row is None or not settings_row.low_stock_alerts:
                continue

            low = list(
                StockItem.objects.select_related("variant__product", "branch")
                .filter(reorder_level__isnull=False, qty_on_hand__lte=F("reorder_level"))
            )
            for item in low:
                events.emit(STOCK_LOW, item=item, scheduled=True)
            low_total += len(low)

            cutoff = timezone.localdate() + timedelta(
                days=settings_row.expiry_warning_days
            )
            expiring = list(
                Batch.objects.filter(expiry_date__lte=cutoff, expiry_date__isnull=False)
            )
            for batch in expiring:
                events.emit(BATCH_EXPIRING, batch=batch)
            expiring_total += len(expiring)

    return {"low_stock": low_total, "expiring": expiring_total}

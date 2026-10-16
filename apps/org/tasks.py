"""Background work for a shop's own settings."""

import logging

from celery import shared_task

from apps.core.context import tenant_context, unscoped
from apps.core.jobs import tracked

logger = logging.getLogger(__name__)


@shared_task
@tracked("refresh-exchange-rates")
def refresh_exchange_rates():
    """
    Every morning, the market rate for each currency a shop follows the
    market in. One request to the rates service serves every shop; a shop's
    own typed rates are never touched.
    """
    from apps.core.currency import fetch_market_table, refresh_market_rates
    from apps.org.models import ExchangeRate
    from apps.tenancy.models import Tenant

    with unscoped():
        tenant_ids = set(ExchangeRate.objects_all.filter(
            is_active=True, source="market").values_list("tenant_id", flat=True))
    if not tenant_ids:
        return 0
    table = fetch_market_table()
    total = 0
    for tenant in Tenant.objects.filter(pk__in=tenant_ids):
        with tenant_context(tenant):
            updated, _ = refresh_market_rates(tenant, table)
            total += updated
    logger.info("exchange rates refreshed: %s", total)
    return total

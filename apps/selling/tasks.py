"""Background work for the selling side."""

import logging

from celery import shared_task

from apps.core.context import unscoped
from apps.core.jobs import tracked

logger = logging.getLogger(__name__)


@shared_task
@tracked("expire-quotations")
def expire_quotations():
    """
    Quotations whose date has passed stop being offers.

    Unscoped on purpose: this runs for every shop on the platform, and a
    price held open for ever is a price the shop has stopped choosing.
    """
    from apps.selling.services import expire_overdue

    with unscoped():
        lapsed = expire_overdue()
    if lapsed:
        logger.info("quotations expired: %s", lapsed)
    return lapsed

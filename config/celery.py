"""
Celery application.

Configured from the start but never wired in, which meant trials never
expired, the fiscal queue never drained and usage was never recorded. The
schedule below is the minimum for the platform to run without somebody
watching it.
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("retail")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Trials and dunning. Run in the small hours, after the shops have closed.
    "advance-subscriptions": {
        "task": "apps.tenancy.tasks.advance_subscriptions",
        "schedule": crontab(hour=2, minute=0),
    },
    # Billing needs a branch count per day, and the platform dashboard needs
    # to know who is pressing against their limits.
    "snapshot-usage": {
        "task": "apps.tenancy.tasks.snapshot_usage",
        "schedule": crontab(hour=2, minute=30),
    },
    # Fiscal receipts queue while a shop is offline; drain them often.
    "send-fiscal-receipts": {
        "task": "apps.pos.tasks.send_pending_fiscal_receipts",
        "schedule": crontab(minute="*/10"),
    },
    # Baskets abandoned mid-sale pile up otherwise.
    "clear-abandoned-carts": {
        "task": "apps.pos.tasks.clear_abandoned_carts",
        "schedule": crontab(hour=3, minute=0),
    },
    # SMS receipts and alerts queue whether or not a gateway is connected.
    "send-queued-messages": {
        "task": "apps.notifications.tasks.send_queued_messages",
        "schedule": crontab(minute="*/5"),
    },
    "expiry-and-low-stock-alerts": {
        "task": "apps.inventory.tasks.raise_stock_alerts",
        "schedule": crontab(hour=6, minute=30),
    },
}

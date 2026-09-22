"""
Shops on a plan with no trial were created already past the end of one.

The Free plan has no trial days, and every shop that signs up lands on it.
Starting a subscription always started a trial, so those shops were born
with ``trial_ends_at`` already in the past -- which the nightly job read as
a shop that had failed to pay for something free, and walked from past due
to grace to suspended.

This puts them where they should have been: on the plan, active, with a
period that rolls forward. Deliberately narrow -- only shops on a free plan
that have never paid for anything, so a shop an operator suspended on
purpose, or one that has a payment history, is left exactly as it is.
"""

from datetime import datetime, time, timedelta

from django.db import migrations
from django.utils import timezone

STUCK = ["trialing", "past_due", "grace"]


def heal(apps, schema_editor):
    Subscription = apps.get_model("tenancy", "Subscription")
    Invoice = apps.get_model("tenancy", "Invoice")

    now = timezone.now()
    today = timezone.localdate()
    period_end = timezone.make_aware(
        datetime.combine(today + timedelta(days=30), time.max)
    )
    rows = Subscription.objects.select_related("plan", "tenant").filter(
        plan__trial_days=0, plan__price_monthly=0, status__in=STUCK,
    )
    for subscription in rows:
        if Invoice.objects.filter(tenant=subscription.tenant, status="paid").exists():
            continue
        subscription.status = "active"
        subscription.trial_ends_at = None
        subscription.period_start = now
        subscription.period_end = period_end
        subscription.save(update_fields=["status", "trial_ends_at", "period_start",
                                         "period_end", "updated_at"])


def unheal(apps, schema_editor):
    """Nothing to undo: this only corrects a state that could not be right."""


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0001_initial")]
    operations = [migrations.RunPython(heal, unheal)]

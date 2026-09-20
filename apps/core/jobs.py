"""
Scheduled jobs report in, so a stopped scheduler is noticed.

    @shared_task
    @tracked("send-fiscal-receipts")
    def send_pending_fiscal_receipts(): ...
"""

import functools
import traceback
from datetime import timedelta

from django.utils import timezone

# Each job, what it is for, and how long without a run is too long -- the
# schedule's interval plus room for a slow run.
JOBS = {
    "advance-subscriptions": ("Renewal invoices and overdue shops", timedelta(hours=26)),
    "snapshot-usage": ("Daily usage for billing", timedelta(hours=26)),
    "send-fiscal-receipts": ("Receipts to the tax authority", timedelta(minutes=30)),
    "clear-abandoned-carts": ("Clearing abandoned baskets", timedelta(hours=26)),
    "send-queued-messages": ("SMS receipts and alerts", timedelta(minutes=20)),
    "expiry-and-low-stock-alerts": ("Expiry and low-stock alerts", timedelta(hours=26)),
}


def tracked(name):
    def decorator(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            from apps.core.models import JobHeartbeat

            JobHeartbeat.objects.update_or_create(
                name=name, defaults={"last_started": timezone.now()}
            )
            try:
                result = func(*args, **kwargs)
            except Exception:
                JobHeartbeat.objects.filter(name=name).update(
                    last_finished=timezone.now(), last_ok=False,
                    last_error=traceback.format_exc()[-2000:],
                )
                raise
            beat = JobHeartbeat.objects.get(name=name)
            beat.last_finished = timezone.now()
            beat.last_ok = True
            beat.last_error = ""
            beat.last_result = result if isinstance(result, dict) else {"result": str(result)[:200]}
            beat.runs += 1
            beat.save()
            return result
        return wrapped
    return decorator


def status():
    """Every known job with a plain verdict: fine, failed, overdue or never run."""
    from apps.core.models import JobHeartbeat

    now = timezone.now()
    beats = {b.name: b for b in JobHeartbeat.objects.all()}
    rows = []
    for name, (label, max_gap) in JOBS.items():
        beat = beats.get(name)
        if beat is None or beat.last_finished is None:
            verdict = "never"
        elif not beat.last_ok:
            verdict = "failed"
        elif now - beat.last_finished > max_gap:
            verdict = "overdue"
        else:
            verdict = "fine"
        rows.append({"name": name, "label": label, "beat": beat, "verdict": verdict})
    return rows

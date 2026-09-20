"""Draining the message queue."""

import logging

from celery import shared_task
from django.utils import timezone

from apps.core.context import unscoped
from apps.core.jobs import tracked
from apps.notifications.models import Message, MessageStatus

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

# Gateways register here. Empty on purpose: nothing is marked sent until a
# real provider is connected, so the queue shows the truth.
GATEWAYS = {}


@shared_task
@tracked("send-queued-messages")
def send_queued_messages(limit=200):
    sent = failed = skipped = 0

    with unscoped():
        queued = Message.objects.filter(
            status=MessageStatus.QUEUED, attempts__lt=MAX_ATTEMPTS
        )[:limit]

        for message in queued:
            gateway = GATEWAYS.get(message.channel)
            if gateway is None:
                skipped += 1
                continue

            message.attempts += 1
            try:
                result = gateway(message)
            except Exception as exc:
                message.status = MessageStatus.FAILED
                message.error = str(exc)[:500]
                failed += 1
                logger.exception("Message to %s failed", message.to)
            else:
                message.status = MessageStatus.SENT
                message.provider_ref = result.get("ref", "")
                message.cost = result.get("cost", 0)
                message.sent_at = timezone.now()
                message.error = ""
                sent += 1

            message.save(update_fields=[
                "attempts", "status", "provider_ref", "cost", "sent_at",
                "error", "updated_at",
            ])

    return {"sent": sent, "failed": failed, "skipped": skipped}

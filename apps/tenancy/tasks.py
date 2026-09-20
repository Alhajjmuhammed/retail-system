"""
Subscription lifecycle and usage.

Nothing here ever deletes a shop's data. The worst that happens to somebody
who stops paying is that they go read-only and keep everything.
"""

import logging

from celery import shared_task
from django.utils import timezone

from apps.core.context import tenant_context, unscoped
from apps.core.events import SUBSCRIPTION_LAPSED, events
from apps.core.jobs import tracked
from apps.tenancy.models import Subscription, SubscriptionStatus, Tenant, UsageSnapshot

logger = logging.getLogger(__name__)


@shared_task
@tracked("advance-subscriptions")
def advance_subscriptions():
    """
    Move subscriptions through the states nobody is watching.

    trialing -> past_due when the trial ends
    past_due -> grace on the first run after the period ends
    grace    -> suspended once the grace days are up
    """
    from apps.tenancy.billing import renew_due

    now = timezone.now()
    moved = {"past_due": 0, "grace": 0, "suspended": 0}

    with unscoped():
        # Renewal invoices go out a week ahead; a paid-up date that has
        # passed with the next period unpaid becomes "past due".
        moved.update(renew_due(now))
        expired_trials = Subscription.objects.filter(
            status=SubscriptionStatus.TRIALING, trial_ends_at__lte=now
        )
        for subscription in expired_trials:
            subscription.status = SubscriptionStatus.PAST_DUE
            subscription.save(update_fields=["status", "updated_at"])
            events.emit(SUBSCRIPTION_LAPSED, subscription=subscription)
            moved["past_due"] += 1

        due = Subscription.objects.filter(
            status=SubscriptionStatus.PAST_DUE, period_end__lte=now
        )
        for subscription in due:
            subscription.enter_grace()
            moved["grace"] += 1

        grace_over = Subscription.objects.filter(
            status=SubscriptionStatus.GRACE, grace_ends_at__lte=now
        )
        for subscription in grace_over:
            # Read-only, never deleted. They keep every record they entered.
            subscription.status = SubscriptionStatus.SUSPENDED
            subscription.save(update_fields=["status", "updated_at"])
            moved["suspended"] += 1

    logger.info("Subscriptions advanced: %s", moved)
    return moved


@shared_task
@tracked("snapshot-usage")
def snapshot_usage():
    """One row per tenant per day: what billing and the platform page read."""
    from django.db.models import Count, Sum

    from apps.core.features import LIMIT_BRANCHES, LIMIT_PRODUCTS, LIMIT_USERS
    from apps.pos.models import Sale, SaleStatus

    written = 0

    with unscoped():
        tenants = list(Tenant.objects.all())

    for tenant in tenants:
        # Each shop's "today" ends at midnight where the shop is.
        with tenant_context(tenant), unscoped():
            today = timezone.localdate()
            sales = Sale.objects_all.filter(
                tenant=tenant, sold_at__date=today,
                status__in=[SaleStatus.COMPLETED, SaleStatus.PART_REFUNDED],
            ).aggregate(count=Count("id"), value=Sum("total"))

            UsageSnapshot.objects.update_or_create(
                tenant=tenant,
                date=today,
                defaults={
                    "branches": tenant.usage_of(LIMIT_BRANCHES),
                    "users": tenant.usage_of(LIMIT_USERS),
                    "products": tenant.usage_of(LIMIT_PRODUCTS),
                    "sales_count": sales["count"] or 0,
                    "sales_value": sales["value"] or 0,
                },
            )
            written += 1

    return written

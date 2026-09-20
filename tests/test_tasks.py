"""
Background work.

Celery was configured from the start but never wired in, so trials never
expired, the fiscal queue never drained and usage was never recorded. These
run the tasks directly -- no broker needed.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.context import tenant_context
from apps.inventory.tasks import raise_stock_alerts
from apps.pos.models import Cart, CartStatus, FiscalReceipt, FiscalStatus, PaymentMethod
from apps.pos.services import add_to_cart, complete_sale, new_cart
from apps.pos.tasks import clear_abandoned_carts, send_pending_fiscal_receipts
from apps.tenancy.models import SubscriptionStatus, UsageSnapshot
from apps.tenancy.tasks import advance_subscriptions, snapshot_usage

pytestmark = pytest.mark.django_db


def test_an_expired_trial_becomes_past_due(shop):
    subscription = shop.subscription
    subscription.trial_ends_at = timezone.now() - timedelta(hours=1)
    subscription.save(update_fields=["trial_ends_at"])

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.PAST_DUE


def test_grace_runs_out_into_read_only_never_deletion(shop, main_branch, stocked):
    subscription = shop.subscription
    subscription.status = SubscriptionStatus.GRACE
    subscription.grace_ends_at = timezone.now() - timedelta(hours=1)
    subscription.save(update_fields=["status", "grace_ends_at"])

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.SUSPENDED
    assert subscription.is_read_only

    # The shop keeps everything it entered.
    with tenant_context(shop):
        from apps.catalog.models import Product

        assert Product.objects.count() == 3


def test_a_paying_shop_is_left_alone(shop):
    subscription = shop.subscription
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.period_end = timezone.now() + timedelta(days=20)
    subscription.save(update_fields=["status", "period_end"])

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.ACTIVE


def test_usage_snapshot_records_what_billing_needs(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=2)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 3000}])

    written = snapshot_usage()
    assert written >= 1

    snapshot = UsageSnapshot.objects.get(tenant=shop, date=timezone.localdate())
    assert snapshot.branches == 1
    assert snapshot.products == 3
    assert snapshot.sales_count == 1
    assert snapshot.sales_value == Decimal("3000.00")


def test_snapshot_is_idempotent_within_a_day(shop):
    snapshot_usage()
    snapshot_usage()
    assert UsageSnapshot.objects.filter(tenant=shop).count() == 1


def test_fiscal_queue_reports_a_backlog_rather_than_pretending(shop, main_branch, stocked, owner):
    """
    With no provider connected nothing is submitted, and the rows stay pending
    so the platform health page shows the real backlog.
    """
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])
        FiscalReceipt.objects.create(
            tenant=shop, sale=sale, provider="tra", status=FiscalStatus.PENDING
        )

    result = send_pending_fiscal_receipts()
    assert result["skipped"] == 1
    assert result["sent"] == 0
    with tenant_context(shop):
        assert FiscalReceipt.objects.filter(status=FiscalStatus.PENDING).count() == 1


def test_a_connected_provider_marks_receipts_sent(shop, main_branch, stocked, owner):
    from apps.pos import tasks

    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])
        FiscalReceipt.objects.create(
            tenant=shop, sale=sale, provider="fake", status=FiscalStatus.PENDING
        )

    tasks.FISCAL_PROVIDERS["fake"] = lambda receipt: {
        "receipt_no": "FR-001", "verification_code": "ABC123"
    }
    try:
        result = send_pending_fiscal_receipts()
    finally:
        tasks.FISCAL_PROVIDERS.pop("fake")

    assert result["sent"] == 1
    with tenant_context(shop):
        receipt = FiscalReceipt.objects.get(sale=sale)
    assert receipt.status == FiscalStatus.SENT
    assert receipt.receipt_no == "FR-001"


def test_abandoned_baskets_are_cleared_but_held_ones_are_kept(
    shop, main_branch, stocked, owner
):
    """
    A held basket is a shelf handoff. Deleting one loses a customer's basket
    in front of them.
    """
    from apps.pos.services import hold_cart

    with tenant_context(shop, branch=main_branch, user=owner):
        stale = new_cart(branch=main_branch)
        add_to_cart(stale, stocked["Mkate"], qty=1)

        held = new_cart(branch=main_branch)
        add_to_cart(held, stocked["Soda 500ml"], qty=1)
        hold_cart(held)

    old = timezone.now() - timedelta(hours=48)
    Cart.objects_all.filter(pk__in=[stale.pk, held.pk]).update(updated_at=old)

    assert clear_abandoned_carts() == 1

    stale.refresh_from_db()
    held.refresh_from_db()
    assert stale.status == CartStatus.ABANDONED
    assert held.status == CartStatus.HELD


def test_stock_alerts_fire_for_low_and_expiring_items(shop, main_branch, stocked, owner):
    from apps.core.events import BATCH_EXPIRING, STOCK_LOW, events
    from apps.inventory.models import Batch, StockItem

    seen = {"low": 0, "expiring": 0}
    events.subscribe(STOCK_LOW, lambda **kw: seen.__setitem__("low", seen["low"] + 1))
    events.subscribe(BATCH_EXPIRING, lambda **kw: seen.__setitem__("expiring", seen["expiring"] + 1))

    with tenant_context(shop, branch=main_branch, user=owner):
        StockItem.objects.filter(variant=stocked["Mkate"]).update(
            reorder_level=500, qty_on_hand=10
        )
        Batch.objects.create(
            variant=stocked["Soda 500ml"], batch_no="B1",
            expiry_date=timezone.localdate() + timedelta(days=3),
        )

    result = raise_stock_alerts()
    assert result["low_stock"] >= 1
    assert result["expiring"] >= 1
    assert seen["low"] >= 1
    assert seen["expiring"] >= 1

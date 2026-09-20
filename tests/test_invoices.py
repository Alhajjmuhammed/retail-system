"""
Invoices and the billing cycle behind them.

Before: nothing raised an invoice, paying extended nothing, a paying shop
whose period ended stayed "active" for ever, and the page's totals counted
void and part-paid invoices wrongly.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import PlatformRole, User
from apps.core.context import unscoped
from apps.tenancy.billing import renew_due
from apps.tenancy.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    SubscriptionStatus,
    TenantStatus,
)

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def boss(db):
    return User.objects.create_user("boss@p.test", "pw", name="Boss", is_platform_staff=True,
                                    platform_role=PlatformRole.objects.get(name="Super admin"))


def _invoice(shop, *, total=60000, days_ago=0, due_days_ago=None, status=InvoiceStatus.OPEN, number=None):
    start = timezone.localdate() - timedelta(days=days_ago)
    with unscoped():
        return Invoice.objects.create(
            tenant=shop, number=number or f"T-{Invoice.objects.count() + 1}",
            period_start=start, period_end=start + timedelta(days=29),
            amount=total, total=total, status=status,
            due_date=None if due_days_ago is None else timezone.localdate() - timedelta(days=due_days_ago),
        )


def _pay(client, invoice, amount, method="mpesa"):
    return client.post(reverse("platform:invoice_pay", args=[invoice.pk]),
                       {"amount": str(amount), "method": method, "reference": "QGR7"}, **HX)


def test_bad_amounts_are_refused_not_a_crash(client, shop, boss):
    inv = _invoice(shop)
    client.force_login(boss)
    for amount in ("abc", "0", "-5", "70000"):
        r = _pay(client, inv, amount)
        assert r.status_code == 200, amount  # the form again, with the reason
    with unscoped():
        assert not Payment.objects.filter(invoice=inv).exists()


def test_a_void_invoice_cannot_be_paid(client, shop, boss):
    inv = _invoice(shop, status=InvoiceStatus.VOID)
    client.force_login(boss)
    _pay(client, inv, 100)
    with unscoped():
        assert not Payment.objects.filter(invoice=inv).exists()


def test_paying_in_full_moves_the_paid_up_date_and_lets_a_held_shop_trade(client, shop, boss):
    with unscoped():
        sub = shop.subscription
        sub.status = SubscriptionStatus.SUSPENDED
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.period_end = timezone.now() - timedelta(days=10)
        sub.save()
        shop.status = TenantStatus.SUSPENDED
        shop.save(update_fields=["status"])
    inv = _invoice(shop, days_ago=10, due_days_ago=10)
    client.force_login(boss)
    _pay(client, inv, 60000)
    with unscoped():
        inv.refresh_from_db()
        shop.refresh_from_db()
        sub.refresh_from_db()
        assert inv.status == InvoiceStatus.PAID
        assert sub.status == SubscriptionStatus.ACTIVE and shop.status == TenantStatus.ACTIVE
        assert timezone.localtime(sub.period_end).date() == inv.period_end


def test_paying_one_invoice_does_not_clear_another_overdue_one(client, shop, boss):
    with unscoped():
        sub = shop.subscription
        sub.status = SubscriptionStatus.PAST_DUE
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.save()
    old = _invoice(shop, days_ago=40, due_days_ago=40, number="OLD")
    new = _invoice(shop, days_ago=5, due_days_ago=5, number="NEW")
    client.force_login(boss)
    _pay(client, new, 60000)
    with unscoped():
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAST_DUE
    assert old.pk  # still owed


def test_a_payment_recorded_by_mistake_can_be_reversed(client, shop, boss):
    with unscoped():
        sub = shop.subscription
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.save()
    inv = _invoice(shop, due_days_ago=0)
    client.force_login(boss)
    _pay(client, inv, 60000)
    with unscoped():
        payment = Payment.objects.get(invoice=inv)
    client.post(reverse("platform:payment_reverse", args=[payment.pk]), {"reason": "wrong shop"})
    with unscoped():
        inv.refresh_from_db()
        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == "refunded" and inv.status == InvoiceStatus.OPEN
        assert inv.outstanding == 60000
        assert sub.status == SubscriptionStatus.PAST_DUE


def test_an_invoice_with_payments_cannot_be_voided(client, shop, boss):
    inv = _invoice(shop)
    client.force_login(boss)
    _pay(client, inv, 1000)
    client.post(reverse("platform:invoice_void", args=[inv.pk]))
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.OPEN


def test_totals_ignore_void_and_count_only_what_is_left(client, shop, boss):
    _invoice(shop, total=1000, status=InvoiceStatus.VOID, number="V")
    part = _invoice(shop, total=5000, number="P")
    client.force_login(boss)
    _pay(client, part, 2000)
    totals = client.get(reverse("platform:invoices")).context["totals"]
    assert totals["billed"] == 5000 and totals["outstanding"] == 3000 and totals["collected"] == 2000


def test_the_list_pages_instead_of_stopping_at_200(client, shop, boss):
    with unscoped():
        Invoice.objects.bulk_create([
            Invoice(tenant=shop, number=f"B-{i}", period_start=timezone.localdate(),
                    period_end=timezone.localdate(), amount=1, total=1, status=InvoiceStatus.OPEN)
            for i in range(230)
        ])
    client.force_login(boss)
    r = client.get(reverse("platform:invoices"), {"page": 5})
    assert r.context["page"].paginator.count == 230 and len(r.context["invoices"]) == 30


def test_raising_checks_dates_and_overlaps(client, shop, boss):
    client.force_login(boss)
    today = timezone.localdate()
    r = client.post(reverse("platform:invoice_create"),
                    {"tenant": shop.pk, "period_start": "not a date"}, **HX)
    assert r.status_code == 200 and b"starts" in r.content
    r = client.post(reverse("platform:invoice_create"),
                    {"tenant": shop.pk, "period_start": today, "period_end": today - timedelta(days=3)}, **HX)
    assert b"ends before it starts" in r.content
    ok = client.post(reverse("platform:invoice_create"), {"tenant": shop.pk, "period_start": today}, **HX)
    assert ok.status_code == 204
    again = client.post(reverse("platform:invoice_create"), {"tenant": shop.pk, "period_start": today}, **HX)
    assert b"already covers" in again.content
    with unscoped():
        inv = Invoice.objects.get(tenant=shop)
        assert inv.total > 0 and inv.period_end > inv.period_start  # priced and dated from the plan


def test_renewals_are_raised_ahead_once_and_lapse_when_unpaid(shop):
    with unscoped():
        sub = shop.subscription
        sub.status = SubscriptionStatus.ACTIVE
        sub.trial_ends_at = timezone.now() - timedelta(days=30)
        sub.period_end = timezone.now() + timedelta(days=3)
        sub.save()
        renew_due()
        renew_due()
        assert Invoice.objects.filter(tenant=shop).count() == 1
        sub.period_end = timezone.now() - timedelta(hours=1)
        sub.save()
        renew_due()
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAST_DUE


def test_read_only_admins_can_look_but_not_take_payments(client, shop):
    reader = User.objects.create_user("r@p.test", "pw", name="R", is_platform_staff=True,
                                      platform_role=PlatformRole.objects.get(name="Read only"))
    inv = _invoice(shop)
    client.force_login(reader)
    assert client.get(reverse("platform:invoices")).status_code == 200
    assert client.get(reverse("platform:invoice_detail", args=[inv.pk]), **HX).status_code == 200
    assert _pay(client, inv, 100).status_code == 403


def test_a_free_plan_is_not_billed_for_nothing(client, shop, boss):
    from apps.tenancy.models import Plan
    with unscoped():
        sub = shop.subscription
        sub.plan = Plan.objects.get(code="free")
        sub.save(update_fields=["plan"])
    client.force_login(boss)
    r = client.post(reverse("platform:invoice_create"),
                    {"tenant": shop.pk, "period_start": timezone.localdate()}, **HX)
    assert b"Nothing to bill" in r.content
    with unscoped():
        assert not Invoice.objects.filter(tenant=shop).exists()


def test_a_lapsed_trial_with_nothing_billed_gets_its_invoice(shop):
    with unscoped():
        sub = shop.subscription
        sub.status = SubscriptionStatus.PAST_DUE
        sub.trial_ends_at = timezone.now() - timedelta(days=10)
        sub.save()
        renew_due()
        inv = Invoice.objects.get(tenant=shop)
        assert inv.period_start == timezone.localtime(sub.trial_ends_at).date()


def test_the_first_paid_period_starts_when_the_trial_ends(shop):
    with unscoped():
        sub = shop.subscription
        sub.status = SubscriptionStatus.TRIALING
        sub.trial_ends_at = timezone.now() + timedelta(days=2)
        sub.period_end = timezone.now() + timedelta(days=20)
        sub.save()
        renew_due()
        inv = Invoice.objects.get(tenant=shop)
        assert inv.period_start == timezone.localtime(sub.trial_ends_at).date()

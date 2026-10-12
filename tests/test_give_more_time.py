"""
Giving a shop more time.

"Another two weeks and they will pay" is the most ordinary thing a
salesperson asks for, and the platform had no way to do it. The ways round
it were both bad: mark an invoice paid that nobody had paid, which puts a
lie in the books, or let the shop switch off and turn it back on by hand.

The test that matters most here is the last one. An extension the nightly
job quietly undoes is worse than no button at all -- the shop is told it has
two more weeks and is switched off that night.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import unscoped
from apps.tenancy.billing import BillingError, extend_subscription
from apps.tenancy.models import SubscriptionStatus, TenantStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(db, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    return owner


def _subscription(shop):
    with unscoped():
        return shop.subscription


def _set(shop, **fields):
    with unscoped():
        subscription = shop.subscription
        for key, value in fields.items():
            setattr(subscription, key, value)
        subscription.save()
        return subscription


# -- which clock moves ------------------------------------------------------

def test_a_trial_simply_runs_longer(shop):
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.TRIALING,
                        trial_ends_at=now + timedelta(days=2))

    until = extend_subscription(subscription, 14)
    subscription.refresh_from_db()

    assert subscription.status == SubscriptionStatus.TRIALING
    assert (subscription.trial_ends_at - now).days == 16
    assert until == subscription.trial_ends_at


def test_a_paid_shop_is_paid_up_to_a_later_date(shop):
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.ACTIVE,
                        period_end=now + timedelta(days=5))

    extend_subscription(subscription, 10)
    subscription.refresh_from_db()

    assert subscription.status == SubscriptionStatus.ACTIVE
    assert (subscription.period_end - now).days == 15


def test_a_shop_in_grace_gets_the_grace_it_was_promised(shop):
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.GRACE,
                        period_end=now - timedelta(days=3),
                        grace_ends_at=now + timedelta(days=1))

    extend_subscription(subscription, 14)
    subscription.refresh_from_db()

    assert subscription.status == SubscriptionStatus.GRACE
    assert (subscription.grace_ends_at - now).days == 15


def test_a_shop_past_due_starts_working_again(shop):
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.PAST_DUE,
                        period_end=now - timedelta(days=2))

    extend_subscription(subscription, 7)
    subscription.refresh_from_db()

    assert subscription.status == SubscriptionStatus.GRACE
    assert not subscription.is_read_only


def test_a_shop_already_switched_off_comes_back(shop):
    """The whole point: they were suspended on Friday and paid on Monday."""
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.SUSPENDED,
                        grace_ends_at=now - timedelta(days=4))
    with unscoped():
        shop.status = TenantStatus.SUSPENDED
        shop.save(update_fields=["status"])

    extend_subscription(subscription, 14)
    subscription.refresh_from_db()
    with unscoped():
        shop.refresh_from_db()

    assert subscription.status == SubscriptionStatus.GRACE
    assert not subscription.is_read_only, "they still cannot sell"
    assert shop.status == TenantStatus.ACTIVE, "the shop itself is still switched off"
    assert (subscription.grace_ends_at - now).days == 14


def test_two_extensions_add_up(shop):
    """Granted a week, then another week, is a fortnight -- not a week."""
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.TRIALING,
                        trial_ends_at=now + timedelta(days=1))

    extend_subscription(subscription, 7)
    subscription.refresh_from_db()
    extend_subscription(subscription, 7)
    subscription.refresh_from_db()

    assert (subscription.trial_ends_at - now).days == 15


# -- what it refuses --------------------------------------------------------

@pytest.mark.parametrize("days", [0, -5])
def test_it_refuses_a_number_that_gives_nothing(shop, days):
    subscription = _subscription(shop)
    with pytest.raises(BillingError, match="how many days"):
        extend_subscription(subscription, days)


def test_it_refuses_more_than_a_year(shop):
    subscription = _subscription(shop)
    with pytest.raises(BillingError, match="year is the most"):
        extend_subscription(subscription, 400)


# -- through the screen -----------------------------------------------------

def test_the_platform_owner_can_do_it_from_the_shops_page(client, shop, staff):
    now = timezone.now()
    _set(shop, status=SubscriptionStatus.GRACE, grace_ends_at=now - timedelta(days=1))

    client.force_login(staff)
    page = client.get(reverse("platform:tenant_detail", args=[shop.pk]))
    assert "Give them more time" in page.content.decode()

    response = client.post(reverse("platform:tenant_extend", args=[shop.pk]),
                           {"days": "14", "reason": "Paying on Friday"}, follow=True)
    assert "14 more days" in response.content.decode()

    with unscoped():
        shop.subscription.refresh_from_db()
        assert shop.subscription.status == SubscriptionStatus.GRACE
        # Counted from now, because their grace had already run out.
        assert (shop.subscription.grace_ends_at - now).days == 14


def test_who_granted_it_and_why_is_recorded(client, shop, staff):
    from apps.accounts.models import PlatformEvent

    _set(shop, status=SubscriptionStatus.GRACE,
         grace_ends_at=timezone.now() - timedelta(days=1))
    client.force_login(staff)
    client.post(reverse("platform:tenant_extend", args=[shop.pk]),
                {"days": "30", "reason": "Agreed with the owner"}, follow=True)

    with unscoped():
        entry = PlatformEvent.objects.filter(action="platform.shop_extended").first()
        assert entry is not None, "a favour nobody can trace is how books go wrong"
        assert entry.user_id == staff.pk, "who granted it"
        assert entry.target == shop.name
        assert "Agreed with the owner" in str(entry.detail)
        assert "30" in str(entry.detail)


def test_a_shopkeeper_cannot_give_themselves_more_time(client, shop, owner):
    """The owner of a shop is not the owner of the platform."""
    client.force_login(owner)
    response = client.post(reverse("platform:tenant_extend", args=[shop.pk]),
                           {"days": "365"})
    assert response.status_code in (302, 403, 404)
    with unscoped():
        assert shop.subscription.status != SubscriptionStatus.GRACE \
            or shop.subscription.grace_ends_at is None


# -- the one that matters ---------------------------------------------------

def test_tonight_the_job_leaves_the_extension_alone(shop):
    """
    A shop told it has another fortnight, switched off the same night by the
    job that moves everybody along, would be worse than having no button.
    """
    from apps.tenancy.tasks import advance_subscriptions

    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.GRACE,
                        period_end=now - timedelta(days=5),
                        grace_ends_at=now - timedelta(days=1))

    extend_subscription(subscription, 14)
    subscription.refresh_from_db()
    granted = subscription.grace_ends_at

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.GRACE, "the job switched them off"
    assert subscription.grace_ends_at == granted, "the job moved the date"
    assert not subscription.is_read_only


def test_an_extended_trial_survives_the_night_too(shop):
    from apps.tenancy.tasks import advance_subscriptions

    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.TRIALING,
                        trial_ends_at=now - timedelta(hours=1),
                        period_end=now - timedelta(hours=1))

    extend_subscription(subscription, 14)
    subscription.refresh_from_db()
    granted = subscription.trial_ends_at

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.TRIALING
    assert subscription.trial_ends_at == granted


# -- back on trial, rather than in grace ------------------------------------

def test_a_shop_can_be_put_back_on_trial(shop):
    """
    A shop that has never paid and is given another month is still trying
    the thing out. "Grace period" down its sidebar reads like a shop in
    trouble and tells its staff something that is not true.
    """
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.GRACE,
                        grace_ends_at=now - timedelta(days=1))

    extend_subscription(subscription, 30, as_trial=True)
    subscription.refresh_from_db()

    assert subscription.status == SubscriptionStatus.TRIALING
    assert (subscription.trial_ends_at - now).days == 30
    assert subscription.period_end == subscription.trial_ends_at
    assert not subscription.is_read_only


def test_a_suspended_shop_can_go_back_on_trial_too(shop):
    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.SUSPENDED,
                        grace_ends_at=now - timedelta(days=9))
    with unscoped():
        shop.status = TenantStatus.SUSPENDED
        shop.save(update_fields=["status"])

    extend_subscription(subscription, 30, as_trial=True)
    subscription.refresh_from_db()
    with unscoped():
        shop.refresh_from_db()

    assert subscription.status == SubscriptionStatus.TRIALING
    assert shop.status == TenantStatus.ACTIVE
    assert not subscription.is_read_only


def test_a_restored_trial_survives_the_night(shop):
    """The job must not end a trial it has already ended once."""
    from apps.tenancy.tasks import advance_subscriptions

    now = timezone.now()
    subscription = _set(shop, status=SubscriptionStatus.GRACE,
                        period_end=now - timedelta(days=8),
                        grace_ends_at=now - timedelta(days=1))

    extend_subscription(subscription, 30, as_trial=True)
    subscription.refresh_from_db()
    granted = subscription.trial_ends_at

    advance_subscriptions()

    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.TRIALING, "the trial was ended again"
    assert subscription.trial_ends_at == granted


def test_the_screen_offers_it(client, shop, staff):
    _set(shop, status=SubscriptionStatus.GRACE,
         grace_ends_at=timezone.now() - timedelta(days=1))
    client.force_login(staff)

    page = client.get(reverse("platform:tenant_detail", args=[shop.pk])).content.decode()
    assert 'name="as_trial"' in page

    client.post(reverse("platform:tenant_extend", args=[shop.pk]),
                {"days": "30", "as_trial": "on", "reason": "Demo shop"}, follow=True)
    with unscoped():
        shop.subscription.refresh_from_db()
        assert shop.subscription.status == SubscriptionStatus.TRIALING

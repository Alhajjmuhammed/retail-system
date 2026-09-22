"""
What happens to a shop that signs up and never pays.

The Free plan costs nothing and offers no trial, and signing up puts every
new shop on it. That combination used to mean a zero-length trial: the shop
was created already past the end of it.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.tenancy.models import Plan, SubscriptionStatus
from apps.tenancy.services import create_tenant

pytestmark = pytest.mark.django_db


def _free_shop(owner):
    tenant, _ = create_tenant(name="Kibanda cha Asha", owner=owner)
    return tenant


def test_signing_up_puts_a_shop_on_the_free_plan(db, owner):
    tenant = _free_shop(owner)
    assert tenant.subscription.plan.code == "free"
    assert tenant.subscription.plan.trial_days == 0


def test_a_free_shop_is_not_born_already_out_of_trial(db, owner):
    """
    A trial of zero days ends the moment it starts, and the nightly job then
    treats the shop as one that failed to pay for something that is free.
    """
    tenant = _free_shop(owner)
    subscription = tenant.subscription
    assert subscription.status != SubscriptionStatus.TRIALING, (
        "a plan with no trial should not put the shop in one"
    )
    assert not (subscription.trial_ends_at and subscription.trial_ends_at <= timezone.now())


def test_the_nightly_job_does_not_suspend_a_free_shop(db, owner):
    from apps.tenancy.tasks import advance_subscriptions

    tenant = _free_shop(owner)
    for _ in range(3):        # trial -> past due -> grace -> suspended
        advance_subscriptions()
    tenant.subscription.refresh_from_db()
    assert tenant.subscription.status not in {
        SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE,
        SubscriptionStatus.SUSPENDED,
    }, f"a free shop ended up {tenant.subscription.status}"
    assert not tenant.subscription.is_read_only, "a free shop cannot be stopped from selling"


def test_a_paid_plan_still_gets_its_trial(db, owner):
    tenant, _ = create_tenant(name="Duka kubwa", owner=owner,
                              plan=Plan.objects.get(code="business"))
    subscription = tenant.subscription
    assert subscription.status == SubscriptionStatus.TRIALING
    assert subscription.trial_ends_at > timezone.now()


def test_the_welcome_message_says_something_true(client, db):
    """"On a 0-day trial" is not a sentence anybody should read."""
    response = client.post(reverse("signup"), {
        "business_name": "Duka la Mwanzo", "name": "Asha",
        "email": "asha@mwanzo.test", "phone": "0712000000",
        "password": "chamchama12345",
    }, follow=True)
    assert response.status_code == 200
    body = response.content.decode()
    assert "0-day trial" not in body

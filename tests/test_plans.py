"""
Plans: checked before they are saved, and never able to break signup.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import PlatformRole, User
from apps.core.context import unscoped
from apps.tenancy.models import Plan

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def boss(db):
    return User.objects.create_user("boss@p.test", "pw", name="Boss", is_platform_staff=True,
                                    platform_role=PlatformRole.objects.get(name="Super admin"))


def _post(client, url, **fields):
    data = {"name": "Probe plan", "price_monthly": "1000", "price_annual": "10000",
            "trial_days": "14", "sort_order": "5", "is_public": "on"}
    data.update(fields)
    return client.post(url, data, **HX)


def test_bad_numbers_are_refused_in_the_form(client, boss):
    client.force_login(boss)
    for field, value in [("trial_days", "abc"), ("price_monthly", "-5"), ("limit:branches", "-1"),
                         ("limit:users", "lots"), ("trial_days", "9999")]:
        r = _post(client, reverse("platform:plan_create"), **{field: value})
        assert r.status_code == 200, field
    assert not Plan.objects.filter(name="Probe plan").exists()


def test_a_duplicate_name_is_refused(client, boss):
    client.force_login(boss)
    r = _post(client, reverse("platform:plan_create"), name=Plan.objects.first().name)
    assert b"already a plan called" in r.content


def test_a_plan_is_created_with_its_limits_and_features(client, boss):
    client.force_login(boss)
    r = _post(client, reverse("platform:plan_create"), **{"limit:branches": "3", "features": ["loyalty"]})
    assert r.status_code == 204
    plan = Plan.objects.get(name="Probe plan")
    assert plan.limit("branches") == 3 and plan.limit("users") is None
    assert plan.feature_keys() == {"loyalty"} and plan.price_monthly == Decimal("1000")


def test_the_last_plan_at_signup_cannot_be_hidden_or_deleted(client, boss):
    only = Plan.objects.order_by("sort_order").first()
    Plan.objects.exclude(pk=only.pk).update(is_public=False)
    client.force_login(boss)
    r = _post(client, reverse("platform:plan_edit", args=[only.pk]), name=only.name, is_public="")
    assert b"At least one plan must be offered" in r.content
    with unscoped():
        only.subscriptions.all().delete()
    client.post(reverse("platform:plan_delete", args=[only.pk]))
    assert Plan.objects.filter(pk=only.pk, is_public=True).exists()


def test_a_plan_with_shops_on_it_cannot_be_deleted(client, boss, shop):
    plan = shop.subscription.plan
    client.force_login(boss)
    client.post(reverse("platform:plan_delete", args=[plan.pk]))
    assert Plan.objects.filter(pk=plan.pk).exists()


def test_the_plan_edit_form_no_longer_hides_a_delete_inside_it(client, boss):
    plan = Plan.objects.first()
    client.force_login(boss)
    html = client.get(reverse("platform:plan_edit", args=[plan.pk]), **HX).content.decode()
    assert html.count("<form") == 1


def test_removing_a_feature_warns_about_the_shops_on_the_plan(client, boss, shop):
    plan = shop.subscription.plan
    keep = sorted(plan.feature_keys())[1:]
    client.force_login(boss)
    r = client.post(reverse("platform:plan_edit", args=[plan.pk]), {
        "name": plan.name, "price_monthly": str(plan.price_monthly), "price_annual": str(plan.price_annual),
        "trial_days": str(plan.trial_days), "sort_order": "0", "is_public": "on", "features": keep,
    }, follow=True)
    assert any("no longer have" in str(m) for m in r.context["messages"])


def test_a_plan_without_offline_selling_can_still_sell(client, shop, main_branch, register, stocked, owner):
    import json
    import uuid

    from apps.core.context import tenant_context
    from apps.pos.services import open_shift
    from apps.tenancy.models import PlanFeature
    PlanFeature.objects.filter(plan=shop.subscription.plan, feature_key="offline_pos").delete()
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    assert client.get(reverse("sync:catalog")).status_code == 200
    r = client.post(reverse("sync:push_sales"), data=json.dumps({"device_id": "till-test", "sales": [{
        "client_uuid": str(uuid.uuid4()),
        "lines": [{"variant_id": stocked["Mkate"].pk, "qty": 1, "unit_price": 1500}],
        "payments": [{"method": "cash", "amount": 1500}],
    }]}), content_type="application/json").json()
    assert r["accepted"]

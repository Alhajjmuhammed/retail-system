"""
Tills and phones: they list themselves, and a switched-off one cannot sell.
"""

import json
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, PlatformRole, Role, User
from apps.core.context import tenant_context, unscoped
from apps.org.models import Device

pytestmark = pytest.mark.django_db


def _register(client, device_id, kind="till", label="Till 1"):
    return client.post(reverse("sync:device_register"), data=json.dumps(
        {"device_id": device_id, "kind": kind, "label": label}), content_type="application/json")


def test_a_till_lists_itself(client, shop, owner):
    client.force_login(owner)
    assert _register(client, "till-abc").status_code == 200
    with tenant_context(shop):
        assert Device.objects.get(device_id="till-abc").label == "Till 1"


def test_a_phone_user_without_a_till_can_list_their_phone(client, shop):
    from apps.accounts.models import Permission, RolePermission
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Floor")
        RolePermission.objects.create(role=role, permission=Permission.objects.get(code="pos.mobile_cart"))
        person = User.objects.create_user("f@x.test", "pw", name="F")
        Membership.objects.create(tenant=shop, user=person, role=role)
    client.force_login(person)
    assert _register(client, "phone-1", kind="phone").status_code == 200


def test_a_switched_off_till_cannot_send_sales(client, shop, owner, main_branch, register, stocked):
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    _register(client, "till-lost")
    with tenant_context(shop):
        Device.objects.filter(device_id="till-lost").update(is_active=False)
    r = client.post(reverse("sync:push_sales"), data=json.dumps({"device_id": "till-lost", "sales": [{
        "client_uuid": str(uuid.uuid4()),
        "lines": [{"variant_id": stocked["Mkate"].pk, "qty": 1, "unit_price": 1500}],
        "payments": [{"method": "cash", "amount": 1500}]}]}), content_type="application/json")
    assert r.status_code == 409 and r.json()["error"] == "device_retired"


def test_a_switched_off_phone_cannot_take_payment(client, shop, owner, stocked):
    client.force_login(owner)
    _register(client, "phone-lost", kind="phone")
    with tenant_context(shop):
        Device.objects.filter(device_id="phone-lost").update(is_active=False)
    r = client.post(reverse("pos:phone_checkout"), data=json.dumps({
        "client_uuid": str(uuid.uuid4()), "method": "mpesa", "reference": "QGR7XK2P",
        "device_id": "phone-lost", "lines": [{"variant_id": stocked["Mkate"].pk, "qty": 1}],
    }), content_type="application/json")
    assert r.status_code == 409


def test_the_till_reports_what_it_is_holding(client, shop, owner):
    client.force_login(owner)
    _register(client, "till-q")
    client.post(reverse("sync:push_sales"), data=json.dumps(
        {"device_id": "till-q", "queued": 3, "sales": []}), content_type="application/json")
    with tenant_context(shop):
        assert Device.objects.get(device_id="till-q").queued == 3


def test_silent_devices_come_first_on_the_platform(client, shop, main_branch):
    boss = User.objects.create_user("b@p.test", "pw", name="B", is_platform_staff=True,
                                    platform_role=PlatformRole.objects.get(name="Super admin"))
    with tenant_context(shop):
        Device.objects.create(branch=main_branch, device_id="fresh", label="Fresh", last_sync_at=timezone.now())
        Device.objects.create(branch=main_branch, device_id="never", label="Never")
        Device.objects.create(branch=main_branch, device_id="old", label="Old",
                              last_sync_at=timezone.now() - timedelta(days=3))
    client.force_login(boss)
    r = client.get(reverse("platform:devices"))
    assert [d.label for d in r.context["devices"]] == ["Never", "Old", "Fresh"]
    assert [d.label for d in client.get(reverse("platform:devices"), {"view": "silent"}).context["devices"]] == ["Never", "Old"]


def test_an_owner_can_switch_off_their_own_device(client, shop, owner, main_branch):
    with tenant_context(shop):
        device = Device.objects.create(branch=main_branch, device_id="shop-dev", label="Counter")
    client.force_login(owner)
    assert client.get(reverse("org:devices")).status_code == 200
    client.post(reverse("org:device_update", args=[device.pk]), {"action": "toggle"})
    with unscoped():
        device.refresh_from_db()
        assert not device.is_active

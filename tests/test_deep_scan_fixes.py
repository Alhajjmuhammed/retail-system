"""
Findings from the final deep scan, each pinned down.

Every test here failed before its fix.
"""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


def _person(shop, email, role, branches=None):
    with tenant_context(shop):
        u = User.objects.create_user(email, "pw", name=email.split("@")[0])
        m = Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name=role))
        if branches is not None:
            m.set_branches(branches, all_branches=False)
    return u, m


# -- High ------------------------------------------------------------------

def test_a_support_session_cannot_create_staff_or_set_passwords(client, shop, db):
    """Support signs in as a stand-in owner; it must not leave a way back in."""
    staff = User.objects.create_user("support@platform.test", "pw", name="Support",
                                     is_platform_staff=True)
    from apps.accounts.models import PlatformRole

    staff.platform_role = PlatformRole.objects.get(name="Support")
    staff.save()
    client.force_login(staff)
    session = client.session
    session["impersonating"] = True
    session["tenant_id"] = shop.pk
    session.save()

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    r = client.post(reverse("accounts:staff_create"),
                    {"name": "Puppet", "email": "puppet@x.test", "role": role.pk,
                     "password": "Puppet-pass-991"})
    assert r.status_code == 403
    with tenant_context(shop):
        assert not Membership.objects.filter(user__email="puppet@x.test").exists()
    assert client.get(reverse("accounts:staff_invite")).status_code == 403


def test_receiving_goods_is_limited_by_value(client, shop, main_branch, stocked):
    from apps.purchasing.models import GoodsReceipt, Supplier, SupplierInvoice

    clerk, _ = _person(shop, "clerk@x.test", "Stock clerk", branches=[main_branch])
    with tenant_context(shop):
        Role.objects.get(name="Stock clerk").grant("stock.receive", limit=Decimal("100000"))
        s = Supplier.objects.create(name="Azam")
    client.force_login(clerk)
    client.post(reverse("purchasing:receipt_create"), {"supplier": s.pk})
    with tenant_context(shop):
        receipt = GoodsReceipt.objects.get()
    url = reverse("purchasing:receipt_detail", args=[receipt.pk])
    client.post(url, {"action": "add_line", "variant": stocked["Mkate"].pk, "qty": "10",
                      "unit_cost": "50000"})          # 500,000 worth
    r = client.post(url, {"action": "post"})
    assert r.status_code == 403                        # needs a manager
    with tenant_context(shop):
        assert not SupplierInvoice.objects.exists()


def test_cash_out_of_the_till_is_limited(client, shop, main_branch, register):
    cashier, _ = _person(shop, "till@x.test", "Cashier", branches=[main_branch])
    with tenant_context(shop, branch=main_branch, user=cashier):
        shift = open_shift(register=register, opening_float=20000)
    client.force_login(cashier)
    r = client.post(reverse("pos:cash_movement"),
                    {"kind": "pay_out", "amount": "9000000", "reason": "supplier"})
    assert r.status_code == 403                        # over the cashier's ceiling
    client.post(reverse("pos:cash_movement"),
                {"kind": "pay_out", "amount": "30000", "reason": "too much"})
    with tenant_context(shop):
        # more than the drawer should hold, so nothing was recorded
        assert shift.compute_expected_cash() == Decimal("20000")


def test_lockout_follows_the_account_not_the_spelling(client, db, settings):
    User.objects.create_user("salma@example.com", "Right-pass-8812", name="Salma")
    url = reverse("accounts:login")
    for i in range(8):
        client.post(url, {"username": "SALMA@example.com", "password": f"no-{i}"})
    # A different spelling of the same account is the same budget.
    r = client.post(url, {"username": "salma@example.com", "password": "Right-pass-8812"})
    assert "_auth_user_id" not in client.session
    assert b"Too many failed attempts" in r.content


def test_forged_address_header_does_not_reset_the_count(client, db):
    User.objects.create_user("owner@example.com", "Right-pass-8812", name="O")
    url = reverse("accounts:login")
    for i in range(8):
        client.post(url, {"username": "owner@example.com", "password": f"no-{i}"},
                    HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
    r = client.post(url, {"username": "owner@example.com", "password": "Right-pass-8812"},
                    HTTP_X_FORWARDED_FOR="10.0.0.99")
    assert "_auth_user_id" not in client.session and b"Too many" in r.content


def test_an_open_item_from_a_till_sale_is_not_a_voucher(client, shop, main_branch, register,
                                                         stocked, owner):
    """Only a basket handed over from a phone vouches for an open item."""
    from apps.pos.models import CartLine, CartStatus
    from apps.pos.services import new_cart

    cashier, _ = _person(shop, "c@x.test", "Cashier", branches=[main_branch])
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        line = CartLine.objects.create(tenant=shop, cart=cart, description="Loose rice",
                                       qty=1, unit_price=Decimal("2000"), tax_rate=0,
                                       added_via="manual")
        cart.status = CartStatus.CONVERTED
        cart.save(update_fields=["status"])
    with tenant_context(shop, branch=main_branch, user=cashier):
        open_shift(register=register)
    client.force_login(cashier)
    res = client.post(reverse("sync:push_sales"), data=json.dumps({
        "device_id": "till-1",
        "sales": [{"client_uuid": str(uuid.uuid4()), "sold_at": timezone.now().isoformat(),
                   "lines": [{"description": "Loose rice", "qty": 1, "unit_price": 2000,
                              "cart_line_id": line.pk, "added_via": "manual"}],
                   "payments": [{"method": "cash", "amount": 2000}]}],
    }), content_type="application/json").json()
    assert not res["accepted"]


# -- Medium ----------------------------------------------------------------

def test_a_branch_manager_cannot_edit_shop_wide_roles(client, shop, main_branch):
    from apps.org.models import Branch

    with tenant_context(shop):
        Branch.objects.create(tenant=shop, name="Kiosk")
        role = Role.objects.create(tenant=shop, name="Branch boss")
        for code in ("user.manage", "role.manage"):
            role.grant(code)
        u = User.objects.create_user("bb@x.test", "pw", name="BB")
        m = Membership.objects.create(tenant=shop, user=u, role=role)
        m.set_branches([main_branch], all_branches=False)
        cashier = Role.objects.get(name="Cashier")
    client.force_login(u)
    assert client.get(reverse("accounts:role_edit", args=[cashier.pk])).status_code == 403
    assert client.post(reverse("accounts:role_duplicate", args=[cashier.pk])).status_code == 403


def test_a_pending_invitation_does_not_undo_a_suspension(client, shop, owner):
    from apps.accounts.models import Invitation

    u, m = _person(shop, "juma@x.test", "Cashier")
    with tenant_context(shop):
        Invitation.objects.create(tenant=shop, email=u.email, role=m.role,
                                  expires_at=timezone.now() + timezone.timedelta(days=7))
    client.force_login(owner)
    client.post(reverse("accounts:staff_toggle", args=[m.pk]))
    with tenant_context(shop):
        assert not Invitation.objects.filter(email=u.email, accepted_at__isnull=True).exists()


def test_a_reversal_cannot_touch_another_branchs_drawer(client, shop, main_branch, register):
    from apps.customers.models import CreditKind, CreditTransaction, Customer
    from apps.org.models import Branch

    with tenant_context(shop):
        other = Branch.objects.create(tenant=shop, name="Kiosk")
        c = Customer.objects.create(name="Neema", credit_limit=50000)
        CreditTransaction.objects.create(customer=c, kind=CreditKind.CHARGE, amount=5000,
                                         balance_after=5000)
    teller, _ = _person(shop, "t@x.test", "Manager", branches=[main_branch])
    with tenant_context(shop, branch=main_branch, user=teller):
        shift = open_shift(register=register)
    client.force_login(teller)
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "2000", "method": "cash"})
    with tenant_context(shop):
        pay = CreditTransaction.objects.get(kind=CreditKind.PAYMENT)
        m = Membership.objects.get(user=teller)
        m.set_branches([other], all_branches=False)   # moved to the other branch
    client.post(reverse("customers:customer_payment_reverse", args=[pay.pk]))
    with tenant_context(shop):
        assert c.balance == Decimal("3000")            # not undone from over there
        assert shift.cash_movements.count() == 1


def test_a_zero_invoice_cannot_be_raised(client, db, shop):
    from apps.accounts.models import PlatformRole

    admin = User.objects.create_user("boss@platform.test", "pw", name="Boss",
                                     is_platform_staff=True)
    admin.platform_role = PlatformRole.objects.get(is_super=True)
    admin.save()
    client.force_login(admin)
    r = client.post(reverse("platform:invoice_create"),
                    {"tenant": shop.pk, "period_start": timezone.localdate().isoformat(),
                     "amount": "0"}, **HX)
    assert r.status_code == 200 and "more than zero" in r.content.decode()


def test_half_a_bottle_is_refused(client, shop, main_branch, register, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    res = client.post(reverse("sync:push_sales"), data=json.dumps({
        "device_id": "till-1",
        "sales": [{"client_uuid": str(uuid.uuid4()), "sold_at": timezone.now().isoformat(),
                   "lines": [{"variant_id": stocked["Soda 500ml"].pk, "qty": 0.0004,
                              "unit_price": 1000}],
                   "payments": [{"method": "cash", "amount": 1000}]}],
    }), content_type="application/json").json()
    assert not res["accepted"] and "whole" in res["rejected"][0]["error"]


def test_collecting_a_basket_needs_a_post(client, shop, main_branch, owner, stocked):
    client.force_login(owner)
    r = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"variant_id": stocked["Mkate"].pk, "qty": 1}]}),
        content_type="application/json")
    code = r.json()["code"]
    assert client.get(reverse("sync:cart_pull", args=[code])).status_code == 405
    assert client.post(reverse("sync:cart_pull", args=[code])).status_code == 200


def test_garbage_input_does_not_crash_sync(client, shop, main_branch, owner, register):
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    assert client.get(reverse("sync:catalog"), {"since": "2026-13-45T00:00:00"}).status_code == 200
    r = client.post(reverse("sync:push_sales"), data=json.dumps(
        {"device_id": "till-1", "queued": 9999999999, "sales": []}),
        content_type="application/json")
    assert r.status_code == 200


def test_the_product_export_is_spreadsheet_safe(client, shop, owner):
    from apps.catalog.models import Product, TaxRate, Unit

    with tenant_context(shop):
        Product.objects.create(name="=cmd()", base_unit=Unit.objects.get(code="pc"),
                               tax_rate=TaxRate.objects.get(is_default=True))
    client.force_login(owner)
    assert "'=cmd()" in client.get(reverse("catalog:product_export")).content.decode()

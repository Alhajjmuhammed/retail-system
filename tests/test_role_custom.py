"""Roles an owner builds: each sees what it can use, and nothing breaks."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Permission, Role, User, UserPermission
from apps.core.context import tenant_context
from apps.customers.models import CreditKind, CreditTransaction, Customer

pytestmark = pytest.mark.django_db


def _person(shop, email, role):
    with tenant_context(shop):
        u = User.objects.create_user(email, "pw", name=email.split("@")[0])
        m = Membership.objects.create(tenant=shop, user=u, role=role)
    return u, m


def test_role_with_nothing_is_told_to_ask(client, shop):
    with tenant_context(shop):
        empty = Role.objects.create(tenant=shop, name="New role")
    u, _ = _person(shop, "new@x.test", empty)
    client.force_login(u)
    r = client.get(reverse("core:dashboard"))
    assert r.context["no_access"] and "does not include anything yet" in r.content.decode()


def test_bookkeeper_reaches_who_owes_and_takes_a_payment(client, shop):
    with tenant_context(shop):
        books = Role.objects.create(tenant=shop, name="Bookkeeper")
        books.grant("credit.collect")
        books.grant("report.sales")
        c = Customer.objects.create(name="Neema", credit_limit=10000)
        CreditTransaction.objects.create(customer=c, kind=CreditKind.CHARGE, amount=4000,
                                         balance_after=4000)
    u, _ = _person(shop, "books@x.test", books)
    client.force_login(u)
    body = client.get(reverse("core:dashboard")).content.decode()
    assert reverse("customers:statements") in body
    detail = client.get(reverse("customers:customer_detail", args=[c.pk]))
    assert detail.status_code == 200 and not detail.context["may_manage"]
    assert reverse("customers:customer_edit", args=[c.pk]) not in detail.content.decode()
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "1000", "method": "mobile"})
    with tenant_context(shop):
        assert c.balance == Decimal("3000")
    assert client.get(reverse("customers:customer_list")).status_code == 403


def test_cashier_with_a_stock_exception_can_adjust_within_it(client, shop, main_branch, stocked):
    from apps.inventory.models import StockItem

    with tenant_context(shop):
        cashier_role = Role.objects.get(name="Cashier")
    u, m = _person(shop, "plus@x.test", cashier_role)
    with tenant_context(shop):
        UserPermission.objects.create(membership=m, effect="grant", limit_value=Decimal("20000"),
                                      permission=Permission.objects.get(code="stock.adjust"))
        item = StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"])
    client.force_login(u)
    assert reverse("inventory:stock_adjust", args=[item.pk]) in \
        client.get(reverse("inventory:stock_list")).content.decode()
    client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                {"new_qty": "98", "reason": "broken"})       # 2 x 1500 = 3000, within
    client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                {"new_qty": "50", "reason": "lost"})         # 48 x 1500, over the exception
    item.refresh_from_db()
    assert item.qty_on_hand == 98


def test_phone_only_seller_gets_the_phone_and_not_the_till(client, shop):
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Phone seller")
        role.grant("pos.mobile_cart")
        role.grant("product.view")
    u, _ = _person(shop, "phone@x.test", role)
    client.force_login(u)
    body = client.get(reverse("core:dashboard")).content.decode()
    assert reverse("pos:phone") in body and 'href="/pos/"' not in body
    assert client.get(reverse("pos:phone")).status_code == 200
    assert client.get(reverse("pos:till")).status_code == 403

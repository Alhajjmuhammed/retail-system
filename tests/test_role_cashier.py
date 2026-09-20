"""The Cashier role, as a cashier meets it."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.pos.services import add_to_cart, complete_sale, new_cart, open_shift

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier_user(shop, main_branch):
    with tenant_context(shop):
        u = User.objects.create_user("till@x.test", "pw", name="Till")
        m = Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Cashier"))
        m.set_branches([main_branch], all_branches=False)
    return u


@pytest.fixture
def sale(shop, main_branch, register, stocked, cashier_user):
    with tenant_context(shop, branch=main_branch, user=cashier_user):
        shift = open_shift(register=register)
        cart = new_cart(branch=main_branch, register=register)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        return complete_sale(cart, [{"method": "cash", "amount": 1500}], shift=shift)


def test_dashboard_lists_their_own_shift_sales_to_reprint(client, cashier_user, sale):
    client.force_login(cashier_user)
    r = client.get(reverse("core:dashboard"))
    assert [s.pk for s in r.context["my_shift"]["sales"]] == [sale.pk]
    body = r.content.decode()
    assert reverse("pos:receipt", args=[sale.pk]) in body
    assert reverse("pos:sale_return", args=[sale.pk]) in body


def test_receipt_back_goes_to_the_dashboard_not_a_refused_page(client, cashier_user, sale):
    client.force_login(cashier_user)
    r = client.get(reverse("pos:receipt", args=[sale.pk]))
    assert r.status_code == 200 and r.context["back"] == reverse("core:dashboard")
    assert client.get(reverse("pos:sale_detail", args=[sale.pk])).status_code == 403


def test_refund_asks_a_manager_then_lands_on_the_dashboard(client, shop, main_branch,
                                                           cashier_user, sale):
    with tenant_context(shop):
        mgr = User.objects.create_user("boss@x.test", "pw", name="Boss")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.set_pin("4321")
        m.save()
        line = sale.lines.get()
    client.force_login(cashier_user)
    url = reverse("pos:sale_return", args=[sale.pk])
    data = {f"qty:{line.pk}": "1", "method": "cash", "reason": "stale"}
    screen = client.post(url, data)
    assert screen.status_code == 403 and "override_pin" in screen.content.decode()
    r = client.post(url, {**data, "override_email": mgr.email, "override_pin": "4321",
                          "override_nonce": screen.context["nonce"]})
    assert r.status_code == 302 and r.url == reverse("core:dashboard")
    with tenant_context(shop):
        assert sale.returns.count() == 1


def test_cashier_cannot_give_credit_or_see_money_pages(client, cashier_user):
    client.force_login(cashier_user)
    r = client.post(reverse("customers:customer_create"),
                    {"name": "X", "credit_limit": "1000"}, HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and "credit" in r.content.decode().lower()
    for name in ("pos:sale_list", "reports:index", "finance:expense_list", "finance:cashups",
                 "accounts:staff", "purchasing:supplier_list"):
        assert client.get(reverse(name)).status_code == 403, name

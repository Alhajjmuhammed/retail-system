"""
The shop dashboard shows each person what their role allows, for the
branches they work in -- and what needs attention.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def _sell(shop, branch, owner, variant, qty=1):
    from apps.pos.services import add_to_cart, complete_sale, new_cart
    with tenant_context(shop, branch=branch, user=owner):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}])


def test_a_cashier_does_not_see_the_shops_takings(client, shop, main_branch, stocked, owner):
    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        person = User.objects.create_user("c@x.test", "pw", name="C")
        Membership.objects.create(tenant=shop, user=person, role=Role.objects.get(name="Cashier"))
    client.force_login(person)
    r = client.get(reverse("core:dashboard"))
    assert r.status_code == 200 and "today" not in r.context


def test_the_owner_sees_today_net_of_refunds(client, shop, main_branch, stocked, owner):
    from apps.pos.services import create_return
    sale = _sell(shop, main_branch, owner, stocked["Mkate"], qty=2)  # 3,000
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(sale, {sale.lines.first().pk: 1}, reason="x")
    client.force_login(owner)
    today = client.get(reverse("core:dashboard")).context["today"]
    assert today["value"] == 1500 and today["refunded"] == 1500


def test_a_branch_manager_sees_only_their_branch(client, shop, main_branch, stocked, owner):
    from apps.org.models import Branch
    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        other = Branch.objects.create(name="Other")
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.branch_links.create(branch=other)
    client.force_login(mgr)
    assert client.get(reverse("core:dashboard")).context["today"]["value"] == 0


def test_sales_to_check_are_flagged(client, shop, main_branch, stocked, owner):
    from apps.pos.models import Sale
    sale = _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        Sale.objects.filter(pk=sale.pk).update(needs_review=True)
    client.force_login(owner)
    attention = client.get(reverse("core:dashboard")).context["attention"]
    assert any("to check" in a["text"] for a in attention)


def test_the_checklist_goes_once_everything_is_done(client, shop, main_branch, stocked, owner):
    _sell(shop, main_branch, owner, stocked["Mkate"])
    with tenant_context(shop):
        u = User.objects.create_user("s@x.test", "pw", name="S")
        Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Cashier"))
    client.force_login(owner)
    assert client.get(reverse("core:dashboard")).context["steps"] == []


def test_an_overdue_shop_is_told(client, shop, owner):
    from apps.tenancy.models import SubscriptionStatus
    sub = shop.subscription
    sub.status = SubscriptionStatus.PAST_DUE
    sub.save(update_fields=["status"])
    client.force_login(owner)
    assert b"payment is overdue" in client.get(reverse("core:dashboard")).content

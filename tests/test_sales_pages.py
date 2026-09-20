"""The owner's Sales pages: history, detail, refunds, voids, shifts and receipts."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context
from apps.pos.models import CashMovement, Sale, SaleStatus
from apps.pos.services import add_to_cart, close_shift, complete_sale, new_cart, open_shift

pytestmark = pytest.mark.django_db


def _sell(shop, branch, register, variant, user, qty=1, shift=None, method="cash"):
    with tenant_context(shop, branch=branch, user=user):
        cart = new_cart(branch=branch, register=register)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": method, "amount": cart.subtotal}], shift=shift)


def _manager(shop, branch):
    with tenant_context(shop, branch=branch):
        u = User.objects.create_user("mgr@x.test", "pw", name="Mgr")
        Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Manager"))
    return u


def test_sale_list_filters_and_paginates(client, shop, main_branch, register, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
    for _ in range(3):
        _sell(shop, main_branch, register, stocked["Soda 500ml"], owner, shift=shift)
    card = _sell(shop, main_branch, register, stocked["Mkate"], owner, shift=shift, method="card")
    client.force_login(owner)

    r = client.get(reverse("pos:sale_list"))
    assert r.status_code == 200 and r.context["page"].paginator.count == 4

    r = client.get(reverse("pos:sale_list"), {"method": "card"})
    assert [s.pk for s in r.context["sales"]] == [card.pk]

    r = client.get(reverse("pos:sale_list"), {"q": card.number})
    assert [s.pk for s in r.context["sales"]] == [card.pk]

    # Nonsense filters are ignored, never a 500.
    r = client.get(reverse("pos:sale_list"), {"from": "garbage", "branch": "x", "page": "999"})
    assert r.status_code == 200


def test_void_refused_once_the_shift_is_counted(client, shop, main_branch, register, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
    sale = _sell(shop, main_branch, register, stocked["Soda 500ml"], owner, shift=shift)
    with tenant_context(shop, branch=main_branch, user=owner):
        close_shift(shift, counted_cash=1000)
    client.force_login(owner)
    client.post(reverse("pos:sale_void", args=[sale.pk]), {"reason": "late"})
    with tenant_context(shop):
        assert Sale.objects.get(pk=sale.pk).status == SaleStatus.COMPLETED


def test_cash_refund_needs_an_open_drawer(client, shop, main_branch, register, stocked, owner):
    sale = _sell(shop, main_branch, register, stocked["Soda 500ml"], owner)
    client.force_login(owner)
    with tenant_context(shop):
        line = sale.lines.get()
    client.post(reverse("pos:sale_return", args=[sale.pk]),
                {f"qty:{line.pk}": "1", "method": "cash", "reason": "x"})
    with tenant_context(shop):
        assert not sale.returns.exists()


def test_refund_limit_counts_the_lines_returned_not_the_whole_sale(
        client, shop, main_branch, register, stocked, owner):
    mgr = _manager(shop, main_branch)
    with tenant_context(shop):
        rp = Role.objects.get(name="Manager").permissions.get(permission__code="pos.refund")
        rp.limit_value = Decimal("2000")
        rp.save()
    with tenant_context(shop, branch=main_branch, user=mgr):
        shift = open_shift(register=register)
    # 10 x 1000 = 10,000 sale; returning one soda (1,000) is within 2,000.
    sale = _sell(shop, main_branch, register, stocked["Soda 500ml"], mgr, qty=10, shift=shift)
    with tenant_context(shop):
        line = sale.lines.get()
    client.force_login(mgr)
    client.post(reverse("pos:sale_return", args=[sale.pk]),
                {f"qty:{line.pk}": "1", "method": "cash", "reason": "broken"})
    with tenant_context(shop):
        assert sale.returns.count() == 1
    # Five more (5,000) is over the limit: needs approval, nothing refunded.
    client.post(reverse("pos:sale_return", args=[sale.pk]),
                {f"qty:{line.pk}": "5", "method": "cash", "reason": "broken"})
    with tenant_context(shop):
        assert sale.returns.count() == 1


def test_cash_movement_from_the_close_page(client, shop, main_branch, register, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=5000)
    client.force_login(owner)
    r = client.get(reverse("pos:shift_close"))
    assert reverse("pos:cash_movement") in r.content.decode()

    r = client.post(reverse("pos:cash_movement"),
                    {"kind": "drop", "amount": "2000", "reason": "Bank slip 55"})
    assert r.status_code == 302 and r.url == reverse("pos:shift_close")
    with tenant_context(shop):
        m = CashMovement.objects.get(shift=shift)
        assert m.amount == Decimal("-2000")
        assert shift.compute_expected_cash() == Decimal("3000")

    # A negative "put in" cannot hide money: the sign comes from the kind.
    client.post(reverse("pos:cash_movement"),
                {"kind": "pay_in", "amount": "-500", "reason": "x"})
    with tenant_context(shop):
        assert CashMovement.objects.filter(shift=shift).count() == 1


def test_cash_movement_without_a_shift_redirects(client, shop, owner):
    client.force_login(owner)
    r = client.post(reverse("pos:cash_movement"), {"kind": "pay_in", "amount": "1", "reason": "x"})
    assert r.status_code == 302 and r.url == reverse("pos:shift_open")


def test_z_report_shows_the_whole_drawer(client, shop, main_branch, register, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=5000)
    _sell(shop, main_branch, register, stocked["Soda 500ml"], owner, shift=shift)
    _sell(shop, main_branch, register, stocked["Mkate"], owner, shift=shift, method="card")
    client.force_login(owner)
    client.post(reverse("pos:cash_movement"), {"kind": "pay_out", "amount": "300", "reason": "Tea"})
    with tenant_context(shop, branch=main_branch, user=owner):
        shift.refresh_from_db()
        close_shift(shift, counted_cash=5700)
    r = client.get(reverse("pos:shift_report", args=[shift.pk]))
    body = r.content.decode()
    assert r.status_code == 200
    assert r.context["expected"] == Decimal("5700.00")
    assert "Opening float" in body and "Tea" in body and "Counted" in body
    labels = {row["label"] for row in r.context["by_method"]}
    assert labels == {"Cash", "Card"}
    assert r.context["money_in"] == Decimal("2500.00")


def test_receipt_reprint_is_marked(client, shop, main_branch, register, stocked, owner):
    sale = _sell(shop, main_branch, register, stocked["Soda 500ml"], owner)
    client.force_login(owner)
    body = client.get(reverse("pos:receipt", args=[sale.pk])).content.decode()
    assert "COPY" in body and "VOIDED" not in body


def test_shift_open_without_tills_says_so(client, shop, main_branch, register, owner):
    with tenant_context(shop):
        register.is_active = False
        register.save()
    client.force_login(owner)
    r = client.get(reverse("pos:shift_open"))
    assert r.status_code == 200 and "No till at" in r.content.decode()


def test_fiscal_page_paginates(client, shop, owner):
    client.force_login(owner)
    r = client.get(reverse("pos:fiscal_receipts"), {"status": "bogus", "page": "x"})
    assert r.status_code == 200 and r.context["status"] == ""

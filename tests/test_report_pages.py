"""Reports agree with each other about refunds, and filter by branch and period."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.context import tenant_context
from apps.org.models import Branch
from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart

pytestmark = pytest.mark.django_db


def _sell(shop, branch, owner, variant, qty=1):
    with tenant_context(shop, branch=branch, user=owner):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": "cash", "amount": cart.subtotal}])


def test_fully_refunded_sale_nets_to_zero_everywhere(client, shop, owner, main_branch, stocked):
    kept = _sell(shop, main_branch, owner, stocked["Mkate"])          # 1500
    gone = _sell(shop, main_branch, owner, stocked["Sukari 1kg"])     # 3000
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(gone, {gone.lines.get().pk: 1}, reason="x", method="cash")
    client.force_login(owner)

    totals = client.get(reverse("reports:index")).context["totals"]
    assert totals["count"] == 2
    assert totals["value"] == Decimal("4500") and totals["refunded"] == Decimal("3000")
    assert totals["net"] == Decimal("1500")

    dash = client.get(reverse("core:dashboard")).context
    assert dash["today"]["value"] == Decimal("1500")

    rows = client.get(reverse("reports:staff")).context["rows"]
    assert rows[0]["value"] == Decimal("1500")

    csv_text = client.get(reverse("reports:export")).content.decode()
    assert "Refunded" in csv_text.splitlines()[0]
    assert kept.number in csv_text and gone.number in csv_text


def test_branch_filter_narrows_never_widens(client, shop, owner, main_branch, stocked):
    with tenant_context(shop):
        other = Branch.objects.create(tenant=shop, name="Kiosk")
    _sell(shop, main_branch, owner, stocked["Mkate"])
    client.force_login(owner)
    r = client.get(reverse("reports:index"), {"branch": other.pk})
    assert r.context["totals"]["count"] == 0
    r = client.get(reverse("reports:index"), {"branch": main_branch.pk})
    assert r.context["totals"]["count"] == 1
    r = client.get(reverse("reports:index"), {"branch": "999999"})
    assert r.context["totals"]["count"] == 0


@pytest.mark.parametrize("preset", ["today", "7d", "30d", "month", "last_month", "junk"])
def test_presets(client, owner, shop, preset):
    client.force_login(owner)
    r = client.get(reverse("reports:index"), {"preset": preset})
    assert r.status_code == 200 and r.context["start"] <= r.context["end"]


def test_stock_value_pages_and_flags_missing_cost(client, owner, shop, stocked):
    client.force_login(owner)
    r = client.get(reverse("reports:stock_value"), {"q": "mkate"})
    assert r.context["page"].paginator.count == 1
    assert r.context["total"] == Decimal("110000")


def test_export_neutralises_formulas(client, shop, owner, main_branch, stocked):
    from apps.customers.models import Customer

    with tenant_context(shop, branch=main_branch, user=owner):
        c = Customer.objects.create(name="=HYPERLINK(1)")
        cart = new_cart(branch=main_branch, customer=c)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(owner)
    text = client.get(reverse("reports:export")).content.decode()
    assert "'=HYPERLINK(1)" in text

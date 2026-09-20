"""Suppliers, bills and payments, orders and deliveries."""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
)

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def supplier(shop):
    with tenant_context(shop):
        return Supplier.objects.create(name="Azam Ltd")


def _bill(supplier, number, amount, days_ago=0):
    return SupplierInvoice.objects.create(
        supplier=supplier, number=number, amount=amount,
        invoice_date=timezone.localdate() - timezone.timedelta(days=days_ago))


def test_duplicate_supplier_name_is_a_form_error(client, shop, owner, supplier):
    client.force_login(owner)
    r = client.post(reverse("purchasing:supplier_create"), {"name": "azam ltd"}, **HX)
    assert r.status_code == 200 and "already a supplier" in r.content.decode()
    r = client.post(reverse("purchasing:supplier_create"),
                    {"name": "Bakhresa", "email": "not-an-email"}, **HX)
    assert "does not look right" in r.content.decode()
    r = client.post(reverse("purchasing:supplier_create"),
                    {"name": "Bakhresa", "phone": "0" * 80, "payment_terms_days": "30"}, **HX)
    assert r.status_code == 204
    with tenant_context(shop):
        assert len(Supplier.objects.get(name="Bakhresa").phone) == 30


def test_unlinked_payment_settles_oldest_bills(client, shop, owner, supplier):
    with tenant_context(shop):
        old = _bill(supplier, "A1", 1000, days_ago=10)
        new = _bill(supplier, "A2", 2000, days_ago=1)
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_pay", args=[supplier.pk]), {"amount": "3500"})
    with tenant_context(shop):
        assert old.outstanding == 0 and new.outstanding == 0
        assert SupplierPayment.objects.filter(invoice__isnull=True).get().amount == Decimal("500")
        assert supplier.balance == Decimal("-500")


def test_cannot_remove_a_supplier_you_owe(client, shop, owner, supplier):
    with tenant_context(shop):
        _bill(supplier, "B1", 700)
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_delete", args=[supplier.pk]))
    with tenant_context(shop):
        assert Supplier.objects.get(pk=supplier.pk).is_active


def test_payment_reversal_logged(client, shop, owner, supplier):
    from apps.accounts.models import AuditLog

    with tenant_context(shop):
        bill = _bill(supplier, "C1", 900)
        p = SupplierPayment.objects.create(supplier=supplier, invoice=bill, amount=900,
                                           paid_at=timezone.localdate())
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_payment_reverse", args=[p.pk]), {"reason": "typo"})
    with tenant_context(shop):
        assert not SupplierPayment.objects.exists()
        assert bill.outstanding == Decimal("900")
        assert AuditLog.objects.filter(action="supplier.payment_reversed").exists()


def test_order_create_bad_supplier_is_404(client, owner, shop, supplier):
    client.force_login(owner)
    assert client.post(reverse("purchasing:order_create"), {"supplier": "x"}).status_code == 404


def test_same_product_same_cost_merges(client, shop, owner, main_branch, supplier, stocked):
    client.force_login(owner)
    client.post(reverse("purchasing:order_create"), {"supplier": supplier.pk})
    with tenant_context(shop):
        order = PurchaseOrder.objects.get()
    url = reverse("purchasing:order_detail", args=[order.pk])
    v = stocked["Mkate"]
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "5", "unit_cost": "1000"})
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "3", "unit_cost": "1000"})
    client.post(url, {"action": "add_line", "variant": "zzz", "qty": "3", "unit_cost": "1000"})
    with tenant_context(shop):
        line = PurchaseOrderLine.objects.get(order=order)
        assert line.qty_ordered == Decimal("8")
    r = client.get(url)
    assert r.context["may_approve"]
    assert client.get(reverse("purchasing:order_print", args=[order.pk])).status_code == 200


def test_cashier_is_told_who_can_approve(client, shop, owner, main_branch, supplier, stocked):
    from apps.accounts.models import Membership, Role, User

    with tenant_context(shop):
        u = User.objects.create_user("buyer@x.test", "pw", name="Buyer")
        role = Role.objects.create(tenant=shop, name="Buyer")
        role.grant("po.manage")
        Membership.objects.create(tenant=shop, user=u, role=role)
        order = PurchaseOrder.objects.create(tenant=shop, supplier=supplier, branch=main_branch,
                                             reference="PO-1")
        PurchaseOrderLine.objects.create(order=order, variant=stocked["Mkate"], qty_ordered=1,
                                         unit_cost=10)
    client.force_login(u)
    r = client.get(reverse("purchasing:order_detail", args=[order.pk]))
    assert not r.context["may_approve"] and "Ask a manager" in r.content.decode()


def test_reference_numbering_past_9999(shop):
    from apps.purchasing.services import next_reference

    with tenant_context(shop):
        yy = f"{timezone.localdate().year % 100:02d}"
        s = Supplier.objects.create(name="N")
        from apps.org.models import Branch

        b = Branch.objects.first()
        for ref in (f"PO{yy}9999", f"PO{yy}10000"):
            PurchaseOrder.objects.create(tenant=shop, supplier=s, branch=b, reference=ref)
        assert next_reference(PurchaseOrder, "PO") == f"PO{yy}10001"


def test_lists_filter_and_page(client, owner, shop, supplier):
    client.force_login(owner)
    for name, params in (("supplier_list", {"view": "owing", "q": "az"}),
                         ("order_list", {"status": "bogus", "page": "9"}),
                         ("receipt_list", {"q": "x"})):
        assert client.get(reverse(f"purchasing:{name}"), params).status_code == 200

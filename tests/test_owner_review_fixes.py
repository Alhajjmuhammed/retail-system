"""
Findings from the independent review of the owner side, each pinned down.

Every test here failed before its fix.
"""

import json
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AuditLog, Invitation, Membership, Role, User
from apps.core.context import tenant_context
from apps.org.models import Branch
from apps.pos.models import CashMovement
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

def test_manager_cannot_open_or_cancel_an_owner_invitation(client, shop, owner):
    mgr, _ = _person(shop, "mgr@x.test", "Manager")
    with tenant_context(shop):
        inv = Invitation.objects.create(
            tenant=shop, email="new-boss@x.test", role=Role.objects.get(is_owner_role=True),
            expires_at=timezone.now() + timezone.timedelta(days=7))
    client.force_login(mgr)
    assert client.get(reverse("accounts:staff_invite_sent", args=[inv.pk])).status_code == 403
    assert client.post(reverse("accounts:staff_invite_cancel", args=[inv.pk])).status_code == 403
    r = client.get(reverse("accounts:staff"))
    assert inv not in r.context["pending"]
    client.force_login(owner)
    assert client.get(reverse("accounts:staff_invite_sent", args=[inv.pk])).status_code == 200


def test_count_cannot_write_off_past_the_limit(client, shop, main_branch, stocked):
    from apps.inventory.models import StockCount, StockItem

    clerk, _ = _person(shop, "clerk@x.test", "Stock clerk")
    with tenant_context(shop):
        role = Role.objects.get(name="Stock clerk")
        role.grant("stock.adjust", limit=Decimal("50000"))
        role.grant("stock.count")
    client.force_login(clerk)
    client.post(reverse("inventory:count_create"))
    with tenant_context(shop):
        count = StockCount.objects.get()
        line = count.lines.get(variant=stocked["Sukari 1kg"])  # 100 x 3000
    client.post(reverse("inventory:count_detail", args=[count.pk]),
                {"action": "apply", f"qty:{line.pk}": "0"})
    with tenant_context(shop):
        assert StockItem.objects.get(branch=main_branch, variant=stocked["Sukari 1kg"]) \
            .qty_on_hand == Decimal("100")
        count.refresh_from_db()
        assert count.status == "open"


def test_default_list_needs_every_price_and_the_till_never_gets_zero(client, shop, owner, stocked):
    from apps.catalog.models import Price, PriceList

    with tenant_context(shop):
        wholesale = PriceList.objects.create(name="Wholesale")
        Price.objects.create(price_list=wholesale, variant=stocked["Mkate"], amount=1200)
    client.force_login(owner)
    client.post(reverse("catalog:price_lists"), {"action": "default", "price_list": wholesale.pk})
    with tenant_context(shop):
        wholesale.refresh_from_db()
        assert not wholesale.is_default

    with tenant_context(shop):
        Price.objects.filter(variant=stocked["Soda 500ml"]).delete()
    data = client.get(reverse("sync:catalog")).json()
    soda = next(v for v in data["variants"] if v["id"] == stocked["Soda 500ml"].pk)
    assert soda["price"] is None


def test_forgotten_device_stays_blocked(client, shop, owner, main_branch):
    from apps.org.models import Device

    with tenant_context(shop):
        d = Device.objects.create(tenant=shop, branch=main_branch, device_id="lost-9",
                                  is_active=True)
    client.force_login(owner)
    client.post(reverse("org:device_update", args=[d.pk]), {"action": "forget"})
    client.post(reverse("sync:device_register"), data=json.dumps({"device_id": "lost-9"}),
                content_type="application/json")
    with tenant_context(shop):
        assert not Device.objects.get(device_id="lost-9").is_active


def test_branch_confined_manager_cannot_open_other_branch_activity(client, shop, main_branch):
    with tenant_context(shop):
        other = Branch.objects.create(tenant=shop, name="Kiosk")
        entry = AuditLog.objects.create(tenant=shop, action="sale.voided", branch=other,
                                        object_type="Sale", object_id="1")
    mgr, _ = _person(shop, "m1@x.test", "Manager", branches=[main_branch])
    client.force_login(mgr)
    assert client.get(reverse("accounts:audit_entry", args=[entry.pk])).status_code == 404
    r = client.get(reverse("accounts:audit_log"))
    assert all(row["action"] != "sale.voided" for row in r.context["actions"])


def test_expense_edit_cannot_change_a_till_payout_and_delete_puts_cash_back(
        client, shop, owner, main_branch, register):
    from apps.finance.models import Expense, ExpenseCategory

    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=10000)
        rent = ExpenseCategory.objects.create(name="Rent")
    client.force_login(owner)
    client.post(reverse("finance:expense_create"),
                {"amount": "3000", "category": rent.pk, "method": "cash", "from_drawer": "on"}, **HX)
    with tenant_context(shop):
        e = Expense.objects.get()
    r = client.post(reverse("finance:expense_edit", args=[e.pk]),
                    {"amount": "100", "category": rent.pk, "method": "bank"}, **HX)
    assert "fixed" in r.content.decode()
    client.post(reverse("finance:expense_delete", args=[e.pk]))
    with tenant_context(shop):
        assert shift.compute_expected_cash() == Decimal("10000")


def test_money_paid_ahead_settles_the_next_bill(client, shop, owner):
    from apps.purchasing.models import Supplier

    with tenant_context(shop):
        s = Supplier.objects.create(name="Azam")
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_pay", args=[s.pk]), {"amount": "1000"})
    client.post(reverse("purchasing:supplier_bill", args=[s.pk]),
                {"number": "B1", "amount": "1000"})
    with tenant_context(shop):
        bill = s.invoices.get()
        assert bill.outstanding == 0 and s.balance == 0


def test_refusals_in_a_modal_are_shown_in_the_modal(client, shop, main_branch):
    from apps.finance.models import ExpenseCategory

    cashier, _ = _person(shop, "c@x.test", "Cashier")
    with tenant_context(shop):
        cat = ExpenseCategory.objects.create(name="Tea")
        role = Role.objects.get(name="Cashier")
        role.grant("expense.create", limit=Decimal("1000"))
    client.force_login(cashier)
    r = client.post(reverse("finance:expense_create"), {"amount": "5000", "category": cat.pk}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#modal"


def test_owner_can_see_the_approval_screen_in_a_modal(client, shop, main_branch, register, stocked):
    """A dangerous action from a pop-up gets the PIN form in the pop-up."""
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    cashier, _ = _person(shop, "c2@x.test", "Cashier")
    with tenant_context(shop, branch=main_branch, user=cashier):
        open_shift(register=register)
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(cashier)
    r = client.post(reverse("pos:sale_void", args=[sale.pk]), {"reason": "x"}, **HX)
    assert r.status_code == 200 and "override_pin" in r.content.decode()


# -- Medium ----------------------------------------------------------------

def test_stock_with_no_cost_or_price_is_not_free_under_a_limit(client, shop, main_branch):
    from apps.catalog.models import Product, TaxRate, Unit
    from apps.inventory.services import get_stock_item

    clerk, _ = _person(shop, "k@x.test", "Stock clerk")
    with tenant_context(shop, branch=main_branch):
        Role.objects.get(name="Stock clerk").grant("stock.adjust", limit=Decimal("100000"))
        p = Product.objects.create(name="Mystery", base_unit=Unit.objects.get(code="pc"),
                                   tax_rate=TaxRate.objects.get(is_default=True))
        item = get_stock_item(p.default_variant, main_branch)
    client.force_login(clerk)
    client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                {"new_qty": "1000000", "reason": "found"})
    item.refresh_from_db()
    assert item.qty_on_hand == 0


def test_removed_barcode_is_not_restored_by_saving(client, shop, owner, stocked):
    from apps.catalog.models import Barcode
    from apps.catalog.services import attach_barcode

    v = stocked["Mkate"]
    with tenant_context(shop):
        code = attach_barcode(v, "777")
    client.force_login(owner)
    client.post(reverse("catalog:barcode_delete", args=[code.pk]), **HX)
    form = client.get(reverse("catalog:product_edit", args=[v.product_id]), **HX).context["form"]
    assert "barcode" not in form.fields
    with tenant_context(shop):
        assert not Barcode.objects.filter(code="777").exists()


def test_product_audit_keeps_the_old_name_and_prices(client, shop, owner, stocked):
    from apps.catalog.models import TaxRate, Unit

    v = stocked["Mkate"]
    client.force_login(owner)
    with tenant_context(shop):
        base = {"base_unit": Unit.objects.get(code="pc").pk,
                "tax_rate": TaxRate.objects.get(is_default=True).pk, "is_active": "on",
                "track_stock": "on", "sellable_at_pos": "on"}
    client.post(reverse("catalog:product_edit", args=[v.product_id]),
                {**base, "name": "Mkate mkubwa", "price": "10"}, **HX)
    with tenant_context(shop):
        row = AuditLog.objects.filter(action="product.updated").latest("created_at")
    assert row.before["name"] == "Mkate" and row.after["name"] == "Mkate mkubwa"
    assert list(row.before["prices"].values()) == ["1500.00"]


def test_split_supplier_payment_is_taken_back_whole(client, shop, owner):
    from apps.purchasing.models import Supplier, SupplierInvoice, SupplierPayment

    with tenant_context(shop):
        s = Supplier.objects.create(name="Bakhresa")
        for n, amt in (("A", 1000), ("B", 2000)):
            SupplierInvoice.objects.create(supplier=s, number=n, amount=amt,
                                           invoice_date=timezone.localdate())
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_pay", args=[s.pk]), {"amount": "3000"})
    with tenant_context(shop):
        first = SupplierPayment.objects.first()
    client.post(reverse("purchasing:supplier_payment_reverse", args=[first.pk]), {"reason": "typo"})
    with tenant_context(shop):
        assert not SupplierPayment.objects.exists()


def test_customer_cash_undo_takes_it_out_of_the_till_once(client, shop, owner, main_branch,
                                                            register):
    from apps.customers.models import CreditKind, CreditTransaction, Customer

    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
        c = Customer.objects.create(name="Neema", credit_limit=10000)
        CreditTransaction.objects.create(customer=c, kind=CreditKind.CHARGE, amount=5000,
                                         balance_after=5000)
    client.force_login(owner)
    client.post(reverse("customers:customer_payment", args=[c.pk]),
                {"amount": "2000", "method": "cash"})
    with tenant_context(shop):
        pay = CreditTransaction.objects.get(kind=CreditKind.PAYMENT)
    client.post(reverse("customers:customer_payment_reverse", args=[pay.pk]))
    client.post(reverse("customers:customer_payment_reverse", args=[pay.pk]))
    with tenant_context(shop):
        assert c.balance == Decimal("5000")
        assert shift.cash_movements_total() == 0
        assert CashMovement.objects.filter(shift=shift).count() == 2


def test_money_with_three_decimals_is_refused(client, shop, owner):
    from apps.purchasing.models import Supplier, SupplierPayment

    with tenant_context(shop):
        s = Supplier.objects.create(name="Cents")
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_pay", args=[s.pk]), {"amount": "100.005"})
    with tenant_context(shop):
        assert not SupplierPayment.objects.exists()


def test_supplier_form_next_cannot_leave_the_site(client, shop, owner):
    client.force_login(owner)
    r = client.post(reverse("purchasing:supplier_create"),
                    {"name": "Safe", "next": "https://evil.example/"})
    assert r.url == reverse("purchasing:supplier_list")


def test_reactivating_staff_respects_the_user_limit(client, db):
    from apps.tenancy.models import Plan
    from apps.tenancy.services import create_tenant

    owner = User.objects.create_user("free@x.test", "pw", name="Free")
    shop, _ = create_tenant(name="Kidogo", owner=owner, plan=Plan.objects.get(code="free"))
    limit = shop.limit_for("users")
    _u, m = _person(shop, "a@x.test", "Cashier")
    client.force_login(owner)
    client.post(reverse("accounts:staff_toggle", args=[m.pk]))  # suspend
    for i in range(limit):  # fill the plan back up
        _person(shop, f"fill{i}@x.test", "Cashier")
    client.post(reverse("accounts:staff_toggle", args=[m.pk]))  # try to reactivate
    with tenant_context(shop):
        m.refresh_from_db()
        assert not m.is_active


def test_supplier_page_query_count_does_not_grow_with_bills(client, shop, owner):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.purchasing.models import Supplier, SupplierInvoice

    with tenant_context(shop):
        s = Supplier.objects.create(name="Many bills")
    client.force_login(owner)

    def count():
        with CaptureQueriesContext(connection) as q:
            client.get(reverse("purchasing:supplier_detail", args=[s.pk]))
        return len(q)

    with tenant_context(shop):
        SupplierInvoice.objects.create(supplier=s, number="0", amount=5,
                                       invoice_date=timezone.localdate())
    small = count()
    with tenant_context(shop):
        for i in range(1, 30):
            SupplierInvoice.objects.create(supplier=s, number=str(i), amount=5,
                                           invoice_date=timezone.localdate())
    assert count() <= small


def test_refund_limit_counts_earlier_refunds(client, shop, main_branch, register, stocked):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    mgr, _ = _person(shop, "rm@x.test", "Manager")
    with tenant_context(shop):
        rp = Role.objects.get(name="Manager").permissions.get(permission__code="pos.refund")
        rp.limit_value = Decimal("2500")
        rp.save()
    with tenant_context(shop, branch=main_branch, user=mgr):
        open_shift(register=register)
        cart = new_cart(branch=main_branch, register=register)
        add_to_cart(cart, stocked["Soda 500ml"], qty=5)
        sale = complete_sale(cart, [{"method": "cash", "amount": 5000}])
        line = sale.lines.get()
    client.force_login(mgr)
    for _ in range(3):  # 2,000 each; the second already goes past 2,500 in total
        client.post(reverse("pos:sale_return", args=[sale.pk]),
                    {f"qty:{line.pk}": "2", "method": "cash", "reason": "x"})
    with tenant_context(shop):
        assert sale.returns.count() == 1

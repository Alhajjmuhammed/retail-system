"""
Regression tests for the shop-side review: each one reproduces a finding.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context, unscoped

pytestmark = pytest.mark.django_db


def _member(shop, email, role_name, *, pin=None, branches=()):
    with tenant_context(shop):
        user = User.objects.create_user(email, "pw", name=email.split("@")[0].title())
        m = Membership.objects.create(tenant=shop, user=user, role=Role.objects.get(name=role_name))
        for b in branches:
            m.branch_links.create(branch=b)
        if pin:
            m.set_pin(pin)
    return user, m


# -- C4: one shop's receipt must not touch another shop's order ------------

def test_a_receipt_cannot_point_at_another_shops_order(client, shop, owner, cashier, business_plan):
    from apps.org.models import Branch
    from apps.purchasing.models import PurchaseOrder, Supplier
    from apps.tenancy.services import create_tenant

    other, _ = create_tenant(name="Other Duka", owner=cashier, plan=business_plan)
    with tenant_context(other, user=cashier):
        their_order = PurchaseOrder.objects.create(
            supplier=Supplier.objects.create(name="Theirs"),
            branch=Branch.objects.first(), reference="PO-X", status="sent",
        )
    with tenant_context(shop):
        mine = Supplier.objects.create(name="Mine")
    client.force_login(owner)
    response = client.post(reverse("purchasing:receipt_create"),
                           {"supplier": mine.pk, "order": their_order.pk})
    assert response.status_code == 404
    with unscoped():
        their_order.refresh_from_db()
        assert their_order.status == "sent"


def test_payments_only_land_on_this_suppliers_bills(client, shop, owner):
    from apps.purchasing.models import Supplier, SupplierInvoice, SupplierPayment

    with tenant_context(shop):
        a = Supplier.objects.create(name="A")
        b = Supplier.objects.create(name="B")
        bill_b = SupplierInvoice.objects.create(supplier=b, number="B1", invoice_date="2026-09-01", amount=100)
    client.force_login(owner)
    client.post(reverse("purchasing:supplier_pay", args=[a.pk]),
                {"amount": "50", "invoice": bill_b.pk, "paid_at": "2026-09-10"})
    with tenant_context(shop):
        assert not SupplierPayment.objects.filter(invoice=bill_b).exists()


def test_receiving_goods_records_what_is_owed(client, shop, owner, main_branch, stocked):
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Azam")
        receipt = GoodsReceipt.objects.create(supplier=supplier, branch=main_branch, reference="GR1")
        GoodsReceiptLine.objects.create(receipt=receipt, variant=stocked["Mkate"], qty=10, unit_cost=1000)
    client.force_login(owner)
    client.post(reverse("purchasing:receipt_detail", args=[receipt.pk]), {"action": "post"})
    with tenant_context(shop):
        supplier.refresh_from_db()
        assert supplier.balance == 10000


# -- H1: the manager PIN actually approves ----------------------------------

def test_a_manager_pin_lets_a_cashier_void(client, shop, main_branch, stocked, register):
    from apps.pos.models import Sale, SaleStatus
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    cashier, _ = _member(shop, "till@shop.test", "Cashier")
    manager, _ = _member(shop, "boss@shop.test", "Manager", pin="4321")
    from apps.pos.services import open_shift

    with tenant_context(shop, branch=main_branch, user=cashier):
        open_shift(register=register)  # the cash goes back out of a drawer
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(cashier)
    url = reverse("pos:sale_void", args=[sale.pk])
    screen = client.post(url, {"reason": "wrong item"})
    assert screen.status_code == 403

    bad = client.post(url, {"reason": "wrong item", "override_email": manager.email,
                            "override_pin": "0000", "override_nonce": screen.context["nonce"]})
    assert b"do not match" in bad.content

    good = {"reason": "wrong item", "override_email": manager.email,
            "override_pin": "4321", "override_nonce": bad.context["nonce"]}
    client.post(url, good)
    # The same approval sent again is refused: one PIN, one action.
    again = client.post(url, good)
    assert b"already used" in again.content
    with tenant_context(shop):
        sale = Sale.objects.get(pk=sale.pk)
        assert sale.status == SaleStatus.VOIDED
        assert sale.void_reason == "wrong item"  # carried through the approval
        assert sale.authorised_by_id == manager.pk


def test_nobody_approves_their_own_request(client, shop, main_branch, stocked):
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    cashier, _m = _member(shop, "till@shop.test", "Cashier", pin="1111")
    with tenant_context(shop, branch=main_branch, user=cashier):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(cashier)
    url = reverse("pos:sale_void", args=[sale.pk])
    screen = client.post(url, {"reason": "x"})
    r = client.post(url, {"reason": "x", "override_email": cashier.email, "override_pin": "1111",
                          "override_nonce": screen.context["nonce"]})
    assert r.status_code == 403 and b"Somebody else" in r.content


# -- H2: cash movements take their sign from the kind ------------------------

def test_a_negative_pay_in_cannot_hide_missing_cash(client, shop, owner, main_branch, register):
    from apps.pos.services import open_shift

    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=0)
    client.force_login(owner)
    client.post(reverse("pos:cash_movement"), {"kind": "pay_in", "amount": "-50000", "reason": "x"})
    client.post(reverse("pos:cash_movement"), {"kind": "bogus", "amount": "10", "reason": "x"})
    client.post(reverse("pos:cash_movement"), {"kind": "pay_out", "amount": "abc", "reason": "x"})
    with tenant_context(shop, branch=main_branch):
        shift.refresh_from_db()
        assert shift.compute_expected_cash() == 0


# -- H3: adjustment limits, wastage, and the item's own branch ----------------

def test_a_write_off_over_the_wastage_limit_needs_approval(client, shop, main_branch, stocked):
    from apps.inventory.models import StockItem

    clerk, _ = _member(shop, "stock@shop.test", "Stock clerk")
    with tenant_context(shop):
        item = StockItem.objects.get(variant=stocked["Sukari 1kg"], branch=main_branch)
    client.force_login(clerk)
    # 100 units at 2,400 = 240,000; the clerk's wastage limit is 50,000.
    r = client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                    {"new_qty": "0", "reason": "expired", "wastage": "on"})
    assert r.status_code == 403
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == 100


# -- H5: transfers cannot create stock ---------------------------------------

def test_lines_cannot_be_added_after_a_transfer_is_sent(client, shop, owner, main_branch, stocked):
    from apps.inventory.models import Transfer, TransferLine
    from apps.inventory.services import send_transfer
    from apps.org.models import Branch

    with tenant_context(shop, branch=main_branch, user=owner):
        other = Branch.objects.create(name="Second")
        t = Transfer.objects.create(from_branch=main_branch, to_branch=other, reference="TR1")
        TransferLine.objects.create(transfer=t, variant=stocked["Mkate"], qty_sent=1)
        send_transfer(t, user=owner)
    client.force_login(owner)
    client.post(reverse("inventory:transfer_detail", args=[t.pk]),
                {"action": "add_line", "variant": stocked["Mkate"].pk, "qty": "50"})
    with tenant_context(shop):
        assert t.lines.count() == 1


# -- M1: starting a count is a POST ------------------------------------------

def test_a_get_does_not_start_a_count(client, shop, owner):
    from apps.inventory.models import StockCount

    client.force_login(owner)
    assert client.get(reverse("inventory:count_create")).status_code == 405
    with tenant_context(shop):
        assert not StockCount.objects.exists()


# -- M4/M6: branches ---------------------------------------------------------

def test_a_duplicate_branch_name_is_a_form_error(client, shop, owner):
    client.force_login(owner)
    r = client.post(reverse("org:branch_create"), {"name": "main", "is_active": "on"})
    assert r.status_code == 200 and b"already a branch" in r.content


def test_the_default_branch_cannot_be_switched_off(client, shop, owner, main_branch):
    client.force_login(owner)
    client.post(reverse("org:branch_edit", args=[main_branch.pk]),
                {"name": "Main", "is_default": "on"})
    main_branch.refresh_from_db()
    assert main_branch.is_active


# -- M5: reactivating counts against the plan --------------------------------

def test_reactivating_a_branch_counts_against_the_limit(shop, owner):
    from apps.core.features import LimitExceeded
    from apps.org.models import Branch

    with tenant_context(shop, user=owner):
        limit = shop.limit_for("branches")
        if not limit:
            pytest.skip("plan has no branch limit")
        extra = []
        while shop.usage_of("branches") < limit:
            extra.append(Branch.objects.create(name=f"B{len(extra)}"))
        spare = Branch.objects.create(name="Spare", is_active=False)
        spare.is_active = True
        with pytest.raises(LimitExceeded):
            spare.save()


# -- M10: import cannot exceed the importer ----------------------------------

def test_an_import_does_not_set_prices_without_permission(client, shop, main_branch, stocked):
    from django.core.files.uploadedfile import SimpleUploadedFile

    clerk, _ = _member(shop, "stock@shop.test", "Stock clerk")
    client.force_login(clerk)
    csv = b"name,price\nMkate,1\n"
    client.post(reverse("catalog:product_import"),
                {"file": SimpleUploadedFile("p.csv", csv, content_type="text/csv"),
                 "update_existing": "on"})
    with tenant_context(shop):
        assert stocked["Mkate"].price_for() == 1500


# -- H4: branch scope on object URLs -----------------------------------------

def test_a_manager_of_one_branch_cannot_void_anothers_sale(client, shop, main_branch, stocked, owner):
    from apps.org.models import Branch
    from apps.pos.models import Sale, SaleStatus
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, user=owner):
        second = Branch.objects.create(name="Second")
    manager, _ = _member(shop, "mgr2@shop.test", "Manager", branches=[second])
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(manager)
    client.post(reverse("pos:sale_void", args=[sale.pk]), {"reason": "nope"})
    with tenant_context(shop):
        assert Sale.objects.get(pk=sale.pk).status == SaleStatus.COMPLETED


# -- M7: refunds go back the way they were paid ------------------------------

def test_a_cash_sale_cannot_be_refunded_on_account(shop, main_branch, stocked, owner):
    from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart

    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=2)
        sale = complete_sale(cart, [{"method": "cash", "amount": 3000}])
        with pytest.raises(ValueError, match="the way it was paid"):
            create_return(sale, {sale.lines.first().pk: 1}, reason="x", method="credit")


# -- approvals that had no button --------------------------------------------

def test_an_expense_can_be_approved_but_not_by_its_author(client, shop, owner, main_branch):
    from apps.finance.models import Expense, ExpenseCategory

    manager, _ = _member(shop, "boss@shop.test", "Manager")
    with tenant_context(shop, branch=main_branch, user=owner):
        cat = ExpenseCategory.objects.create(name="Rent")
        mine = Expense.objects.create(branch=main_branch, category=cat, amount=100, spent_at="2026-09-01")
    client.force_login(owner)
    client.post(reverse("finance:expense_approve", args=[mine.pk]))
    mine.refresh_from_db()
    assert mine.approved_by_id is None
    client.force_login(manager)
    client.post(reverse("finance:expense_approve", args=[mine.pk]))
    mine.refresh_from_db()
    assert mine.approved_by_id == manager.pk


# -- own password ------------------------------------------------------------

def test_you_can_change_your_own_password(client, shop, owner):
    owner.set_password("old-password-1")
    owner.save()
    client.force_login(owner)
    client.post(reverse("accounts:password_change"), {
        "old_password": "old-password-1",
        "new_password1": "a-brand-new-one-9", "new_password2": "a-brand-new-one-9",
    })
    owner.refresh_from_db()
    assert owner.check_password("a-brand-new-one-9")
    assert client.get(reverse("core:dashboard")).status_code == 200  # still signed in


def test_login_ignores_email_case(client, shop, owner):
    owner.set_password("pw-for-case-1")
    owner.save()
    r = client.post(reverse("accounts:login"), {"username": owner.email.upper(), "password": "pw-for-case-1"})
    assert r.status_code == 302


def test_a_pin_must_be_digits(client, shop, owner):
    _, m = _member(shop, "till@shop.test", "Cashier")
    client.force_login(owner)
    r = client.post(reverse("accounts:staff_edit", args=[m.pk]),
                    {"all_branches": "on", "name": "Till", "role": m.role_id, "pin": "ab"})
    assert b"4 to 8 digits" in r.content

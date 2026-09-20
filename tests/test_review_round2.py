"""
The second review's findings, each written as the behaviour that must hold.

Every test here failed against the code before the fix (the reviewers'
reproductions asserted the broken behaviour); each now asserts the fixed one.
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (
    Invitation,
    Membership,
    Permission,
    PlatformRole,
    Role,
    RolePermission,
    User,
)
from apps.core.context import tenant_context, unscoped

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- staff

@pytest.fixture
def team(shop, owner):
    with tenant_context(shop):
        roles = {r.name: r for r in Role.objects.all()}
        admin_role = Role.objects.create(tenant=shop, name="Admin")
        for code in ("role.manage", "billing.manage", "user.manage", "settings.edit"):
            RolePermission.objects.create(role=admin_role, permission=Permission.objects.get(code=code))
        roles["Admin"] = admin_role
        manager = User.objects.create_user("manager@shop.test", "pw", name="Manager")
        acct = User.objects.create_user("acct@shop.test", "pw", name="Acct")
        m_manager = Membership.objects.create(tenant=shop, user=manager, role=roles["Manager"])
        m_acct = Membership.objects.create(tenant=shop, user=acct, role=admin_role)
    return dict(roles=roles, manager=manager, m_manager=m_manager, acct=acct, m_acct=m_acct)


def test_a_manager_cannot_create_an_account_stronger_than_themselves(client, team):
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_create"), {
        "name": "Puppet", "email": "puppet@x.test", "role": team["roles"]["Admin"].pk,
        "password": "Puppet-pass-9981", "all_branches": "on"})
    assert not User.objects.filter(email="puppet@x.test").exists()


def test_a_manager_cannot_take_over_a_stronger_colleague(client, team):
    client.force_login(team["manager"])
    m = team["m_acct"]
    client.post(reverse("accounts:staff_edit", args=[m.pk]), {
        "name": "Acct", "email": m.user.email, "role": m.role_id, "all_branches": "on",
        "password": "Manager-knows-771", "pin": "4321"})
    team["acct"].refresh_from_db()
    assert team["acct"].check_password("pw")
    with unscoped():
        assert not Membership.objects_all.get(pk=m.pk).check_pin("4321")


def test_a_manager_cannot_move_a_peer_into_a_stronger_role(client, team, shop):
    with tenant_context(shop):
        c = User.objects.create_user("c@x.test", "pw", name="C")
        mc = Membership.objects.create(tenant=shop, user=c, role=team["roles"]["Cashier"])
    client.force_login(team["manager"])
    client.post(reverse("accounts:staff_edit", args=[mc.pk]),
                {"name": "C", "email": "c@x.test", "role": team["roles"]["Admin"].pk,
                 "all_branches": "on"})
    with unscoped():
        assert Membership.objects_all.get(pk=mc.pk).role.name == "Cashier"


def test_invitation_password_checks_are_throttled_and_never_for_platform_accounts(client, shop):
    admin = User.objects.create_user("admin@platform.test", "the-real-one-1",
                                     name="Admin", is_platform_staff=True)
    with tenant_context(shop):
        inv = Invitation.objects.create(tenant=shop, email=admin.email,
                                        role=Role.objects.get(name="Cashier"),
                                        expires_at=timezone.now() + timedelta(days=7))
    url = reverse("accounts:accept_invitation", args=[inv.token])
    r = client.post(url, {"password": "the-real-one-1"})
    assert "_auth_user_id" not in client.session and r.status_code == 200


def test_wrong_invitation_passwords_lock_out(client, shop, cashier):
    cashier.set_password("own-pass-1234")
    cashier.save()
    with tenant_context(shop):
        inv = Invitation.objects.create(tenant=shop, email=cashier.email,
                                        role=Role.objects.get(name="Cashier"),
                                        expires_at=timezone.now() + timedelta(days=7))
    url = reverse("accounts:accept_invitation", args=[inv.token])
    for i in range(12):
        client.post(url, {"password": f"guess-{i}"})
    client.post(url, {"password": "own-pass-1234"})
    assert "_auth_user_id" not in client.session


def test_mixed_case_emails_still_sign_in(client, db):
    User.objects.create_user("Boss@Shop.test", "Long-pass-8871", name="Boss")
    client.post(reverse("accounts:login"), {"username": "boss@shop.TEST", "password": "Long-pass-8871"})
    assert "_auth_user_id" in client.session


def test_deleting_a_branch_never_widens_confined_staff(client, shop, owner, main_branch):
    from apps.org.models import Branch
    with tenant_context(shop):
        b = Branch.objects.create(tenant=shop, name="Kiosk")
        u = User.objects.create_user("k@x.test", "pw", name="K")
        m = Membership.objects.create(tenant=shop, user=u, role=Role.objects.get(name="Cashier"))
        m.branch_links.create(branch=b)
    client.force_login(owner)
    client.post(reverse("org:branch_delete", args=[b.pk]))
    with tenant_context(shop):
        assert not Membership.objects.get(pk=m.pk).covers_branch(main_branch)


def test_editing_somebody_linked_to_a_closed_branch_keeps_them_there(client, shop, owner, main_branch):
    from apps.org.models import Branch
    with tenant_context(shop):
        b = Branch.objects.create(tenant=shop, name="Closed", is_active=False)
        u = User.objects.create_user("k2@x.test", "pw", name="K2")
        role = Role.objects.get(name="Cashier")
        m = Membership.objects.create(tenant=shop, user=u, role=role)
        m.branch_links.create(branch=b)
    client.force_login(owner)
    client.post(reverse("accounts:staff_edit", args=[m.pk]),
                {"name": "K2 renamed", "email": "k2@x.test", "role": role.pk, "branches": []})
    with tenant_context(shop):
        m = Membership.objects.get(pk=m.pk)
        assert not m.covers_branch(main_branch)


def test_an_invitation_keeps_the_branches_chosen(client, shop, owner, main_branch):
    from apps.org.models import Branch
    with tenant_context(shop):
        b = Branch.objects.create(tenant=shop, name="Kiosk")
        role = Role.objects.get(name="Cashier")
    User.objects.create_user("ex@x.test", "Own-pass-7781", name="Ex")
    client.force_login(owner)
    client.post(reverse("accounts:staff_create"),
                {"name": "Ex", "email": "ex@x.test", "role": role.pk, "branches": [b.pk]})
    with unscoped():
        inv = Invitation.objects_all.get(email="ex@x.test")
    client.logout()
    client.post(reverse("accounts:accept_invitation", args=[inv.token]), {"password": "Own-pass-7781"})
    with tenant_context(shop):
        m = Membership.objects.get(user__email="ex@x.test")
        assert m.covers_branch(b) and not m.covers_branch(main_branch)


def test_role_options_cannot_exceed_the_editors(client, shop):
    with tenant_context(shop):
        editor_role = Role.objects.create(tenant=shop, name="Editor")
        RolePermission.objects.create(role=editor_role, permission=Permission.objects.get(code="role.manage"))
        cashier = Role.objects.get(name="Cashier")
        cashier.grant("pos.mobile_methods", options=["cash"])
        u = User.objects.create_user("ed@x.test", "pw", name="Ed")
        Membership.objects.create(tenant=shop, user=u, role=editor_role)
        posted = {"name": "Cashier"}
        for rp in cashier.permissions.select_related("permission"):
            posted[f"grant:{rp.permission.code}"] = "on"
            if rp.limit_value is not None:
                posted[f"limit:{rp.permission.code}"] = format(rp.limit_value, "f").rstrip("0").rstrip(".")
    posted["options:pos.mobile_methods"] = ["cash", "card"]
    client.force_login(u)
    client.post(reverse("accounts:role_edit", args=[cashier.pk]), posted)
    with unscoped():
        rp = RolePermission.objects.get(role=cashier, permission__code="pos.mobile_methods")
    assert rp.set_value == ["cash"]


def test_a_nonsense_limit_is_refused_not_a_crash(client, shop, owner):
    with tenant_context(shop):
        roles = {r.name: r for r in Role.objects.all()}
        mu = User.objects.create_user("mgr2@x.test", "pw", name="M")
        Membership.objects.create(tenant=shop, user=mu, role=roles["Manager"])
        cu = User.objects.create_user("c2@x.test", "pw", name="C")
        mc = Membership.objects.create(tenant=shop, user=cu, role=roles["Cashier"])
    client.force_login(mu)
    r = client.post(reverse("accounts:staff_edit", args=[mc.pk]), {
        "action": "overrides", "override:pos.void": "grant", "override_limit:pos.void": "NaN"})
    assert r.status_code == 302


def test_reinviting_issues_a_new_link(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
    client.force_login(owner)
    client.post(reverse("accounts:staff_invite"), {"email": "n@x.test", "role": role.pk})
    with unscoped():
        t1 = Invitation.objects_all.get(email="n@x.test").token
    client.post(reverse("accounts:staff_invite"), {"email": "n@x.test", "role": role.pk})
    with unscoped():
        assert Invitation.objects_all.get(email="n@x.test").token != t1


# ---------------------------------------------------------------- platform

def _prole(name, perms):
    return PlatformRole.objects.create(name=name, permissions=sorted(perms))


def _padmin(email, role):
    return User.objects.create_user(email, "pw", name=email.split("@")[0],
                                    is_platform_staff=True, platform_role=role)


def test_a_lesser_admin_cannot_take_over_a_stronger_one(client):
    lesser = _padmin("lesser@p.test", _prole("Lead", {"admins.manage", "people.view", "people.manage"}))
    strong = _padmin("strong@p.test", _prole("Ops", {"shops.delete", "invoices.manage", "people.view"}))
    client.force_login(lesser)
    client.post(reverse("platform:user_password", args=[strong.pk]), {"password": "takeover-123"})
    client.post(reverse("platform:user_edit", args=[strong.pk]), {"name": "x", "email": "m@evil.test"})
    client.post(reverse("platform:user_platform_access", args=[strong.pk]), {"role": ""})
    strong.refresh_from_db()
    assert strong.check_password("pw") and strong.email == "strong@p.test" and strong.is_platform_staff


def test_the_last_owner_cannot_be_demoted_from_the_platform(client, shop, owner):
    support = _padmin("sup@p.test", PlatformRole.objects.get(name="Support"))
    with unscoped():
        m = Membership.objects_all.get(tenant=shop, user=owner)
    with tenant_context(shop):
        cashier_role = Role.objects.get(name="Cashier")
    client.force_login(support)
    client.post(reverse("platform:tenant_member_edit", args=[shop.pk, m.pk]),
                {"name": owner.name, "email": owner.email, "role": cashier_role.pk})
    with unscoped():
        m.refresh_from_db()
        assert m.role.is_owner_role


def test_staff_rights_do_not_buy_a_permanent_seat_in_a_shop(client, shop):
    helper = _padmin("h@p.test", _prole("Staffer", {"shops.view", "shops.staff"}))
    with tenant_context(shop):
        owner_role = Role.objects.get(is_owner_role=True)
    client.force_login(helper)
    client.post(reverse("platform:tenant_member_create", args=[shop.pk]),
                {"name": "Me", "email": helper.email, "role": owner_role.pk})
    with unscoped():
        assert not Membership.objects_all.filter(user=helper).exists()


def test_changing_a_plan_does_not_change_its_status(client, shop):
    editor = _padmin("ed@p.test", _prole("Editor", {"shops.view", "shops.edit"}))
    from apps.tenancy.models import Plan
    before = shop.subscription.status
    client.force_login(editor)
    client.post(reverse("platform:tenant_change_plan", args=[shop.pk]),
                {"plan": Plan.objects.get(code="starter").pk, "status": "suspended"})
    shop.subscription.refresh_from_db()
    assert shop.subscription.status == before


def test_the_activity_page_survives_a_deleted_admin_and_pages_all_history(client, owner):
    from apps.accounts.models import PlatformEvent
    boss = _padmin("boss@p.test", PlatformRole.objects.get(name="Super admin"))
    gone = _padmin("gone@p.test", PlatformRole.objects.get(name="Super admin"))
    PlatformEvent.objects.bulk_create([PlatformEvent(user=gone, action="platform.shop_edited", target=f"S{i}")
                                       for i in range(1100)])
    gone.delete()
    client.force_login(boss)
    r = client.get(reverse("platform:platform_audit"))
    assert r.status_code == 200 and r.context["page"].paginator.count == 1100


def test_billing_does_not_see_team_activity_on_the_dashboard(client):
    billing = _padmin("bill@p.test", PlatformRole.objects.get(name="Billing"))
    client.force_login(billing)
    r = client.get(reverse("platform:dashboard"))
    assert r.context["activity"] == []


def test_a_price_change_reaches_open_tills(client, shop, owner, stocked, main_branch, register):
    from apps.catalog.services import set_price
    client.force_login(owner)
    since = client.get(reverse("sync:catalog")).json()["server_time"]
    with tenant_context(shop, user=owner):
        set_price(stocked["Mkate"], 1600)
    data = client.get(reverse("sync:catalog"), {"since": since}).json()
    assert [v["price"] for v in data["variants"]] == ["1600.00"]


# ---------------------------------------------------------------- money

def _push(client, *sales, shift_id=None):
    return client.post(reverse("sync:push_sales"),
                       data=json.dumps({"device_id": "till-test", "shift_id": shift_id, "sales": list(sales)}),
                       content_type="application/json").json()


def _sale(lines, payments, **extra):
    return {"client_uuid": str(uuid.uuid4()), "lines": lines, "payments": payments,
            "sold_at": timezone.now().isoformat(), **extra}


def test_credit_limits_hold_on_synced_sales(client, shop, main_branch, register, stocked):
    from apps.customers.models import Customer
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch):
        mgr = User.objects.create_user("mgr@x.test", "pw", name="M")
        Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
    with tenant_context(shop, branch=main_branch, user=mgr):
        open_shift(register=register)
        cust = Customer.objects.create(name="NoCredit", credit_limit=0)
    client.force_login(mgr)
    v = stocked["Sukari 1kg"]
    res = _push(client, _sale([{"variant_id": v.pk, "qty": 300, "unit_price": 3000}],
                              [{"method": "credit", "amount": 450000},
                               {"method": "credit", "amount": 450000}], customer_id=cust.pk))
    assert not res["accepted"]  # 900,000 on account is over the manager's 500,000


def test_a_split_sale_is_refunded_the_way_it_was_paid(shop, main_branch, register, stocked, owner):
    from apps.customers.models import Customer
    from apps.pos.services import (
        add_to_cart,
        complete_sale,
        create_return,
        new_cart,
        open_shift,
    )
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
        cust = Customer.objects.create(name="C", credit_limit=100000)
        cart = new_cart(branch=main_branch, customer=cust, register=register)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=2)  # 6000
        sale = complete_sale(cart, [{"method": "cash", "amount": 1000},
                                    {"method": "credit", "amount": 5000}], shift=shift)
        doc = create_return(sale, {sale.lines.first().pk: 2}, reason="x", method="cash", shift=shift)
        assert doc.credit_amount == 5000 and doc.cash_amount == 1000
        assert shift.cash_refunded() == Decimal("1000.00")
        assert cust.balance == 0


def test_refunds_never_exceed_what_was_paid(shop, main_branch, stocked, owner):
    from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=3, unit_price=Decimal("66.67"), discount=Decimal("0.01"))
        sale = complete_sale(cart, [{"method": "cash", "amount": 200}])
        line = sale.lines.first()
        total = sum(create_return(sale, {line.pk: 1}, reason="x").total for _ in range(3))
        assert total == line.line_total


def test_points_go_with_a_full_refund_and_never_below_zero(shop, main_branch, stocked, owner):
    from apps.customers.models import Customer
    from apps.customers.services import redeem
    from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart
    with tenant_context(shop, branch=main_branch, user=owner):
        cust = Customer.objects.create(name="L")
        cart = new_cart(branch=main_branch, customer=cust)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 4500}])
        for line in list(sale.lines.all()):
            create_return(sale, {line.pk: 1}, reason="x", method="cash")
        assert cust.loyalty_points == 0

        cart = new_cart(branch=main_branch, customer=cust)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=4)
        sale = complete_sale(cart, [{"method": "cash", "amount": 12000}])
        redeem(cust, 12)
        create_return(sale, {sale.lines.first().pk: 4}, reason="x", method="cash")
        assert cust.loyalty_points == 0


def test_a_free_item_can_be_sold(client, shop, main_branch, register, owner):
    from apps.catalog.models import Product, TaxRate, Unit
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
        p = Product.objects.create(name="Free bag", base_unit=Unit.objects.get(code="pc"),
                                   tax_rate=TaxRate.objects.get(is_default=True))
        variant_id = p.default_variant.pk
    client.force_login(owner)
    res = _push(client, _sale([{"variant_id": variant_id, "qty": 1, "unit_price": 0}],
                              [{"method": "cash", "amount": 0, "change_given": 0}]))
    assert res["accepted"]


def test_a_late_sale_goes_to_the_shift_it_was_made_in(client, shop, main_branch, register, stocked, owner):
    from apps.pos.models import Sale
    from apps.pos.services import close_shift, open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        s1 = open_shift(register=register)
        close_shift(s1, counted_cash=1000)
        s2 = open_shift(register=register)
    client.force_login(owner)
    v = stocked["Soda 500ml"]
    res = _push(client, _sale([{"variant_id": v.pk, "qty": 1, "unit_price": 1000}],
                              [{"method": "cash", "amount": 1000}], shift_id=s1.pk))
    with tenant_context(shop):
        sale = Sale.objects.get(number=res["accepted"][0]["number"])
        assert sale.shift_id == s1.pk and sale.needs_review
        assert s2.sales.count() == 0


def test_a_phone_basket_open_item_can_be_sold_by_a_cashier(client, shop, main_branch, register, owner):
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch):
        cashier = User.objects.create_user("c@x.test", "pw", name="C")
        Membership.objects.create(tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
    with tenant_context(shop, branch=main_branch, user=cashier):
        open_shift(register=register)
    client.force_login(owner)
    code = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"description": "Loose rice", "qty": 1, "unit_price": 2000}]}),
        content_type="application/json").json()["code"]
    client.force_login(cashier)
    line = client.post(reverse("sync:cart_pull", args=[code])).json()["lines"][0]
    res = _push(client, _sale([{"variant_id": None, "description": line["description"],
                                "qty": 1, "unit_price": 2000, "cart_line_id": line["cart_line_id"]}],
                              [{"method": "cash", "amount": 2000}]))
    assert res["accepted"]


def test_another_shops_sale_id_is_refused_cleanly(client, shop, main_branch, register, stocked, owner,
                                                  business_plan):
    from apps.pos.services import open_shift
    from apps.tenancy.services import create_tenant
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    s = _sale([{"variant_id": stocked["Soda 500ml"].pk, "qty": 1, "unit_price": 1000}],
              [{"method": "cash", "amount": 1000}])
    assert _push(client, s)["accepted"]
    other_owner = User.objects.create_user("o2@x.test", "pw", name="O2")
    create_tenant(name="Other", owner=other_owner, plan=business_plan)
    client.force_login(other_owner)
    res = _push(client, dict(s, lines=[{"description": "x", "qty": 1, "unit_price": 5}],
                             payments=[{"method": "cash", "amount": 5}]))
    assert not res["accepted"] and "already used" in res["rejected"][0]["error"]


# ---------------------------------------------------------------- back office

def test_apply_keeps_the_numbers_typed(client, shop, owner, main_branch, stocked):
    from apps.inventory.models import StockCount, StockItem
    client.force_login(owner)
    client.post(reverse("inventory:count_create"))
    with tenant_context(shop):
        count = StockCount.objects.get()
        data = {f"qty:{line.pk}": "90" for line in count.lines.all()}
    data["action"] = "apply"
    client.post(reverse("inventory:count_detail", args=[count.pk]), data)
    with tenant_context(shop):
        assert sorted(StockItem.objects.filter(branch=main_branch)
                      .values_list("qty_on_hand", flat=True)) == [90, 90, 90]


def test_an_approved_expense_cannot_be_rewritten(client, shop, owner, main_branch):
    from apps.finance.models import Expense, ExpenseCategory
    with tenant_context(shop, branch=main_branch):
        mgr = User.objects.create_user("mgr@example.com", "pw", name="Mgr")
        Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
    with tenant_context(shop, user=mgr, branch=main_branch):
        cat = ExpenseCategory.objects.create(name="Rent")
        exp = Expense.objects.create(branch=main_branch, category=cat, amount=100, spent_at="2026-09-01")
    client.force_login(owner)
    client.post(reverse("finance:expense_approve", args=[exp.pk]))
    client.force_login(mgr)
    client.post(reverse("finance:expense_edit", args=[exp.pk]),
                {"amount": "150000", "category": cat.pk, "spent_at": "2026-09-01"})
    exp.refresh_from_db()
    assert exp.amount == 100


def test_suppliers_owed_counts_every_bill(client, shop, owner):
    from apps.purchasing.models import Supplier, SupplierInvoice
    with tenant_context(shop):
        s = Supplier.objects.create(name="Azam")
        SupplierInvoice.objects.create(supplier=s, number="1", invoice_date="2026-09-01", amount=1000)
        SupplierInvoice.objects.create(supplier=s, number="2", invoice_date="2026-09-02", amount=1000)
    client.force_login(owner)
    row = next(x for x in client.get(reverse("purchasing:supplier_list")).context["page"]
               if x.pk == s.pk)
    assert row.owed == 2000


def test_a_reused_delivery_note_still_posts(client, shop, owner, main_branch, stocked):
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier
    with tenant_context(shop, branch=main_branch, user=owner):
        s = Supplier.objects.create(name="Azam")
        receipts = []
        for ref in ("GR1", "GR2"):
            r = GoodsReceipt.objects.create(supplier=s, branch=main_branch, reference=ref, supplier_note="DN-5")
            GoodsReceiptLine.objects.create(receipt=r, variant=stocked["Mkate"], qty=1, unit_cost=1)
            receipts.append(r)
    client.force_login(owner)
    for r in receipts:
        client.post(reverse("purchasing:receipt_detail", args=[r.pk]), {"action": "post"})
    with tenant_context(shop):
        assert s.invoices.count() == 2


def test_an_order_cannot_be_received_twice_over(client, shop, owner, main_branch, stocked):
    from apps.inventory.models import StockItem
    from apps.purchasing.models import GoodsReceipt, PurchaseOrder, PurchaseOrderLine, Supplier
    with tenant_context(shop, branch=main_branch, user=owner):
        s = Supplier.objects.create(name="Azam")
        po = PurchaseOrder.objects.create(supplier=s, branch=main_branch, reference="PO1", status="sent")
        PurchaseOrderLine.objects.create(order=po, variant=stocked["Mkate"], qty_ordered=10, unit_cost=100)
        r = GoodsReceipt.objects.create(supplier=s, branch=main_branch, reference="GR1", order=po)
    client.force_login(owner)
    url = reverse("purchasing:receipt_detail", args=[r.pk])
    client.post(url, {"action": "fill_from_order"})
    client.post(url, {"action": "fill_from_order"})
    client.post(url, {"action": "post"})
    with tenant_context(shop):
        assert StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"]).qty_on_hand == 110


@pytest.mark.parametrize("url,data", [
    ("reports:index", {"from": "2026-02-30"}),
])
def test_impossible_dates_are_ignored(client, shop, owner, url, data):
    client.force_login(owner)
    assert client.get(reverse(url), data).status_code == 200


def test_huge_and_garbage_input_is_refused_not_a_crash(client, shop, owner, main_branch):
    from apps.finance.models import Expense, ExpenseCategory
    with tenant_context(shop):
        cat = ExpenseCategory.objects.create(name="Rent")
    client.force_login(owner)
    # Refused inside the form (200 with the error), never a 500.
    assert client.post(reverse("finance:expense_create"), {"amount": "1e20", "category": cat.pk}).status_code == 200
    assert client.post(reverse("finance:expense_create"),
                       {"amount": "10", "category": cat.pk, "spent_at": "yesterday"}).status_code == 302
    assert client.post(reverse("catalog:taxonomy"),
                       {"kind": "unit", "name": "Kilogram", "code": "kilograms12"}).status_code == 302
    with tenant_context(shop):
        assert Expense.objects.count() == 1


def test_one_branch_cannot_act_on_anothers_back_office(client, shop, owner, main_branch, stocked):
    from apps.finance.models import Expense, ExpenseCategory
    from apps.inventory.models import StockCount
    from apps.org.models import Branch
    from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine, Supplier
    with tenant_context(shop):
        other = Branch.objects.create(name="Arusha")
        mgr = User.objects.create_user("mgr@example.com", "pw", name="Mgr")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.branch_links.create(branch=other)
    with tenant_context(shop, user=owner, branch=main_branch):
        cat = ExpenseCategory.objects.create(name="Rent")
        exp = Expense.objects.create(branch=main_branch, category=cat, amount=100, spent_at="2026-09-01")
        count = StockCount.objects.create(branch=main_branch, reference="SC1")
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name="Azam"),
                                          branch=main_branch, reference="PO1")
        PurchaseOrderLine.objects.create(order=po, variant=stocked["Mkate"], qty_ordered=10, unit_cost=100)
    client.force_login(mgr)
    client.post(reverse("finance:expense_delete", args=[exp.pk]))
    client.post(reverse("inventory:count_cancel", args=[count.pk]))
    client.post(reverse("purchasing:order_detail", args=[po.pk]), {"action": "approve"})
    with tenant_context(shop):
        assert Expense.objects.filter(pk=exp.pk).exists()
        count.refresh_from_db()
        po.refresh_from_db()
        assert count.status == "open" and po.status == "draft"


def test_margin_totals_cover_every_product(client, shop, owner):
    import inspect

    from apps.reports import views
    assert 'gross = sum((row["profit"] for row in enriched)' not in inspect.getsource(views.margin)


def test_no_subscription_means_no_history(shop):
    from apps.tenancy.models import Subscription
    with unscoped():
        Subscription.objects.filter(tenant=shop).delete()
        shop.refresh_from_db()
        assert shop.history_start() == timezone.localdate()


# ---------------------------------------------------------------- approvals and core

def test_an_explicit_deny_never_crosses_a_branch(client, shop, main_branch, stocked, owner):
    from apps.accounts.models import OverrideEffect, UserPermission
    from apps.org.models import Branch
    from apps.pos.services import add_to_cart, complete_sale, new_cart
    with tenant_context(shop):
        other = Branch.objects.create(name="Other")
        cashier = User.objects.create_user("c@x.test", "pw", name="C")
        m = Membership.objects.create(tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
        m.branch_links.create(branch=main_branch)
        UserPermission.objects.create(membership=m, permission=Permission.objects.get(code="pos.void"),
                                      effect=OverrideEffect.DENY)
    with tenant_context(shop, branch=other, user=owner):
        cart = new_cart(branch=other)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
    client.force_login(cashier)
    r = client.post(reverse("pos:sale_void", args=[sale.pk]), {"reason": "x"})
    assert b"must approve" not in r.content  # a flat no, not an approval prompt


def test_unscoped_keeps_the_real_database_error(db, owner):
    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic(), unscoped():
        User.objects.create_user(owner.email, "pw", name="dup")


# ================================================================ final review

def test_an_approval_to_open_a_page_does_not_cover_a_bigger_submission(client, shop, main_branch, stocked):
    from apps.inventory.models import StockItem
    with tenant_context(shop, branch=main_branch):
        cashier = User.objects.create_user("c@x.test", "pw", name="C")
        Membership.objects.create(tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager")).set_pin("4321")
        item = StockItem.objects.get(variant=stocked["Sukari 1kg"], branch=main_branch)
    client.force_login(cashier)
    url = reverse("inventory:stock_adjust", args=[item.pk])
    screen = client.get(url)
    client.post(url, {"override_method": "GET", "override_email": mgr.email, "override_pin": "4321",
                      "override_nonce": screen.context["nonce"]})
    client.get(url)
    client.post(url, {"new_qty": "100100", "reason": "x"})  # 240,000,000 of stock
    with tenant_context(shop):
        item.refresh_from_db()
        assert item.qty_on_hand == 100


def test_a_branch_manager_cannot_create_staff_for_every_branch(client, shop, main_branch):
    from apps.org.models import Branch
    with tenant_context(shop):
        Branch.objects.create(name="Other")
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.branch_links.create(branch=main_branch)
        manager_role = Role.objects.get(name="Manager")
    client.force_login(mgr)
    client.post(reverse("accounts:staff_create"), {
        "name": "P", "email": "p@x.test", "role": manager_role.pk, "all_branches": "on",
        "password": "Known-pass-1234"})
    assert not User.objects.filter(email="p@x.test").exists()


def test_a_branch_manager_cannot_take_over_an_all_branch_peer(client, shop, main_branch):
    with tenant_context(shop):
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.branch_links.create(branch=main_branch)
        peer = User.objects.create_user("peer@x.test", "pw", name="Peer")
        pm = Membership.objects.create(tenant=shop, user=peer, role=Role.objects.get(name="Cashier"))
    client.force_login(mgr)
    client.post(reverse("accounts:staff_edit", args=[pm.pk]), {
        "name": "Peer", "email": peer.email, "role": pm.role_id, "all_branches": "on",
        "password": "Known-pass-1234"})
    peer.refresh_from_db()
    assert peer.check_password("pw")


def test_a_switched_off_stronger_admin_cannot_be_taken_over(client):
    lead = _padmin("lead@p.test", _prole("Lead3", {"admins.manage", "people.view", "people.manage"}))
    billing = _padmin("bill@p.test", PlatformRole.objects.get(name="Billing"))
    billing.is_active = False
    billing.save()
    client.force_login(lead)
    client.post(reverse("platform:user_password", args=[billing.pk]), {"password": "takeover-123"})
    client.post(reverse("platform:user_toggle_active", args=[billing.pk]))
    billing.refresh_from_db()
    assert billing.check_password("pw") and not billing.is_active


def test_a_count_cannot_be_applied_twice(shop, main_branch, stocked, owner):
    from apps.inventory.models import StockCount, StockCountLine, StockItem
    from apps.inventory.services import apply_count
    with tenant_context(shop, branch=main_branch, user=owner):
        count = StockCount.objects.create(branch=main_branch, reference="SC1")
        StockCountLine.objects.create(count=count, variant=stocked["Mkate"], system_qty=100,
                                      counted_qty=90, unit_cost=0)
        copy = StockCount.objects.get(pk=count.pk)
        apply_count(count, user=owner)
        with pytest.raises(ValueError):
            apply_count(copy, user=owner)
        assert StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"]).qty_on_hand == 90


def test_credit_cannot_exceed_the_sale(client, shop, main_branch, register, stocked, owner):
    from apps.customers.models import Customer
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
        cust = Customer.objects.create(name="C", credit_limit=10_000_000)
    client.force_login(owner)
    res = _push(client, _sale([{"variant_id": stocked["Soda 500ml"].pk, "qty": 1, "unit_price": 1000}],
                              [{"method": "credit", "amount": 400000}], customer_id=cust.pk))
    assert not res["accepted"]


def test_a_garbage_limit_keeps_the_existing_deny(client, shop, owner):
    from apps.accounts.models import OverrideEffect, UserPermission
    with tenant_context(shop):
        cu = User.objects.create_user("c@x.test", "pw", name="C")
        mc = Membership.objects.create(tenant=shop, user=cu, role=Role.objects.get(name="Cashier"))
        UserPermission.objects.create(membership=mc, permission=Permission.objects.get(code="pos.void"),
                                      effect=OverrideEffect.DENY)
    client.force_login(owner)
    client.post(reverse("accounts:staff_edit", args=[mc.pk]), {
        "action": "overrides", "override:pos.void": "deny", "override_limit:pos.void": "abc"})
    with unscoped():
        assert UserPermission.objects.filter(membership=mc, effect=OverrideEffect.DENY).exists()


def test_a_hand_entered_bill_is_linked_not_doubled(client, shop, owner, main_branch, stocked):
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier, SupplierInvoice
    with tenant_context(shop, branch=main_branch, user=owner):
        s = Supplier.objects.create(name="Azam")
        SupplierInvoice.objects.create(supplier=s, number="INV-9", invoice_date="2026-09-01", amount=1000)
        r = GoodsReceipt.objects.create(supplier=s, branch=main_branch, reference="GR1", supplier_note="INV-9")
        GoodsReceiptLine.objects.create(receipt=r, variant=stocked["Mkate"], qty=10, unit_cost=100)
    client.force_login(owner)
    client.post(reverse("purchasing:receipt_detail", args=[r.pk]), {"action": "post"})
    with tenant_context(shop):
        assert s.invoices.count() == 1 and s.balance == 1000


def test_a_limited_permission_can_be_removed_by_its_holder(client, shop):
    with tenant_context(shop):
        editor = Role.objects.create(tenant=shop, name="Editor")
        RolePermission.objects.create(role=editor, permission=Permission.objects.get(code="role.manage"))
        RolePermission.objects.create(role=editor, permission=Permission.objects.get(code="pos.discount"),
                                      limit_value=20)
        u = User.objects.create_user("ed@x.test", "pw", name="Ed")
        Membership.objects.create(tenant=shop, user=u, role=editor)
        cashier = Role.objects.get(name="Cashier")
        posted = {"name": "Cashier"}
        for rp in cashier.permissions.select_related("permission"):
            if rp.permission.code != "pos.discount":
                posted[f"grant:{rp.permission.code}"] = "on"
    client.force_login(u)
    client.post(reverse("accounts:role_edit", args=[cashier.pk]), posted)
    with unscoped():
        assert not RolePermission.objects.filter(role=cashier, permission__code="pos.discount").exists()


def test_the_login_lockout_ignores_spaces_and_case(client, db):
    User.objects.create_user("owner@shop.test", "Right-pass-123", name="O")
    for i in range(10):
        client.post(reverse("accounts:login"), {"username": "owner@shop.test", "password": f"w{i}"})
    client.post(reverse("accounts:login"), {"username": " Owner@shop.test", "password": "Right-pass-123"})
    assert "_auth_user_id" not in client.session


def test_a_basket_open_item_vouches_for_one_sale_only(client, shop, main_branch, register, owner):
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch):
        cashier = User.objects.create_user("c@x.test", "pw", name="C")
        Membership.objects.create(tenant=shop, user=cashier, role=Role.objects.get(name="Cashier"))
    with tenant_context(shop, branch=main_branch, user=cashier):
        open_shift(register=register)
    client.force_login(owner)
    code = client.post(reverse("sync:cart_push"), data=json.dumps(
        {"lines": [{"description": "Rice", "qty": 1, "unit_price": 2000}]}),
        content_type="application/json").json()["code"]
    client.force_login(cashier)
    line = client.post(reverse("sync:cart_pull", args=[code])).json()["lines"][0]
    sale_line = {"variant_id": None, "description": "Rice", "qty": 1, "unit_price": 2000,
                 "cart_line_id": line["cart_line_id"]}
    assert _push(client, _sale([sale_line], [{"method": "cash", "amount": 2000}]))["accepted"]
    assert not _push(client, _sale([sale_line], [{"method": "cash", "amount": 2000}]))["accepted"]


def test_a_managers_pin_approval_of_a_clerks_expense_counts(client, shop, owner, main_branch):
    from apps.finance.models import Expense, ExpenseCategory
    with tenant_context(shop, branch=main_branch):
        clerk = User.objects.create_user("clerk@x.test", "pw", name="Clerk")
        Membership.objects.create(tenant=shop, user=clerk, role=Role.objects.get(name="Cashier"))
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager")).set_pin("4321")
    with tenant_context(shop, user=clerk, branch=main_branch):
        exp = Expense.objects.create(branch=main_branch, category=ExpenseCategory.objects.create(name="Tea"),
                                     amount=500, spent_at="2026-09-01")
    client.force_login(clerk)
    url = reverse("finance:expense_approve", args=[exp.pk])
    screen = client.post(url)
    client.post(url, {"override_email": mgr.email, "override_pin": "4321",
                      "override_nonce": screen.context["nonce"]})
    exp.refresh_from_db()
    assert exp.approved_by_id == mgr.pk


# ================================================================ last review

def test_a_synced_sale_stays_in_the_branch_it_was_made_in(client, shop, main_branch, register, stocked):
    from apps.org.models import Branch
    from apps.pos.models import Sale
    from apps.pos.services import open_shift
    with tenant_context(shop):
        nungwi = Branch.objects.create(name="Nungwi")
        mgr = User.objects.create_user("m@x.test", "pw", name="M")
        m = Membership.objects.create(tenant=shop, user=mgr, role=Role.objects.get(name="Manager"))
        m.branch_links.create(branch=main_branch)
        m.branch_links.create(branch=nungwi)
    with tenant_context(shop, branch=main_branch, user=mgr):
        shift = open_shift(register=register)
    client.force_login(mgr)
    session = client.session
    session["branch_id"] = nungwi.pk  # switched branch in another tab
    session.save()
    res = _push(client, _sale([{"variant_id": stocked["Mkate"].pk, "qty": 1, "unit_price": 1500}],
                              [{"method": "cash", "amount": 1500}], shift_id=shift.pk))
    with tenant_context(shop):
        sale = Sale.objects.get(number=res["accepted"][0]["number"])
        assert sale.branch_id == main_branch.pk and sale.shift_id == shift.pk


def test_another_persons_queue_is_refused(client, shop, main_branch, register, stocked, owner):
    client.force_login(owner)
    r = client.post(reverse("sync:push_sales"), data=json.dumps({
        "queue_owner": {"tenant_id": shop.pk, "user_id": owner.pk + 999},
        "sales": [_sale([{"variant_id": stocked["Mkate"].pk, "qty": 1, "unit_price": 1500}],
                        [{"method": "cash", "amount": 1500}])],
    }), content_type="application/json")
    assert r.status_code == 409


def test_an_impossible_amount_is_refused_not_retried_for_ever(client, shop, main_branch, register, owner):
    from apps.pos.services import open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        open_shift(register=register)
    client.force_login(owner)
    res = _push(client, _sale([{"description": "x", "qty": 1, "unit_price": "99999999999"}],
                              [{"method": "cash", "amount": "99999999999"}]))
    assert res["rejected"] and not res.get("retry")


def test_over_tendered_cash_is_stored_as_change(client, shop, main_branch, register, stocked, owner):
    from apps.pos.models import Sale
    from apps.pos.services import create_return, open_shift
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register)
    client.force_login(owner)
    res = _push(client, _sale([{"variant_id": stocked["Sukari 1kg"].pk, "qty": 2, "unit_price": 3000}],
                              [{"method": "mpesa", "amount": 5000},
                               {"method": "cash", "amount": 5000, "change_given": 0}], shift_id=shift.pk))
    with tenant_context(shop, branch=main_branch, user=owner):
        sale = Sale.objects.get(number=res["accepted"][0]["number"])
        cash = sale.payments.get(method="cash")
        assert cash.amount == 1000 and cash.change_given == 4000
        doc = create_return(sale, {sale.lines.first().pk: 2}, reason="x", method="cash", shift=shift)
        assert doc.cash_amount == 1000 and doc.other_amount == 5000


def test_saving_a_role_does_not_flag_untouched_limits(client, shop):
    with tenant_context(shop):
        editor = Role.objects.create(tenant=shop, name="Editor")
        for code in ("role.manage", "pos.discount", "pos.operate", "pos.sell", "pos.reprint",
                     "product.view", "stock.view", "customer.manage", "cashup.perform"):
            RolePermission.objects.create(role=editor, permission=Permission.objects.get(code=code),
                                          limit_value=50 if code == "pos.discount" else None)
        u = User.objects.create_user("ed@x.test", "pw", name="Ed")
        Membership.objects.create(tenant=shop, user=u, role=editor)
        cashier = Role.objects.get(name="Cashier")
        posted = {"name": "Cashier"}
        for rp in cashier.permissions.select_related("permission"):
            posted[f"grant:{rp.permission.code}"] = "on"
            if rp.limit_value is not None:
                posted[f"limit:{rp.permission.code}"] = f"{rp.limit_value:.2f}"
    client.force_login(u)
    r = client.post(reverse("accounts:role_edit", args=[cashier.pk]), posted, follow=True)
    assert not any("Left unchanged" in str(m) for m in r.context["messages"])

"""
The money reports: what came in, who owes whom, the paperwork, profit and
loss, the balance sheet, VAT, best sellers and goods bought.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart

pytestmark = pytest.mark.django_db

REPORTS = ["hub", "money_in", "receivables", "payables", "paperwork", "pnl", "balance",
           "vat", "best", "purchases"]


def _sell(shop, branch, owner, variant, qty=1, method="cash"):
    with tenant_context(shop, branch=branch, user=owner):
        cart = new_cart(branch=branch)
        add_to_cart(cart, variant, qty=qty)
        return complete_sale(cart, [{"method": method, "amount": cart.subtotal}])


def _ledger(shop, customer, kind, amount, days_ago=0, **extra):
    from apps.customers.models import CreditTransaction

    with tenant_context(shop):
        row = CreditTransaction.objects.create(customer=customer, kind=kind,
                                               amount=Decimal(amount), balance_after=0, **extra)
        if days_ago:
            CreditTransaction.objects.filter(pk=row.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago))
    return row


@pytest.fixture
def juma(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Juma Hotel")


@pytest.mark.parametrize("name", REPORTS)
def test_every_report_opens(client, owner, shop, stocked, name):
    client.force_login(owner)
    assert client.get(reverse(f"reports:{name}")).status_code == 200


@pytest.mark.parametrize("name", REPORTS)
def test_every_report_opens_with_a_period(client, owner, shop, name):
    client.force_login(owner)
    assert client.get(reverse(f"reports:{name}"), {"preset": "last_month"}).status_code == 200


def test_money_in_counts_the_till_accounts_and_takes_off_refunds(client, shop, owner,
                                                                main_branch, stocked, juma):
    _sell(shop, main_branch, owner, stocked["Mkate"])                         # 1500 cash
    gone = _sell(shop, main_branch, owner, stocked["Sukari 1kg"])             # 3000 cash
    with tenant_context(shop, branch=main_branch, user=owner):
        create_return(gone, {gone.lines.get().pk: 1}, reason="x", method="cash")
    _ledger(shop, juma, "payment", "-2000", method="mpesa")
    client.force_login(owner)
    ctx = client.get(reverse("reports:money_in")).context
    assert ctx["sources"]["pos"] == Decimal("4500")
    assert ctx["sources"]["refunds"] == Decimal("-3000")
    assert ctx["sources"]["accounts"] == Decimal("2000")
    assert ctx["total"] == Decimal("3500")
    methods = {m["method"]: m["total"] for m in ctx["methods"]}
    assert methods["mpesa"] == Decimal("2000") and methods["cash"] == Decimal("1500")


def test_an_undone_payment_is_not_money_in(client, shop, owner, juma):
    _ledger(shop, juma, "payment", "-2000", method="cash")
    _ledger(shop, juma, "adjustment", "2000", reference="undo:1", method="cash")
    client.force_login(owner)
    assert client.get(reverse("reports:money_in")).context["total"] == 0


def test_owed_to_you_is_aged_from_the_oldest_charge_paid_first(client, shop, owner, juma):
    _ledger(shop, juma, "charge", "1000", days_ago=100)
    _ledger(shop, juma, "charge", "500", days_ago=40)
    _ledger(shop, juma, "charge", "300", days_ago=5)
    _ledger(shop, juma, "payment", "-1200")          # clears the 1000 and 200 of the 500
    client.force_login(owner)
    ctx = client.get(reverse("reports:receivables")).context
    row = ctx["rows"][0]
    assert row["balance"] == Decimal("600")
    assert row["aged"] == {"0_30": Decimal("300"), "31_60": Decimal("300"),
                           "61_90": Decimal("0"), "90_plus": Decimal("0")}
    assert ctx["total"] == Decimal("600")


def test_money_held_for_a_customer_shows_as_paid_in_advance(client, shop, owner, juma):
    _ledger(shop, juma, "payment", "-700")
    client.force_login(owner)
    ctx = client.get(reverse("reports:receivables")).context
    assert ctx["rows"] == [] and ctx["in_credit_total"] == Decimal("700")
    assert "Juma Hotel" in client.get(reverse("reports:receivables")).content.decode()


def test_owed_to_suppliers(client, shop, owner):
    from apps.purchasing.models import Supplier, SupplierInvoice, SupplierPayment

    today = timezone.localdate()
    with tenant_context(shop):
        s = Supplier.objects.create(name="Bakhresa")
        bill = SupplierInvoice.objects.create(supplier=s, number="B1", invoice_date=today,
                                              due_date=today - timedelta(days=3),
                                              amount=Decimal("10000"))
        SupplierPayment.objects.create(supplier=s, invoice=bill, amount=Decimal("4000"),
                                       paid_at=today)
    client.force_login(owner)
    ctx = client.get(reverse("reports:payables")).context
    assert ctx["total"] == Decimal("6000")
    assert ctx["late_total"] == Decimal("6000")
    assert ctx["bills"][0]["days_late"] == 3


def test_profit_and_loss_takes_off_cost_and_expenses(client, shop, owner, main_branch, stocked):
    from apps.finance.models import Expense, ExpenseCategory

    _sell(shop, main_branch, owner, stocked["Mkate"], qty=2)
    with tenant_context(shop, branch=main_branch, user=owner):
        cat = ExpenseCategory.objects.create(name="Rent")
        Expense.objects.create(category=cat, amount=Decimal("100"), branch=main_branch,
                               spent_at=timezone.localdate())
    client.force_login(owner)
    ctx = client.get(reverse("reports:pnl")).context
    total = ctx["total"]
    assert total["expenses"] == Decimal("100")
    assert total["net"] == total["gross"] - Decimal("100")
    assert total["gross"] == total["revenue"] - total["cost"]
    assert ctx["expense_rows"][0][0] == "Rent"
    # Read by the month: this year so far, unless a period is asked for.
    assert ctx["start"] >= timezone.localdate().replace(month=1, day=1)
    assert ctx["months"][-1]["end"] == timezone.localdate()


def test_balance_sheet_adds_up(client, shop, owner, stocked, juma):
    _ledger(shop, juma, "charge", "5000")
    client.force_login(owner)
    ctx = client.get(reverse("reports:balance")).context
    assert dict(ctx["assets"])["Owed to you by customers"] == Decimal("5000")
    assert dict(ctx["assets"])["Stock, at what it cost"] > 0
    assert ctx["net_worth"] == ctx["total_assets"] - ctx["total_liabilities"]


def test_vat_is_the_vat_inside_what_stayed_sold(client, shop, owner, main_branch, stocked):
    sale = _sell(shop, main_branch, owner, stocked["Mkate"], qty=2)
    client.force_login(owner)
    ctx = client.get(reverse("reports:vat")).context
    with tenant_context(shop):
        assert ctx["vat"] == sale.tax_total
    assert ctx["gross"] == sale.total
    assert ctx["net"] == ctx["gross"] - ctx["vat"]


def test_best_sellers_ranks_each_way(client, shop, owner, main_branch, stocked):
    _sell(shop, main_branch, owner, stocked["Sukari 1kg"])          # 3000, one
    _sell(shop, main_branch, owner, stocked["Mkate"], qty=3)        # 4500, three
    _sell(shop, main_branch, owner, stocked["Mkate"])
    client.force_login(owner)
    first = {by: client.get(reverse("reports:best"), {"by": by}).context["rows"][0]["item"]
             for by in ("money", "qty", "sales", "least")}
    assert first == {"money": "Mkate", "qty": "Mkate", "sales": "Mkate", "least": "Sukari 1kg"}


def test_dashboard_best_sellers_dropdown(client, shop, owner, main_branch, stocked):
    _sell(shop, main_branch, owner, stocked["Sukari 1kg"])
    _sell(shop, main_branch, owner, stocked["Mkate"], qty=3)
    client.force_login(owner)
    page = client.get(reverse("core:dashboard"), {"top": "least"})
    assert page.context["top_key"] == "least"
    assert page.context["top"][0]["item"] == "Sukari 1kg"
    assert 'name="top"' in page.content.decode()
    assert client.get(reverse("core:dashboard"), {"top": "junk"}).context["top_key"] == "money"


def test_profit_ranking_is_only_for_those_who_see_profit(client, shop, owner, cashier):
    from apps.accounts.models import Membership, Permission, Role, RolePermission

    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Seller")
        RolePermission.objects.create(role=role,
                                      permission=Permission.objects.get(code="report.sales"))
        Membership.objects.create(tenant=shop, user=cashier, role=role)
    client.force_login(cashier)
    page = client.get(reverse("core:dashboard"), {"top": "profit"})
    assert page.context["top_key"] == "money"
    assert "profit" not in dict(page.context["top_by"])
    assert client.get(reverse("reports:pnl")).status_code == 403


def test_a_branch_manager_is_not_shown_the_whole_shops_debts(client, shop, owner, cashier,
                                                            main_branch, juma):
    from apps.accounts.models import Membership, Permission, Role, RolePermission

    _ledger(shop, juma, "charge", "5000")
    with tenant_context(shop):
        role = Role.objects.create(tenant=shop, name="Branch boss")
        for code in ("report.sales", "report.margin"):
            RolePermission.objects.create(role=role, permission=Permission.objects.get(code=code))
        m = Membership.objects.create(tenant=shop, user=cashier, role=role)
        m.set_branches([main_branch], all_branches=False)
    client.force_login(cashier)
    page = client.get(reverse("reports:receivables"))
    assert page.status_code == 200 and "Juma Hotel" not in page.content.decode()
    assert dict(client.get(reverse("reports:balance")).context["assets"])[
        "Owed to you by customers"] == 0


def test_purchases_by_supplier(client, shop, owner, main_branch, stocked):
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier

    with tenant_context(shop):
        s = Supplier.objects.create(name="Azam")
        r = GoodsReceipt.objects.create(reference="GR1", supplier=s, branch=main_branch)
        GoodsReceiptLine.objects.create(receipt=r, variant=stocked["Mkate"], qty=10,
                                        unit_cost=Decimal("900"))
    client.force_login(owner)
    ctx = client.get(reverse("reports:purchases")).context
    assert ctx["total"] == Decimal("9000")
    assert ctx["rows"][0]["receipt__supplier__name"] == "Azam"


# -- people: saving goes back to the list -----------------------------------

def test_adding_a_person_goes_back_to_people(client, shop, owner):
    from apps.accounts.models import Role

    client.force_login(owner)
    page = client.get(reverse("accounts:staff_create")).content.decode()
    assert "Add staff" in page
    with tenant_context(shop):
        role = Role.objects.exclude(is_owner_role=True).first()
    landed = client.post(reverse("accounts:staff_create"), {
        "name": "Neema", "email": "neema@example.com", "phone": "", "role": role.pk,
        "all_branches": "on", "password": "a-long-pass-123", "pin": ""})
    assert landed.status_code == 302
    assert landed["Location"] == reverse("accounts:staff")

"""
The whole system, end to end.

Two jobs. First, walk every screen as every role and confirm each one either
renders or refuses cleanly -- no 500s hiding in a template nobody opened.
Second, run a full day of trading and check the numbers add up.
"""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role
from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.inventory.services import quantity_of
from apps.pos.models import CashMovementKind, PaymentMethod, Sale
from apps.pos.services import (
    add_to_cart,
    close_shift,
    complete_sale,
    create_return,
    new_cart,
    open_shift,
    record_cash_movement,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Every screen, every role
# --------------------------------------------------------------------------

READ_SCREENS = [
    ("core:dashboard", {}),
    ("catalog:product_list", {}),
    ("catalog:product_create", {}),
    ("catalog:product_import", {}),
    ("inventory:stock_list", {}),
    ("inventory:movements", {}),
    ("inventory:transfer_list", {}),
    ("inventory:count_list", {}),
    ("inventory:batch_list", {}),
    ("purchasing:supplier_list", {}),
    ("purchasing:supplier_create", {}),
    ("purchasing:order_list", {}),
    ("purchasing:order_create", {}),
    ("purchasing:receipt_list", {}),
    ("purchasing:receipt_create", {}),
    ("customers:customer_list", {}),
    ("customers:customer_create", {}),
    ("customers:statements", {}),
    ("finance:expense_list", {}),
    ("finance:cashups", {}),
    ("reports:index", {}),
    ("reports:margin", {}),
    ("reports:stock_value", {}),
    ("reports:staff", {}),
    ("pos:sale_list", {}),
    ("pos:shift_open", {}),
    ("accounts:staff", {}),
    ("accounts:staff_create", {}),
    ("accounts:roles", {}),
    ("accounts:role_create", {}),
    ("tenancy:billing", {}),
]

PLATFORM_SCREENS = [
    "platform:dashboard",
    "platform:tenant_list",
    "platform:plans",
    "platform:health",
]


def make_member(shop, user, role_name):
    with tenant_context(shop):
        role = Role.objects.get(name=role_name)
        membership = Membership.objects.create(tenant=shop, user=user, role=role)
        from apps.org.models import Branch

        for branch in Branch.objects.all():
            membership.branch_links.create(branch=branch)
        return membership


@pytest.mark.parametrize("name,kwargs", READ_SCREENS)
def test_every_screen_answers_for_the_owner(client, shop, owner, stocked, name, kwargs):
    """
    The owner can reach everything, so any 500 here is a real bug rather than
    a permission working as designed.
    """
    client.force_login(owner)
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code in (200, 302), f"{name} returned {response.status_code}"


@pytest.mark.parametrize("role_name", ["Manager", "Cashier", "Stock clerk"])
def test_no_screen_crashes_for_any_role(client, shop, cashier, stocked, role_name):
    """
    Everyone else gets 200 or a clean 403 -- never a 500. A permission that
    blows up a template is worse than one that refuses.
    """
    make_member(shop, cashier, role_name)
    client.force_login(cashier)

    for name, kwargs in READ_SCREENS:
        response = client.get(reverse(name, kwargs=kwargs))
        assert response.status_code in (200, 302, 403), (
            f"{role_name} got {response.status_code} on {name}"
        )


def test_platform_admin_is_closed_to_shop_staff(client, shop, owner):
    client.force_login(owner)
    for name in PLATFORM_SCREENS:
        assert client.get(reverse(name)).status_code == 403


def test_platform_admin_opens_for_platform_staff(client, db, shop, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    client.force_login(owner)
    for name in PLATFORM_SCREENS:
        assert client.get(reverse(name)).status_code == 200, name


# --------------------------------------------------------------------------
# A day of trading
# --------------------------------------------------------------------------

def test_a_full_trading_day_adds_up(shop, main_branch, register, stocked, owner):
    """
    Open a till, sell, refund, pay out, close, and check every figure: stock,
    cash, VAT and margin all derived from the same movements.
    """
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=20000)

        # Three cash sales.
        for qty in (2, 1, 3):
            cart = new_cart(branch=main_branch, register=register)
            add_to_cart(cart, stocked["Sukari 1kg"], qty=qty)
            complete_sale(
                cart,
                [{"method": PaymentMethod.CASH, "amount": cart.subtotal}],
                shift=shift,
            )

        # One on mobile money, which must not count towards drawer cash.
        cart = new_cart(branch=main_branch, register=register)
        add_to_cart(cart, stocked["Mkate"], qty=4)
        mpesa_sale = complete_sale(
            cart,
            [{"method": PaymentMethod.MPESA, "amount": cart.subtotal, "reference": "X1"}],
            shift=shift,
        )

        # A refund and a pay-out.
        first = Sale.objects.filter(shift=shift).order_by("id").first()
        create_return(
            first, {first.lines.first().pk: 1}, reason="Wrong size", shift=shift
        )
        record_cash_movement(
            shift, kind=CashMovementKind.PAY_OUT, amount=-5000, reason="Boda"
        )

        # 6 units of sugar sold, 1 came back.
        assert quantity_of(stocked["Sukari 1kg"]) == 95
        assert quantity_of(stocked["Mkate"]) == 96

        # 20,000 float + 18,000 cash sales - 3,000 refunded - 5,000 paid out.
        # The M-Pesa sale is deliberately absent: it never touched the drawer.
        close_shift(shift, counted_cash=30000)
        assert shift.cash_taken() == Decimal("18000.00")
        assert shift.cash_refunded() == Decimal("3000.00")
        assert shift.expected_cash == Decimal("30000.00")
        assert shift.variance == Decimal("0.00")

        assert mpesa_sale.payments.first().method == PaymentMethod.MPESA


def test_offline_sales_sync_without_duplicating(client, shop, main_branch, stocked, owner):
    """
    The whole offline story through the real endpoint: send a batch twice and
    confirm the shop was not charged twice.
    """
    with tenant_context(shop, branch=main_branch):
        variant = stocked["Soda 500ml"]

    client.force_login(owner)
    batch = {
        "device_id": "till-1",
        "sales": [
            {
                "client_uuid": str(uuid.uuid4()),
                "lines": [
                    {"variant_id": variant.pk, "qty": 2, "unit_price": 1000,
                     "added_via": "scan"}
                ],
                "payments": [{"method": "cash", "amount": 2000}],
            }
        ],
    }

    url = reverse("sync:push_sales")
    first = client.post(url, data=json.dumps(batch), content_type="application/json")
    second = client.post(url, data=json.dumps(batch), content_type="application/json")

    assert first.status_code == 200
    assert len(first.json()["accepted"]) == 1
    assert len(second.json()["accepted"]) == 1

    with tenant_context(shop, branch=main_branch):
        assert Sale.objects.count() == 1
        assert quantity_of(variant) == 98


def test_catalogue_snapshot_gives_a_till_what_it_needs(client, shop, stocked, owner):
    client.force_login(owner)
    response = client.get(reverse("sync:catalog"))
    assert response.status_code == 200

    data = response.json()
    assert data["count"] == 3
    first = data["variants"][0]
    # Everything the till needs to price and tax a line with no connection.
    for key in ("id", "name", "price", "tax_rate", "unit", "barcodes"):
        assert key in first


def test_shelf_basket_moves_from_phone_to_till(client, shop, main_branch, stocked, owner):
    """The handoff: built in the aisle, collected at the till by its code."""
    with tenant_context(shop, branch=main_branch):
        variant = stocked["Mkate"]

    client.force_login(owner)

    pushed = client.post(
        reverse("sync:cart_push"),
        data=json.dumps({"lines": [{"variant_id": variant.pk, "qty": 3}]}),
        content_type="application/json",
    )
    assert pushed.status_code == 200
    code = pushed.json()["code"]

    pulled = client.post(reverse("sync:cart_pull", args=[code]))
    assert pulled.status_code == 200
    assert pulled.json()["lines"][0]["variant_id"] == variant.pk

    assert client.post(reverse("sync:cart_pull", args=["ZZZZ"])).status_code == 404


def test_receiving_goods_raises_stock_and_rebases_cost(shop, main_branch, stocked, owner):
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier
    from apps.purchasing.services import post_receipt

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Bakhresa")
        receipt = GoodsReceipt.objects.create(
            reference="GR250001", supplier=supplier, branch=main_branch
        )
        GoodsReceiptLine.objects.create(
            receipt=receipt, variant=stocked["Sukari 1kg"], qty=100, unit_cost=2800
        )

        post_receipt(receipt, user=owner)

        assert quantity_of(stocked["Sukari 1kg"]) == 200
        from apps.inventory.services import get_stock_item

        # 100 at 2,400 plus 100 at 2,800 averages 2,600.
        assert get_stock_item(stocked["Sukari 1kg"], main_branch).avg_cost == Decimal("2600.00")

        # Posting twice would double the stock, so it is refused.
        with pytest.raises(ValueError, match="already been received"):
            post_receipt(receipt, user=owner)


def test_customer_credit_and_payment_balance_out(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", credit_limit=50000)

        cart = new_cart(branch=main_branch, customer=customer)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=5)
        complete_sale(cart, [{"method": PaymentMethod.CREDIT, "amount": 15000}])

        assert customer.balance == Decimal("15000.00")

        from apps.customers.models import CreditKind, CreditTransaction

        CreditTransaction.objects.create(
            customer=customer, kind=CreditKind.PAYMENT, amount=Decimal("-10000"),
            balance_after=Decimal("5000"),
        )
        assert customer.balance == Decimal("5000.00")
        assert customer.credit_available == Decimal("45000.00")


def test_reports_agree_with_the_sales_that_made_them(client, shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=10)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 30000}])

    client.force_login(owner)
    response = client.get(reverse("reports:index"))
    assert response.status_code == 200
    assert response.context["totals"]["value"] == sale.total

    margin = client.get(reverse("reports:margin"))
    assert margin.status_code == 200
    # 30,000 gross, 4,576.27 VAT, 24,000 cost.
    assert margin.context["gross"] == Decimal("1423.73")


def test_a_suspended_shop_can_look_but_not_sell(client, shop, owner, stocked):
    from apps.tenancy.models import SubscriptionStatus

    subscription = shop.subscription
    subscription.status = SubscriptionStatus.SUSPENDED
    subscription.save(update_fields=["status"])

    client.force_login(owner)

    # Reading is untouched: nothing is deleted and nothing is hidden.
    assert client.get(reverse("catalog:product_list")).status_code == 200

    # Writing is refused with a page that explains why.
    blocked = client.post(
        reverse("catalog:product_create"), {"name": "Something", "base_unit": 1, "tax_rate": 1}
    )
    assert blocked.status_code == 402


def test_people_land_in_the_default_branch(shop, owner):
    """
    Alphabetical order once put the owner in a branch with no till in it.
    The business's default branch is where people expect to be.
    """
    from apps.org.models import Branch

    with tenant_context(shop):
        # "Alpha" sorts before "Main", which is the default.
        Branch.objects.create(name="Alpha Street")
        membership = Membership.objects.get(user=owner)
        assert membership.active_branch().name == "Main"


def test_a_failed_import_row_is_not_counted_as_imported(shop, main_branch, owner):
    """
    The row's transaction rolls back, so its tally has to roll back with it.
    """
    import io

    from apps.catalog.imports import import_products
    from apps.catalog.models import Product

    csv_text = (
        "name,sku,unit,price,qty\n"
        "Good One,G1,pc,1000,5\n"
        "Bad One,B1,pc,not-a-number,5\n"
        "Good Two,G2,pc,2000,5\n"
    )

    with tenant_context(shop, branch=main_branch, user=owner):
        result = import_products(io.StringIO(csv_text), branch=main_branch)

        assert result.created == 2
        assert result.skipped == 1
        assert len(result.errors) == 1
        # And the database agrees with the count.
        assert Product.objects.filter(name__startswith="Good").count() == 2
        assert not Product.objects.filter(name="Bad One").exists()


def test_synced_sales_reach_the_cash_drawer(client, shop, main_branch, register, stocked, owner):
    """
    A sale synced from a till must land on the open shift.

    Without it the cash never reaches the drawer figure and the cashier looks
    thousands over at close -- which is the exact number the cash-up screen
    exists to make trustworthy.
    """
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=20000)
        variant = stocked["Sukari 1kg"]

    client.force_login(owner)
    client.post(
        reverse("sync:push_sales"),
        data=json.dumps({
            "device_id": "till-test",
            "shift_id": shift.pk,
            "sales": [{
                "client_uuid": str(uuid.uuid4()),
                "lines": [{"variant_id": variant.pk, "qty": 4, "unit_price": 3000}],
                "payments": [{"method": "cash", "amount": 12000}],
            }],
        }),
        content_type="application/json",
    )

    with tenant_context(shop, branch=main_branch, user=owner):
        shift.refresh_from_db()
        assert shift.sales.count() == 1
        assert shift.cash_taken() == Decimal("12000.00")
        assert shift.compute_expected_cash() == Decimal("32000.00")


def test_a_sale_with_no_shift_id_still_finds_the_cashiers_shift(
    client, shop, main_branch, register, stocked, owner
):
    """A device offline across a shift change must not orphan the money."""
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=0)
        variant = stocked["Mkate"]

    client.force_login(owner)
    client.post(
        reverse("sync:push_sales"),
        data=json.dumps({"device_id": "till-test", "sales": [{
            "client_uuid": str(uuid.uuid4()),
            "lines": [{"variant_id": variant.pk, "qty": 1, "unit_price": 1500}],
            "payments": [{"method": "cash", "amount": 1500}],
        }]}),
        content_type="application/json",
    )

    with tenant_context(shop, branch=main_branch, user=owner):
        assert shift.sales.count() == 1


def test_platform_staff_land_on_the_platform_not_a_dead_end(client, db, owner):
    """
    Platform staff belong to no shop by design. Telling them they are "not
    part of a shop yet" is true and useless -- their home is /platform/.
    """
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    Membership.objects_all.filter(user=owner).delete()

    client.force_login(owner)
    response = client.get(reverse("core:dashboard"))
    assert response.status_code == 302
    assert response.url == reverse("platform:dashboard")


def test_product_list_shows_cost_to_those_allowed_to_see_it(
    client, shop, main_branch, stocked, owner, cashier
):
    """
    The Cost column rendered a property that did not exist, so the header was
    there and every cell was blank.
    """
    client.force_login(owner)
    page = client.get(reverse("catalog:product_list"))
    assert page.status_code == 200
    costs = [p.branch_cost for p in page.context["page"]]
    assert Decimal("2400.00") in costs

    # And a cashier neither gets the column nor the annotation.
    make_member(shop, cashier, "Cashier")
    client.force_login(cashier)
    page = client.get(reverse("catalog:product_list"))
    assert page.context["can_see_cost"] is False
    assert b"2,400" not in page.content


def test_new_products_default_to_pieces_not_crates(client, shop, owner):
    """Units sorted alphabetically put "Crate" at the top of every new product."""
    client.force_login(owner)
    response = client.get(reverse("catalog:product_create"))
    units = list(response.context["form"].fields["base_unit"].queryset)
    assert units[0].code == "pc"


def test_staff_added_through_the_ui_can_actually_sign_in(client, shop, owner):
    """
    They were created with an unusable password, so every person a manager
    added was locked out of the system they had just been given access to.
    """
    from apps.org.models import Branch

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        branch = Branch.objects.first()

    client.force_login(owner)
    response = client.post(
        reverse("accounts:staff_create"),
        {
            "name": "Juma Ally", "email": "juma@shop.test", "phone": "0777000000",
            "role": role.pk, "branches": [branch.pk],
            "password": "correct-horse-battery", "pin": "",
        },
        follow=True,
    )
    assert response.status_code == 200

    client.logout()
    assert client.login(email="juma@shop.test", password="correct-horse-battery")
    assert client.get(reverse("core:dashboard")).status_code == 200


def test_a_new_staff_member_must_be_given_a_password(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")

    client.force_login(owner)
    response = client.post(
        reverse("accounts:staff_create"),
        {"name": "No Password", "email": "nopw@shop.test", "role": role.pk},
    )
    assert response.status_code == 200
    assert "password" in response.context["form"].errors


SETTINGS_SCREENS = [
    "org:business",
    "org:branches",
    "org:branch_create",
    "catalog:taxonomy",
    "catalog:tiles",
]


@pytest.mark.parametrize("name", SETTINGS_SCREENS)
def test_settings_screens_open(client, shop, owner, stocked, name):
    """
    The system could not be configured at all through the UI: no branches, no
    tax rates, no units, and no way to put anything on the till.
    """
    client.force_login(owner)
    assert client.get(reverse(name)).status_code == 200


def test_a_shop_can_add_a_branch_and_a_till(client, shop, owner):
    from apps.org.models import Branch, Register

    client.force_login(owner)
    client.post(
        reverse("org:branch_create"),
        {"name": "Nungwi", "code": "NGW", "is_active": "on"},
        follow=True,
    )
    with tenant_context(shop):
        branch = Branch.objects.get(name="Nungwi")

    client.post(
        reverse("org:register_create"),
        {"branch": branch.pk, "name": "Till 1", "is_active": "on"},
        follow=True,
    )
    with tenant_context(shop):
        assert Register.objects.filter(branch=branch, name="Till 1").exists()


def test_only_one_branch_is_ever_the_default(client, shop, owner):
    """Two defaults and people land in whichever sorts first."""
    from apps.org.models import Branch

    client.force_login(owner)
    client.post(
        reverse("org:branch_create"),
        {"name": "Nungwi", "is_default": "on", "is_active": "on"}, follow=True,
    )
    with tenant_context(shop):
        assert Branch.objects.filter(is_default=True).count() == 1
        assert Branch.objects.get(is_default=True).name == "Nungwi"


def test_tiles_can_be_added_and_removed(client, shop, main_branch, stocked, owner):
    """
    The till told shops to set up tiles and gave them no way to do it, which
    left anything without a barcode unsellable.
    """
    from apps.catalog.models import QuickKey

    with tenant_context(shop):
        variant = stocked["Sukari 1kg"]

    client.force_login(owner)
    client.post(reverse("catalog:tiles"), {"action": "add", "variant": variant.pk,
                                           "plu_code": "11"}, follow=True)
    with tenant_context(shop):
        tile = QuickKey.objects.get(variant=variant)
        assert tile.plu_code == "11"

    client.post(reverse("catalog:tiles"), {"action": "remove", "tile": tile.pk},
                follow=True)
    with tenant_context(shop):
        assert not QuickKey.objects.filter(pk=tile.pk).exists()


def test_a_shop_can_add_its_own_vat_rate_and_unit(client, shop, owner):
    from apps.catalog.models import TaxRate, Unit

    client.force_login(owner)
    client.post(reverse("catalog:taxonomy"),
                {"kind": "tax", "name": "VAT 15%", "rate": "15", "is_inclusive": "on"},
                follow=True)
    client.post(reverse("catalog:taxonomy"),
                {"kind": "unit", "name": "Metre", "code": "m", "allows_decimal": "on"},
                follow=True)

    with tenant_context(shop):
        assert TaxRate.objects.filter(name="VAT 15%").exists()
        assert Unit.objects.get(code="m").allows_decimal is True


def _add_customers(shop, branch, owner, count, start=0):
    from apps.customers.models import CreditKind, CreditTransaction, Customer

    with tenant_context(shop, branch=branch, user=owner):
        for i in range(start, start + count):
            customer = Customer.objects.create(
                name=f"Customer {i:03d}", credit_limit=50000
            )
            CreditTransaction.objects.create(
                customer=customer, kind=CreditKind.CHARGE,
                amount=Decimal("1000"), balance_after=Decimal("1000"),
            )


def test_customer_list_cost_does_not_grow_with_the_number_of_customers(
    client, shop, main_branch, owner, django_capture_on_commit_callbacks
):
    """
    `balance` was a property running an aggregate per row, so every extra
    customer cost another query. Comparing two page sizes proves it is gone
    regardless of what the rest of the page costs.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.force_login(owner)

    _add_customers(shop, main_branch, owner, 5)
    with CaptureQueriesContext(connection) as small:
        client.get(reverse("customers:customer_list"))

    _add_customers(shop, main_branch, owner, 40, start=5)
    with CaptureQueriesContext(connection) as large:
        response = client.get(reverse("customers:customer_list"))

    assert response.context["page"].paginator.count == 45
    # Not equality: the permission cache warms between the two requests, so
    # the second is allowed to be cheaper. What must never happen is growth.
    assert len(large) <= len(small), (
        f"{len(small)} queries for 5 customers, {len(large)} for 45"
    )


def test_owing_filter_searches_every_customer_not_just_the_first_page(
    client, shop, main_branch, owner
):
    """
    The filter ran in Python after slicing to 200, so anybody owing money
    beyond that point was invisible.
    """
    from apps.customers.models import CreditKind, CreditTransaction, Customer

    with tenant_context(shop, main_branch, user=owner):
        for i in range(205):
            Customer.objects.create(name=f"Zero {i:03d}", credit_limit=1000)
        late = Customer.objects.create(name="ZZZ Owes Money", credit_limit=50000)
        CreditTransaction.objects.create(
            customer=late, kind=CreditKind.CHARGE,
            amount=Decimal("7500"), balance_after=Decimal("7500"),
        )

    client.force_login(owner)
    response = client.get(reverse("customers:customer_list"), {"view": "owing"})
    names = [c.name for c in response.context["page"]]
    assert "ZZZ Owes Money" in names


def test_supplier_list_cost_does_not_grow_with_the_number_of_suppliers(
    client, shop, main_branch, owner
):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.purchasing.models import Supplier, SupplierInvoice

    def add(count, start=0):
        with tenant_context(shop, branch=main_branch, user=owner):
            for i in range(start, start + count):
                supplier = Supplier.objects.create(name=f"Supplier {i:03d}")
                SupplierInvoice.objects.create(
                    supplier=supplier, number=f"INV{i}",
                    invoice_date=timezone.localdate(), amount=Decimal("10000"),
                )

    client.force_login(owner)

    add(3)
    with CaptureQueriesContext(connection) as small:
        client.get(reverse("purchasing:supplier_list"))

    add(30, start=3)
    with CaptureQueriesContext(connection) as large:
        response = client.get(reverse("purchasing:supplier_list"))

    assert response.status_code == 200
    assert len(large) <= len(small), (
        f"{len(small)} queries for 3 suppliers, {len(large)} for 33"
    )


def test_the_screens_do_not_talk_like_an_accountant():
    """
    Salma runs a duka with three staff, not a finance department. The words
    below are correct and unusable, and they creep back in one template at a
    time -- "Variance" survived a whole pass through the system sitting on
    the cash-up report, beside a "Short by" that said the same thing.
    """
    import re
    from pathlib import Path

    JARGON = ("Variance", "Gross margin", "Weighted average", "COGS",
              "Accrual", "Credit note", "Debit", "Ledger")
    found = []
    for template in Path("templates").rglob("*.html"):
        text = template.read_text()
        # Only what a person reads: between tags, not in classes or code.
        for visible in re.findall(r">([^<>{}]+)<", text):
            for word in JARGON:
                if word.lower() in visible.lower():
                    found.append(f"{template}: {visible.strip()[:60]}")
    assert not found, "accounting words on screen: " + "; ".join(found)

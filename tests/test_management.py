"""
Adding, changing and removing things.

The system could create and edit but barely delete anything, which left shops
stuck with a typo in a category forever. The rule everywhere is the same:
delete when nothing references it, switch it off when something does, and
refuse outright only when removing it would lock somebody out or make a
report wrong.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.catalog.models import Barcode, Category, Product, TaxRate, Unit
from apps.core.context import tenant_context, unscoped
from apps.customers.models import Customer
from apps.org.models import Branch, Register

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def test_an_unused_role_can_be_deleted(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(name="Stock clerk")

    client.force_login(owner)
    client.post(reverse("accounts:role_delete", args=[role.pk]), follow=True)

    with tenant_context(shop):
        assert not Role.objects.filter(pk=role.pk).exists()


def test_a_role_somebody_holds_cannot_be_deleted(client, shop, owner, cashier):
    """Silently moving people to another role is how a cashier ends up voiding sales."""
    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        Membership.objects.create(tenant=shop, user=cashier, role=role)

    client.force_login(owner)
    response = client.post(reverse("accounts:role_delete", args=[role.pk]), follow=True)

    assert b"Move them to another role" in response.content
    with tenant_context(shop):
        assert Role.objects.filter(pk=role.pk).exists()


def test_the_owner_role_can_never_be_deleted(client, shop, owner):
    with tenant_context(shop):
        role = Role.objects.get(is_owner_role=True)

    client.force_login(owner)
    response = client.post(reverse("accounts:role_delete", args=[role.pk]), follow=True)

    assert b"cannot be deleted" in response.content
    with tenant_context(shop):
        assert Role.objects.filter(pk=role.pk).exists()


def test_a_role_can_be_copied_with_its_permissions(client, shop, owner):
    """How a shop builds a second role: copy the cashier and change two things."""
    with tenant_context(shop):
        source = Role.objects.get(name="Cashier")
        original = set(source.permissions.values_list("permission__code", flat=True))

    client.force_login(owner)
    client.post(reverse("accounts:role_duplicate", args=[source.pk]), follow=True)

    with tenant_context(shop):
        copy = Role.objects.get(name="Cashier copy")
        assert set(copy.permissions.values_list("permission__code", flat=True)) == original
        assert copy.pk != source.pk


# --------------------------------------------------------------------------
# Staff
# --------------------------------------------------------------------------

def test_staff_can_be_removed_and_their_history_stays(client, shop, main_branch, stocked, owner, cashier):
    from apps.pos.models import PaymentMethod, Sale
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=cashier):
        membership = Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )
        cart = new_cart(branch=main_branch, user=cashier)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}],
                             user=cashier)

    client.force_login(owner)
    client.post(reverse("accounts:staff_remove", args=[membership.pk]), follow=True)

    with tenant_context(shop):
        assert not Membership.objects.filter(pk=membership.pk).exists()
        # The sale they rang up is untouched.
        assert Sale.objects.filter(pk=sale.pk).exists()


def test_the_only_owner_cannot_be_removed(client, shop, owner):
    with tenant_context(shop):
        membership = Membership.objects.get(user=owner)

    client.force_login(owner)
    response = client.post(
        reverse("accounts:staff_remove", args=[membership.pk]), follow=True
    )
    assert b"cannot remove yourself" in response.content.lower()


# --------------------------------------------------------------------------
# Catalogue reference data
# --------------------------------------------------------------------------

def test_a_category_can_be_renamed(client, shop, owner):
    with tenant_context(shop):
        category = Category.objects.create(name="Drnks")

    client.force_login(owner)
    client.post(
        reverse("catalog:taxonomy_edit", args=["category", category.pk]),
        {"name": "Drinks"}, follow=True,
    )
    category.refresh_from_db()
    assert category.name == "Drinks"


def test_an_unused_category_is_deleted_outright(client, shop, owner):
    with tenant_context(shop):
        category = Category.objects.create(name="Typo")

    client.force_login(owner)
    client.post(
        reverse("catalog:taxonomy_delete", args=["category", category.pk]), follow=True
    )
    with tenant_context(shop):
        assert not Category.objects.filter(pk=category.pk).exists()


def test_a_vat_rate_that_has_been_charged_is_switched_off_not_deleted(
    client, shop, main_branch, stocked, owner
):
    """Last year's returns have to keep meaning what they meant."""
    with tenant_context(shop):
        extra = TaxRate.objects.create(name="VAT 15%", rate=15)
        Product.objects.create(
            name="Taxed thing", base_unit=Unit.objects.get(code="pc"), tax_rate=extra
        )

    client.force_login(owner)
    response = client.post(
        reverse("catalog:taxonomy_delete", args=["tax", extra.pk]), follow=True
    )

    extra.refresh_from_db()
    assert b"switched off" in response.content
    assert extra.is_active is False


def test_the_default_vat_rate_cannot_be_removed(client, shop, owner):
    with tenant_context(shop):
        default = TaxRate.objects.get(is_default=True)

    client.force_login(owner)
    response = client.post(
        reverse("catalog:taxonomy_delete", args=["tax", default.pk]), follow=True
    )
    assert b"another default" in response.content
    with tenant_context(shop):
        assert TaxRate.objects.filter(pk=default.pk, is_default=True).exists()


def test_a_barcode_can_be_removed(client, shop, stocked, owner):
    """Happens whenever a supplier changes packaging."""
    from apps.catalog.services import attach_barcode

    with tenant_context(shop):
        variant = stocked["Mkate"]
        barcode = attach_barcode(variant, "6009999999999")

    client.force_login(owner)
    client.post(reverse("catalog:barcode_delete", args=[barcode.pk]), follow=True)

    with tenant_context(shop):
        assert not Barcode.objects.filter(code="6009999999999").exists()


def test_a_sold_product_is_switched_off_rather_than_deleted(
    client, shop, main_branch, stocked, owner
):
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=owner):
        variant = stocked["Sukari 1kg"]
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, variant, qty=1)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 3000}])
        product = variant.product

    client.force_login(owner)
    response = client.post(
        reverse("catalog:product_delete", args=[product.pk]), follow=True
    )

    product.refresh_from_db()
    assert b"switched off" in response.content
    assert product.is_active is False


def test_price_lists_can_be_created_and_made_default(client, shop, owner):
    from apps.catalog.models import PriceList

    client.force_login(owner)
    client.post(reverse("catalog:price_lists"),
                {"action": "create", "name": "Wholesale", "kind": "wholesale"},
                follow=True)

    with tenant_context(shop):
        wholesale = PriceList.objects.get(name="Wholesale")

    client.post(reverse("catalog:price_lists"),
                {"action": "default", "price_list": wholesale.pk}, follow=True)

    with tenant_context(shop):
        assert PriceList.objects.filter(is_default=True).count() == 1
        assert PriceList.objects.get(is_default=True).name == "Wholesale"


# --------------------------------------------------------------------------
# Branches and tills
# --------------------------------------------------------------------------

def test_a_till_can_be_renamed_and_removed(client, shop, main_branch, owner):
    with tenant_context(shop):
        register = Register.objects.create(branch=main_branch, name="Till 2")

    client.force_login(owner)
    client.post(reverse("org:register_edit", args=[register.pk]),
                {"branch": main_branch.pk, "name": "Counter 2", "is_active": "on"},
                follow=True)
    register.refresh_from_db()
    assert register.name == "Counter 2"

    client.post(reverse("org:register_delete", args=[register.pk]), follow=True)
    with tenant_context(shop):
        assert not Register.objects.filter(pk=register.pk).exists()


def test_the_only_branch_cannot_be_removed(client, shop, main_branch, owner):
    client.force_login(owner)
    response = client.post(
        reverse("org:branch_delete", args=[main_branch.pk]), follow=True
    )
    assert b"only branch" in response.content
    with tenant_context(shop):
        assert Branch.objects.filter(pk=main_branch.pk, is_active=True).exists()


def test_a_second_branch_can_be_removed(client, shop, main_branch, owner):
    with tenant_context(shop):
        spare = Branch.objects.create(name="Spare")

    client.force_login(owner)
    client.post(reverse("org:branch_delete", args=[spare.pk]), follow=True)
    with tenant_context(shop):
        assert not Branch.objects.filter(pk=spare.pk, is_active=True).exists()


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

def test_a_draft_transfer_can_be_cancelled_but_a_sent_one_cannot(
    client, shop, main_branch, stocked, owner
):
    from apps.inventory.models import Transfer, TransferLine, TransferStatus
    from apps.inventory.services import send_transfer

    with tenant_context(shop, branch=main_branch, user=owner):
        nungwi = Branch.objects.create(name="Nungwi")
        draft = Transfer.objects.create(
            reference="TR0001", from_branch=main_branch, to_branch=nungwi
        )
        sent = Transfer.objects.create(
            reference="TR0002", from_branch=main_branch, to_branch=nungwi
        )
        TransferLine.objects.create(transfer=sent, variant=stocked["Mkate"], qty_sent=2)
        send_transfer(sent, user=owner)

    client.force_login(owner)

    client.post(reverse("inventory:transfer_cancel", args=[draft.pk]), follow=True)
    draft.refresh_from_db()
    assert draft.status == TransferStatus.CANCELLED

    response = client.post(
        reverse("inventory:transfer_cancel", args=[sent.pk]), follow=True
    )
    assert b"already been sent" in response.content
    sent.refresh_from_db()
    assert sent.status == TransferStatus.SENT


def test_a_posted_delivery_cannot_be_deleted(client, shop, main_branch, stocked, owner):
    """Once it is in the stock ledger the way back is an adjustment."""
    from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, Supplier
    from apps.purchasing.services import post_receipt

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Bakhresa")
        receipt = GoodsReceipt.objects.create(
            reference="GR0001", supplier=supplier, branch=main_branch
        )
        GoodsReceiptLine.objects.create(
            receipt=receipt, variant=stocked["Mkate"], qty=10, unit_cost=1100
        )
        post_receipt(receipt, user=owner)

    client.force_login(owner)
    response = client.post(
        reverse("purchasing:receipt_delete", args=[receipt.pk]), follow=True
    )
    assert b"already in stock" in response.content
    with tenant_context(shop):
        assert GoodsReceipt.objects.filter(pk=receipt.pk).exists()


def test_an_unposted_delivery_can_be_deleted(client, shop, main_branch, owner):
    from apps.purchasing.models import GoodsReceipt, Supplier

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Bakhresa")
        receipt = GoodsReceipt.objects.create(
            reference="GR0002", supplier=supplier, branch=main_branch
        )

    client.force_login(owner)
    client.post(reverse("purchasing:receipt_delete", args=[receipt.pk]), follow=True)
    with tenant_context(shop):
        assert not GoodsReceipt.objects.filter(pk=receipt.pk).exists()


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------

def test_a_customer_who_owes_money_cannot_be_removed(client, shop, main_branch, owner):
    from apps.customers.models import CreditKind, CreditTransaction

    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", credit_limit=50000)
        CreditTransaction.objects.create(
            customer=customer, kind=CreditKind.CHARGE,
            amount=Decimal("5000"), balance_after=Decimal("5000"),
        )

    client.force_login(owner)
    response = client.post(
        reverse("customers:customer_delete", args=[customer.pk]), follow=True
    )
    assert b"still owes" in response.content
    with tenant_context(shop):
        assert Customer.objects.filter(pk=customer.pk).exists()


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------

def test_an_expense_can_be_corrected_and_removed(client, shop, main_branch, owner):
    from apps.finance.models import Expense, ExpenseCategory

    with tenant_context(shop, branch=main_branch, user=owner):
        category = ExpenseCategory.objects.create(name="Transport")
        expense = Expense.objects.create(
            branch=main_branch, category=category, amount=Decimal("5000"),
            spent_at="2026-09-01", description="Boda",
        )

    client.force_login(owner)
    client.post(
        reverse("finance:expense_edit", args=[expense.pk]),
        {"category": category.pk, "amount": "7500", "spent_at": "2026-09-01",
         "method": "cash", "description": "Boda fare"},
        follow=True,
    )
    expense.refresh_from_db()
    assert expense.amount == Decimal("7500.00")

    client.post(reverse("finance:expense_delete", args=[expense.pk]), follow=True)
    with tenant_context(shop):
        assert not Expense.objects.filter(pk=expense.pk).exists()


# --------------------------------------------------------------------------
# Platform: plans and the permission catalogue
# --------------------------------------------------------------------------

@pytest.fixture
def staff(db, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    return owner


def test_a_plan_can_be_built_and_changed(client, db, staff):
    from apps.core.features import LIMIT_BRANCHES, MULTI_BRANCH
    from apps.tenancy.models import Plan

    client.force_login(staff)
    client.post(
        reverse("platform:plan_create"),
        {"name": "Growth", "price_monthly": "40000", "trial_days": "14",
         "is_public": "on", "limit:branches": "3", "features": [MULTI_BRANCH]},
        follow=True,
    )

    with unscoped():
        plan = Plan.objects.get(name="Growth")
        assert plan.limit(LIMIT_BRANCHES) == 3
        assert MULTI_BRANCH in plan.feature_keys()

        client.post(
            reverse("platform:plan_edit", args=[plan.pk]),
            {"name": "Growth", "price_monthly": "45000", "trial_days": "14",
             "limit:branches": "", "features": []},
            follow=True,
        )
        plan.refresh_from_db()
        # Blank means unlimited.
        assert plan.limit(LIMIT_BRANCHES) is None
        assert plan.feature_keys() == set()


def test_a_plan_with_shops_on_it_cannot_be_deleted(client, shop, staff):
    from apps.tenancy.models import Plan

    client.force_login(staff)
    with unscoped():
        plan = Plan.objects.get(code="business")

    response = client.post(
        reverse("platform:plan_delete", args=[plan.pk]), follow=True
    )
    assert b"Move them to another plan" in response.content
    with unscoped():
        assert Plan.objects.filter(pk=plan.pk).exists()


def test_changing_a_plan_invalidates_cached_permissions(client, shop, staff):
    """A feature removed must stop working on the very next request."""
    from apps.core.features import STOCK_TRANSFERS
    from apps.tenancy.models import Plan

    with tenant_context(shop):
        membership = Membership.objects.get(user=staff)
        assert membership.can("stock.transfer")
        before = membership.permissions_version

    client.force_login(staff)
    with unscoped():
        plan = Plan.objects.get(code="business")
    client.post(
        reverse("platform:plan_edit", args=[plan.pk]),
        {"name": plan.name, "price_monthly": "60000", "trial_days": "14",
         "features": []},
        follow=True,
    )

    with tenant_context(shop):
        membership.refresh_from_db()
        assert membership.permissions_version > before
        assert not shop.has_feature(STOCK_TRANSFERS)
        assert not membership.can("stock.transfer")


def test_the_permission_catalogue_is_visible_to_platform_staff(client, db, staff):
    client.force_login(staff)
    response = client.get(reverse("platform:permissions"))
    assert response.status_code == 200
    from apps.core.permissions import registry

    assert response.context["total"] == len(registry.all())
    assert len(response.context["items"]) == len(registry.all())
    assert b"pos.void" in response.content


def test_the_permission_catalogue_is_one_filterable_table(client, db, staff):
    """
    It was eight stacked cards, so reading forty-four rows meant scrolling
    past eight headers on a page capped to half the screen width.
    """
    client.force_login(staff)
    response = client.get(reverse("platform:permissions"))
    content = response.content.decode()

    # One table, not one per area.
    assert content.count("<table") == 1
    # With something to narrow it down.
    assert 'x-model="q"' in content
    assert 'x-model="module"' in content
    assert response.context["modules"]
    # And the counts that make the page worth opening.
    assert response.context["dangerous"] > 0
    assert response.context["gated"] > 0


def test_shop_staff_cannot_see_the_permission_catalogue(client, shop, owner):
    client.force_login(owner)
    assert client.get(reverse("platform:permissions")).status_code == 403


# --------------------------------------------------------------------------
# Controls that existed as views but had no button pointing at them
# --------------------------------------------------------------------------

def test_a_line_can_be_taken_off_a_draft_transfer(client, shop, main_branch, stocked, owner):
    from apps.inventory.models import Transfer, TransferLine

    with tenant_context(shop, branch=main_branch, user=owner):
        nungwi = Branch.objects.create(name="Nungwi")
        transfer = Transfer.objects.create(
            reference="TR0100", from_branch=main_branch, to_branch=nungwi
        )
        line = TransferLine.objects.create(
            transfer=transfer, variant=stocked["Mkate"], qty_sent=3
        )

    client.force_login(owner)
    page = client.get(reverse("inventory:transfer_detail", args=[transfer.pk]))
    assert reverse("inventory:transfer_line_delete", args=[line.pk]).encode() in page.content

    client.post(reverse("inventory:transfer_line_delete", args=[line.pk]), follow=True)
    with tenant_context(shop):
        assert not TransferLine.objects.filter(pk=line.pk).exists()


def test_a_line_can_be_taken_off_a_draft_order(client, shop, main_branch, stocked, owner):
    from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine, Supplier

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Bakhresa")
        order = PurchaseOrder.objects.create(
            reference="PO0100", supplier=supplier, branch=main_branch
        )
        line = PurchaseOrderLine.objects.create(
            order=order, variant=stocked["Mkate"], qty_ordered=5, unit_cost=1100
        )

    client.force_login(owner)
    page = client.get(reverse("purchasing:order_detail", args=[order.pk]))
    assert reverse("purchasing:order_line_delete", args=[line.pk]).encode() in page.content

    client.post(reverse("purchasing:order_line_delete", args=[line.pk]), follow=True)
    with tenant_context(shop):
        assert not PurchaseOrderLine.objects.filter(pk=line.pk).exists()


def test_an_unposted_delivery_shows_its_delete_button(client, shop, main_branch, owner):
    from apps.purchasing.models import GoodsReceipt, Supplier

    with tenant_context(shop, branch=main_branch, user=owner):
        supplier = Supplier.objects.create(name="Bakhresa")
        receipt = GoodsReceipt.objects.create(
            reference="GR0100", supplier=supplier, branch=main_branch
        )

    client.force_login(owner)
    page = client.get(reverse("purchasing:receipt_detail", args=[receipt.pk]))
    assert reverse("purchasing:receipt_delete", args=[receipt.pk]).encode() in page.content


def test_an_expense_category_can_be_removed(client, shop, main_branch, owner):
    from apps.finance.models import ExpenseCategory

    with tenant_context(shop, branch=main_branch, user=owner):
        category = ExpenseCategory.objects.create(name="Mistake")

    client.force_login(owner)
    page = client.get(reverse("finance:expense_list"))
    assert reverse("finance:expense_category_delete", args=[category.pk]).encode() in page.content

    client.post(
        reverse("finance:expense_category_delete", args=[category.pk]), follow=True
    )
    with tenant_context(shop):
        assert not ExpenseCategory.objects.filter(pk=category.pk).exists()


def test_the_health_page_offers_the_fiscal_retry(client, shop, main_branch, stocked, owner):
    from apps.pos.models import FiscalReceipt, FiscalStatus, PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])

    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])
        FiscalReceipt.objects.create(
            tenant=shop, sale=sale, provider="tra", status=FiscalStatus.FAILED,
            error="gateway timeout",
        )

    client.force_login(owner)
    page = client.get(reverse("platform:health"))
    assert reverse("platform:fiscal_retry").encode() in page.content

    client.post(reverse("platform:fiscal_retry"), follow=True)
    with tenant_context(shop):
        assert FiscalReceipt.objects.get(sale=sale).status == FiscalStatus.PENDING


def test_every_plan_card_has_a_visible_edit_button(client, db, owner):
    """
    The only way to edit a plan used to be clicking its name, which looked
    like a heading.
    """
    from apps.tenancy.models import Plan

    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])

    client.force_login(owner)
    page = client.get(reverse("platform:plans"))
    content = page.content.decode()

    with unscoped():
        plans = list(Plan.objects.all())

    assert plans
    for plan in plans:
        assert reverse("platform:plan_edit", args=[plan.pk]) in content
    # And the buttons say what they do.
    assert "<span>Edit</span>" in content

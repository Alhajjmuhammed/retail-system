"""
Every detail page, opened for real.

The list screens were covered from the start; the detail pages were not, which
is exactly where a bug like `request.branch_id` sat unnoticed -- the template
was fine, the view raised, and nothing ever opened it.

This creates one of every document a shop produces and opens its page.
"""


import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.catalog.models import Product
from apps.core.context import tenant_context
from apps.customers.models import Customer
from apps.inventory.models import (
    StockCount,
    StockCountLine,
    StockItem,
    Transfer,
    TransferLine,
)
from apps.org.models import Branch
from apps.pos.models import PaymentMethod
from apps.pos.services import add_to_cart, complete_sale, new_cart, open_shift
from apps.purchasing.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def everything(shop, main_branch, register, stocked, owner):
    """One of every document a shop produces."""
    with tenant_context(shop, branch=main_branch, user=owner):
        sugar = stocked["Sukari 1kg"]
        nungwi = Branch.objects.create(name="Nungwi")

        supplier = Supplier.objects.create(name="Bakhresa", phone="0777000111")

        order = PurchaseOrder.objects.create(
            reference="PO250001", supplier=supplier, branch=main_branch
        )
        PurchaseOrderLine.objects.create(
            order=order, variant=sugar, qty_ordered=50, unit_cost=2400
        )

        receipt = GoodsReceipt.objects.create(
            reference="GR250001", supplier=supplier, branch=main_branch,
            received_by=owner,
        )
        GoodsReceiptLine.objects.create(
            receipt=receipt, variant=sugar, qty=50, unit_cost=2400
        )

        transfer = Transfer.objects.create(
            reference="TR250001", from_branch=main_branch, to_branch=nungwi
        )
        TransferLine.objects.create(transfer=transfer, variant=sugar, qty_sent=5)

        count = StockCount.objects.create(reference="SC250001", branch=main_branch)
        StockCountLine.objects.create(
            count=count, variant=sugar, system_qty=100, unit_cost=2400
        )

        customer = Customer.objects.create(
            name="Mama Asha", phone="0777222333", credit_limit=50000
        )

        shift = open_shift(register=register, opening_float=10000)
        cart = new_cart(branch=main_branch, register=register, customer=customer)
        add_to_cart(cart, sugar, qty=2)
        sale = complete_sale(
            cart,
            [{"method": PaymentMethod.CASH, "amount": 6000}],
            shift=shift,
        )

        return {
            "product": Product.objects.get(name="Sukari 1kg"),
            "stock_item": StockItem.objects.get(variant=sugar, branch=main_branch),
            "supplier": supplier,
            "order": order,
            "receipt": receipt,
            "transfer": transfer,
            "count": count,
            "customer": customer,
            "shift": shift,
            "sale": sale,
            "role": Role.objects.get(name="Cashier"),
            "membership": Membership.objects.get(user=owner),
        }


DETAIL_PAGES = [
    ("catalog:product_edit", "product"),
    ("inventory:stock_adjust", "stock_item"),
    ("inventory:transfer_detail", "transfer"),
    ("inventory:count_detail", "count"),
    ("purchasing:supplier_detail", "supplier"),
    ("purchasing:supplier_edit", "supplier"),
    ("purchasing:order_detail", "order"),
    ("purchasing:receipt_detail", "receipt"),
    ("customers:customer_detail", "customer"),
    ("customers:customer_edit", "customer"),
    ("pos:sale_detail", "sale"),
    ("pos:sale_return", "sale"),
    ("pos:receipt", "sale"),
    ("pos:shift_report", "shift"),
    ("accounts:role_edit", "role"),
    ("accounts:staff_edit", "membership"),
]


@pytest.mark.parametrize("name,key", DETAIL_PAGES)
def test_every_detail_page_opens(client, shop, owner, everything, name, key):
    client.force_login(owner)
    response = client.get(reverse(name, args=[everything[key].pk]))
    assert response.status_code in (200, 302), (
        f"{name} returned {response.status_code}"
    )


def test_platform_tenant_detail_opens(client, shop, owner, everything):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    client.force_login(owner)
    assert client.get(reverse("platform:tenant_detail", args=[shop.pk])).status_code == 200


def test_transfer_detail_knows_whether_this_branch_can_receive(
    client, shop, main_branch, owner, everything
):
    """
    The receiving check read request.branch_id, which does not exist, so this
    page raised AttributeError for anyone with a branch resolved.
    """
    client.force_login(owner)
    response = client.get(
        reverse("inventory:transfer_detail", args=[everything["transfer"].pk])
    )
    assert response.status_code == 200
    # Draft, and this is the sending branch: it can be sent, not received.
    assert response.context["can_send"] is True
    assert response.context["can_receive"] is False


def test_a_tenant_with_no_subscription_does_not_break_the_app(client, shop, owner):
    """
    Accessing a missing one-to-one raises instead of returning None, so every
    `if subscription is None` guard was dead code.
    """
    shop.subscription.delete()
    shop.refresh_from_db()

    assert shop.active_subscription is None
    assert shop.has_feature("multi_branch") is False
    assert shop.limit_for("branches") == 0

    client.force_login(owner)
    assert client.get(reverse("core:dashboard")).status_code == 200


def test_receipt_renders_for_printing(client, shop, owner, everything):
    client.force_login(owner)
    response = client.get(reverse("pos:receipt", args=[everything["sale"].pk]))
    assert response.status_code == 200
    content = response.content.decode()
    assert everything["sale"].number in content
    assert "Duka la Salma" in content

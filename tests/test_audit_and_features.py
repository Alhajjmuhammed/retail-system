"""
The audit trail, loyalty and messaging.

All three were half-built: forty-four places wrote audit rows nothing could
read, and two features shops pay for -- loyalty and SMS -- had plan flags
with no code behind them.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import AuditLog, Membership, Role
from apps.core.context import tenant_context, unscoped
from apps.customers.models import Customer, LoyaltyKind, LoyaltyTransaction
from apps.notifications.models import Message, MessageStatus, MessageTemplate
from apps.pos.models import PaymentMethod
from apps.pos.services import add_to_cart, complete_sale, new_cart, void_sale

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def test_the_activity_log_shows_what_people_did(client, shop, main_branch, stocked, owner,
                                                register):
    """Recording who voided a sale is pointless if nobody can read it."""
    from apps.pos.services import open_shift

    with tenant_context(shop, branch=main_branch, user=owner):
        # The cash goes back out of a drawer, so one has to be open.
        open_shift(register=register)
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=2)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 3000}])

    client.force_login(owner)
    client.post(
        reverse("pos:sale_void", args=[sale.pk]),
        {"reason": "Wrong item scanned"}, follow=True,
    )

    response = client.get(reverse("accounts:audit_log"))
    assert response.status_code == 200
    assert b"sale.voided" in response.content

    with tenant_context(shop):
        entry = AuditLog.objects.filter(action="sale.voided").first()
    detail = client.get(reverse("accounts:audit_entry", args=[entry.pk]))
    assert detail.status_code == 200
    assert b"Wrong item scanned" in detail.content


def test_the_activity_log_can_be_filtered(client, shop, main_branch, stocked, owner):
    client.force_login(owner)
    client.post(reverse("catalog:taxonomy"),
                {"kind": "category", "name": "Drinks"}, follow=True)

    response = client.get(reverse("accounts:audit_log"), {"action": "category.created"})
    assert response.status_code == 200
    rows = list(response.context["page"])
    assert rows and all(r.action == "category.created" for r in rows)


def test_a_cashier_cannot_read_the_activity_log(client, shop, cashier):
    with tenant_context(shop):
        Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )
    client.force_login(cashier)
    assert client.get(reverse("accounts:audit_log")).status_code == 403


def test_the_platform_records_who_opened_a_shop_for_support(client, shop, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])

    client.force_login(owner)
    client.post(reverse("platform:impersonate", args=[shop.pk]), follow=True)

    response = client.get(reverse("platform:platform_audit"))
    assert response.status_code == 200
    assert b"Opened a shop for support" in response.content
    assert shop.name.encode() in response.content


# --------------------------------------------------------------------------
# Loyalty
# --------------------------------------------------------------------------

def test_points_are_earned_on_a_sale(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", phone="0777000111")
        cart = new_cart(branch=main_branch, customer=customer)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=5)  # 15,000
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 15000}])

        # One point per 1,000 spent.
        assert customer.loyalty_points == 15


def test_voiding_a_sale_takes_its_points_back(shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha")
        cart = new_cart(branch=main_branch, customer=customer)
        add_to_cart(cart, stocked["Sukari 1kg"], qty=4)  # 12,000
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 12000}])
        assert customer.loyalty_points == 12

        void_sale(sale, reason="Mistake", user=owner)
        assert customer.loyalty_points == 0


def test_points_cannot_be_earned_without_the_plan_feature(
    db, owner, free_plan, main_branch
):
    from apps.catalog.models import Price, PriceList, Product, TaxRate, Unit
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement
    from apps.org.models import Branch
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Small Duka", owner=owner, plan=free_plan)
    with tenant_context(tenant, user=owner):
        branch = Branch.objects.first()
        product = Product.objects.create(
            name="Mkate", base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True),
        )
        Price.objects.create(
            price_list=PriceList.objects.get(is_default=True),
            variant=product.default_variant, amount=5000,
        )
        record_movement(
            variant=product.default_variant, qty_delta=10,
            reason=MovementReason.PURCHASE, unit_cost=3000, branch=branch,
        )

        customer = Customer.objects.create(name="Someone")
        cart = new_cart(branch=branch, customer=customer)
        add_to_cart(cart, product.default_variant, qty=2)
        complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 10000}])

        assert not tenant.has_feature("loyalty")
        assert customer.loyalty_points == 0


def test_points_can_be_redeemed_but_not_overdrawn(client, shop, main_branch, stocked, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha")
        LoyaltyTransaction.objects.create(
            tenant=shop, customer=customer, kind=LoyaltyKind.EARN, points=20
        )

    client.force_login(owner)
    client.post(reverse("customers:customer_redeem", args=[customer.pk]),
                {"points": "15", "note": "Free bread"}, follow=True)
    assert customer.loyalty_points == 5

    response = client.post(
        reverse("customers:customer_redeem", args=[customer.pk]),
        {"points": "50"}, follow=True,
    )
    assert b"points, not 50" in response.content
    assert customer.loyalty_points == 5


# --------------------------------------------------------------------------
# Messaging
# --------------------------------------------------------------------------

def test_a_new_shop_gets_message_templates(shop):
    with tenant_context(shop):
        assert MessageTemplate.objects.count() == 3
        assert MessageTemplate.objects.filter(key="sale_receipt").exists()


def test_a_sale_to_a_customer_with_a_phone_queues_a_receipt(
    shop, main_branch, stocked, owner
):
    """Queued by a listener, so the checkout code knows nothing about SMS."""
    with tenant_context(shop, branch=main_branch, user=owner):
        customer = Customer.objects.create(name="Mama Asha", phone="0777000111")
        cart = new_cart(branch=main_branch, customer=customer)
        add_to_cart(cart, stocked["Mkate"], qty=2)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 3000}])

        message = Message.objects.filter(template_key="sale_receipt").first()
        assert message is not None
        assert message.to == "0777000111"
        assert sale.number in message.body
        assert "Duka la Salma" in message.body
        assert message.status == MessageStatus.QUEUED


def test_no_message_without_the_plan_feature(db, owner, free_plan):
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Small Duka", owner=owner, plan=free_plan)
    with tenant_context(tenant, user=owner):
        customer = Customer.objects.create(name="Someone", phone="0777000111")
        from apps.notifications.services import send_from_template

        assert send_from_template("sale_receipt", to=customer.phone) is None
        assert Message.objects.count() == 0


def test_a_customer_with_no_phone_is_recorded_as_not_sent(shop):
    """So a shop sees they had no number, rather than assuming they were told."""
    from apps.notifications.services import queue

    with tenant_context(shop):
        message = queue(channel="sms", to="", body="Hello")
        assert message.status == MessageStatus.SKIPPED
        assert "No number" in message.error


def test_the_message_queue_drains_through_a_gateway(shop):
    from apps.notifications import tasks
    from apps.notifications.services import queue

    with tenant_context(shop):
        queue(channel="sms", to="0777000111", body="Hello")

    tasks.GATEWAYS["sms"] = lambda m: {"ref": "SMS-1", "cost": Decimal("35")}
    try:
        result = tasks.send_queued_messages()
    finally:
        tasks.GATEWAYS.pop("sms")

    assert result["sent"] == 1
    with tenant_context(shop):
        message = Message.objects.first()
        assert message.status == MessageStatus.SENT
        assert message.cost == Decimal("35.00")


def test_a_shop_can_reword_its_own_messages(client, shop, owner):
    """The default English is a starting point, not the product."""
    with tenant_context(shop):
        template = MessageTemplate.objects.get(key="sale_receipt")

    client.force_login(owner)
    client.post(
        reverse("notifications:template_edit", args=[template.pk]),
        {"name": "Risiti", "body": "{shop}: asante. Risiti {receipt}, jumla {amount}.",
         "is_active": "on"},
        follow=True,
    )
    template.refresh_from_db()
    assert template.name == "Risiti"
    assert "asante" in template.body


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------

def test_an_invited_person_sets_their_own_password_and_joins(client, shop, owner):
    """
    The alternative -- a manager typing a password and reading it out -- means
    the manager knows it.
    """
    from apps.accounts.models import Invitation

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")

    client.force_login(owner)
    client.post(
        reverse("accounts:staff_invite"),
        {"email": "juma@shop.test", "role": role.pk},
        follow=True,
    )

    with unscoped():
        invitation = Invitation.objects_all.get(email="juma@shop.test")

    client.logout()
    accept = reverse("accounts:accept_invitation", args=[invitation.token])
    assert client.get(accept).status_code == 200

    client.post(accept, {"name": "Juma Ally", "password": "chosen-by-them-99"},
                follow=True)

    with tenant_context(shop):
        membership = Membership.objects.get(user__email="juma@shop.test")
        assert membership.role == role
        # Every branch, including any opened later -- not links to today's.
        assert membership.all_branches

    client.logout()
    assert client.login(email="juma@shop.test", password="chosen-by-them-99")


def test_an_invitation_works_only_once(client, shop, owner):
    from apps.accounts.models import Invitation

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")

    client.force_login(owner)
    client.post(reverse("accounts:staff_invite"),
                {"email": "juma@shop.test", "role": role.pk}, follow=True)
    with unscoped():
        invitation = Invitation.objects_all.get(email="juma@shop.test")

    client.logout()
    accept = reverse("accounts:accept_invitation", args=[invitation.token])
    client.post(accept, {"name": "Juma", "password": "chosen-by-them-99"}, follow=True)

    client.logout()
    assert client.get(accept).status_code == 410


def test_an_expired_invitation_is_refused(client, shop, owner):
    from datetime import timedelta

    from django.utils import timezone

    from apps.accounts.models import Invitation

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        invitation = Invitation.objects.create(
            tenant=shop, email="late@shop.test", role=role,
            expires_at=timezone.now() - timedelta(days=1),
        )

    response = client.get(
        reverse("accounts:accept_invitation", args=[invitation.token])
    )
    assert response.status_code == 410


def test_a_weak_password_is_refused_at_the_invitation(client, shop, owner):
    from datetime import timedelta

    from django.utils import timezone

    from apps.accounts.models import Invitation

    with tenant_context(shop):
        role = Role.objects.get(name="Cashier")
        invitation = Invitation.objects.create(
            tenant=shop, email="weak@shop.test", role=role,
            expires_at=timezone.now() + timedelta(days=7),
        )

    response = client.post(
        reverse("accounts:accept_invitation", args=[invitation.token]),
        {"name": "Weak", "password": "123"},
    )
    assert response.status_code == 200
    assert response.context["errors"]
    with unscoped():
        assert not Membership.objects_all.filter(user__email="weak@shop.test").exists()


def test_an_invitation_opens_even_while_signed_in_to_another_shop(
    client, db, owner, cashier, business_plan
):
    from datetime import timedelta

    from django.utils import timezone

    """
    Row-level security was bound to the shop they were already in, so the
    invitation looked like it had expired.
    """
    from apps.accounts.models import Invitation
    from apps.tenancy.services import create_tenant

    create_tenant(name="Shop One", owner=owner, plan=business_plan)
    second, _ = create_tenant(name="Shop Two", owner=cashier, plan=business_plan)

    with tenant_context(second):
        invitation = Invitation.objects.create(
            tenant=second,
            email="newcomer@shop.test",
            role=Role.objects.get(name="Cashier"),
            expires_at=timezone.now() + timedelta(days=7),
        )

    # Signed in to Shop One, opening Shop Two's invitation.
    client.force_login(owner)
    response = client.get(
        reverse("accounts:accept_invitation", args=[invitation.token])
    )
    assert response.status_code == 200
    assert b"Shop Two" in response.content

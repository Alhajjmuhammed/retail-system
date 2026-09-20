"""
The permission engine resolves: plan -> deny -> scope -> grant -> limit.

These tests exist because that order is the whole security model. If any one
of them breaks, a shop can either lose access it paid for or gain access it
did not.
"""

import pytest

from apps.accounts.models import Membership, OverrideEffect, Role, UserPermission
from apps.core.context import tenant_context
from apps.core.features import STOCK_TRANSFERS
from apps.org.models import Branch
from apps.tenancy.services import create_tenant

pytestmark = pytest.mark.django_db


def membership_for(tenant, user, role_name):
    with tenant_context(tenant):
        role = Role.objects.get(name=role_name)
        return Membership.objects.create(tenant=tenant, user=user, role=role)


# --------------------------------------------------------------------------
# Grants and limits
# --------------------------------------------------------------------------

def test_cashier_can_sell_but_not_see_cost(shop, cashier):
    membership = membership_for(shop, cashier, "Cashier")
    with tenant_context(shop):
        assert membership.can("pos.sell")
        assert not membership.can("product.view_cost")


def test_discount_ceiling_is_enforced(shop, cashier):
    membership = membership_for(shop, cashier, "Cashier")
    with tenant_context(shop):
        assert membership.can("pos.discount", value=5)
        assert not membership.can("pos.discount", value=15)

        decision = membership.check_permission("pos.discount", value=15)
        assert decision.limit == 5
        # Dangerous permissions end at "ask a manager", never at a wall.
        assert decision.can_override


def test_manager_has_a_higher_ceiling(shop, cashier):
    membership = membership_for(shop, cashier, "Manager")
    with tenant_context(shop):
        assert membership.can("pos.discount", value=15)
        assert not membership.can("pos.discount", value=25)


def test_owner_holds_everything(shop, owner):
    with tenant_context(shop):
        # Membership.objects is tenant-scoped: outside a context it returns
        # nothing at all, which is the isolation guarantee doing its job.
        membership = Membership.objects.get(user=owner)
        assert membership.can("billing.manage")
        assert membership.can("pos.void", value=10_000_000)


# --------------------------------------------------------------------------
# Per-user exceptions
# --------------------------------------------------------------------------

def test_user_grant_adds_to_a_role(shop, cashier):
    membership = membership_for(shop, cashier, "Cashier")
    with tenant_context(shop):
        assert not membership.can("stock.adjust", value=1000)

        UserPermission.objects.create(
            membership=membership,
            permission_id=_permission_id("stock.adjust"),
            effect=OverrideEffect.GRANT,
            limit_value=50_000,
        )
        membership.refresh_from_db()
        assert membership.can("stock.adjust", value=1000)
        assert not membership.can("stock.adjust", value=90_000)


def test_user_deny_beats_a_role_grant(shop, cashier):
    membership = membership_for(shop, cashier, "Manager")
    with tenant_context(shop):
        assert membership.can("pos.void", value=1000)

        UserPermission.objects.create(
            membership=membership,
            permission_id=_permission_id("pos.void"),
            effect=OverrideEffect.DENY,
            reason="Under investigation",
        )
        membership.refresh_from_db()
        assert not membership.can("pos.void", value=1000)


# --------------------------------------------------------------------------
# Plan beats everything
# --------------------------------------------------------------------------

def test_plan_gate_overrides_every_role(owner, free_plan):
    tenant, membership = create_tenant(
        name="Small Duka", owner=owner, plan=free_plan
    )
    with tenant_context(tenant):
        # The Free plan has no stock transfers. Not even the owner gets them,
        # and no manager PIN can buy a subscription.
        assert not tenant.has_feature(STOCK_TRANSFERS)
        decision = membership.check_permission("stock.transfer")
        assert not decision.allowed
        assert not decision.can_override
        assert "plan" in decision.reason.lower()


def test_business_plan_includes_transfers(shop, owner):
    with tenant_context(shop):
        membership = Membership.objects.get(user=owner)
        assert shop.has_feature(STOCK_TRANSFERS)
        assert membership.can("stock.transfer")


# --------------------------------------------------------------------------
# Branch scope
# --------------------------------------------------------------------------

def test_manager_is_scoped_to_their_branches(shop, cashier):
    membership = membership_for(shop, cashier, "Manager")
    with tenant_context(shop):
        stone_town = Branch.objects.get(name="Main")
        nungwi = Branch.objects.create(name="Nungwi")

        membership.branch_links.create(branch=stone_town)

        assert membership.can("cashup.perform", branch=stone_town)
        assert not membership.can("cashup.perform", branch=nungwi)


# --------------------------------------------------------------------------
# Set-valued permissions -- the shelf payment rule
# --------------------------------------------------------------------------

def test_mobile_payment_method_is_restricted(shop, cashier):
    """
    The owner decides who may take money on a phone, how much, and by what
    method. Cash away from the drawer is the actual risk, so a shop can allow
    mobile money on the phone and require cash at the till.
    """
    membership = membership_for(shop, cashier, "Manager")
    with tenant_context(shop):
        assert membership.can("pos.mobile_payment", value=100_000)
        assert not membership.can("pos.mobile_payment", value=900_000)
        assert membership.can("pos.mobile_methods", value="mpesa")

        role_permission = membership.role.permissions.get(
            permission__code="pos.mobile_methods"
        )
        role_permission.set_value = ["mpesa", "tigopesa"]
        role_permission.save()
        membership.role.bump_version()
        membership.refresh_from_db()

        assert membership.can("pos.mobile_methods", value="mpesa")
        assert not membership.can("pos.mobile_methods", value="cash")


def _permission_id(code):
    from apps.accounts.models import Permission

    return Permission.objects.get(code=code).pk

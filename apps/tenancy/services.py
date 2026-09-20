"""
Creating a tenant.

One function, called by signup and by your platform admin. It does everything
a new business needs to be usable in one transaction: the tenant, its trial,
its first branch and till, the starter roles cloned from the templates, the
owner's membership, and sensible defaults for tax and pricing.

Roles are *cloned*, not shared. The tenant owns its copies and can rename and
re-tick them however it likes without affecting anybody else.
"""

from django.db import transaction
from django.utils.text import slugify

from apps.accounts.models import Membership, Permission, Role, RolePermission
from apps.accounts.permissions import STARTER_ROLES
from apps.core.context import tenant_context
from apps.core.events import TENANT_CREATED, events
from apps.tenancy.models import Plan, Subscription, Tenant


@transaction.atomic
def create_tenant(*, name, owner, plan=None, branch_name="Main", currency="TZS", **extra):
    plan = plan or Plan.objects.filter(is_public=True).order_by("sort_order").first()
    if plan is None:
        raise ValueError("No plan available. Run `manage.py seed_plans` first.")

    tenant = Tenant.objects.create(
        name=name,
        slug=_unique_slug(name),
        currency=currency,
        **extra,
    )

    subscription = Subscription.objects.create(tenant=tenant, plan=plan)
    subscription.start_trial()

    with tenant_context(tenant, user=owner):
        roles = _clone_starter_roles(tenant)
        owner_role = roles["Owner"]

        membership = Membership.objects.create(
            tenant=tenant, user=owner, role=owner_role
        )

        branch = _create_first_branch(tenant, branch_name)
        _create_defaults(tenant, branch)

    owner.last_tenant = tenant
    owner.save(update_fields=["last_tenant", "updated_at"])

    events.emit(TENANT_CREATED, tenant=tenant, owner=owner, membership=membership)
    return tenant, membership


def _unique_slug(name) -> str:
    base = slugify(name)[:50] or "shop"
    slug, counter = base, 1
    while Tenant.objects.filter(slug=slug).exists():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def _clone_starter_roles(tenant) -> dict[str, Role]:
    permissions = {p.code: p for p in Permission.objects.all()}
    roles: dict[str, Role] = {}

    for role_name, spec in STARTER_ROLES.items():
        role = Role.objects.create(
            tenant=tenant,
            name=role_name,
            description=spec.get("description", ""),
            is_owner_role=spec.get("is_owner_role", False),
        )
        roles[role_name] = role

        # The owner role holds everything implicitly, so it stores no rows.
        # That way a permission added next year is held without a data fix.
        if role.is_owner_role:
            continue

        rows = []
        for code, limit in spec["permissions"].items():
            permission = permissions.get(code)
            if permission is None:
                continue
            rows.append(
                RolePermission(
                    role=role,
                    permission=permission,
                    granted=True,
                    limit_value=limit if not isinstance(limit, list) else None,
                    set_value=limit if isinstance(limit, list) else [],
                )
            )
        RolePermission.objects.bulk_create(rows)

    return roles


def _create_first_branch(tenant, branch_name):
    from apps.org.models import Branch, Register

    branch = Branch.objects.create(name=branch_name, is_default=True)
    Register.objects.create(branch=branch, name="Till 1")
    return branch


def _create_defaults(tenant, branch):
    from apps.catalog.models import PriceList, PriceListKind, TaxRate, Unit
    from apps.org.models import TenantSettings

    units = [
        ("Piece", "pc", False),
        ("Kilogram", "kg", True),
        ("Litre", "l", True),
        ("Packet", "pkt", False),
        ("Crate", "crate", False),
    ]
    for unit_name, code, decimal in units:
        Unit.objects.create(name=unit_name, code=code, allows_decimal=decimal)

    vat = TaxRate.objects.create(
        name="VAT 18%", rate=18, is_inclusive=True, is_default=True
    )
    TaxRate.objects.create(name="Zero rated", rate=0, is_inclusive=True)
    TaxRate.objects.create(name="Exempt", rate=0, is_inclusive=True)

    PriceList.objects.create(name="Retail", kind=PriceListKind.RETAIL, is_default=True)

    TenantSettings.objects.create(tenant=tenant, default_tax_rate=vat)

    from apps.notifications.services import install_default_templates

    install_default_templates(tenant)

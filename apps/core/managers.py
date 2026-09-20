"""
Managers that scope every query to the current tenant.

The filter is applied in ``get_queryset`` so it cannot be skipped by a caller
who forgets. Code that genuinely needs to cross tenants says so out loud with
``core.context.unscoped()``.
"""

from django.db import models

from apps.core.context import get_current_tenant_id, is_unscoped


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)

    def for_branch(self, branch):
        return self.filter(branch=branch)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Default manager: returns only rows belonging to the current tenant."""

    def get_queryset(self):
        qs = super().get_queryset()
        if is_unscoped():
            return qs
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            # No tenant resolved and no explicit escape hatch: return nothing
            # rather than everything. A bug becomes an empty page, never a
            # leak of another shop's data.
            return qs.none()
        return qs.filter(tenant_id=tenant_id)


class AllTenantsManager(models.Manager.from_queryset(TenantQuerySet)):
    """
    Unfiltered access, attached to every tenant model as ``objects_all``.

    For the platform admin and billing jobs. Using it inside a tenant-facing
    view is a bug.
    """


"""
Abstract bases every model in the system inherits from.

Three ideas live here and nowhere else:

* ``TenantModel``  -- a row belongs to exactly one tenant, always scoped.
* ``BranchModel``  -- a row belongs to one shop within that tenant.
* ``LedgerModel``  -- a row is written once and never changed or deleted.
"""

import uuid

from django.db import models

from apps.core.context import get_current_tenant, get_current_user
from apps.core.managers import AllTenantsManager, TenantManager


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(TimeStampedModel):
    """
    Base for anything a shop owns.

    ``tenant`` is filled from the request context on first save, so no view or
    form ever has to set it -- and no view can set it to somebody else's
    tenant, because the value never comes from user input.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
        editable=False,
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    objects = TenantManager()
    objects_all = AllTenantsManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            tenant = get_current_tenant()
            if tenant is None:
                raise ValueError(
                    f"{type(self).__name__} saved with no tenant in context. "
                    "Wrap the call in core.context.tenant_context()."
                )
            self.tenant = tenant
        if self._state.adding and self.created_by_id is None:
            self.created_by = get_current_user()
        super().save(*args, **kwargs)


class BranchModel(TenantModel):
    """Tenant-owned and tied to one shop. Stock, sales and shifts all are."""

    branch = models.ForeignKey(
        "org.Branch",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    class Meta:
        abstract = True


class LedgerModel(BranchModel):
    """
    Append-only.

    Stock movements, credit transactions and cash movements inherit this. A
    correction is a new opposing row, never an edit -- that is what keeps the
    stock figure and the cash variance defensible months later.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError(
                f"{type(self).__name__} is append-only. "
                "Write a reversing entry instead of editing this row."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            f"{type(self).__name__} is append-only and cannot be deleted."
        )


class SyncableModel(models.Model):
    """
    Anything a till or phone can create while offline.

    ``client_uuid`` is generated on the device before the record is sent. The
    server upserts on it, which is what makes a retried sync harmless instead
    of a duplicate sale.
    """

    client_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_offline_origin = models.BooleanField(default=False)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class JobHeartbeat(models.Model):
    """
    When each scheduled job last ran, and how it went.

    Renewals, fiscal receipts, SMS and dunning all ride on the scheduler. If
    it stops, they stop silently -- this is what lets the Health page say so.
    """

    name = models.CharField(max_length=80, unique=True)
    last_started = models.DateTimeField(null=True, blank=True)
    last_finished = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(default=True)
    last_error = models.TextField(blank=True)
    last_result = models.JSONField(default=dict, blank=True)
    runs = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name

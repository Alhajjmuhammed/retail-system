"""
People and permissions.

The permission vocabulary is fixed in code and synced into ``Permission`` by
migration. Everything above it -- roles, who holds them, the ceilings on them,
the per-person exceptions -- belongs to the tenant.
"""

import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core import permissions as perms
from apps.core.context import unscoped
from apps.core.models import TenantModel, TimeStampedModel

# --------------------------------------------------------------------------
# User -- one global identity, membership in one or more tenants
# --------------------------------------------------------------------------

class UserManager(BaseUserManager):
    @classmethod
    def normalize_email(cls, email):
        # Wholly lower-case, not just the domain as Django does: people type
        # their address however they like, and must always get the same account.
        return (email or "").strip().lower()

    def get_by_natural_key(self, username):
        email = (username or "").strip()
        exact = self.filter(email=email.lower()).first() or self.filter(email=email).first()
        if exact is not None:
            return exact
        return self.get(email__iexact=email)

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_platform_staff", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Identity is global; authorisation is per tenant.

    One person can own two businesses and sign in once, then switch. The active
    tenant lives in the session, never in the URL.
    """

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # Access to /platform/ -- your own team, not a shop's staff.
    is_platform_staff = models.BooleanField(default=False)
    # What they may do there. Empty on a platform admin means everything:
    # accounts made from the command line have no role, and must not be
    # locked out of the platform they were created to run.
    platform_role = models.ForeignKey(
        "accounts.PlatformRole",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="admins",
    )

    last_tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or self.email

    @property
    def is_super_admin(self) -> bool:
        return bool(
            self.is_platform_staff
            and (self.platform_role_id is None or self.platform_role.is_super)
        )

    @property
    def platform_permissions(self) -> frozenset:
        """Every platform permission this person holds. Empty for shop staff."""
        from apps.core import platform_perms

        if not (self.is_active and self.is_platform_staff):
            return frozenset()
        if self.is_super_admin:
            return platform_perms.ALL
        return frozenset(self.platform_role.permissions) & platform_perms.ALL

    def has_platform_perm(self, code: str) -> bool:
        return code in self.platform_permissions

    @property
    def platform_role_permissions(self) -> frozenset:
        """
        What their role grants, switched on or off.

        For deciding who may manage whom: an inactive admin holds nothing
        *now*, but reactivating them restores everything, so comparing against
        "nothing" let a lesser admin reset and reactivate a stronger one.
        """
        from apps.core import platform_perms

        if not self.is_platform_staff:
            return frozenset()
        if self.platform_role_id is None or self.platform_role.is_super:
            return platform_perms.ALL
        return frozenset(self.platform_role.permissions) & platform_perms.ALL

    def shop_may_set_password(self, tenant) -> bool:
        """
        Whether `tenant` may choose this person's password.

        An account is one identity across every shop. A shop that could reset
        the password of somebody who also works elsewhere -- or runs the
        platform -- could sign in as them there. So only an account that
        belongs to this shop alone is the shop's to reset.
        """
        if self.pk is None:
            return True
        if self.is_platform_staff:
            return False
        with unscoped():
            return not Membership.objects_all.filter(user=self).exclude(
                tenant=tenant
            ).exists()

    def active_membership(self, tenant_id=None):
        """
        The membership this request runs under.

        Preference order: the tenant in the session, then the last one used,
        then whichever single tenant they belong to. Cached on the instance so
        template permission checks do not re-query.
        """
        cached = getattr(self, "_active_membership", None)
        if cached is not None and (tenant_id is None or cached.tenant_id == tenant_id):
            return cached

        # unscoped, not self.memberships: the related manager is tenant
        # scoped, and this method is what decides the tenant. Identity
        # legitimately crosses tenants, so both locks come off.
        with unscoped():
            memberships = list(
                Membership.objects_all.select_related(
                    "tenant", "tenant__subscription", "role"
                ).filter(user=self, is_active=True)
            )

        def pick(wanted):
            return next((m for m in memberships if m.tenant_id == wanted), None)

        membership = pick(int(tenant_id)) if tenant_id else None
        if membership is None and self.last_tenant_id:
            membership = pick(self.last_tenant_id)
        if membership is None:
            membership = memberships[0] if memberships else None

        self._active_membership = membership
        return membership

    @property
    def tenant_count(self) -> int:
        with unscoped():
            return Membership.objects_all.filter(user=self, is_active=True).count()


# --------------------------------------------------------------------------
# Permissions -- the fixed vocabulary
# --------------------------------------------------------------------------

class Permission(models.Model):
    """
    Seeded from ``apps.core.permissions.registry`` by migration.

    Tenants never create these. A new module adds new codes, which default to
    denied for every custom role and granted to Owner.
    """

    code = models.CharField(max_length=60, unique=True)
    module = models.CharField(max_length=40, db_index=True)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    value_type = models.CharField(max_length=10, default="bool")
    is_dangerous = models.BooleanField(default=False)
    requires_feature = models.CharField(max_length=40, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["module", "sort_order", "code"]

    def __str__(self):
        return self.label

    @property
    def spec(self):
        return perms.registry.get(self.code)


class Role(TenantModel):
    """
    A named set of permissions, owned by the tenant.

    System templates (``tenant`` null) are cloned into each new tenant at
    signup so nobody starts at a blank screen, and the copies are theirs to
    rename and re-tick.
    """

    name = models.CharField(max_length=60)
    description = models.CharField(max_length=200, blank=True)
    is_system = models.BooleanField(
        default=False, help_text="A template cloned into new tenants."
    )
    is_owner_role = models.BooleanField(
        default=False,
        help_text="Holds every permission, always. Cannot be edited or removed.",
    )

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return self.name

    def grant(self, code: str, *, limit=None, options=None):
        permission = Permission.objects.get(code=code)
        row, _ = RolePermission.objects.update_or_create(
            role=self,
            permission=permission,
            defaults={"granted": True, "limit_value": limit, "set_value": options or []},
        )
        self.bump_version()
        return row

    def revoke(self, code: str):
        RolePermission.objects.filter(role=self, permission__code=code).delete()
        self.bump_version()

    def bump_version(self):
        """Invalidates the cached permission map for everyone holding this role."""
        self.memberships.update(permissions_version=models.F("permissions_version") + 1)


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    granted = models.BooleanField(default=True)
    # The ceiling: discount up to 10%, void up to 50,000. Null means no limit.
    limit_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # For set-valued permissions, e.g. which payment methods on a phone.
    set_value = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = [("role", "permission")]

    def __str__(self):
        ceiling = f" up to {self.limit_value:,.0f}" if self.limit_value else ""
        return f"{self.role}: {self.permission}{ceiling}"


# --------------------------------------------------------------------------
# Membership -- a person, in a tenant, at some branches
# --------------------------------------------------------------------------

class Membership(TenantModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")

    # For approving a colleague's action on the spot. Hashed, never stored raw,
    # and usable offline because the till caches it for the shift.
    pin_hash = models.CharField(max_length=128, blank=True)

    is_active = models.BooleanField(default=True)
    # Every branch, including ones opened later -- or only the linked ones.
    # Stated outright: inferring "every branch" from having no links meant
    # deleting somebody's only branch quietly gave them all the others.
    all_branches = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # Set on the unsaved stand-in used during a platform support session.
    is_support = False

    # Bumped whenever this person's permissions change, which is what the
    # permission cache keys on.
    permissions_version = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = [("tenant", "user")]
        ordering = ["user__name"]

    def __str__(self):
        return f"{self.user} at {self.tenant}"

    # -- branches -----------------------------------------------------------

    def branches(self, include_closed=False):
        """
        Where this person works. `include_closed` for history: a branch that
        has since closed still sold things, and its sales must not vanish
        from every report and list.
        """
        from apps.org.models import Branch

        rows = Branch.objects.filter(tenant=self.tenant)
        if not include_closed:
            rows = rows.filter(is_active=True)
        if self.role.is_owner_role or self.all_branches:
            return rows
        return rows.filter(membership_links__membership=self)

    def covers_branch(self, branch) -> bool:
        if branch is None:
            return True
        if branch.tenant_id != self.tenant_id:
            return False
        if self.role.is_owner_role or self.all_branches:
            return True
        return self.branch_links.filter(branch=branch).exists()

    def set_branches(self, branches, *, all_branches):
        """
        Change where this person works.

        Links to branches that are closed are kept: the form cannot show them,
        and dropping them silently used to change what somebody may do.
        """
        keep_closed = list(self.branch_links.filter(branch__is_active=False)
                           .values_list("branch_id", flat=True))
        self.branch_links.all().delete()
        for branch_id in {b.pk for b in branches} | set(keep_closed):
            self.branch_links.create(branch_id=branch_id)
        self.all_branches = all_branches
        self.save(update_fields=["all_branches", "updated_at"])

    def active_branch(self, branch_id=None):
        """
        Which shop this person is working in.

        The session's choice wins, then the business's default branch, then
        whatever comes first. Falling back to alphabetical order would land
        somebody in a branch that has no till in it.
        """
        branches = self.branches()
        if branch_id:
            found = branches.filter(pk=branch_id).first()
            if found:
                return found
        return branches.filter(is_default=True).first() or branches.first()

    # -- permissions --------------------------------------------------------

    def check_permission(self, code: str, *, branch=None, value=None):
        """The one entry point for every permission question in the system."""
        return perms.resolve(self, code, branch=branch, value=value)

    def can(self, code: str, *, branch=None, value=None) -> bool:
        return bool(self.check_permission(code, branch=branch, value=value))

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(raw_pin)
        self.save(update_fields=["pin_hash", "updated_at"])

    def check_pin(self, raw_pin: str) -> bool:
        if not self.pin_hash:
            return False
        return check_password(raw_pin, self.pin_hash)

    def invalidate_permissions(self):
        self.permissions_version = models.F("permissions_version") + 1
        self.save(update_fields=["permissions_version", "updated_at"])
        # Read back: left as an F() expression, the next cache key contained
        # the expression itself and a stale permission map was cached.
        self.refresh_from_db(fields=["permissions_version"])


class MembershipBranch(models.Model):
    """Which shops this person works at. A manager at one is not a manager at all."""

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="branch_links"
    )
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.CASCADE, related_name="membership_links"
    )

    class Meta:
        unique_together = [("membership", "branch")]

    def __str__(self):
        return f"{self.membership.user} at {self.branch}"

    def save(self, *args, **kwargs):
        # Linking somebody to a branch means "works here" -- so they no longer
        # work everywhere. `set_branches` states the flag afterwards anyway.
        super().save(*args, **kwargs)
        if self.membership.all_branches:
            type(self.membership).objects_all.filter(pk=self.membership_id).update(
                all_branches=False
            )
            self.membership.all_branches = False


class OverrideEffect(models.TextChoices):
    GRANT = "grant", "Also allowed"
    DENY = "deny", "Withheld"


class UserPermission(models.Model):
    """
    A per-person exception on top of the role.

    "Juma is a Cashier but he may also adjust stock", or the reverse. A deny
    always beats a role grant -- withholding something has to be reliable, or
    it is worthless.
    """

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="overrides"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    effect = models.CharField(
        max_length=6, choices=OverrideEffect.choices, default=OverrideEffect.GRANT
    )
    limit_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    set_value = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("membership", "permission")]

    def __str__(self):
        return f"{self.membership.user}: {self.get_effect_display()} {self.permission}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.membership.invalidate_permissions()


class Invitation(TenantModel):
    email = models.EmailField()
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    # Where they will work and their approval PIN, chosen when inviting and
    # applied when they accept. Both were dropped before.
    branch_ids = models.JSONField(default=list, blank=True)
    pin_hash = models.CharField(max_length=128, blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("tenant", "email")]

    @property
    def is_valid(self) -> bool:
        return self.accepted_at is None and self.expires_at > timezone.now()


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

class AuditLog(TenantModel):
    """
    Append-only record of anything that matters.

    ``authorised_by`` is what makes manager override trustworthy: the row names
    the cashier who acted and the manager who approved.
    """

    branch = models.ForeignKey(
        "org.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    authorised_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=40, blank=True)

    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "action", "-created_at"]),
            models.Index(fields=["tenant", "object_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.action} by {self.user}"


class PlatformRole(TimeStampedModel):
    """
    A job on your own team -- support, billing, read only.

    Shops build their own roles out of the tenant permission catalogue. These
    are the platform's equivalent, built from `apps.core.platform_perms`.
    """

    name = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=200, blank=True)
    permissions = models.JSONField(default=list, blank=True)
    # Holds every permission, including ones added later. Locked.
    is_super = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_super", "name"]

    def __str__(self):
        return self.name

    @property
    def permission_set(self) -> frozenset:
        from apps.core import platform_perms

        if self.is_super:
            return platform_perms.ALL
        return frozenset(self.permissions) & platform_perms.ALL

    @classmethod
    def ensure_defaults(cls):
        """Create any default role that is missing. Never changes existing ones."""
        from apps.core import platform_perms

        for name, description, is_super, permissions in platform_perms.DEFAULT_ROLES:
            if is_super and cls.objects.filter(is_super=True).exists():
                continue
            cls.objects.get_or_create(
                name=name,
                defaults={"description": description, "is_super": is_super,
                          "permissions": list(permissions)},
            )

    @classmethod
    def super_role(cls):
        role = cls.objects.filter(is_super=True).first()
        if role is None:
            role = cls.objects.create(
                name="Super admin",
                description="Everything, including future permissions. Cannot be changed.",
                is_super=True,
            )
        return role


class PlatformEvent(models.Model):
    """
    Something your own team did that belongs to no shop.

    The shop audit log needs a shop. Creating a role, giving somebody admin
    access or changing a plan has none, so until this existed those actions
    left no trace at all -- the ones that most need one.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=60, db_index=True)
    # What it was done to, as it was called then: survives the thing's deletion.
    target = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.target}"

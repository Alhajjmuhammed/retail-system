"""
The permission registry and resolver.

The *vocabulary* is fixed in code -- modules declare their permissions here at
import time, and a migration syncs them into the database. *Who holds what* is
entirely the tenant's, defined through roles they build themselves.

Resolution order, and it never varies:

    plan -> user deny -> branch scope -> grant -> limit

Plan beats everything: if the subscription does not include a feature, no role
can grant it. An explicit user-level deny beats any role grant.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from django.core.cache import cache


class ValueType(StrEnum):
    BOOL = "bool"        # can / cannot
    AMOUNT = "amount"    # can, up to this money value
    PERCENT = "percent"  # can, up to this percentage
    SET = "set"          # can, but only these options


@dataclass(frozen=True)
class PermissionSpec:
    code: str
    module: str
    label: str
    value_type: ValueType = ValueType.BOOL
    # Dangerous permissions always write an audit row and always offer the
    # manager-override path instead of a dead end.
    is_dangerous: bool = False
    # The plan feature this permission depends on. None means every plan has
    # it. No role can grant a permission whose feature the plan lacks.
    requires_feature: str | None = None
    options: tuple[str, ...] = field(default_factory=tuple)


class PermissionRegistry:
    """Collects permission declarations from every app at import time."""

    def __init__(self):
        self._specs: dict[str, PermissionSpec] = {}

    def register(self, spec: PermissionSpec) -> PermissionSpec:
        if spec.code in self._specs:
            raise ValueError(f"Permission {spec.code!r} is declared twice.")
        self._specs[spec.code] = spec
        return spec

    def register_many(self, specs) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, code: str) -> PermissionSpec:
        try:
            return self._specs[code]
        except KeyError:
            raise LookupError(
                f"Unknown permission {code!r}. Declare it in the owning app's "
                "permissions.py before checking it."
            ) from None

    def all(self) -> list[PermissionSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.module, s.code))

    def by_module(self) -> dict[str, list[PermissionSpec]]:
        grouped: dict[str, list[PermissionSpec]] = {}
        for spec in self.all():
            grouped.setdefault(spec.module, []).append(spec)
        return grouped

    def codes(self) -> set[str]:
        return set(self._specs)


registry = PermissionRegistry()


# --------------------------------------------------------------------------
# The resolved answer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """
    The outcome of one check.

    ``can_override`` is the difference between "no" and "ask a manager". It is
    true when the permission is dangerous and the only thing standing in the
    way is the user's own grant or limit -- never when the plan excludes the
    feature, because no manager PIN can buy a subscription.
    """

    allowed: bool
    reason: str = ""
    limit: Decimal | None = None
    can_override: bool = False

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Decision(allowed=True)


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------

def resolve(membership, code: str, branch=None, value=None) -> Decision:
    """
    Answer one question: may this membership do this, here, at this size?

    ``value`` is the amount or percentage being attempted, checked against the
    ceiling on the grant. A permission with no ceiling ignores it.
    """
    spec = registry.get(code)

    # Owner short-circuit, after the plan check below -- an owner still cannot
    # use a feature their subscription does not include.
    tenant = membership.tenant

    # 1. Plan
    if spec.requires_feature and not tenant.has_feature(spec.requires_feature):
        return Decision(
            allowed=False,
            reason=f"Your plan does not include {spec.label.lower()}.",
            can_override=False,
        )

    # Owner holds everything the plan allows. Checked before the grant lookup:
    # it needs no rows, and a support session's stand-in membership has none.
    if membership.role.is_owner_role:
        return ALLOWED

    grants = effective_permissions(membership)

    entry = grants.get(code)

    # 2. Branch scope. First, and never overridable: a deny used to be
    # checked before it and offered a manager's approval, which then carried
    # the action into a branch this person does not work in at all.
    if branch is not None and not membership.covers_branch(branch):
        return Decision(
            allowed=False,
            reason=f"You do not work at {branch}.",
            can_override=False,
        )

    # 3. Explicit deny beats every grant
    if entry is not None and entry.get("effect") == "deny":
        return Decision(
            allowed=False,
            reason=f"{spec.label} has been withheld from this user.",
            can_override=spec.is_dangerous,
        )

    # 4. Grant
    if entry is None:
        return Decision(
            allowed=False,
            reason=f"You do not have permission to {spec.label.lower()}.",
            can_override=spec.is_dangerous,
        )

    # 5. Limit
    limit = entry.get("limit")
    if limit is not None and value is not None and Decimal(str(value)) > limit:
        return Decision(
            allowed=False,
            reason=f"{spec.label} is limited to {limit:,.0f} for this user.",
            limit=limit,
            can_override=spec.is_dangerous,
        )

    if spec.value_type is ValueType.SET and value is not None:
        allowed_options = entry.get("options") or ()
        if value not in allowed_options:
            return Decision(
                allowed=False,
                reason=f"{value} is not permitted for this user.",
                can_override=spec.is_dangerous,
            )

    return Decision(allowed=True, limit=limit)


def effective_permissions(membership) -> dict[str, dict]:
    """
    Flatten role grants and per-user exceptions into one map, cached per
    membership. Invalidated whenever a role, membership or subscription
    changes -- without the cache every page load runs four joins.
    """
    key = cache_key(membership)
    cached = cache.get(key)
    if cached is not None:
        return cached

    resolved: dict[str, dict] = {}

    for rp in membership.role.permissions.select_related("permission"):
        if not rp.granted:
            continue
        resolved[rp.permission.code] = {
            "effect": "grant",
            "limit": rp.limit_value,
            "options": rp.set_value or (),
        }

    # Per-user exceptions are applied last so they win. A deny is kept in the
    # map rather than removed, so the resolver can tell "withheld from this
    # person" apart from "never granted".
    for up in membership.overrides.select_related("permission"):
        resolved[up.permission.code] = {
            "effect": up.effect,
            "limit": up.limit_value,
            "options": up.set_value or (),
        }

    from django.conf import settings

    cache.set(key, resolved, settings.PERMISSION_CACHE_SECONDS)
    return resolved


def cache_key(membership) -> str:
    return f"perms:{membership.tenant_id}:{membership.pk}:{membership.permissions_version}"


def invalidate(membership) -> None:
    cache.delete(cache_key(membership))

"""
Who may change whose access inside a shop.

`user.manage` and `role.manage` let somebody run the team. They must not let
that somebody promote themselves -- directly, or by creating an account with
a stronger role and signing in as it, or by resetting the password or PIN of
a colleague who holds more than they do. Every staff and role screen asks
these questions, so the answers live in one place.

The rule underneath all of them: you may only hand out, or take charge of,
what you hold yourself. Owners hold everything.
"""

from apps.core.parsing import decimal_or_none


def is_owner(membership) -> bool:
    return bool(membership and membership.is_active and membership.role.is_owner_role)


def other_active_owner_exists(target) -> bool:
    from apps.accounts.models import Membership

    return Membership.objects.filter(
        role__is_owner_role=True, is_active=True, user__is_active=True
    ).exclude(pk=target.pk).exists()


def _held(actor, code):
    from apps.core.permissions import effective_permissions

    entry = effective_permissions(actor).get(code)
    if entry is None or entry.get("effect") == "deny":
        return None
    return entry


def holds(actor, code) -> bool:
    return _held(actor, code) is not None


def may_grant(actor, code, limit=None, options=None) -> bool:
    """
    Whether `actor` may hand out `code` -- to a role or as an exception.

    Never with a bigger limit than their own (no limit counts as the biggest),
    never with options they do not hold, and never with a limit that is not a
    number: "NaN" used to crash this.
    """
    if is_owner(actor):
        return True
    entry = _held(actor, code)
    if entry is None:
        return False
    wanted = None
    if limit not in (None, ""):
        wanted = decimal_or_none(limit)
        if wanted is None or wanted < 0:
            return False
    held_limit = entry.get("limit")
    if held_limit is not None and (wanted is None or wanted > held_limit):
        return False
    return not (options and not set(options) <= set(entry.get("options") or ()))


def role_fits(actor, role) -> bool:
    """Everything `role` grants, `actor` holds too."""
    if is_owner(actor):
        return True
    if role.is_owner_role:
        return False
    return all(
        may_grant(actor, rp.permission.code, rp.limit_value, rp.set_value)
        for rp in role.permissions.select_related("permission")
        if rp.granted
    )


def _covers_all(membership) -> bool:
    return membership.role.is_owner_role or membership.all_branches


def _branch_ids(membership) -> set:
    return set(membership.branch_links.values_list("branch_id", flat=True))


def branches_fit(actor, *, all_branches, branches) -> bool:
    """
    Whether `actor` may place somebody in these branches.

    A manager of one branch creating an account that works everywhere -- with
    a password they chose -- had walked out of their own branch.
    """
    if is_owner(actor) or _covers_all(actor):
        return True
    if all_branches:
        return False
    return {b.pk for b in branches} <= _branch_ids(actor)


def outranks(target, actor) -> bool:
    """
    Whether `target` holds anything `actor` does not.

    Taking charge of such a person -- their password, their PIN -- is a way
    to act as them, so it is refused.
    """
    if is_owner(actor):
        return False
    if target.role.is_owner_role:
        return True
    if not role_fits(actor, target.role):
        return True
    # Somebody working where you do not is not yours to take charge of.
    if not _covers_all(actor) and (
        target.all_branches or not _branch_ids(target) <= _branch_ids(actor)
    ):
        return True
    return any(
        not may_grant(actor, o.permission.code, o.limit_value, o.set_value)
        for o in target.overrides.select_related("permission").filter(effect="grant")
    )


def may_handle_invitation(actor, invitation) -> bool:
    """
    Whether `actor` may see, copy or cancel this invitation.

    The link *is* the account: whoever opens it chooses the password. A
    manager copying the link of an Owner invitation became that owner.
    """
    if is_owner(actor):
        return True
    if not role_fits(actor, invitation.role):
        return False
    from apps.org.models import Branch

    ids = invitation.branch_ids or []
    return branches_fit(actor, all_branches=not ids,
                        branches=list(Branch.objects.filter(pk__in=ids)))


def refuse_staff_change(actor, target, new_role=None):
    """
    Why `actor` may not change `target`, or None if they may.

    `new_role` is the role being given, or None when something else about the
    person is changing (exceptions, password, PIN, branches).
    """
    if target.pk == actor.pk:
        return "You cannot change your own role or permissions. Ask an owner."
    if target.role.is_owner_role and not is_owner(actor):
        return "Only an owner can change an owner."
    if outranks(target, actor):
        return (f"{target.user.name} can do things you cannot, so only an owner "
                "can change their account.")
    if new_role is not None and new_role.is_owner_role and not is_owner(actor):
        return "Only an owner can make somebody an owner."
    if new_role is not None and not role_fits(actor, new_role):
        return f"The {new_role.name} role can do things you cannot, so you cannot give it out."
    if (
        target.role.is_owner_role
        and new_role is not None
        and not new_role.is_owner_role
        and not other_active_owner_exists(target)
    ):
        return "This is the only owner. Make somebody else an owner first."
    return None

"""
Audit trail helper.

Every dangerous action writes one row: what happened, who did it, and -- when
a manager approved it on the spot -- who authorised it. This is the record an
owner reads when the till is short at the end of the day.
"""

from django.forms.models import model_to_dict

from apps.core.context import get_current_branch, get_current_tenant, get_current_user


def record(
    action: str,
    *,
    obj=None,
    before=None,
    after=None,
    user=None,
    authorised_by=None,
    branch=None,
    ip: str | None = None,
    **extra,
):
    from apps.accounts.models import AuditLog

    tenant = get_current_tenant()
    if tenant is None:
        return None

    from apps.core.context import get_current_approver

    return AuditLog.objects.create(
        tenant=tenant,
        branch=branch or get_current_branch(),
        user=user or get_current_user(),
        # Both names on the row whenever a manager's PIN let this through.
        authorised_by=authorised_by or get_current_approver(),
        action=action,
        object_type=obj.__class__.__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        before=before or {},
        after=after or {},
        ip=ip,
        extra=extra,
    )


def snapshot(obj, fields=None) -> dict:
    """A plain dict of an object's state, for the before/after columns."""
    if obj is None:
        return {}
    data = model_to_dict(obj, fields=fields)
    return {key: str(value) for key, value in data.items()}


def client_ip(request) -> str | None:
    """
    The address to record, and to count failed sign-ins against.

    Only as far back as our own proxies reach (TRUSTED_PROXY_HOPS), and
    never the part of X-Forwarded-For the client wrote itself. Taking the
    first entry let anyone rotate addresses at will to dodge the sign-in
    lockout, and put "not-an-ip" into an inet column, which was a 500.
    """
    import ipaddress

    from django.conf import settings

    hops = getattr(settings, "TRUSTED_PROXY_HOPS", 0)
    candidates = []
    if hops:
        forwarded = [part.strip() for part in
                     request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
        # Our own proxies appended the last `hops` entries; the one before
        # them is the furthest we can believe.
        if len(forwarded) >= hops:
            candidates.append(forwarded[-hops])
    candidates.append(request.META.get("REMOTE_ADDR") or "")
    for value in candidates:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return None


def record_platform(request, action: str, target: str = "", **detail):
    """
    Log a platform-team action that belongs to no shop.

    Roles, admin access, plans, accounts in no shop: `record` needs a tenant
    and silently does nothing without one, which is how these went unlogged.
    """
    from apps.accounts.models import PlatformEvent

    user = getattr(request, "user", None)
    return PlatformEvent.objects.create(
        user=user if user is not None and user.is_authenticated else None,
        action=action,
        target=str(target)[:200],
        detail={key: value for key, value in detail.items() if value not in (None, "")},
        ip=client_ip(request),
    )

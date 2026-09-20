"""
Per-request tenant context.

Every tenant-scoped query reads the tenant from here rather than taking it as
an argument, so no caller can forget to pass it. Context variables are used
instead of thread locals because they behave correctly under ASGI and inside
Celery's prefork pool.

Nothing outside this module touches the variables directly.
"""

from contextlib import contextmanager, suppress
from contextvars import ContextVar

_current_tenant: ContextVar = ContextVar("current_tenant", default=None)
_current_branch: ContextVar = ContextVar("current_branch", default=None)
_current_user: ContextVar = ContextVar("current_user", default=None)
# The manager who approved the action in progress, if one had to.
_current_approver: ContextVar = ContextVar("current_approver", default=None)
_unscoped: ContextVar = ContextVar("unscoped", default=False)


# --------------------------------------------------------------------------
# Tenant
# --------------------------------------------------------------------------

def set_current_tenant(tenant):
    return _current_tenant.set(tenant)


def get_current_tenant():
    return _current_tenant.get()


def get_current_tenant_id():
    tenant = _current_tenant.get()
    return tenant.pk if tenant is not None else None


# --------------------------------------------------------------------------
# Branch — the shop the user is currently working in
# --------------------------------------------------------------------------

def set_current_branch(branch):
    return _current_branch.set(branch)


def get_current_branch():
    return _current_branch.get()


# --------------------------------------------------------------------------
# User — so models can stamp created_by and audit rows without plumbing
# --------------------------------------------------------------------------

def set_current_user(user):
    return _current_user.set(user)


def set_current_approver(user):
    return _current_approver.set(user)


def get_current_approver():
    return _current_approver.get()


def reset_current_approver(token):
    _current_approver.reset(token)


def get_current_user():
    user = _current_user.get()
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


# --------------------------------------------------------------------------
# Escape hatches
# --------------------------------------------------------------------------

def is_unscoped() -> bool:
    return _unscoped.get()


@contextmanager
def tenant_context(tenant, branch=None, user=None):
    """
    Run a block as a given tenant, in that tenant's own time.

    Used by Celery tasks, management commands and the platform admin's
    impersonation, all of which start with no request to read a tenant from.
    The timezone matters here as much as in a request: a nightly job that
    records "today" has to mean the shop's today.
    """
    import zoneinfo

    from django.utils import timezone as django_timezone

    tokens = [
        _current_tenant.set(tenant),
        _current_branch.set(branch),
        _current_user.set(user),
        # Inside unscoped(), a tenant_context is still one tenant: the
        # manager filter comes back on, not only row-level security.
        _unscoped.set(False),
    ]

    # Bind the database to this tenant too. Switching only the application's
    # idea of the tenant left row-level security pinned to whichever shop the
    # request started in, so writing a row for another one was refused.
    previous_binding = None
    if tenant is not None:
        try:
            previous_binding = _bind_database_tenant(str(tenant.pk))
        except Exception:
            previous_binding = None

    name = getattr(tenant, "timezone", "") if tenant is not None else ""
    previous_zone = django_timezone.get_current_timezone()
    activated = False
    if name:
        try:
            django_timezone.activate(zoneinfo.ZoneInfo(name))
            activated = True
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass

    try:
        yield
    finally:
        if activated:
            # Back to the zone the caller was in, not the server default.
            django_timezone.activate(previous_zone)
        if previous_binding is not None:
            with suppress(Exception):
                _bind_database_tenant(previous_binding)
        _current_tenant.reset(tokens[0])
        _current_branch.reset(tokens[1])
        _current_user.reset(tokens[2])
        _unscoped.reset(tokens[3])


def _bind_database_tenant(value: str) -> str:
    """
    Set ``app.tenant_id`` on the connection and return what it was.

    Session-level, like TenantMiddleware's: a transaction-local setting made
    outside a transaction is gone before the next query runs. Every caller
    restores the previous value in a ``finally``, so nothing leaks.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT set_config('app.tenant_id', %s, false)", [value])
    return previous


@contextmanager
def unscoped():
    """
    Query across every tenant.

    Lifts both locks: the manager filter *and* the row-level security binding.
    Lifting only the first would silently return nothing inside a bound
    request, which is the confusing half-failure worth avoiding.

    The legitimate callers are the platform admin, billing jobs, migrations,
    and working out which businesses a person belongs to before any tenant is
    resolved. It is deliberately noisy to type so it is easy to grep for.
    """
    token = _unscoped.set(True)
    previous = _bind_database_tenant("")
    try:
        yield
    finally:
        # After a database error the transaction is broken and any query
        # raises -- which used to replace the real error with a confusing
        # one. The rollback restores the binding anyway.
        from django.db import connection

        if not connection.needs_rollback:
            with suppress(Exception):
                _bind_database_tenant(previous)
        _unscoped.reset(token)

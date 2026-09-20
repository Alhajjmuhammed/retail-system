"""
Request-level tenant resolution and subscription gating.

The whole platform runs on one domain, so the tenant is resolved from the
signed-in user's active membership -- never from a header, a query parameter
or a form field. That is the single rule that keeps one shop's data away from
another's.
"""

import zoneinfo
from contextlib import suppress

from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone as django_timezone

from apps.core.context import (
    set_current_branch,
    set_current_tenant,
    set_current_user,
)

# Routes reachable with no tenant resolved.
TENANT_EXEMPT_PREFIXES = (
    "/accounts/",
    "/join/",
    "/signup/",
    "/platform/",
    "/static/",
    "/media/",
    "/healthz",
    "/favicon.ico",
    # Fetched by the browser in the background; a redirect would break it.
    "/pos/sw.js",
)


def _set_database_tenant(value: str) -> None:
    """
    Bind the connection to a tenant for the rest of the request.

    Session-level, not transaction-local. The middleware runs in autocommit,
    before the view's transaction opens, so a transaction-local setting
    evaporated the moment it was made -- and with it the whole second lock:
    every request ran with row-level security wide open.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, false)", [value])


def _is_exempt(path: str) -> bool:
    return path.startswith(TENANT_EXEMPT_PREFIXES)


class TenantMiddleware:
    """
    Resolves tenant, branch and user into the request context.

    Runs after AuthenticationMiddleware because it reads ``request.user``, and
    before anything that queries tenant-scoped data.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A pooled connection may still carry the last request's binding, and
        # a reused thread the last shop's time zone. Start clean, and always
        # leave clean, whatever happens in between.
        django_timezone.deactivate()
        try:
            _set_database_tenant("")
        except Exception:
            # Database down: let the health check say so, rather than failing
            # here before any view runs.
            if request.path.startswith("/healthz"):
                return self.get_response(request)
            raise
        try:
            return self._handle(request)
        finally:
            django_timezone.deactivate()
            with suppress(Exception):
                _set_database_tenant("")

    def _handle(self, request):
        membership = None
        tenant = None
        branch = None

        user = getattr(request, "user", None)
        set_current_user(user)

        if user is not None and user.is_authenticated:
            membership = user.active_membership(
                tenant_id=request.session.get("tenant_id")
            )
            # A support session always wins. Without this, a platform admin who
            # is also a member of some shop would open a *different* shop for
            # support and quietly land back in their own.
            if request.session.get("impersonating") and not user.has_platform_perm(
                "shops.support"
            ):
                # Their role lost support access mid-session: end it now,
                # not whenever they happen to click "End support session".
                request.session.pop("impersonating", None)
                request.session.pop("tenant_id", None)
                request.session.pop("branch_id", None)
            if user.is_platform_staff and request.session.get("impersonating"):
                wanted = request.session.get("tenant_id")
                if membership is None or membership.tenant_id != wanted:
                    membership = self._support_membership(request, user)
            if membership is not None:
                tenant = membership.tenant

                # The tenant has to be in context and bound to the connection
                # before anything else is looked up, because every other query
                # -- including the one that finds the branch -- is scoped by
                # it and would otherwise quietly return nothing.
                set_current_tenant(tenant)
                self._bind_database_session(tenant)

                branch = membership.active_branch(
                    branch_id=request.session.get("branch_id")
                )
                # Keep the session honest if the membership moved on.
                request.session["tenant_id"] = tenant.pk

                self._activate_timezone(tenant, branch)

        set_current_tenant(tenant)
        set_current_branch(branch)

        request.tenant = tenant
        request.branch = branch
        request.membership = membership

        if tenant is None and not _is_exempt(request.path):
            if user is not None and user.is_authenticated:
                # Platform staff belong to no shop by design. Sending them to
                # "you are not part of a shop yet" is technically true and
                # completely useless -- their home is the platform admin.
                if user.is_platform_staff:
                    return redirect(reverse("platform:dashboard"))
                # Signed in but belonging to no tenant: send them to create or
                # join one rather than to a dead page.
                return redirect(reverse("accounts:no_tenant"))
            return redirect(f"{reverse('accounts:login')}?next={request.path}")

        return self.get_response(request)

    @staticmethod
    def _support_membership(request, user):
        """
        A stand-in membership for a support session.

        Platform staff belong to no shop, so without this the session could
        never resolve a tenant and "open for support" silently bounced back to
        the platform. The stand-in holds the shop's Owner role -- support needs
        to see and fix what the owner can -- and is never saved: nothing about
        the shop's own staff list changes, and every audit row still names the
        staff member, not the owner.
        """
        if not request.session.get("impersonating"):
            return None
        tenant_id = request.session.get("tenant_id")
        if not tenant_id:
            return None

        from apps.accounts.models import Membership, Role
        from apps.core.context import unscoped
        from apps.tenancy.models import Tenant

        with unscoped():
            tenant = (
                Tenant.objects.select_related("subscription__plan")
                .filter(pk=tenant_id)
                .first()
            )
            role = (
                Role.objects_all.filter(tenant=tenant, is_owner_role=True).first()
                if tenant
                else None
            )
        if tenant is None or role is None:
            request.session.pop("impersonating", None)
            request.session.pop("tenant_id", None)
            return None

        membership = Membership(tenant=tenant, user=user, role=role)
        membership.is_support = True
        return membership

    @staticmethod
    def _activate_timezone(tenant, branch):
        """
        Work in the shop's own time.

        Every report asks "what did we sell today", and today ends at
        midnight where the shop is -- not where the server is. The tenant
        carried a timezone field that nothing ever read, so a shop outside
        the server's zone had its days cut in the wrong place.

        A branch may override it: a chain can cross a border.
        """
        name = (branch.timezone if branch and branch.timezone else tenant.timezone)
        if not name:
            return
        try:
            django_timezone.activate(zoneinfo.ZoneInfo(name))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            # A bad value must not take the shop down; fall back to the
            # server default rather than raising on every request.
            django_timezone.deactivate()

    @staticmethod
    def _bind_database_session(tenant):
        """
        Hand the tenant to Postgres so row-level security can enforce it.

        This is the second lock. If application code ever bypasses the manager
        with raw SQL, the database still refuses to return another tenant's
        rows.
        """
        _set_database_tenant(str(tenant.pk))


class SubscriptionMiddleware:
    """
    Enforces subscription state.

    A lapsed shop is never locked out of its own data and nothing is ever
    deleted. It goes read-only: they can look, they cannot transact.
    """

    WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    ALWAYS_ALLOWED_PREFIXES = (
        "/settings/billing/",
        "/accounts/",
        "/platform/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None or _is_exempt(request.path):
            return self.get_response(request)

        subscription = tenant.active_subscription
        request.subscription = subscription
        if subscription is None:
            return self.get_response(request)

        if (
            subscription.is_read_only
            and request.method in self.WRITE_METHODS
            and not request.path.startswith(self.ALWAYS_ALLOWED_PREFIXES)
        ):
            return render(
                request,
                "core/read_only.html",
                {"subscription": subscription, "tenant": tenant},
                status=402,
            )

        return self.get_response(request)


class PlanLimitMiddleware:
    """
    Turn "your plan allows N" into a message instead of a server error.

    Limits are checked at write time, deep inside services, and several views
    let the exception escape -- a shop at its staff limit pressing "Add" got a
    500 page. Wherever it comes from, send them back with the reason.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.contrib import messages
        from django.utils.http import url_has_allowed_host_and_scheme

        from apps.core.features import LimitExceeded

        if not isinstance(exception, LimitExceeded):
            return None
        messages.error(request, str(exception))
        back = request.META.get("HTTP_REFERER", "")
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            back = reverse("core:dashboard")
        return redirect(back)

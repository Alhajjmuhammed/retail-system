"""
The platform admin. Yours, not a shop's.

Sits outside tenant scoping entirely, which is why every query here goes
through ``unscoped()`` and why the whole module is gated on
``is_platform_staff`` rather than on any tenant permission.
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.core import audit, platform_perms
from apps.core.context import unscoped
from apps.core.decorators import platform_perm, platform_staff_only
from apps.core.features import ALL_LIMITS
from apps.tenancy.models import (
    Invoice,
    InvoiceStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    Tenant,
    TenantStatus,
)


@login_required
@platform_staff_only
def dashboard(request):
    """
    What an operator needs before their first coffee.

    Money first, then who is about to leave, then what is broken. The counts
    that were here before said how many shops existed but nothing about
    whether the business was working.
    """
    from django.db.models.functions import TruncMonth

    from apps.accounts.models import PlatformEvent
    from apps.core.context_processors import PLATFORM_NAV

    # Everyone on the team lands here after signing in. A role without the
    # overview goes to the first page it can see instead of a refusal.
    if not request.user.has_platform_perm("dashboard.view"):
        granted = request.user.platform_permissions
        for _, name, _, code in PLATFORM_NAV:
            if code in granted:
                return redirect(name)
        return render(request, "platform/no_access.html", status=403)

    from apps.core.features import LIMIT_BRANCHES
    from apps.org.models import Device
    from apps.pos.models import FiscalReceipt, FiscalStatus

    now = timezone.now()
    today = timezone.localdate()

    with unscoped():
        subs = Subscription.objects.select_related("plan", "tenant")
        tenants = Tenant.objects.select_related("subscription__plan")

        # Recurring revenue, priced the way the plans are: per branch.
        billing = subs.filter(
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
        )
        mrr = sum(
            (s.plan.price_monthly * max(s.tenant.usage_of(LIMIT_BRANCHES), 1)
             for s in billing),
            Decimal("0"),
        )
        # What the trials are worth if they convert -- the number that says
        # whether this month's calls are worth making.
        trial_value = sum(
            (s.plan.price_monthly * max(s.tenant.usage_of(LIMIT_BRANCHES), 1)
             for s in subs.filter(status=SubscriptionStatus.TRIALING)),
            Decimal("0"),
        )

        counts = {
            "total": tenants.count(),
            "paying": subs.filter(status=SubscriptionStatus.ACTIVE).count(),
            "trialing": subs.filter(status=SubscriptionStatus.TRIALING).count(),
            "at_risk": subs.filter(status__in=[
                SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE,
            ]).count(),
            "suspended": subs.filter(status=SubscriptionStatus.SUSPENDED).count(),
        }

        outstanding = Invoice.objects.exclude(
            status__in=[InvoiceStatus.PAID, InvoiceStatus.VOID]
        ).select_related("tenant").order_by("due_date")
        outstanding_total = outstanding.aggregate(t=Sum("total"))["t"] or Decimal("0")

        # Shops joined, by month. One series, so the chart needs no legend.
        since = today - timedelta(days=182)
        by_month = dict(
            tenants.filter(created_at__date__gte=since)
            .annotate(month=TruncMonth("created_at"))
            .values_list("month")
            .annotate(n=Count("id"))
        )
        signups = []
        cursor = today.replace(day=1) - timedelta(days=150)
        cursor = cursor.replace(day=1)
        for _ in range(6):
            found = next(
                (n for m, n in by_month.items()
                 if m.year == cursor.year and m.month == cursor.month),
                0,
            )
            signups.append({"label": cursor.strftime("%b"), "value": found})
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        peak = max((row["value"] for row in signups), default=0)

        by_plan = list(
            subs.values("plan__name")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
        plan_peak = max((row["n"] for row in by_plan), default=0)

        stale = now - timedelta(hours=24)
        health = {
            "devices_stale": Device.objects.filter(is_active=True).filter(
                Q(last_sync_at__lt=stale) | Q(last_sync_at__isnull=True)
            ).count(),
            "fiscal_pending": FiscalReceipt.objects.filter(
                status=FiscalStatus.PENDING
            ).count(),
            "fiscal_failed": FiscalReceipt.objects.filter(
                status=FiscalStatus.FAILED
            ).count(),
        }

        context = {
            "mrr": mrr,
            "trial_value": trial_value,
            "counts": counts,
            "new_this_week": tenants.filter(
                created_at__gte=now - timedelta(days=7)
            ).count(),
            "outstanding_total": outstanding_total,
            "outstanding": list(outstanding[:6]),
            "signups": signups,
            "signup_peak": peak,
            "by_plan": by_plan,
            "plan_peak": plan_peak,
            "trials_ending": list(
                subs.filter(
                    status=SubscriptionStatus.TRIALING,
                    trial_ends_at__lte=now + timedelta(days=7),
                ).order_by("trial_ends_at")[:6]
            ),
            "newest": list(tenants.order_by("-created_at")[:6]),
            "health": health,
            "activity": [
                {"label": ACTION_LABELS.get(e.action, e.action), "target": e.target,
                 "user": e.user, "when": e.created_at}
                for e in PlatformEvent.objects.select_related("user").order_by("-created_at")[:6]
            ] if request.user.has_platform_perm("audit.view") else [],
        }

    return render(request, "platform/dashboard.html", context)


@login_required
@platform_perm("shops.view")
def tenant_list(request):
    """
    Every shop, with enough on each row to decide whether to open it.

    A name and a status is not enough: the questions are always how big are
    they, are they paying, and when did anyone last hear from them.
    """
    from django.db.models import Max

    from apps.accounts.models import Membership
    from apps.pos.models import Sale

    term = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    order = request.GET.get("order", "-created_at")
    plan_code = request.GET.get("plan", "")

    with unscoped():
        tenants = Tenant.objects.select_related("subscription__plan").annotate(
            branch_count=Count("org_branch_set", distinct=True),
            user_count=Count("accounts_membership_set", distinct=True),
        )
        if term:
            tenants = tenants.filter(
                Q(name__icontains=term)
                | Q(slug__icontains=term)
                | Q(tin__icontains=term)
                | Q(accounts_membership_set__user__email__icontains=term)
            ).distinct()
        if plan_code:
            tenants = tenants.filter(subscription__plan__code=plan_code)
        if status == "at_risk":
            # Matches what the At risk card counts, not just one of its parts.
            tenants = tenants.filter(subscription__status__in=[
                SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE,
                SubscriptionStatus.SUSPENDED,
            ])
        elif status:
            tenants = tenants.filter(subscription__status=status)

        allowed_order = {
            "-created_at": "-created_at", "created_at": "created_at",
            "name": "name", "-name": "-name",
        }
        rows = list(tenants.order_by(allowed_order.get(order, "-created_at"))[:200])

        # Last sign-in and last sale, in two queries rather than two per row.
        ids = [t.pk for t in rows]
        last_seen = dict(
            Membership.objects_all.filter(tenant_id__in=ids)
            .values_list("tenant_id")
            .annotate(seen=Max("user__last_login"))
        )
        last_sale = dict(
            Sale.objects_all.filter(tenant_id__in=ids)
            .values_list("tenant_id")
            .annotate(sold=Max("sold_at"))
        )
        for tenant in rows:
            tenant.last_seen = last_seen.get(tenant.pk)
            tenant.last_sale = last_sale.get(tenant.pk)

        summary = {
            "total": Tenant.objects.count(),
            "active": Subscription.objects.filter(
                status=SubscriptionStatus.ACTIVE
            ).count(),
            "trialing": Subscription.objects.filter(
                status=SubscriptionStatus.TRIALING
            ).count(),
            "at_risk": Subscription.objects.filter(status__in=[
                SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE,
                SubscriptionStatus.SUSPENDED,
            ]).count(),
        }

    return render(
        request,
        "platform/tenants.html",
        {
            "tenants": rows,
            "q": term,
            "status": status,
            "plan_code": plan_code,
            "order": order,
            "statuses": SubscriptionStatus.choices,
            "summary": summary,
        },
    )


@login_required
@platform_perm("shops.view")
def tenant_detail(request, pk):
    from apps.accounts.models import Membership
    from apps.core.context import tenant_context
    from apps.pos.models import Sale, SaleStatus

    with unscoped():
        tenant = get_object_or_404(
            Tenant.objects.select_related("subscription__plan"), pk=pk
        )
        sales = Sale.objects_all.filter(
            tenant=tenant,
            status__in=[SaleStatus.COMPLETED, SaleStatus.PART_REFUNDED],
        )
        members = list(
            Membership.objects_all.select_related("user", "role")
            .prefetch_related("branch_links__branch")
            .filter(tenant=tenant)
            .order_by("-role__is_owner_role", "user__name")
        )
        context = {
            "tenant": tenant,
            "usage": [
                {"label": label, "used": tenant.usage_of(key),
                 "allowed": tenant.limit_for(key)}
                for key, label in ALL_LIMITS
                if key != "history_days"
            ],
            "invoices": list(tenant.invoices.all()[:12]),
            "outstanding": tenant.invoices.exclude(
                status__in=[InvoiceStatus.PAID, InvoiceStatus.VOID]
            ).aggregate(t=Sum("total"))["t"] or Decimal("0"),
            "plans": list(Plan.objects.order_by("sort_order")),
            "members": members,
            "sales_count": sales.count(),
            "sales_value": sales.aggregate(t=Sum("total"))["t"] or Decimal("0"),
            "last_sale": sales.order_by("-sold_at").values_list(
                "sold_at", flat=True
            ).first(),
            "can_delete": sales.count() == 0,
        }

    # Roles belong to the tenant, so they are read in its own context.
    with tenant_context(tenant):
        from apps.accounts.models import Role
        from apps.org.models import Branch

        context["roles"] = list(Role.objects.order_by("-is_owner_role", "name"))
        context["branches"] = list(Branch.objects.filter(is_active=True))

    return render(request, "platform/tenant_detail.html", context)


@login_required
@platform_perm("shops.staff")
def tenant_member_form(request, pk, member_pk=None):
    """
    Add or change somebody in a shop, from your side.

    Support work: an owner rings because their manager left and nobody else
    can approve a void. Doing it here beats talking them through four screens
    over a bad line.
    """
    from apps.accounts.models import Membership, Role, User
    from apps.core.context import tenant_context
    from apps.org.models import Branch

    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)
        membership = (
            get_object_or_404(
                Membership.objects_all.select_related("user", "role"),
                pk=member_pk, tenant=tenant,
            )
            if member_pk
            else None
        )

    with tenant_context(tenant):
        roles = list(Role.objects.order_by("-is_owner_role", "name"))
        branches = list(Branch.objects.filter(is_active=True))

        if request.method == "POST":
            errors = []
            name = request.POST.get("name", "").strip()
            email = request.POST.get("email", "").strip().lower()
            password = request.POST.get("password", "")
            role = next(
                (r for r in roles if str(r.pk) == request.POST.get("role")), None
            )

            if not name or not email:
                errors.append("Name and email are required.")
            if role is None:
                errors.append("Pick a role.")
            if membership is None and len(password) < 8:
                with unscoped():
                    is_new = not User.objects.filter(email=email).exists()
                if is_new:
                    errors.append("Give them a password of at least 8 characters.")
            if password and len(password) < 8:
                errors.append("A password needs at least 8 characters.")

            with unscoped():
                target = membership.user if membership else User.objects.filter(
                    email__iexact=email).first()
            pin = request.POST.get("pin", "").strip()
            if target is not None and (target.is_platform_staff or target.pk == request.user.pk) \
                    and not request.user.is_super_admin:
                # A seat in a shop is permanent access with no support-session
                # trail. For the platform team that is a Super admin's call.
                errors.append("Only a Super admin can put a platform admin into a shop.")
            if membership is not None and (password or pin or name != target.name):
                refusal = platform_perms.refusal_to_manage(request.user, target)
                if refusal:
                    errors.append(refusal)
            if (membership is not None and role is not None
                    and membership.role.is_owner_role and not role.is_owner_role
                    and _is_last_owner(membership)):
                errors.append(f"{target.name} is the only owner of {tenant.name}. "
                              "Make somebody else an owner first.")
            if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
                errors.append("A PIN is 4 to 8 digits.")
            if (password or pin) and "people.manage" not in request.user.platform_permissions:
                # Setting somebody's password is taking their account: the
                # shop-staff permission alone let an admin become the owner.
                errors.append("Setting a password or PIN needs the People permission.")
            if (password or pin) and membership is not None \
                    and membership.role.is_owner_role and not request.user.is_super_admin:
                errors.append("Only a Super admin may set an owner's password or PIN.")
            if role is not None and role.is_owner_role and not request.user.is_super_admin \
                    and not (membership is not None and membership.role.is_owner_role):
                # An owner seat, with a password the admin chose, is lasting
                # control of a shop. A shop's owner comes with the shop.
                errors.append("Only a Super admin can make somebody an owner from here.")

            with unscoped():
                clash = Membership.objects_all.filter(
                    tenant=tenant, user__email=email
                )
                if membership is not None:
                    clash = clash.exclude(pk=membership.pk)
                if email and clash.exists():
                    errors.append(f"{email} is already in this shop.")

            if errors:
                return _modal_or_redirect(
                    request, "platform/_member_form.html",
                    {"tenant": tenant, "membership": membership, "roles": roles,
                     "branches": branches, "errors": errors,
                     "values": request.POST},
                    "platform:tenant_list",
                )

            with unscoped():
                if membership is None:
                    user = User.objects.filter(email=email).first()
                    if user is None:
                        user = User.objects.create_user(
                            email=email, password=password, name=name
                        )
                    elif password:
                        # One identity across every shop: adding somebody to a
                        # shop never rewrites the password they already have.
                        messages.info(
                            request,
                            f"{email} already has an account and keeps their own password.",
                        )
                        password = ""  # so the activity log does not claim it was set
                else:
                    user = membership.user
                    user.name = name
                    if password and user.shop_may_set_password(tenant):
                        user.set_password(password)
                    elif password:
                        messages.info(request, f"{user.email} works elsewhere too, so their "
                                               "password was left alone. Use People to reset it.")
                        password = ""
                    user.save()

                if membership is None:
                    membership = Membership.objects.create(
                        tenant=tenant, user=user, role=role
                    )
                else:
                    membership.role = role
                    membership.save(update_fields=["role", "updated_at"])

            chosen = request.POST.getlist("branches")
            picked = [b for b in branches if str(b.pk) in chosen]
            membership.set_branches(picked, all_branches=not picked)
            membership.invalidate_permissions()

            if pin:
                membership.set_pin(pin)

            audit.record_platform(request, "platform.shop_staff_saved", user.email, shop=tenant.name,
                                  role=role.name, password_set=bool(password) or None)
            messages.success(request, f"{user.name} saved in {tenant.name}.")
            return _close_modal_to(request, "platform:tenant_detail", pk=tenant.pk)

    return _modal_or_redirect(
        request, "platform/_member_form.html",
        {"tenant": tenant, "membership": membership, "roles": roles,
         "branches": branches},
        "platform:tenant_list",
    )


@login_required
@platform_perm("shops.staff")
@require_POST
def tenant_member_remove(request, pk, member_pk):
    """
    Take somebody out of a shop.

    Never the last owner: the shop would have nobody who could administer it,
    and only you could put that right.
    """
    from apps.accounts.models import Membership

    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)
        membership = get_object_or_404(
            Membership.objects_all.select_related("user", "role"),
            pk=member_pk, tenant=tenant,
        )

        if membership.role.is_owner_role and not Membership.objects_all.filter(
            tenant=tenant, role__is_owner_role=True, is_active=True
        ).exclude(pk=membership.pk).exists():
            messages.error(
                request,
                f"{membership.user.name} is the only owner of {tenant.name}. "
                "Make somebody else an owner first.",
            )
        else:
            name = membership.user.name
            audit.record_platform(request, "platform.shop_staff_removed", membership.user.email,
                                  shop=tenant.name)
            membership.delete()
            messages.success(
                request, f"{name} removed from {tenant.name}. Their records stay."
            )

    return redirect("platform:tenant_detail", pk=pk)


@login_required
@platform_perm("shops.edit")
def tenant_change_plan(request, pk):
    """
    Move a shop between plans.

    Never removes anything they already have: a downgrade below their current
    branch count leaves the branches alone and simply stops them adding more.
    """
    if request.method != "POST":
        return redirect("platform:tenant_detail", pk=pk)

    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)
        plan = get_object_or_404(Plan, pk=request.POST.get("plan"))
        subscription = tenant.subscription

        subscription.plan = plan
        # Only the plan. The status used to be taken from the request as sent,
        # so "edit a shop" could suspend or un-suspend it, or store nonsense.
        subscription.save(update_fields=["plan", "updated_at"])

        # Everyone's cached permissions depend on plan features, so they all
        # have to be recomputed on the next request.
        tenant.accounts_membership_set.update(
            permissions_version=F("permissions_version") + 1
        )

    audit.record_platform(request, "platform.shop_plan_changed", tenant.name, plan=plan.name,
                          status=subscription.status)
    messages.success(request, f"{tenant.name} moved to {plan.name}.")
    return redirect("platform:tenant_detail", pk=pk)


@login_required
@platform_perm("shops.suspend")
def tenant_suspend(request, pk):
    if request.method != "POST":
        return redirect("platform:tenant_detail", pk=pk)

    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)
        suspending = tenant.status == TenantStatus.ACTIVE
        tenant.status = TenantStatus.SUSPENDED if suspending else TenantStatus.ACTIVE
        tenant.save(update_fields=["status", "updated_at"])

        tenant.subscription.status = (
            SubscriptionStatus.SUSPENDED if suspending
            else _status_on_reactivation(tenant)
        )
        tenant.subscription.save(update_fields=["status", "updated_at"])

    audit.record_platform(request, "platform.shop_suspended" if suspending else "platform.shop_reactivated",
                          tenant.name, status=tenant.subscription.status)
    messages.success(
        request,
        f"{tenant.name} {'suspended' if suspending else 'reactivated'}. "
        "Their data is untouched.",
    )
    return redirect(
        _next_url(request, reverse("platform:tenant_detail", args=[pk]))
    )


def _status_on_reactivation(tenant):
    """What a shop really is once it can trade again: the billing rule."""
    from apps.tenancy.billing import status_now

    return status_now(tenant)


@login_required
@platform_perm("shops.support")
@require_POST
def impersonate(request, pk):
    """
    Support access.

    Switches the session's tenant without changing who you are, so every audit
    row still carries your own name. Logged on the way in.
    """
    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)

    request.session["tenant_id"] = tenant.pk
    request.session["impersonating"] = True

    from apps.core import audit
    from apps.core.context import tenant_context

    audit.record_platform(request, "platform.impersonated", tenant.name)
    with tenant_context(tenant, user=request.user):
        audit.record(
            "platform.impersonated",
            after={"tenant": tenant.name, "staff": request.user.email},
            ip=audit.client_ip(request),
        )

    messages.warning(request, f"You are now viewing {tenant.name} as support.")
    return redirect("core:dashboard")


@login_required
@platform_staff_only
@require_POST
def end_support(request):
    """
    Leave a support session and go back to the shop you were helping.

    Without this the session tenant stayed set, and the only way out was to
    sign out altogether.
    """
    tenant_id = request.session.pop("tenant_id", None)
    if request.session.pop("impersonating", None) and tenant_id:
        with unscoped():
            shop = Tenant.objects.filter(pk=tenant_id).first()
        audit.record_platform(request, "platform.support_ended", getattr(shop, "name", ""))
    request.session.pop("branch_id", None)
    if tenant_id:
        return redirect("platform:tenant_detail", pk=tenant_id)
    return redirect("platform:dashboard")


@login_required
@platform_perm("health.view")
def health(request):
    """
    Is everything running, and is anything piling up?

    The background jobs come first: renewals, fiscal receipts and SMS all
    stop silently if the scheduler stops, and nothing else would tell you.
    """
    from django.core.cache import cache
    from django.db import connection

    from apps.accounts.models import AuditLog
    from apps.core import jobs
    from apps.inventory.models import StockItem
    from apps.notifications.models import Message, MessageStatus
    from apps.org.models import Device
    from apps.pos.models import FiscalReceipt, FiscalStatus, Sale

    now = timezone.now()
    stale = now - timedelta(hours=24)

    services = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        services.append(("Database", True, ""))
    except Exception as exc:  # pragma: no cover - the page itself needs the DB
        services.append(("Database", False, str(exc)[:200]))
    try:
        cache.set("health-probe", "1", 5)
        ok = cache.get("health-probe") == "1"
        services.append(("Cache (Redis)", ok, "" if ok else "A value did not come back."))
    except Exception as exc:
        services.append(("Cache (Redis)", False, str(exc)[:200]))

    with unscoped():
        job_rows = jobs.status()
        failed_fiscal = FiscalReceipt.objects.filter(status=FiscalStatus.FAILED)
        context = {
            "services": services,
            "jobs": job_rows,
            "jobs_bad": sum(1 for j in job_rows if j["verdict"] != "fine"),
            "fiscal_pending": FiscalReceipt.objects.filter(status=FiscalStatus.PENDING).count(),
            "fiscal_oldest": FiscalReceipt.objects.filter(status=FiscalStatus.PENDING)
            .order_by("created_at").values_list("created_at", flat=True).first(),
            # Counted in full; the list below shows the latest.
            "fiscal_failed_count": failed_fiscal.count(),
            "fiscal_failed": list(failed_fiscal.select_related("sale", "tenant").order_by("-updated_at")[:20]),
            "sms_queued": Message.objects.filter(status=MessageStatus.QUEUED).count(),
            "sms_failed": Message.objects.filter(status=MessageStatus.FAILED,
                                                 created_at__gte=now - timedelta(days=7)).count(),
            "devices_silent": Device.objects.filter(is_active=True).filter(
                Q(last_sync_at__lt=stale) | Q(last_sync_at__isnull=True)).count(),
            "devices_holding": Device.objects.filter(is_active=True, queued__gt=0).count(),
            "sales_review": Sale.objects.filter(needs_review=True).count(),
            "sync_refused": AuditLog.objects.filter(action="sale.sync_rejected",
                                                    created_at__gte=now - timedelta(days=7)).count(),
            "negative_stock": list(
                StockItem.objects.filter(qty_on_hand__lt=0)
                .values("tenant__name").annotate(n=Count("id")).order_by("-n")[:10]
            ),
        }
        context["negative_total"] = sum(row["n"] for row in context["negative_stock"])
    return render(request, "platform/health.html", context)


@login_required
@platform_perm("plans.view")
def plans(request):
    from apps.core.features import ALL_LIMITS

    with unscoped():
        rows = list(
            Plan.objects.prefetch_related("features", "limits")
            .annotate(subscribers=Count("subscriptions"))
            .order_by("sort_order", "price_monthly")
        )
    return render(request, "platform/plans.html", {"plans": rows, "all_limits": ALL_LIMITS})


# --------------------------------------------------------------------------
# People, across every shop
# --------------------------------------------------------------------------

@login_required
@platform_perm("people.view")
def user_list(request):
    """
    Everyone with an account, whichever shop they belong to.

    Support work starts here: somebody rings saying they cannot sign in, and
    this is the only place that can find them without knowing their shop.
    """
    from django.core.paginator import Paginator
    from django.db.models import Prefetch

    from apps.accounts.models import Membership, User

    term = request.GET.get("q", "").strip()
    view = request.GET.get("view", "")
    order = request.GET.get("order", "name")

    orders = {
        "name": ("name",),
        "recent": ("-last_login",),
        "newest": ("-created_at",),
    }

    with unscoped():
        base = User.objects.select_related("platform_role").annotate(
            shop_count=Count("memberships", distinct=True)
        )

        users = base.prefetch_related(
            Prefetch(
                "memberships",
                queryset=Membership.objects_all.select_related("tenant", "role"),
            )
        )
        if term:
            users = users.filter(
                Q(email__icontains=term)
                | Q(name__icontains=term)
                | Q(phone__icontains=term)
                | Q(memberships__tenant__name__icontains=term)
            ).distinct()

        filters = {
            "platform": Q(is_platform_staff=True),
            "inactive": Q(is_active=False),
            # Signed up, never joined a shop: they reach a dead end on login.
            "orphaned": Q(shop_count=0, is_platform_staff=False),
            # Given an account and never used it -- the ones worth a call.
            "never": Q(last_login__isnull=True),
        }
        if view in filters:
            users = users.filter(filters[view])

        users = users.order_by(*orders.get(order, orders["name"]), "email")

        # Paged rather than capped. A hard limit silently hid everybody past
        # the three-hundredth account, with nothing on the page to say so.
        page = Paginator(users, 50).get_page(request.GET.get("page"))

        counts = {
            "total": base.count(),
            "platform": base.filter(filters["platform"]).count(),
            "inactive": base.filter(filters["inactive"]).count(),
            "orphaned": base.filter(filters["orphaned"]).count(),
            "never": base.filter(filters["never"]).count(),
        }

    keep = request.GET.copy()
    keep.pop("page", None)

    return render(
        request,
        "platform/users.html",
        {"page": page, "users": page.object_list, "q": term, "view": view,
         "order": order, "counts": counts, "keep": keep.urlencode()},
    )


@login_required
@platform_perm("people.view")
def user_quick_view(request, pk):
    """A person at a glance: who they are, where they work, and what they can do."""
    from apps.accounts.models import Membership, User

    with unscoped():
        person = get_object_or_404(User, pk=pk)
        memberships = list(
            Membership.objects_all.select_related("tenant", "role")
            .prefetch_related("branch_links__branch")
            .filter(user=person)
        )
    return _modal_or_redirect(
        request, "platform/_user_quick.html",
        {"person": person, "memberships": memberships},
        "platform:user_list",
    )


@login_required
@platform_perm("people.manage")
def user_edit(request, pk):
    """
    Correct somebody's name, email or phone.

    There was no way to do this anywhere: a mistyped email on signup meant the
    person could never sign in, and nothing on the platform could fix it.
    """
    from apps.accounts.models import User

    with unscoped():
        person = get_object_or_404(User, pk=pk)
        refusal = platform_perms.refusal_to_manage(request.user, person)
        if refusal:
            messages.error(request, refusal)
            return _close_modal_url(request, _next_url(request, reverse("platform:user_list")))

        if request.method == "POST":
            name = request.POST.get("name", "").strip()
            email = request.POST.get("email", "").strip().lower()
            phone = request.POST.get("phone", "").strip()

            errors = []
            if not name or not email:
                errors.append("Name and email are required.")
            elif User.objects.filter(email=email).exclude(pk=person.pk).exists():
                errors.append(f"{email} already belongs to another account.")

            if errors:
                return _modal_or_redirect(
                    request, "platform/_user_edit_form.html",
                    {"person": person, "errors": errors, "values": request.POST,
                     "next": _next_url(request, reverse("platform:user_list"))},
                    "platform:user_list",
                )

            before = {"name": person.name, "email": person.email, "phone": person.phone}
            person.name, person.email, person.phone = name, email, phone
            person.save(update_fields=["name", "email", "phone", "updated_at"])
            after = {"name": name, "email": email, "phone": phone}
            audit.record_platform(
                request, "platform.user_edited", person.email,
                changed={k: [before[k], after[k]] for k in after if before[k] != after[k]},
            )
            messages.success(request, f"{person.name} saved.")
            return _close_modal_url(
                request, _next_url(request, reverse("platform:user_list"))
            )

    return _modal_or_redirect(
        request, "platform/_user_edit_form.html",
        {"person": person,
         "next": _next_url(request, reverse("platform:user_list"))},
        "platform:user_list",
    )


@login_required
@platform_perm("people.manage")
def user_password(request, pk):
    """
    Set a new password for somebody who is locked out.

    Changing your own used to sign you straight out: Django ends every session
    when a password changes, and this one was not carried across.
    """
    from django.contrib.auth import update_session_auth_hash

    from apps.accounts.models import User

    with unscoped():
        person = get_object_or_404(User, pk=pk)
        refusal = platform_perms.refusal_to_manage(request.user, person)
        if refusal:
            messages.error(request, refusal)
            return _close_modal_url(request, _next_url(request, reverse("platform:user_list")))

        if request.method == "POST":
            password = request.POST.get("password", "")
            if len(password) < 8:
                return _modal_or_redirect(
                    request, "platform/_user_password_form.html",
                    {"person": person, "errors": ["Use at least 8 characters."],
                     "next": _next_url(request, reverse("platform:user_list"))},
                    "platform:user_list",
                )

            person.set_password(password)
            person.save(update_fields=["password", "updated_at"])
            if person.pk == request.user.pk:
                update_session_auth_hash(request, person)

            _audit_person(request, person, "platform.user_password_set")
            messages.success(
                request,
                f"New password set for {person.email}. Their other sessions have "
                "ended — tell them the new one directly.",
            )
            return _close_modal_url(
                request, _next_url(request, reverse("platform:user_list"))
            )

    return _modal_or_redirect(
        request, "platform/_user_password_form.html",
        {"person": person,
         "next": _next_url(request, reverse("platform:user_list"))},
        "platform:user_list",
    )


@login_required
@platform_perm("people.manage")
@require_POST
def user_toggle_active(request, pk):
    """Switch an account off or on. Their records stay either way."""
    from apps.accounts.models import User

    with unscoped():
        person = get_object_or_404(User, pk=pk)
        refusal = platform_perms.refusal_to_manage(request.user, person)
        if person.pk == request.user.pk:
            messages.error(request, "You cannot deactivate your own account.")
        elif refusal:
            messages.error(request, refusal)
        elif person.is_active and _is_last_super_admin(person):
            messages.error(request, "This is the last Super admin. Make somebody else one first.")
        else:
            person.is_active = not person.is_active
            person.save(update_fields=["is_active", "updated_at"])
            _audit_person(request, person, "platform.user_toggle_active")
            messages.success(
                request,
                f"{person.email} "
                f"{'reactivated' if person.is_active else 'deactivated — signed out everywhere'}.",
            )
    return redirect(_next_url(request, reverse("platform:user_list")))


def _is_last_super_admin(person) -> bool:
    from apps.accounts.models import User

    if not person.is_super_admin:
        return False
    return not User.objects.filter(is_platform_staff=True, is_active=True).filter(
        Q(platform_role__isnull=True) | Q(platform_role__is_super=True)
    ).exclude(pk=person.pk).exists()


def _assignable_roles(actor):
    """Roles `actor` may hand out: never more than they hold themselves."""
    from apps.accounts.models import PlatformRole

    mine = actor.platform_permissions
    return [
        (role, role.permission_set <= mine)
        for role in PlatformRole.objects.annotate(admin_count=Count("admins")).order_by("-is_super", "name")
    ]


@login_required
@platform_perm("admins.manage")
def user_platform_access(request, pk):
    """
    Give somebody platform access with a role, change it, or take it away.

    Nobody changes their own, nobody hands out more than they hold, and the
    last Super admin always stays one.
    """
    from apps.accounts.models import PlatformRole, User

    with unscoped():
        person = get_object_or_404(User.objects.select_related("platform_role"), pk=pk)
        back = _next_url(request, reverse("platform:user_list"))

        def refuse(message):
            if request.htmx and request.method == "GET":
                return _modal_or_redirect(
                    request, "platform/_user_access_form.html",
                    {"person": person, "roles": [], "errors": [message], "next": back},
                    "platform:user_list",
                )
            messages.error(request, message)
            return _close_modal_url(request, back)

        if person.pk == request.user.pk:
            return refuse("You cannot change your own platform access.")
        if person.is_super_admin and not request.user.is_super_admin:
            return refuse("Only a Super admin can change a Super admin's access.")
        if not person.platform_role_permissions <= request.user.platform_permissions:
            return refuse("They can do things you cannot, so you cannot change their access.")

        if request.method == "POST":
            choice = request.POST.get("role", "")
            role = PlatformRole.objects.filter(pk=choice).first() if choice else None

            if choice and role is None:
                return refuse("That role no longer exists.")
            if role is not None and not person.is_active:
                return refuse("Reactivate this account before giving it platform access.")
            if role is not None and not role.permission_set <= request.user.platform_permissions:
                return refuse(f"{role.name} can do things you cannot, so you cannot give it out.")
            if (role is None or not role.is_super) and _is_last_super_admin(person):
                return refuse("This is the last Super admin. Make somebody else one first.")

            person.is_platform_staff = role is not None
            person.platform_role = role
            person.save(update_fields=["is_platform_staff", "platform_role", "updated_at"])
            _audit_person(request, person, "platform.user_platform_access")
            messages.success(
                request,
                f"{person.email} is now {role.name}." if role
                else f"{person.email} no longer has platform access.",
            )
            return _close_modal_url(request, back)

    return _modal_or_redirect(
        request, "platform/_user_access_form.html",
        {"person": person, "roles": _assignable_roles(request.user), "next": back},
        "platform:user_list",
    )


def _audit_person(request, person, action):
    """
    Record a change to somebody's account against each shop they belong to.

    An audit row needs a tenant, and a person with no shop has none -- which
    is exactly why the old version of these actions logged nothing at all.
    """
    from apps.accounts.models import Membership
    from apps.core import audit
    from apps.core.context import tenant_context

    audit.record_platform(
        request, action, person.email,
        active=person.is_active,
        platform_role=(person.platform_role.name if person.platform_role_id else
                       ("Super admin" if person.is_platform_staff else "none")),
    )
    tenants = [
        m.tenant for m in Membership.objects_all.select_related("tenant").filter(user=person)
    ]
    for tenant in tenants:
        with tenant_context(tenant, user=request.user):
            audit.record(
                action, obj=person,
                after={"person": person.email, "by": request.user.email},
                ip=audit.client_ip(request),
            )


@login_required
@platform_perm("people.view")
def user_detail(request, pk):
    from apps.accounts.models import Membership, User

    with unscoped():
        user = get_object_or_404(User, pk=pk)
        memberships = list(
            Membership.objects_all.select_related("tenant", "role")
            .prefetch_related("branch_links__branch")
            .filter(user=user)
        )

    if request.method == "POST":
        from django.core.exceptions import PermissionDenied

        if not request.user.has_platform_perm("people.manage"):
            raise PermissionDenied
        refusal = platform_perms.refusal_to_manage(request.user, user)
        if refusal:
            messages.error(request, refusal)
            return redirect("platform:user_detail", pk=user.pk)
        return _apply_user_action(request, user)

    return render(
        request,
        "platform/user_detail.html",
        {"user_row": user, "memberships": memberships},
    )


def _apply_user_action(request, user):
    """
    Take somebody out of one shop, never the last owner of it.

    The other account actions have their own endpoints so the list and this
    page share one implementation of each guard.
    """
    from apps.accounts.models import Membership
    from apps.core import audit
    from apps.core.context import tenant_context

    if request.POST.get("action") != "remove_membership":
        messages.error(request, "Unknown action.")
        return redirect("platform:user_detail", pk=user.pk)

    with unscoped():
        membership = Membership.objects_all.filter(
            pk=request.POST.get("membership"), user=user
        ).select_related("tenant", "role").first()
        if membership is None:
            messages.error(request, "That membership no longer exists.")
        elif membership.role.is_owner_role and _is_last_owner(membership):
            messages.error(
                request,
                f"{membership.user.name} is the only owner of "
                f"{membership.tenant.name}. The shop would be locked out.",
            )
        else:
            tenant = membership.tenant
            membership.delete()
            audit.record_platform(request, "platform.membership_removed", user.email,
                                  shop=tenant.name)
            with tenant_context(tenant, user=request.user):
                audit.record(
                    "platform.membership_removed", obj=user,
                    after={"person": user.email, "by": request.user.email},
                    ip=audit.client_ip(request),
                )
            messages.success(request, f"Removed from {tenant.name}.")

    return redirect("platform:user_detail", pk=user.pk)


def _is_last_owner(membership) -> bool:
    from apps.accounts.models import Membership

    return not Membership.objects_all.filter(
        tenant=membership.tenant, role__is_owner_role=True, is_active=True
    ).exclude(pk=membership.pk).exists()


# --------------------------------------------------------------------------
# Plans and the permission catalogue
# --------------------------------------------------------------------------

@login_required
@platform_perm("plans.manage")
def plan_edit(request, pk=None):
    """
    Build and change plans.

    Limits and features are rows, so what a plan includes changes here and
    takes effect on every shop's next request -- no deploy. Which is exactly
    why a change is checked before it is saved, and its effect on the shops
    already on the plan is spelt out afterwards.
    """
    from apps.core.features import ALL_FEATURES, ALL_LIMITS, FEATURES_BY_KEY
    from apps.core.parsing import BadInput, int_or, parse_decimal
    from apps.tenancy.models import PlanFeature, PlanLimit

    with unscoped():
        plan = get_object_or_404(Plan, pk=pk) if pk else None
        subscribers = plan.subscriptions.count() if plan else 0

        def form(errors=None, values=None):
            return _modal_or_redirect(request, "platform/_plan_form.html", {
                "plan": plan, "all_features": ALL_FEATURES, "all_limits": ALL_LIMITS,
                "current_features": (set(values.getlist("features")) if values is not None
                                     else set(plan.feature_keys()) if plan else set()),
                "current_limits": (
                    {k: (values.get(f"limit:{k}") or "") for k, _ in ALL_LIMITS} if values is not None
                    else {row.key: row.value for row in plan.limits.all()} if plan else {}
                ),
                "subscribers": subscribers, "errors": errors or [], "values": values,
            }, "platform:plans")

        if request.method != "POST":
            return form()

        errors = []
        name = request.POST.get("name", "").strip()[:80]
        if not name:
            errors.append("A name is required.")
        elif Plan.objects.filter(name__iexact=name).exclude(pk=getattr(plan, "pk", None)).exists():
            errors.append(f"There is already a plan called {name}.")
        try:
            monthly = parse_decimal(request.POST.get("price_monthly"), "Monthly price", minimum=0, default=0)
            annual = parse_decimal(request.POST.get("price_annual"), "Yearly price", minimum=0, default=0)
        except BadInput as exc:
            errors.append(str(exc))
            monthly = annual = None
        trial_days = int_or(request.POST.get("trial_days"), -1)
        if not 0 <= trial_days <= 365:
            errors.append("Trial days must be a whole number from 0 to 365.")
        sort_order = max(int_or(request.POST.get("sort_order"), 0), 0)
        limits = {}
        for key, label in ALL_LIMITS:
            raw = (request.POST.get(f"limit:{key}") or "").strip()
            if raw and int_or(raw, -1) < 0:
                errors.append(f"{label}: a whole number, or blank for unlimited.")
            limits[key] = int_or(raw, None) if raw else None
        chosen = [k for k in request.POST.getlist("features") if k in FEATURES_BY_KEY]
        is_public = request.POST.get("is_public") == "on"
        others_public = Plan.objects.filter(is_public=True).exclude(pk=getattr(plan, "pk", None))
        if not is_public and not others_public.exists():
            # Signup gives a new shop the first public plan; with none, signup fails.
            errors.append("At least one plan must be offered at signup.")
        if errors:
            return form(errors, request.POST)

        before_features = set(plan.feature_keys()) if plan else set()
        fields = {"name": name, "description": request.POST.get("description", "").strip(),
                  "price_monthly": monthly, "price_annual": annual, "trial_days": trial_days,
                  "is_public": is_public, "sort_order": sort_order}
        if plan is None:
            code = slugify(name)[:40] or "plan"
            base, n = code, 2
            while Plan.objects.filter(code=code).exists():
                code = f"{base[:36]}-{n}"
                n += 1
            plan = Plan.objects.create(code=code, **fields)
        else:
            for key, value in fields.items():
                setattr(plan, key, value)
            plan.save()

        PlanLimit.objects.filter(plan=plan).delete()
        PlanLimit.objects.bulk_create([PlanLimit(plan=plan, key=k, value=v) for k, v in limits.items()])
        PlanFeature.objects.filter(plan=plan).delete()
        PlanFeature.objects.bulk_create([PlanFeature(plan=plan, feature_key=k) for k in chosen])

        # Feature changes alter what every member of every shop on this plan
        # may do, so their cached permissions have to go.
        from apps.accounts.models import Membership

        Membership.objects_all.filter(tenant__subscription__plan=plan).update(
            permissions_version=F("permissions_version") + 1
        )

        audit.record_platform(request, "platform.plan_saved", plan.name,
                              features=sorted(chosen), limits=limits)
        messages.success(request, f"{plan.name} saved.")
        if subscribers:
            removed = before_features - set(chosen)
            if removed:
                labels = ", ".join(FEATURES_BY_KEY[k].label for k in sorted(removed))
                messages.warning(request, f"{subscribers} shop(s) on {plan.name} no longer have: {labels}.")
            over = [
                f"{t.name} ({label.lower()}: {t.usage_of(key)} of {limits[key]})"
                for t in Tenant.objects.filter(subscription__plan=plan)
                for key, label in ALL_LIMITS
                if limits[key] is not None and key != "history_days" and t.usage_of(key) > limits[key]
            ]
            if over:
                messages.warning(request, "Already over the new limits -- they keep what they have "
                                          "but cannot add more: " + "; ".join(over[:6]) + ".")
        return _close_modal(request, "platform:plans")


@login_required
@platform_perm("plans.manage")
@require_POST
def plan_delete(request, pk):
    with unscoped():
        plan = get_object_or_404(Plan, pk=pk)
        subscribers = plan.subscriptions.count()
        if subscribers:
            messages.error(
                request,
                f"{subscribers} shop{'' if subscribers == 1 else 's'} are on "
                f"{plan.name}. Move them to another plan first.",
            )
        elif plan.is_public and not Plan.objects.filter(is_public=True).exclude(pk=plan.pk).exists():
            messages.error(request, "This is the only plan offered at signup. Offer another first.")
        else:
            name = plan.name
            plan.delete()
            audit.record_platform(request, "platform.plan_deleted", name)
            messages.success(request, f"{name} deleted.")
    return _close_modal(request, "platform:plans")


@login_required
@platform_perm("catalogue.view")
def permissions(request):
    """
    The permission catalogue, read-only.

    It is defined in code and synced by migration, so this is a reference:
    what exists, which module owns it, which plan feature gates it, and how
    many shops have granted it.
    """
    from django.db.models import Count as DBCount

    from apps.accounts.models import Permission
    from apps.core.features import FEATURES_BY_KEY
    from apps.core.permissions import registry

    with unscoped():
        rows = list(
            Permission.objects.annotate(
                roles_granting=DBCount("rolepermission", distinct=True),
                people_granted=DBCount("userpermission", filter=Q(userpermission__effect="grant"),
                                       distinct=True),
            ).order_by("module", "sort_order")
        )

    kinds = {"bool": "Yes or no", "limit": "Up to an amount", "set": "Chosen options"}
    items = []
    for permission in rows:
        feature = FEATURES_BY_KEY.get(permission.requires_feature)
        items.append({
            "permission": permission,
            "feature": feature.label if feature else None,
            "kind": kinds.get(permission.value_type, permission.value_type),
            "unused": not permission.roles_granting and not permission.people_granted,
            # Everything the filter box searches, lowercased once here rather
            # than on every keystroke in the browser.
            "haystack": " ".join([
                permission.code, permission.label, permission.module, permission.description,
                feature.label if feature else "",
            ]).lower(),
        })

    # The catalogue is defined in code and copied to the database by a
    # command. If they drift, roles quietly lose or cannot find permissions.
    in_code = registry.codes()
    in_db = {row.code for row in rows}

    modules = sorted({item["permission"].module for item in items})

    return render(
        request,
        "platform/permissions.html",
        {
            "items": items,
            "modules": modules,
            "total": len(rows),
            "dangerous": sum(1 for i in items if i["permission"].is_dangerous),
            "gated": sum(1 for i in items if i["feature"]),
            "ungranted": sum(1 for i in items if i["unused"]),
            "rows_json": json.dumps([
                {"h": i["haystack"], "m": i["permission"].module, "a": i["permission"].is_dangerous,
                 "g": bool(i["feature"]), "u": i["unused"]} for i in items
            ]),
            "missing": sorted(in_code - in_db),
            "stale": sorted(in_db - in_code),
        },
    )


# --------------------------------------------------------------------------
# Onboarding and editing a shop
# --------------------------------------------------------------------------

@login_required
@platform_perm("shops.create")
def tenant_create(request):
    """
    Set a shop up yourself.

    Signup handles the ones that find you. This handles the two you already
    have, and every one you sell to in person -- which in this market is most
    of them.
    """
    from apps.accounts.models import User
    from apps.tenancy.services import create_tenant

    with unscoped():
        plans = list(Plan.objects.order_by("sort_order"))

    if request.method == "POST":
        business = request.POST.get("business_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")

        errors = []
        if not (business and email and name):
            errors.append("Business name, owner name and email are all required.")
        if len(password) < 8:
            errors.append("Give the owner a password of at least 8 characters.")
        with unscoped():
            if email and User.objects.filter(email=email).exists():
                errors.append(f"{email} already has an account.")

        if errors:
            with unscoped():
                plans = list(Plan.objects.order_by("sort_order"))
            return _modal_or_redirect(
                request, "platform/_tenant_form.html",
                {"plans": plans, "errors": errors, "values": request.POST},
                "platform:tenant_list",
            )

        with unscoped():

            owner = User.objects.create_user(
                email=email, password=password, name=name,
                phone=request.POST.get("phone", ""),
            )
            plan = Plan.objects.filter(pk=request.POST.get("plan")).first()
            tenant, _ = create_tenant(
                name=business, owner=owner, plan=plan,
                branch_name=request.POST.get("branch_name") or "Main",
            )

        audit.record_platform(request, "platform.shop_created", tenant.name, owner=email,
                              plan=getattr(plan, "name", ""))
        messages.success(
            request,
            f"{tenant.name} created. {email} can sign in with the password you set.",
        )
        return _close_modal(request, "platform:tenant_list")

    return _modal_or_redirect(
        request, "platform/_tenant_form.html", {"plans": plans},
        "platform:tenant_list",
    )


@login_required
@platform_perm("shops.edit")
def tenant_edit(request, pk):
    """Correct the details that end up printed on a receipt."""
    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)

        if request.method == "POST":
            for field in ("name", "legal_name", "tin", "vrn", "phone", "email",
                          "address", "currency", "timezone"):
                if field in request.POST:
                    setattr(tenant, field, request.POST.get(field, ""))
            tenant.save()
            audit.record_platform(request, "platform.shop_edited", tenant.name)
            messages.success(request, f"{tenant.name} updated.")
            return _close_modal_url(
                request, _next_url(request, reverse("platform:tenant_list"))
            )

    return _modal_or_redirect(
        request, "platform/_tenant_edit_form.html",
        {"tenant": tenant,
         "next": _next_url(request, reverse("platform:tenant_list"))},
        "platform:tenant_list",
    )


# --------------------------------------------------------------------------
# Devices and the fiscal queue
# --------------------------------------------------------------------------

@login_required
@platform_perm("devices.view")
def devices(request):
    """
    Every till and phone, silent ones first.

    A till that cannot sync is holding sales nobody can see -- the failure
    worth catching early, so it leads the list instead of trailing it.
    """
    from django.core.paginator import Paginator
    from django.db.models import F as DbF

    from apps.org.models import Device

    view = request.GET.get("view", "")
    term = request.GET.get("q", "").strip()
    stale_before = timezone.now() - timedelta(hours=24)

    with unscoped():
        base = Device.objects.select_related("tenant", "branch")
        views = {
            "silent": Q(is_active=True) & (Q(last_sync_at__lt=stale_before) | Q(last_sync_at__isnull=True)),
            "holding": Q(is_active=True, queued__gt=0),
            "retired": Q(is_active=False),
        }
        rows = base.filter(views[view]) if view in views else base
        if term:
            rows = rows.filter(Q(label__icontains=term) | Q(device_id__icontains=term)
                               | Q(tenant__name__icontains=term) | Q(branch__name__icontains=term))
        rows = rows.order_by("-is_active", DbF("last_sync_at").asc(nulls_first=True))
        page = Paginator(rows, 50).get_page(request.GET.get("page"))
        counts = {"total": base.count(), **{k: base.filter(q).count() for k, q in views.items()}}

    keep = request.GET.copy()
    keep.pop("page", None)
    return render(request, "platform/devices.html", {
        "page": page, "devices": page.object_list, "view": view, "q": term,
        "counts": counts, "stale_before": stale_before, "keep": keep.urlencode(),
    })


@login_required
@platform_perm("devices.manage")
@require_POST
def device_retire(request, pk):
    from apps.org.models import Device

    with unscoped():
        device = get_object_or_404(Device, pk=pk)
        device.is_active = not device.is_active
        device.save(update_fields=["is_active", "updated_at"])
        audit.record_platform(request, "platform.device_reactivated" if device.is_active else "platform.device_retired",
                              device.label or device.device_id, shop=device.tenant.name)
        messages.success(
            request,
            f"{device.label or device.device_id} "
            f"{'is back in use' if device.is_active else 'switched off: it can no longer send sales'}.",
        )
    return redirect(_next_url(request, reverse("platform:devices")))


@login_required
@platform_perm("health.manage")
@require_POST
def fiscal_retry(request):
    """
    Put failed fiscal receipts back in the queue.

    A provider outage marks a batch failed; once it is back these need to go
    again rather than sit there forever.
    """
    from apps.pos.models import FiscalReceipt, FiscalStatus

    with unscoped():
        reset = FiscalReceipt.objects.filter(status=FiscalStatus.FAILED).update(
            status=FiscalStatus.PENDING, attempts=0, error=""
        )
    audit.record_platform(request, "platform.fiscal_retried", f"{reset} receipts")
    messages.success(request, f"{reset} receipt{'' if reset == 1 else 's'} queued to be sent again "
                              "within ten minutes.")
    return redirect("platform:health")


@login_required
@platform_perm("audit.view")
def platform_audit(request):
    """
    Everything your own team did, and who did it.

    Support access matters most -- somebody looking at a shop's data should
    always be answerable for it -- but so does who changed a plan, a role or
    somebody's admin access.
    """
    from django.core.paginator import Paginator

    from apps.accounts.models import PlatformEvent, User

    area = request.GET.get("area", "")
    who = request.GET.get("who", "")

    events = PlatformEvent.objects.select_related("user").order_by("-created_at")
    if area in PLATFORM_AREAS:
        events = events.filter(action__in=PLATFORM_AREAS[area][1])
    if who.isdigit():
        events = events.filter(user_id=int(who))

    # Paged in the database: the whole history, not the newest thousand.
    page = Paginator(events, 50).get_page(request.GET.get("page"))
    rows = [
        {"when": e.created_at, "action": e.action, "label": ACTION_LABELS.get(e.action, e.action),
         "user": e.user, "target": e.target, "detail": e.detail, "ip": e.ip}
        for e in page.object_list
    ]
    with unscoped():
        team = User.objects.filter(is_platform_staff=True).order_by("name")
    keep = request.GET.copy()
    keep.pop("page", None)

    return render(request, "platform/audit.html", {
        "page": page, "rows": rows, "areas": PLATFORM_AREAS,
        "area": area, "who": who, "team": team, "keep": keep.urlencode(),
    })


ACTION_LABELS = {
    "platform.impersonated": "Opened a shop for support",
    "platform.support_ended": "Ended a support session",
    "platform.shop_created": "Set up a shop",
    "platform.shop_edited": "Edited a shop's details",
    "platform.shop_plan_changed": "Changed a shop's plan",
    "platform.shop_suspended": "Suspended a shop",
    "platform.shop_reactivated": "Reactivated a shop",
    "platform.shop_deleted": "Deleted a shop",
    "platform.shop_staff_saved": "Added or changed shop staff",
    "platform.shop_staff_removed": "Removed someone from a shop",
    "platform.membership_removed": "Removed someone from a shop",
    "platform.user_edited": "Edited a person's details",
    "platform.user_password_set": "Set a new password",
    "platform.user_toggle_active": "Switched an account off or on",
    "platform.user_deactivated": "Deactivated an account",
    "platform.user_deleted": "Deleted an account",
    "platform.admin_added": "Added a platform admin",
    "platform.user_platform_access": "Changed platform access",
    "platform.role_created": "Created an admin role",
    "platform.role_changed": "Changed an admin role",
    "platform.role_deleted": "Removed an admin role",
    "platform.plan_saved": "Saved a plan",
    "platform.plan_deleted": "Deleted a plan",
    "platform.invoice_raised": "Raised an invoice",
    "platform.invoice_edited": "Edited an invoice",
    "platform.invoice_paid": "Recorded a payment",
    "platform.invoice_voided": "Voided an invoice",
    "platform.payment_reversed": "Reversed a payment",
    "platform.device_edited": "Edited a device",
    "platform.device_retired": "Retired a device",
    "platform.device_reactivated": "Brought a device back",
    "platform.device_removed": "Removed a device",
    "platform.fiscal_retried": "Retried fiscal receipts",
}

PLATFORM_AREAS = {
    "support": ("Support sessions", ["platform.impersonated", "platform.support_ended"]),
    "shops": ("Shops", [a for a in ACTION_LABELS if a.startswith("platform.shop_")]
              + ["platform.membership_removed"]),
    "people": ("People", ["platform.user_edited", "platform.user_password_set",
                          "platform.user_toggle_active", "platform.user_deactivated",
                          "platform.user_deleted"]),
    "team": ("Admins & roles", ["platform.admin_added", "platform.user_platform_access",
                                "platform.role_created", "platform.role_changed",
                                "platform.role_deleted"]),
    "billing": ("Plans & invoices", [a for a in ACTION_LABELS
                                     if a.startswith(("platform.plan_", "platform.invoice_",
                                                      "platform.payment_"))]),
    "devices": ("Devices & fiscal", [a for a in ACTION_LABELS
                                     if a.startswith(("platform.device_", "platform.fiscal_"))]),
}


# --------------------------------------------------------------------------
# The operations each list screen was missing
# --------------------------------------------------------------------------

@login_required
@platform_perm("shops.delete")
@require_POST
def tenant_delete(request, pk):
    """
    Delete a shop and everything in it.

    Only one that never traded. A shop with sales behind it holds records
    somebody may still be answerable for -- that one gets cancelled, which
    keeps the data and stops the billing.
    """
    from django.db.models import ProtectedError

    from apps.pos.models import Sale
    from apps.tenancy.models import InvoiceStatus

    with unscoped():
        tenant = get_object_or_404(Tenant, pk=pk)
        sales = Sale.objects_all.filter(tenant=tenant).count()

        if sales:
            messages.error(
                request,
                f"{tenant.name} has {sales} sale{'' if sales == 1 else 's'} on "
                "record and cannot be deleted. Cancel the subscription instead "
                "— they keep their data and you stop billing them.",
            )
            return redirect("platform:tenant_detail", pk=pk)

        if request.POST.get("confirm", "").strip() != tenant.name:
            messages.error(
                request, "Type the shop's name exactly to confirm."
            )
            return redirect("platform:tenant_detail", pk=pk)

        paid = Invoice.objects.filter(tenant=tenant, status=InvoiceStatus.PAID).count()
        if paid:
            # Deleting paid invoices leaves gaps in the platform's own
            # numbering, which is exactly what voiding exists to avoid.
            messages.error(
                request,
                f"{tenant.name} has {paid} paid invoice{'' if paid == 1 else 's'}. "
                "Cancel the subscription instead; the records stay.",
            )
            return redirect("platform:tenant_detail", pk=pk)

        name = tenant.name
        try:
            _delete_tenant(tenant)
        except ProtectedError:
            # Something in the shop still protects a row (stock, a product a
            # movement points at). Say so instead of a 500.
            messages.error(
                request,
                f"{name} still has records that cannot be deleted. Cancel the "
                "subscription instead; they keep their data and you stop billing them.",
            )
            return redirect("platform:tenant_detail", pk=pk)
        audit.record_platform(request, "platform.shop_deleted", name)

    messages.success(request, f"{name} deleted.")
    return redirect("platform:tenant_list")


def _delete_tenant(tenant):
    """
    Remove a shop in an order the database will accept.

    Cascading from the tenant alone deadlocks against itself: it would delete
    the roles, but every membership protects the role it points at. So the
    memberships go first, then everything else cascades cleanly.
    """
    from django.db import transaction
    from django.db.models import ProtectedError

    from apps.accounts.models import Membership, User

    with transaction.atomic():
        members = Membership.objects_all.filter(tenant=tenant)
        people = list(members.values_list("user_id", flat=True))
        members.delete()
        tenant.delete()

        # Accounts that only ever belonged to this shop go with it. Left
        # behind, they sat in no shop at all, and setting the shop up again
        # for the same owner failed with "already has an account".
        orphans = User.objects.filter(pk__in=people, is_platform_staff=False).exclude(
            pk__in=Membership.objects_all.values("user_id")
        )
        for user in orphans:
            try:
                with transaction.atomic():
                    user.delete()
            except ProtectedError:
                # Referenced by a record somewhere else: keep the account.
                pass


@login_required
@platform_perm("admins.manage")
def user_create(request):
    """
    Add somebody to the platform team.

    Always an administrator. An account made here without platform access
    belonged to no shop and could do nothing at all once signed in, so the
    choice was removed. A shop's staff are added from that shop's page.
    """
    from apps.accounts.models import PlatformRole, User

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")

        errors = []
        if not email or not name:
            errors.append("Name and email are required.")
        if len(password) < 8:
            errors.append("Use a password of at least 8 characters.")
        role = PlatformRole.objects.filter(pk=request.POST.get("role") or 0).first()
        if role is None:
            errors.append("Choose what they may do.")
        elif not role.permission_set <= request.user.platform_permissions:
            errors.append(f"{role.name} can do things you cannot, so you cannot give it out.")
        with unscoped():
            if email and User.objects.filter(email=email).exists():
                errors.append(f"{email} already has an account.")

        if errors:
            return _modal_or_redirect(
                request, "platform/_user_form.html",
                {"errors": errors, "values": request.POST,
                 "roles": _assignable_roles(request.user)},
                "platform:user_list",
            )

        with unscoped():
            user = User.objects.create_user(
                email=email, password=password, name=name,
                phone=request.POST.get("phone", ""),
                is_platform_staff=True, platform_role=role,
            )
        audit.record_platform(request, "platform.admin_added", user.email, role=role.name)
        messages.success(
            request, f"{user.email} added to the platform team. Tell them the password."
        )
        return _close_modal(request, "platform:user_list")

    return _modal_or_redirect(
        request, "platform/_user_form.html",
        {"roles": _assignable_roles(request.user)}, "platform:user_list",
    )


@login_required
@platform_perm("people.delete")
@require_POST
def user_delete(request, pk):
    """
    Delete an account outright.

    Only one that belongs to no shop and has never signed in. Anybody who has
    used the system is deactivated instead, because their name is on sales,
    adjustments and audit rows that have to keep meaning something.
    """
    from apps.accounts.models import Membership, User

    with unscoped():
        user = get_object_or_404(User, pk=pk)

        refusal = platform_perms.refusal_to_manage(request.user, user)
        if refusal:
            messages.error(request, refusal)
            return redirect(_next_url(request, reverse("platform:user_list")))
        if user.pk == request.user.pk:
            messages.error(request, "You cannot delete your own account.")
            return redirect(_next_url(request, reverse("platform:user_detail", args=[pk])))

        shops = Membership.objects_all.filter(user=user).count()
        if shops or user.last_login:
            messages.error(
                request,
                f"{user.email} has used the system, so the account is "
                "deactivated rather than deleted — their name is on records "
                "that must keep meaning something.",
            )
            user.is_active = False
            user.save(update_fields=["is_active", "updated_at"])
            audit.record_platform(request, "platform.user_deactivated", user.email,
                                  reason="asked to delete an account that had been used")
            return redirect(_next_url(request, reverse("platform:user_detail", args=[pk])))

        email = user.email
        user.delete()
        audit.record_platform(request, "platform.user_deleted", email)

    messages.success(request, f"{email} deleted.")
    return redirect(_next_url(request, reverse("platform:user_list")))


@login_required
@platform_perm("devices.manage")
def device_edit(request, pk):
    """Name a device, so a support call is not about a 64-character id."""
    from apps.org.models import Device, DeviceKind

    back = _next_url(request, reverse("platform:devices"))
    with unscoped():
        device = get_object_or_404(Device.objects.select_related("tenant", "branch"), pk=pk)
        if request.method == "POST":
            device.label = request.POST.get("label", "").strip()[:60]
            kind = request.POST.get("kind")
            device.kind = kind if kind in DeviceKind.values else device.kind
            device.save(update_fields=["label", "kind", "updated_at"])
            audit.record_platform(request, "platform.device_edited", str(device), shop=device.tenant.name)
            messages.success(request, f"{device} saved.")
            return _close_modal_url(request, back)

    return _modal_or_redirect(request, "platform/_device_form.html",
                              {"device": device, "next": back}, "platform:devices")


@login_required
@platform_perm("devices.manage")
@require_POST
def device_delete(request, pk):
    """
    Forget a device.

    Safe: it holds no records of its own, and the sales it sent are on the
    shop. A device that comes back simply registers again.
    """
    from apps.org.models import Device

    with unscoped():
        device = get_object_or_404(Device, pk=pk)
        label = device.label or device.device_id
        audit.record_platform(request, "platform.device_removed", label, shop=device.tenant.name)
        device.delete()

    messages.success(request, f"{label} removed.")
    return redirect(_next_url(request, reverse("platform:devices")))


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

@login_required
@platform_perm("invoices.view")
def invoices(request):
    """
    What each shop has been billed, what has come in, and what is late.

    Mobile-money collection is not automated yet, so this is also where a
    transfer that arrived in your account is matched to the shop that sent it.
    """
    from django.core.paginator import Paginator
    from django.db.models import DecimalField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    from apps.tenancy.models import InvoiceStatus, Payment, PaymentStatus

    view = request.GET.get("view", "")
    term = request.GET.get("q", "").strip()
    today = timezone.localdate()

    paid = Coalesce(Subquery(
        Payment.objects.filter(invoice=OuterRef("pk"), status=PaymentStatus.SUCCEEDED)
        .values("invoice").annotate(t=Sum("amount")).values("t"),
        output_field=DecimalField(),
    ), Decimal("0"))

    with unscoped():
        base = Invoice.objects.select_related("tenant").annotate(paid=paid).annotate(
            left=F("total") - F("paid")
        )
        unpaid = Q(status__in=[InvoiceStatus.OPEN, InvoiceStatus.DRAFT])
        views = {
            "open": unpaid,
            "overdue": unpaid & Q(due_date__lt=today),
            "paid": Q(status=InvoiceStatus.PAID),
            "void": Q(status=InvoiceStatus.VOID),
        }
        rows = base
        if view in views:
            rows = rows.filter(views[view])
        if term:
            rows = rows.filter(Q(number__icontains=term) | Q(tenant__name__icontains=term))
        rows = rows.order_by("-period_start", "-number")
        # Paged, not capped at 200.
        page = Paginator(rows, 50).get_page(request.GET.get("page"))

        counted = base.exclude(status=InvoiceStatus.VOID)
        totals = {
            # Void invoices were never owed, and part-paid ones only owe the rest.
            "billed": counted.aggregate(t=Sum("total"))["t"] or Decimal("0"),
            "collected": Payment.objects.filter(status=PaymentStatus.SUCCEEDED)
            .aggregate(t=Sum("amount"))["t"] or Decimal("0"),
            "outstanding": base.filter(unpaid).aggregate(t=Sum("left"))["t"] or Decimal("0"),
            "overdue": base.filter(views["overdue"]).count(),
            "overdue_value": base.filter(views["overdue"]).aggregate(t=Sum("left"))["t"]
            or Decimal("0"),
        }

    keep = request.GET.copy()
    keep.pop("page", None)
    return render(request, "platform/invoices.html", {
        "page": page, "invoices": page.object_list, "view": view, "q": term,
        "totals": totals, "today": today, "keep": keep.urlencode(),
    })


@login_required
@platform_perm("invoices.view")
def invoice_detail(request, pk):
    """One invoice, its payments, and what can still be done with it."""
    with unscoped():
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), pk=pk)
        payments = list(invoice.payments.order_by("-created_at"))
    return _modal_or_redirect(request, "platform/_invoice_detail.html", {
        "invoice": invoice, "payments": payments, "today": timezone.localdate(),
        "next": _next_url(request, reverse("platform:invoices")),
    }, "platform:invoices")


@login_required
@platform_perm("invoices.manage")
def invoice_create(request):
    """Raise an invoice for a shop, priced per branch from their plan unless you say otherwise."""
    from apps.core.parsing import BadInput, date_or, parse_decimal
    from apps.tenancy.billing import BillingError, period_after, price_for, raise_invoice

    back = _next_url(request, reverse("platform:invoices"))
    with unscoped():
        tenants = list(Tenant.objects.select_related("subscription__plan").order_by("name"))
        if request.method == "POST":
            tenant = get_object_or_404(Tenant, pk=request.POST.get("tenant") or 0)
            errors = []
            try:
                start = date_or(request.POST.get("period_start"))
                if start is None:
                    raise BadInput("Choose when the period starts.")
                subscription = tenant.active_subscription
                if subscription is None:
                    raise BadInput(f"{tenant.name} has no subscription.")
                end = date_or(request.POST.get("period_end")) or period_after(subscription, start)[1]
                amount = (parse_decimal(request.POST.get("amount"), "Amount",
                                        positive=True, places=2)
                          if request.POST.get("amount", "").strip() else price_for(subscription))
                invoice = raise_invoice(
                    tenant, period_start=start, period_end=end, amount=amount,
                    due_date=date_or(request.POST.get("due_date"), start),
                )
            except (BadInput, BillingError) as exc:
                errors.append(str(exc))
            if errors:
                return _modal_or_redirect(request, "platform/_invoice_form.html", {
                    "tenants": tenants, "errors": errors, "values": request.POST,
                    "selected_tenant": str(tenant.pk), "next": back,
                }, "platform:invoices")
            audit.record_platform(request, "platform.invoice_raised", invoice.number,
                                  shop=tenant.name, total=str(invoice.total))
            messages.success(request, f"{invoice.number} raised for {tenant.name}: "
                                      f"{invoice.total:,.0f} {invoice.currency}.")
            return _close_modal_url(request, back)

    suggestions = {}
    from apps.tenancy.billing import next_period_start, period_after, price_for
    with unscoped():
        for t in tenants:
            sub = getattr(t, "subscription", None)
            if sub is None:
                continue
            start = next_period_start(sub)
            suggestions[t.pk] = {
                "amount": str(price_for(sub)), "start": start.isoformat(),
                "end": period_after(sub, start)[1].isoformat(),
            }
    import json

    return _modal_or_redirect(request, "platform/_invoice_form.html", {
        "tenants": tenants, "selected_tenant": request.GET.get("tenant", ""),
        "suggestions_json": json.dumps({str(k): v for k, v in suggestions.items()}),
        "next": back,
    }, "platform:invoices")


@login_required
@platform_perm("invoices.manage")
def invoice_edit(request, pk):
    """Correct an invoice that nothing has been paid against yet."""
    from apps.core.parsing import BadInput, date_or, parse_decimal
    from apps.tenancy.models import InvoiceStatus, PaymentStatus

    back = _next_url(request, reverse("platform:invoices"))
    with unscoped():
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), pk=pk)
        locked = (invoice.status not in {InvoiceStatus.OPEN, InvoiceStatus.DRAFT}
                  or invoice.payments.filter(status=PaymentStatus.SUCCEEDED).exists())
        if request.method == "POST":
            errors = []
            if locked:
                errors.append(f"{invoice.number} is {invoice.get_status_display().lower()} or has "
                              "payments; void it and raise a new one instead.")
            else:
                try:
                    # Positive, and to the cent: a zero invoice could never
                    # be paid and stayed open for ever, and 0.004 became 0.00.
                    amount = parse_decimal(request.POST.get("amount"), "Amount",
                                           positive=True, places=2)
                    start = date_or(request.POST.get("period_start"), invoice.period_start)
                    end = date_or(request.POST.get("period_end"), invoice.period_end)
                    if end < start:
                        raise BadInput("The period ends before it starts.")
                    # The same period twice was refused when raising an
                    # invoice, but not when editing one into place.
                    clash = Invoice.objects.filter(
                        tenant=invoice.tenant, period_start__lte=end, period_end__gte=start,
                        status__in=[InvoiceStatus.OPEN, InvoiceStatus.DRAFT, InvoiceStatus.PAID],
                    ).exclude(pk=invoice.pk).first()
                    if clash is not None:
                        raise BadInput(f"{clash.number} already covers part of that period.")
                except BadInput as exc:
                    errors.append(str(exc))
            if errors:
                return _modal_or_redirect(request, "platform/_invoice_form.html", {
                    "invoice": invoice, "errors": errors, "values": request.POST, "next": back,
                }, "platform:invoices")
            invoice.amount = invoice.total = amount
            invoice.period_start, invoice.period_end = start, end
            invoice.due_date = date_or(request.POST.get("due_date"), invoice.due_date)
            invoice.save()
            audit.record_platform(request, "platform.invoice_edited", invoice.number,
                                  shop=invoice.tenant.name, total=str(invoice.total))
            messages.success(request, f"{invoice.number} updated.")
            return _close_modal_url(request, back)

    return _modal_or_redirect(request, "platform/_invoice_form.html", {
        "invoice": invoice, "locked": locked, "next": back,
    }, "platform:invoices")


@login_required
@platform_perm("invoices.manage")
def invoice_pay(request, pk):
    """
    Record money that arrived, in a form rather than squeezed into the table.

    Never more than is owed, never on a void or paid invoice, and a full
    payment moves the shop's paid-up date on and lets a held-back shop trade.
    """
    from apps.core.parsing import BadInput, parse_decimal
    from apps.tenancy.billing import BillingError, record_payment
    from apps.tenancy.models import PaymentMethod

    back = _next_url(request, reverse("platform:invoices"))
    with unscoped():
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), pk=pk)
        if request.method == "POST":
            method = request.POST.get("method", "")
            try:
                if method not in PaymentMethod.values:
                    raise BadInput("Choose how it was paid.")
                amount = parse_decimal(request.POST.get("amount"), "Amount",
                                       positive=True, places=2)
                was = invoice.tenant.subscription.status
                record_payment(invoice, amount=amount, method=method,
                               reference=request.POST.get("reference", "").strip())
            except (BadInput, BillingError) as exc:
                return _modal_or_redirect(request, "platform/_invoice_pay.html", {
                    "invoice": invoice, "methods": PaymentMethod.choices,
                    "errors": [str(exc)], "values": request.POST, "next": back,
                }, "platform:invoices")
            invoice.refresh_from_db()
            audit.record_platform(request, "platform.invoice_paid", invoice.number,
                                  shop=invoice.tenant.name, amount=str(amount), method=method)
            note = ""
            invoice.tenant.subscription.refresh_from_db()
            if was != invoice.tenant.subscription.status:
                note = f" {invoice.tenant.name} is now {invoice.tenant.subscription.get_status_display().lower()}."
            messages.success(request, f"{amount:,.0f} recorded against {invoice.number}.{note}")
            return _close_modal_url(request, back)

    return _modal_or_redirect(request, "platform/_invoice_pay.html", {
        "invoice": invoice, "methods": PaymentMethod.choices, "next": back,
    }, "platform:invoices")


@login_required
@platform_perm("invoices.manage")
@require_POST
def payment_reverse(request, pk):
    """Undo a payment recorded against the wrong invoice or amount."""
    from apps.tenancy.billing import BillingError, reverse_payment
    from apps.tenancy.models import Payment

    back = _next_url(request, reverse("platform:invoices"))
    reason = request.POST.get("reason", "").strip()
    with unscoped():
        payment = get_object_or_404(Payment.objects.select_related("invoice__tenant"), pk=pk)
        if not reason:
            messages.error(request, "Say why the payment is being reversed.")
            return redirect(back)
        try:
            reverse_payment(payment, reason=reason)
        except BillingError as exc:
            messages.error(request, str(exc))
            return redirect(back)
    audit.record_platform(request, "platform.payment_reversed", payment.invoice.number,
                          shop=payment.invoice.tenant.name, amount=str(payment.amount),
                          reason=reason)
    messages.success(request, f"{payment.amount:,.0f} on {payment.invoice.number} reversed.")
    return redirect(back)


@login_required
@platform_perm("invoices.manage")
@require_POST
def invoice_void(request, pk):
    """
    Void an invoice raised in error.

    Voided, not deleted: an invoice number that simply disappears is the kind
    of gap an accountant asks about a year later.
    """
    from apps.tenancy.billing import BillingError, void_invoice

    back = _next_url(request, reverse("platform:invoices"))
    with unscoped():
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), pk=pk)
        try:
            void_invoice(invoice)
        except BillingError as exc:
            messages.error(request, str(exc))
            return redirect(back)
    audit.record_platform(request, "platform.invoice_voided", invoice.number,
                          shop=invoice.tenant.name)
    messages.success(request, f"{invoice.number} voided.")
    return redirect(back)


# --------------------------------------------------------------------------
# Platform roles
# --------------------------------------------------------------------------

@login_required
@platform_perm("admins.manage")
def roles(request):
    """What each job on your own team may do, and who holds it."""
    from apps.accounts.models import PlatformRole

    with unscoped():
        rows = list(
            PlatformRole.objects.annotate(admin_count=Count("admins")).order_by("-is_super", "name")
            .prefetch_related("admins")
        )
    return render(request, "platform/roles.html", {
        "roles": rows,
        "total_perms": len(platform_perms.ALL),
    })


@login_required
@platform_perm("admins.manage")
def role_edit(request, pk=None):
    """
    Create or change a role by ticking what it may do.

    You can only tick what you can do yourself: otherwise anybody allowed to
    edit roles could write themselves a bigger one. Permissions the role
    already holds that you lack are kept, not silently dropped.
    """
    from apps.accounts.models import PlatformRole

    role = get_object_or_404(PlatformRole, pk=pk) if pk else None
    back = reverse("platform:roles")
    if role is not None and role.is_super:
        messages.error(request, "Super admin holds everything and cannot be changed.")
        return _close_modal_url(request, back)
    if role is not None and not request.user.is_super_admin:
        # Editing the role is editing the people who hold it: an admin could
        # not manage a stronger colleague directly, but could take
        # permissions off their role.
        stronger = next(
            (a for a in role.admins.all()
             if platform_perms.refusal_to_manage(request.user, a)),
            None,
        )
        if stronger is not None:
            messages.error(request, f"{stronger.name} holds this role and can do more than you. "
                                    "A Super admin changes it.")
            return _close_modal_url(request, back)

    mine = request.user.platform_permissions
    held = set(role.permissions) if role else set()
    errors = []

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        ticked = set(request.POST.getlist("perms")) & platform_perms.ALL
        chosen = (ticked & mine) | (held - mine)

        if not name:
            errors.append("Give the role a name.")
        elif PlatformRole.objects.filter(name__iexact=name).exclude(pk=getattr(role, "pk", None)).exists():
            errors.append(f"There is already a role called {name}.")
        if not chosen:
            errors.append("Tick at least one thing this role may do.")
        # Managing admins without seeing people is a role that cannot find
        # anybody to manage.
        if "admins.manage" in chosen and "people.view" not in chosen:
            chosen.add("people.view")

        if not errors:
            created = role is None
            before = set(role.permissions) if role else set()
            if role is None:
                role = PlatformRole(name=name)
            role.name, role.description = name, description
            role.permissions = sorted(chosen)
            role.save()
            audit.record_platform(
                request, "platform.role_created" if created else "platform.role_changed",
                role.name,
                granted=sorted(chosen - before), removed=sorted(before - chosen),
            )
            messages.success(request, f"{role.name} saved.")
            return _close_modal_url(request, back)
        held = ticked

    return _modal_or_redirect(
        request, "platform/_role_form.html",
        {"role": role, "catalogue": platform_perms.CATALOGUE, "held": held, "mine": mine,
         "errors": errors, "values": request.POST if errors else None},
        "platform:roles",
    )


@login_required
@platform_perm("admins.manage")
@require_POST
def role_delete(request, pk):
    """Remove a role nobody holds. Super admin can never go."""
    from apps.accounts.models import PlatformRole

    role = get_object_or_404(PlatformRole, pk=pk)
    in_use = role.admins.count()
    if role.is_super:
        messages.error(request, "Super admin cannot be removed.")
    elif in_use:
        messages.error(
            request,
            f"{in_use} admin{'s' if in_use != 1 else ''} still hold{'s' if in_use == 1 else ''} "
            f"{role.name}. Give them another role first.",
        )
    else:
        messages.success(request, f"{role.name} removed.")
        audit.record_platform(request, "platform.role_deleted", role.name,
                              permissions=role.permissions)
        role.delete()
    return redirect("platform:roles")


# --------------------------------------------------------------------------
# Modal plumbing
# --------------------------------------------------------------------------

def _modal_or_redirect(request, template, context, fallback):
    """
    Render a form into the modal, or as its own page without HTMX.

    The fallback matters: a form that only exists inside a modal is a form
    that cannot be opened in a new tab or reached when a script fails.
    """
    context = {**context, "modal": request.htmx}
    if request.htmx:
        return render(request, template, context)
    return render(request, "platform/modal_page.html",
                  {**context, "inner": template, "back": fallback})


def _next_url(request, fallback):
    """
    Where to go after an action: back where it was started from.

    Only paths inside the platform admin are accepted, so a crafted link
    cannot use this to bounce somebody off the site.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate.startswith("/platform/") and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}
    ):
        return candidate
    return fallback


def _close_modal_url(request, url):
    """Close the modal and load an explicit URL underneath."""
    from django.http import HttpResponse

    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _close_modal_to(request, redirect_to, **kwargs):
    """Close the modal and reload a page that takes arguments."""
    from django.http import HttpResponse

    target = reverse(redirect_to, kwargs=kwargs)
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


def _close_modal(request, redirect_to):
    """
    Close the modal and reload the list underneath it.

    HX-Redirect rather than swapping the row: after a create the list needs
    re-sorting and re-counting anyway, and a full reload is honest about
    what changed.
    """
    from django.http import HttpResponse

    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse(redirect_to)
        return response
    return redirect(redirect_to)


@login_required
@platform_perm("shops.view")
def tenant_quick_view(request, pk):
    """
    A shop at a glance, without leaving the list.

    Most of the time the question is "who are they and are they alright",
    which does not deserve a page load and a trip back. The full page is a
    click away for the times it does.
    """
    from apps.accounts.models import Membership
    from apps.core.features import ALL_LIMITS
    from apps.pos.models import Sale, SaleStatus

    with unscoped():
        tenant = get_object_or_404(
            Tenant.objects.select_related("subscription__plan"), pk=pk
        )
        sales = Sale.objects_all.filter(
            tenant=tenant,
            status__in=[SaleStatus.COMPLETED, SaleStatus.PART_REFUNDED],
        )
        context = {
            "tenant": tenant,
            "usage": [
                {"label": label, "used": tenant.usage_of(key),
                 "allowed": tenant.limit_for(key)}
                for key, label in ALL_LIMITS
                if key != "history_days"
            ],
            "owners": list(
                Membership.objects_all.select_related("user", "role")
                .filter(tenant=tenant, role__is_owner_role=True)[:3]
            ),
            "sales_count": sales.count(),
            "sales_value": sales.aggregate(t=Sum("total"))["t"] or Decimal("0"),
            "last_sale": sales.order_by("-sold_at").values_list(
                "sold_at", flat=True
            ).first(),
            "outstanding": tenant.invoices.exclude(
                status__in=[InvoiceStatus.PAID, InvoiceStatus.VOID]
            ).aggregate(t=Sum("total"))["t"] or Decimal("0"),
        }

    return _modal_or_redirect(
        request, "platform/_tenant_quick.html", context, "platform:tenant_list"
    )

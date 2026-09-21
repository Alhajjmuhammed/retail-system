"""
Reading the audit trail.

Forty-four places in this system write an audit row and, until now, nothing
could read one. That made the whole trail decorative: the point of recording
who voided a sale at 11pm is that somebody can go and look.
"""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.accounts.models import AuditLog, User
from apps.core.decorators import requires
from apps.core.parsing import int_or

# Actions worth surfacing on their own, in the order a shop cares about them.
DANGEROUS = [
    ("sale.voided", "Sales voided"),
    ("sale.returned", "Refunds"),
    ("stock.adjusted", "Stock adjusted"),
    ("role.permissions_changed", "Permissions changed"),
    ("staff.permissions_changed", "Exceptions changed"),
    ("platform.impersonated", "Support access"),
]


def visible_to(membership):
    """
    The activity this person may read: everything for an owner or someone
    across all branches; otherwise their branches plus shop-wide events.

    Taken by membership rather than by request because the dashboard shows
    the same rows in its activity feed and must scope them the same way.
    """
    rows = AuditLog.objects.all()
    if not (membership.role.is_owner_role or membership.all_branches):
        rows = rows.filter(Q(branch__isnull=True)
                           | Q(branch__in=membership.branches(include_closed=True)))
    return rows


def _visible(request):
    return visible_to(request.membership)


@login_required
@requires("report.staff")
def audit_log(request):
    """
    Who did what.

    Deliberately behind the staff-reports permission rather than a settings
    one: this is the screen an owner reads when the till is short, and it is
    about people.
    """
    visible = _visible(request)
    rows = visible.select_related("user", "authorised_by", "branch")

    term = request.GET.get("q", "").strip()
    action = request.GET.get("action", "")
    who = request.GET.get("who", "")
    days = min(max(int_or(request.GET.get("days"), 30), 1), 3650)

    since = timezone.now() - timedelta(days=days)
    rows = rows.filter(created_at__gte=since)

    action = action[:60]
    if action:
        rows = rows.filter(action=action)
    who = int_or(who) or ""
    if who:
        # A typed "who=abc" used to reach the database and fail as a 500.
        rows = rows.filter(user_id=who)
    if term:
        rows = rows.filter(
            Q(action__icontains=term)
            | Q(object_type__icontains=term)
            | Q(user__name__icontains=term)
        )

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    # The rows in words, for reading. `page` still holds the rows themselves,
    # for counting, paging and for anything that asks this view a question.
    from apps.core.audit import describe

    context = {
        "page": page,
        "entries": [describe(row) for row in page.object_list],
        "q": term,
        "action": action,
        "who": who,
        "days": days,
        "actions": (
            visible.filter(created_at__gte=since)
            .values("action")
            .annotate(count=Count("id"))
            .order_by("-count")[:40]
        ),
        "people": User.objects.filter(
            pk__in=visible.filter(created_at__gte=since).values("user")
        ).order_by("name"),
        "highlights": [
            {
                "label": label,
                "code": code,
                "count": visible.filter(action=code, created_at__gte=since).count(),
            }
            for code, label in DANGEROUS
        ],
    }

    if request.htmx:
        return render(request, "accounts/_audit_rows.html", context)
    return render(request, "accounts/audit.html", context)


@login_required
@requires("report.staff")
def audit_entry(request, pk):
    """One row in full: what it was before, and what it became."""
    # Through the same scope as the list: opening another branch's entry by
    # its number used to work.
    entry = get_object_or_404(
        _visible(request).select_related("user", "authorised_by", "branch"), pk=pk
    )

    changed = []
    keys = set(entry.before) | set(entry.after)
    for key in sorted(keys):
        was = entry.before.get(key)
        now = entry.after.get(key)
        if was != now:
            changed.append({"field": key, "was": was, "now": now})

    return render(
        request, "accounts/audit_entry.html", {"entry": entry, "changed": changed}
    )

"""
The stretch of time a page is describing.

Both dashboards offer the same four choices and compare against the same
number of days immediately before, so "up 12%" means the same thing on the
shop's front page as it does on the platform's.
"""

from datetime import timedelta

from django.utils import timezone

PERIODS = [("today", "Today"), ("7d", "Last 7 days"),
           ("30d", "Last 30 days"), ("month", "This month")]


def resolve(request, floor=None):
    """
    Read ``?range=`` and work out both periods.

    ``floor`` is the earliest date the reader may ask about -- a plan that
    keeps three months of history cannot be asked about the fourth. Anything
    unreadable falls back to the last seven days rather than to a 500 page.
    """
    today = timezone.localdate()
    key = request.GET.get("range", "7d")
    if key == "today":
        start = end = today
    elif key == "30d":
        start, end = today - timedelta(days=29), today
    elif key == "month":
        start, end = today.replace(day=1), today
    else:
        key, start, end = "7d", today - timedelta(days=6), today

    if floor and start < floor:
        start = min(max(floor, start), end)

    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    return {
        "key": key, "label": dict(PERIODS)[key], "start": start, "end": end,
        "days": days, "single_day": start == end,
        "previous_start": previous_end - timedelta(days=days - 1),
        "previous_end": previous_end,
    }

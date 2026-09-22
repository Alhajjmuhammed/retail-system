"""
The packages, as a shop owner reads them.

The plan table is the truth about what a shop may do; this turns it into the
handful of lines somebody decides from. Two rules shape it:

* Each package says what it *adds* to the one before, not everything it
  contains. Four columns of identical ticks tells a shopkeeper nothing, and
  the thing they are looking for is buried in it.
* Nothing here is written down twice. The prices, the limits and the
  features all come from the plan rows, so a plan edited on the platform
  side changes this page with it.
"""

from dataclasses import dataclass

from apps.core.features import (
    FEATURES_BY_KEY,
    LIMIT_BRANCHES,
    LIMIT_HISTORY_DAYS,
    LIMIT_PRODUCTS,
    LIMIT_USERS,
)
from apps.tenancy.models import Plan

# Priced per shop, never per person: a duka with four cashiers on one till
# should not pay four times.
POPULAR = "starter"


@dataclass
class Package:
    plan: Plan
    allowance: list       # "1 shop", "5 staff", ...
    adds: list            # what this package adds to the one before it
    inherits: str         # "Everything in Starter", or ""
    popular: bool

    @property
    def is_free(self):
        return not self.plan.price_monthly

    @property
    def months_free_on_annual(self):
        """12 months for the price of 10 is worth saying out loud."""
        if not self.plan.price_monthly or not self.plan.price_annual:
            return 0
        months = self.plan.price_annual / self.plan.price_monthly
        saved = 12 - months
        return int(saved) if saved >= 1 else 0


def _count(value, one, many=None):
    """``1 shop``, ``5 staff``, ``Unlimited products``."""
    many = many or one
    if value is None:
        return f"Unlimited {many}"
    return f"{value} {one if value == 1 else many}"


def _allowance(limits):
    branches = limits.get(LIMIT_BRANCHES)
    users = limits.get(LIMIT_USERS)
    products = limits.get(LIMIT_PRODUCTS)
    history = limits.get(LIMIT_HISTORY_DAYS)

    rows = [_count(branches, "shop", "shops"), _count(users, "staff account", "staff accounts")]
    rows.append(_count(products, "product", "products"))
    if history is None:
        rows.append("Sales history kept for good")
    elif history >= 365:
        years = history // 365
        rows.append(f"{years} year{'s' if years > 1 else ''} of sales history")
    else:
        rows.append(f"{history} days of sales history")
    return rows


def packages():
    """
    Every package offered at signup, cheapest first.

    One query for the plans and one each for their features and limits --
    this is a page that gets opened and never edited, so it should not cost
    a query per package.
    """
    plans = list(
        Plan.objects.filter(is_public=True)
        .prefetch_related("features", "limits")
        .order_by("sort_order", "price_monthly")
    )

    out, before = [], set()
    previous_name = ""
    for plan in plans:
        keys = {row.feature_key for row in plan.features.all()}
        limits = {row.key: row.value for row in plan.limits.all()}
        added = [
            FEATURES_BY_KEY[key].for_shops
            for key in sorted(keys - before)
            if key in FEATURES_BY_KEY
        ]
        out.append(
            Package(
                plan=plan,
                allowance=_allowance(limits),
                adds=added,
                inherits=f"Everything in {previous_name}" if previous_name else "",
                popular=plan.code == POPULAR,
            )
        )
        before |= keys
        previous_name = plan.name
    return out

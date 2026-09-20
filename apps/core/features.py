"""
Plan feature keys.

A feature is a string, a row on a plan, and nothing else. Turning one on for a
tenant is a database change, never a deploy and never a branch in the code.

There is no ``if plan == "business"`` anywhere in this system. If you are
tempted to write one, add a feature key here instead.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str = ""


MULTI_BRANCH = "multi_branch"
STOCK_TRANSFERS = "stock_transfers"
PURCHASING = "purchasing"
CUSTOMER_CREDIT = "customer_credit"
LOYALTY = "loyalty"
BATCH_EXPIRY = "batch_expiry"
FISCAL_RECEIPTS = "fiscal_receipts"
OFFLINE_POS = "offline_pos"
MOBILE_SELLING = "mobile_selling"
WHOLESALE_PRICING = "wholesale_pricing"
REPORT_EXPORT = "report_export"
SMS_NOTIFICATIONS = "sms_notifications"

ALL_FEATURES = [
    Feature(MULTI_BRANCH, "Multiple branches", "More than one shop under one business."),
    Feature(STOCK_TRANSFERS, "Stock transfers", "Move stock between branches."),
    Feature(PURCHASING, "Purchasing", "Purchase orders, suppliers and goods received."),
    Feature(CUSTOMER_CREDIT, "Customer credit", "Sell on account and track what is owed."),
    Feature(LOYALTY, "Loyalty", "Points earned and redeemed."),
    Feature(BATCH_EXPIRY, "Batches and expiry", "Track batch numbers and expiry dates."),
    Feature(FISCAL_RECEIPTS, "Fiscal receipts", "EFD/VFD submission to the revenue authority."),
    Feature(OFFLINE_POS, "Offline selling", "Keep selling when the connection drops."),
    Feature(MOBILE_SELLING, "Phone selling", "Build baskets in the aisle on a phone."),
    Feature(WHOLESALE_PRICING, "Wholesale pricing", "A second price list for bulk buyers."),
    Feature(REPORT_EXPORT, "Report export", "Download reports as CSV or PDF."),
    Feature(SMS_NOTIFICATIONS, "SMS", "Send receipts and alerts by SMS."),
]

FEATURES_BY_KEY = {f.key: f for f in ALL_FEATURES}


# --------------------------------------------------------------------------
# Limits -- the countable side of a plan
# --------------------------------------------------------------------------

LIMIT_BRANCHES = "branches"
LIMIT_USERS = "users"
LIMIT_PRODUCTS = "products"
LIMIT_HISTORY_DAYS = "history_days"

ALL_LIMITS = [
    (LIMIT_BRANCHES, "Branches"),
    (LIMIT_USERS, "Staff accounts"),
    (LIMIT_PRODUCTS, "Products"),
    (LIMIT_HISTORY_DAYS, "Days of sales history"),
]


class LimitExceeded(Exception):
    """Raised at write time when a tenant would exceed a plan limit."""

    def __init__(self, limit_key: str, allowed: int, label: str = ""):
        self.limit_key = limit_key
        self.allowed = allowed
        self.label = label or limit_key
        super().__init__(
            f"Your plan allows {allowed} {self.label.lower()}. "
            "Upgrade to add more."
        )

"""
Create the starting plans.

Priced per branch. Limits and features are rows, never code -- changing what a
plan includes is a database edit, not a deploy.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core import features as F
from apps.tenancy.models import Plan, PlanFeature, PlanLimit

PLANS = [
    {
        "code": "free", "name": "Free", "price_monthly": 0, "price_annual": 0,
        "sort_order": 0, "trial_days": 0,
        "description": "One shop, the basics, no card needed.",
        "limits": {F.LIMIT_BRANCHES: 1, F.LIMIT_USERS: 2, F.LIMIT_PRODUCTS: 200,
                   F.LIMIT_HISTORY_DAYS: 30},
        "features": [F.OFFLINE_POS],
    },
    {
        "code": "starter", "name": "Starter", "price_monthly": 25000,
        "price_annual": 250000, "sort_order": 1,
        "description": "One shop, full stock and purchasing.",
        "limits": {F.LIMIT_BRANCHES: 1, F.LIMIT_USERS: 5, F.LIMIT_PRODUCTS: None,
                   F.LIMIT_HISTORY_DAYS: 365},
        "features": [F.OFFLINE_POS, F.MOBILE_SELLING, F.PURCHASING,
                     F.CUSTOMER_CREDIT, F.BATCH_EXPIRY, F.FISCAL_RECEIPTS,
                     F.SMS_NOTIFICATIONS],
    },
    {
        "code": "business", "name": "Business", "price_monthly": 60000,
        "price_annual": 600000, "sort_order": 2,
        "description": "Up to five shops, transfers and full reporting.",
        "limits": {F.LIMIT_BRANCHES: 5, F.LIMIT_USERS: 20, F.LIMIT_PRODUCTS: None,
                   F.LIMIT_HISTORY_DAYS: None},
        "features": [F.OFFLINE_POS, F.MOBILE_SELLING, F.PURCHASING,
                     F.CUSTOMER_CREDIT, F.LOYALTY, F.BATCH_EXPIRY,
                     F.FISCAL_RECEIPTS, F.MULTI_BRANCH, F.STOCK_TRANSFERS,
                     F.WHOLESALE_PRICING, F.REPORT_EXPORT, F.SMS_NOTIFICATIONS],
    },
    {
        "code": "enterprise", "name": "Enterprise", "price_monthly": 150000,
        "price_annual": 1500000, "sort_order": 3,
        "description": "Unlimited shops and staff, every feature, priority support.",
        "limits": {F.LIMIT_BRANCHES: None, F.LIMIT_USERS: None,
                   F.LIMIT_PRODUCTS: None, F.LIMIT_HISTORY_DAYS: None},
        "features": [f.key for f in F.ALL_FEATURES],
    },
]


class Command(BaseCommand):
    help = "Create or update the standard subscription plans."

    @transaction.atomic
    def handle(self, *args, **options):
        for entry in PLANS:
            # Copy before popping: PLANS is module level, and mutating it here
            # means the command works once per process and fails after that.
            spec = dict(entry)
            limits = spec.pop("limits")
            feature_keys = spec.pop("features")
            code = spec.pop("code")

            plan, created = Plan.objects.update_or_create(code=code, defaults=spec)

            PlanLimit.objects.filter(plan=plan).delete()
            for key, value in limits.items():
                PlanLimit.objects.create(plan=plan, key=key, value=value)

            PlanFeature.objects.filter(plan=plan).delete()
            for key in feature_keys:
                PlanFeature.objects.create(plan=plan, feature_key=key)

            verb = "Created" if created else "Updated"
            self.stdout.write(
                f"{verb} {plan.name}: {len(feature_keys)} features, "
                f"{len(limits)} limits"
            )

        self.stdout.write(self.style.SUCCESS("Plans seeded."))

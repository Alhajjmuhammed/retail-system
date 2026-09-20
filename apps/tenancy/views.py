from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.core.decorators import requires
from apps.core.features import ALL_LIMITS


@login_required
@requires("billing.manage")
def billing(request):
    from django.utils import timezone

    from apps.core.features import FEATURES_BY_KEY

    tenant = request.tenant
    subscription = getattr(tenant, "subscription", None)

    usage = [
        {
            "key": key,
            "label": label,
            "used": tenant.usage_of(key),
            "allowed": tenant.limit_for(key),
        }
        for key, label in ALL_LIMITS
        if key != "history_days"
    ]
    for row in usage:
        # None is "no limit"; 0 is a real limit of nothing and used to read
        # as unlimited.
        row["unlimited"] = row["allowed"] is None
        if row["unlimited"]:
            row["pct"] = 0
        elif row["allowed"] == 0:
            row["pct"] = 100
        else:
            row["pct"] = min(int(row["used"] * 100 / row["allowed"]), 100)

    invoices = list(tenant.invoices.exclude(status="draft").order_by("-period_start")[:24])
    for invoice in invoices:
        invoice.left = invoice.outstanding if invoice.status == "open" else 0  # once each
    unpaid = [i for i in invoices if i.status == "open"]
    features = []
    if subscription is not None:
        for row in subscription.plan.features.all():
            feature = FEATURES_BY_KEY.get(row.feature_key)
            features.append(feature.label if feature else row.feature_key)

    return render(
        request,
        "tenancy/billing.html",
        {
            "subscription": subscription,
            "usage": usage,
            "invoices": invoices,
            "unpaid": unpaid,
            "owed": sum((i.left for i in unpaid), 0),
            "today": timezone.localdate(),
            "features": sorted(features),
        },
    )

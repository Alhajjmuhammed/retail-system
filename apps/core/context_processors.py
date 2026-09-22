def tenant(request):
    """Makes the resolved tenant, branch and membership available to templates."""
    return {
        "tenant": getattr(request, "tenant", None),
        "branch": getattr(request, "branch", None),
        "membership": getattr(request, "membership", None),
        "subscription": getattr(request, "subscription", None),
    }


def navigation(request):
    """
    The sidebar, as data.

    Built here rather than in the template so that "may I see it", "am I on
    it" and "what is it called" are one answer each, in one place, testable.
    """
    from apps.core.navigation import build

    if getattr(request, "tenant", None) is None:
        return {}
    return {"navigation": build(request)}


PLATFORM_NAV = [
    ("Overview", "platform:dashboard", "home", "dashboard.view"),
    ("Shops", "platform:tenant_list", "building", "shops.view"),
    ("People", "platform:user_list", "users", "people.view"),
    ("Admin roles", "platform:roles", "key", "admins.manage"),
    ("Invoices", "platform:invoices", "receipt", "invoices.view"),
    ("Plans", "platform:plans", "credit-card", "plans.view"),
    ("Permissions", "platform:permissions", "shield", "catalogue.view"),
    ("Devices", "platform:devices", "cart", "devices.view"),
    ("Health", "platform:health", "activity", "health.view"),
    ("Activity", "platform:platform_audit", "clipboard", "audit.view"),
]


def platform_nav(request):
    """
    Navigation for the platform admin shell, trimmed to the viewer's role.

    `pp` is their permission set, so templates can hide what they cannot do:
    {% if "shops.delete" in pp %}. The views check again regardless.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_platform_staff", False):
        return {"pp": frozenset()}
    granted = user.platform_permissions
    return {
        "pp": granted,
        "nav_items": [
            (label, name, icon) for label, name, icon, code in PLATFORM_NAV if code in granted
        ],
    }

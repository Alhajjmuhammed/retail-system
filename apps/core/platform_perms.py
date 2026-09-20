"""
What a member of the platform team may do.

Shops have their own, tenant-built roles. This is the other side: your own
staff, who can see every shop. Not all of them should be able to delete one,
issue invoices, or open a shop's till for support, so each area of the admin
is split into what can be seen and what can be changed.

A role holds a set of these codes. The Super admin role holds all of them,
now and in future, and cannot be edited or removed.
"""

CATALOGUE = [
    ("Overview", [
        ("dashboard.view", "See the overview", "Revenue, shops at risk and recent activity."),
    ]),
    ("Shops", [
        ("shops.view", "See shops", "The list, each shop's page and its staff."),
        ("shops.create", "Set up new shops", "Create a shop and its owner."),
        ("shops.edit", "Edit shops", "Change a shop's details and its plan."),
        ("shops.suspend", "Suspend and reactivate shops", "Stop a shop from selling, or let it trade again."),
        ("shops.delete", "Delete shops", "Remove a shop outright, or archive one that has traded."),
        ("shops.staff", "Manage a shop's staff", "Add people to a shop, change their role, remove them."),
        ("shops.support", "Open a shop for support", "Sign in to a shop as its owner to help them."),
    ]),
    ("People", [
        ("people.view", "See people", "Everyone with an account, across every shop."),
        ("people.manage", "Manage people", "Edit details, set passwords, deactivate and reactivate."),
        ("people.delete", "Delete accounts", "Only accounts that were never used."),
    ]),
    ("Platform team", [
        ("admins.manage", "Manage platform admins and roles",
         "Add admins, choose their role, and edit roles. Never beyond your own permissions."),
    ]),
    ("Invoices", [
        ("invoices.view", "See invoices", "What every shop has been billed and paid."),
        ("invoices.manage", "Manage invoices", "Issue, correct, void and record payments."),
    ]),
    ("Plans", [
        ("plans.view", "See plans", "Prices, features and limits."),
        ("plans.manage", "Manage plans", "Create, change and remove plans."),
    ]),
    ("Permission catalogue", [
        ("catalogue.view", "See the shop permission catalogue", "What a shop's roles can be built from."),
    ]),
    ("Devices", [
        ("devices.view", "See devices", "Every till and scanner registered to a shop."),
        ("devices.manage", "Manage devices", "Rename, retire and remove devices."),
    ]),
    ("Health", [
        ("health.view", "See system health", "Queues, failures and background jobs."),
        ("health.manage", "Retry failed fiscal submissions", "Send stuck receipts to the tax authority again."),
    ]),
    ("Activity", [
        ("audit.view", "See platform activity", "Everything done in the platform admin, and by whom."),
    ]),
]

ALL = frozenset(code for _, perms in CATALOGUE for code, _, _ in perms)
LABELS = {code: label for _, perms in CATALOGUE for code, label, _ in perms}

# Roles every installation starts with. Editable afterwards, except Super admin.
DEFAULT_ROLES = [
    ("Super admin", "Everything, including future permissions. Cannot be changed.", True, []),
    ("Support", "Helps shops day to day: finds people, resets passwords, opens a shop to help.", False, [
        "dashboard.view", "shops.view", "shops.staff", "shops.support",
        "people.view", "people.manage", "devices.view", "health.view", "audit.view",
    ]),
    ("Billing", "Handles plans, invoices and payments.", False, [
        "dashboard.view", "shops.view", "shops.suspend", "invoices.view", "invoices.manage",
        "plans.view",
    ]),
    ("Read only", "Can look at everything and change nothing.", False, [
        code for code in sorted(ALL) if code.endswith(".view")
    ]),
]


def refusal_to_manage(actor, person):
    """
    Why `actor` may not change `person`'s account, or None if they may.

    Resetting another admin's password is a way to become them. So touching
    a platform admin needs the right to manage admins, and touching a Super
    admin needs to be one.
    """
    if person.pk == actor.pk or not person.is_platform_staff:
        return None
    if not actor.has_platform_perm("admins.manage"):
        return "Only someone who manages platform admins can change another admin's account."
    if person.is_super_admin and not actor.is_super_admin:
        return "Only a Super admin can change a Super admin's account."
    if not person.platform_role_permissions <= actor.platform_permissions:
        # Resetting a stronger admin's password is a way to become them.
        return "They can do things you cannot, so you cannot change their account."
    return None

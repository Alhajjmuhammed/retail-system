"""Start every installation with the default platform roles."""

from django.db import migrations

# Frozen here rather than imported, so this migration means the same thing
# however the live catalogue changes later.
VIEW_ALL = [
    "audit.view", "catalogue.view", "dashboard.view", "devices.view", "health.view",
    "invoices.view", "people.view", "plans.view", "shops.view",
]
ROLES = [
    ("Super admin", "Everything, including future permissions. Cannot be changed.", True, []),
    ("Support", "Helps shops day to day: finds people, resets passwords, opens a shop to help.", False, [
        "dashboard.view", "shops.view", "shops.staff", "shops.support",
        "people.view", "people.manage", "devices.view", "health.view", "audit.view",
    ]),
    ("Billing", "Handles plans, invoices and payments.", False, [
        "dashboard.view", "shops.view", "shops.suspend", "invoices.view", "invoices.manage",
        "plans.view",
    ]),
    ("Read only", "Can look at everything and change nothing.", False, VIEW_ALL),
]


def seed(apps, schema_editor):
    PlatformRole = apps.get_model("accounts", "PlatformRole")
    User = apps.get_model("accounts", "User")

    for name, description, is_super, permissions in ROLES:
        PlatformRole.objects.get_or_create(
            name=name,
            defaults={"description": description, "is_super": is_super,
                      "permissions": permissions},
        )

    # Everybody who was an admin before roles existed could do everything.
    # Keep it that way until somebody decides otherwise.
    super_role = PlatformRole.objects.get(is_super=True)
    User.objects.filter(is_platform_staff=True, platform_role__isnull=True).update(
        platform_role=super_role
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_platform_roles")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

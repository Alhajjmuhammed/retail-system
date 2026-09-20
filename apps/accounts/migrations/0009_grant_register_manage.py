"""
Tills and devices moved from "Manage branches" to their own permission.

Every role that could manage branches keeps managing its tills, so nobody
loses access when the new permission appears.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    RolePermission = apps.get_model("accounts", "RolePermission")
    new, _ = Permission.objects.get_or_create(
        code="register.manage",
        defaults={"module": "Administration", "label": "Manage tills and devices"},
    )
    rows = RolePermission.objects.filter(permission__code="branch.manage", granted=True)
    for row in rows:
        RolePermission.objects.get_or_create(
            role_id=row.role_id, permission=new,
            defaults={"granted": True},
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_copy_support_sessions")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

"""
Tills and devices for the people who run the shop day to day.

New shops' Manager role includes "Manage tills and devices"; roles in shops
created earlier did not, so their managers could not switch off a lost
phone. Any role that can change business settings gets it now.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    RolePermission = apps.get_model("accounts", "RolePermission")
    new = Permission.objects.filter(code="register.manage").first()
    if new is None:
        return
    for row in RolePermission.objects.filter(permission__code="settings.edit", granted=True):
        RolePermission.objects.get_or_create(role_id=row.role_id, permission=new,
                                             defaults={"granted": True})


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_grant_register_manage")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

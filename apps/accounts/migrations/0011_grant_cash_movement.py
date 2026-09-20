"""
Taking money out of the till becomes its own, limited permission.

It used to ride along with "close a till", unlimited, so a cashier could
record a huge pay-out and leave the cash-up looking perfect. Existing roles
keep being able to do it, but within the ceiling they already have for
spending money (or a modest default).
"""

from decimal import Decimal

from django.db import migrations

DEFAULT_LIMIT = Decimal("50000")


def forwards(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    RolePermission = apps.get_model("accounts", "RolePermission")
    new = Permission.objects.filter(code="cash.movement").first()
    if new is None:
        return
    for row in RolePermission.objects.filter(permission__code="cashup.perform", granted=True):
        spend = RolePermission.objects.filter(
            role_id=row.role_id, permission__code="expense.create", granted=True
        ).first()
        RolePermission.objects.get_or_create(
            role_id=row.role_id, permission=new,
            defaults={"granted": True,
                      "limit_value": spend.limit_value if spend and spend.limit_value
                      else DEFAULT_LIMIT},
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0010_grant_register_manage_to_managers")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

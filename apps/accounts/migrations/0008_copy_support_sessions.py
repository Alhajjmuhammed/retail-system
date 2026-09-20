"""
Copy support sessions logged before the platform activity log existed.

They were only in each shop's own log. With them copied, the activity page
reads one table and pages through all of it in the database.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    AuditLog = apps.get_model("accounts", "AuditLog")
    PlatformEvent = apps.get_model("accounts", "PlatformEvent")
    first = PlatformEvent.objects.order_by("created_at").values_list("created_at", flat=True).first()
    rows = AuditLog.objects.filter(action="platform.impersonated").select_related("tenant")
    if first:
        rows = rows.filter(created_at__lt=first)
    for row in rows:
        event = PlatformEvent.objects.create(
            user_id=row.user_id, action=row.action, target=row.tenant.name, ip=row.ip,
        )
        PlatformEvent.objects.filter(pk=event.pk).update(created_at=row.created_at)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_branches_backfill")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

"""Row-level security for the notifications tables."""

from django.core.management import call_command
from django.db import migrations


def apply_rls(apps, schema_editor):
    call_command("apply_rls", verbosity=0)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_rls_all_apps"),
        ("notifications", "0001_initial"),
    ]

    operations = [migrations.RunPython(apply_rls, noop)]

"""Re-apply row-level security now that inventory and purchasing tables exist."""

from django.core.management import call_command
from django.db import migrations


def apply_rls(apps, schema_editor):
    call_command("apply_rls", verbosity=0)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_row_level_security"),
        ("inventory", "0002_initial"),
        ("purchasing", "0001_initial"),
    ]

    operations = [migrations.RunPython(apply_rls, noop)]

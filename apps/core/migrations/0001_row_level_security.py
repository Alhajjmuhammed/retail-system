"""
Turn on row-level security once the tenant tables exist.

Re-run ``manage.py apply_rls`` after adding an app; this migration only covers
the tables present when it runs.
"""

from django.core.management import call_command
from django.db import migrations


def apply_rls(apps, schema_editor):
    call_command("apply_rls", verbosity=0)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_initial"),
        ("catalog", "0002_initial"),
        ("org", "0001_initial"),
        ("tenancy", "0001_initial"),
    ]

    operations = [migrations.RunPython(apply_rls, noop)]

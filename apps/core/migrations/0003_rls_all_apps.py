"""Re-apply row-level security after the POS, customers and finance tables."""

from django.core.management import call_command
from django.db import migrations


def apply_rls(apps, schema_editor):
    call_command("apply_rls", verbosity=0)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_rls_inventory_purchasing"),
        ("pos", "0001_initial"),
        ("customers", "0002_initial"),
        ("finance", "0001_initial"),
    ]

    operations = [migrations.RunPython(apply_rls, noop)]

"""Existing refunds went back entirely one way; record them that way."""

from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    Return = apps.get_model("pos", "Return")
    Return.objects.filter(method="cash").update(cash_amount=F("total"))
    Return.objects.filter(method="credit").update(credit_amount=F("total"))
    Return.objects.exclude(method__in=["cash", "credit"]).update(other_amount=F("total"))


class Migration(migrations.Migration):
    dependencies = [("pos", "0003_return_split")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

from django.db import migrations, models

import apps.org.models


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0009_who_sets_the_price_on_paperwork"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantsettings",
            name="print_columns",
            field=models.JSONField(
                blank=True, default=apps.org.models.default_print_columns),
        ),
    ]

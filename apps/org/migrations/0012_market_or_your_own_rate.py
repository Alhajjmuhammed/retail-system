from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0011_rates_for_other_currencies"),
    ]

    operations = [
        migrations.AddField(
            model_name="exchangerate",
            name="fetched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exchangerate",
            name="source",
            field=models.CharField(
                choices=[("market", "Market"), ("manual", "Yours")],
                default="market",
                max_length=10,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customers", "0006_customer_zra_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="credittransaction",
            name="doc_amount",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=14, null=True
            ),
        ),
    ]

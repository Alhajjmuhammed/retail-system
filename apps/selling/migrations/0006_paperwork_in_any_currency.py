from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("selling", "0005_a_draft_is_a_proforma"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="currency",
            field=models.CharField(blank=True, max_length=3),
        ),
        migrations.AddField(
            model_name="invoice",
            name="exchange_rate",
            field=models.DecimalField(decimal_places=6, default=1, max_digits=18),
        ),
        migrations.AddField(
            model_name="quotation",
            name="currency",
            field=models.CharField(blank=True, max_length=3),
        ),
        migrations.AddField(
            model_name="quotation",
            name="exchange_rate",
            field=models.DecimalField(decimal_places=6, default=1, max_digits=18),
        ),
        # Every document written so far was in its shop's own currency.
        migrations.RunSQL(
            sql=[
                "UPDATE selling_invoice d SET currency = t.currency "
                "FROM tenancy_tenant t WHERE t.id = d.tenant_id AND d.currency = ''",
                "UPDATE selling_quotation d SET currency = t.currency "
                "FROM tenancy_tenant t WHERE t.id = d.tenant_id AND d.currency = ''",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

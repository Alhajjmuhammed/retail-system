"""
Room for a discount: per line, and on the whole document.

Both default to zero, so every quotation and invoice already written
means exactly what it meant before.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('selling', '0002_invoice_deliverynote_invoiceline_deliveryline'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='invoiceline',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='quotation',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='quotationline',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
    ]

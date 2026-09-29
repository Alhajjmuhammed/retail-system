"""
Keep the number a proforma went out with.

`reference` becomes the invoice number when it is issued, so the proforma
number needs a column of its own -- otherwise the sequence would hand it
straight to the next customer.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('selling', '0003_room_for_a_discount'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='proforma_reference',
            field=models.CharField(blank=True, db_index=True, max_length=30),
        ),
    ]

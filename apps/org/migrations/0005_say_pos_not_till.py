"""
The word on screen only: a device is a POS now, not a till.

The stored value stays "till", because it is what every existing row
holds and renaming data to match a label is how history gets lost.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org', '0004_drop_the_switch_that_did_nothing'),
    ]

    operations = [
        migrations.AlterField(
            model_name='device',
            name='kind',
            field=models.CharField(choices=[('till', 'POS'), ('phone', 'Phone')], default='till', max_length=10),
        ),
        migrations.AlterField(
            model_name='tenantsettings',
            name='negative_stock_allowed',
            field=models.BooleanField(default=True, help_text='Offline POS can oversell. Blocking it loses real sales.'),
        ),
    ]

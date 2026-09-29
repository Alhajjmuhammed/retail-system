"""
The other codes one product is known by.

The factory prints one on the carton, the shop used another before the
last stock-take, and the supplier's list has a third. All three are
searchable, because a customer rings quoting whichever one they have.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_drop_the_switch_that_did_nothing'),
        ('purchasing', '0003_payment_cash_movement'),
        ('tenancy', '0002_free_shops_are_not_on_trial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='factory_code',
            field=models.CharField(blank=True, db_index=True, max_length=40),
        ),
        migrations.AddField(
            model_name='product',
            name='old_code',
            field=models.CharField(blank=True, db_index=True, max_length=40),
        ),
        migrations.AddField(
            model_name='product',
            name='supplier',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='products', to='purchasing.supplier'),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(fields=['tenant', 'factory_code'], name='catalog_pro_tenant__f3ac8c_idx'),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(fields=['tenant', 'old_code'], name='catalog_pro_tenant__bb461b_idx'),
        ),
    ]

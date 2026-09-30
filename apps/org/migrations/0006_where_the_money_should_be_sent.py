"""
Where a customer should send the money.

Several accounts per shop: one invoice asks for shillings, another for
dollars, and the account printed has to be the one that can take it.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org', '0005_say_pos_not_till'),
        ('tenancy', '0002_free_shops_are_not_on_trial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BankAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bank_name', models.CharField(max_length=120)),
                ('branch', models.CharField(blank=True, max_length=120)),
                ('account_name', models.CharField(max_length=160)),
                ('account_number', models.CharField(max_length=60)),
                ('swift_code', models.CharField(blank=True, max_length=20, verbose_name='SWIFT code')),
                ('currency', models.CharField(default='TZS', max_length=3)),
                ('is_active', models.BooleanField(default=True)),
                ('position', models.PositiveIntegerField(default=0)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.CASCADE, related_name='%(app_label)s_%(class)s_set', to='tenancy.tenant')),
            ],
            options={
                'ordering': ['position', 'bank_name'],
            },
        ),
    ]

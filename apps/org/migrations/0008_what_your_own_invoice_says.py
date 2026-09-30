"""
What a shop's own invoice says, and what it shows.

Terms differ from shop to shop -- when payment is due, who owns the goods
until it arrives -- so they are typed in Settings rather than written into
the system, and each switch decides whether that part prints at all.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org', '0007_pictures_on_paperwork_are_a_choice'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantsettings',
            name='invoice_terms',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='tenantsettings',
            name='show_amount_in_words',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='tenantsettings',
            name='show_bank_details',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='tenantsettings',
            name='show_terms_on_invoice',
            field=models.BooleanField(default=True),
        ),
    ]

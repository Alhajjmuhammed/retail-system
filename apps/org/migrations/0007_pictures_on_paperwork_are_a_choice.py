"""
Whether a quotation or invoice carries product pictures.

Some shops sell things that look alike and want them; others want plain
paperwork. Picking a product always shows its picture -- that is about
choosing the right one, not about the document.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org', '0006_where_the_money_should_be_sent'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantsettings',
            name='show_product_images',
            field=models.BooleanField(default=True),
        ),
    ]

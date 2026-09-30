"""
The word on screen: a draft invoice is a proforma.

The stored value stays "draft", because it is what every existing row
holds and the document has not changed -- only what it is called.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('selling', '0004_keep_the_proforma_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='status',
            field=models.CharField(choices=[('draft', 'Proforma'), ('open', 'Owed'), ('part_paid', 'Part paid'), ('paid', 'Paid'), ('void', 'Cancelled')], default='draft', max_length=10),
        ),
    ]

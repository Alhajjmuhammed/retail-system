from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0008_what_your_own_invoice_says"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantsettings",
            name="lock_document_prices",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="tenantsettings",
            name="max_document_discount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("100"), max_digits=5,
                validators=[MinValueValidator(0), MaxValueValidator(100)]),
        ),
    ]

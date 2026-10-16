import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.core.management import call_command
from django.db import migrations, models


def protect_the_new_table(apps, schema_editor):
    # A table with a tenant_id gets the same row-level security as every
    # other; run here so a deploy cannot leave it open by forgetting.
    call_command("apply_rls", verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0010_choose_what_the_paper_shows"),
        ("tenancy", "0003_tenant_website"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExchangeRate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=3)),
                (
                    "rate",
                    models.DecimalField(
                        decimal_places=6,
                        max_digits=18,
                        validators=[
                            django.core.validators.MinValueValidator(
                                Decimal("0.000001")
                            )
                        ],
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s_set",
                        to="tenancy.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["code"],
                "unique_together": {("tenant", "code")},
            },
        ),
        migrations.RunPython(protect_the_new_table, migrations.RunPython.noop),
    ]

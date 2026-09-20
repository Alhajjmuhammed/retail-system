"""
Push the code-declared permission catalogue into the database.

Run on every deploy. New permissions appear for the role builder, default to
denied for every custom role, and are automatically held by Owner. Existing
tenant roles are never touched -- a shop's own permission choices survive
upgrades, which is the whole point of separating vocabulary from grants.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Permission
from apps.core.permissions import registry


class Command(BaseCommand):
    help = "Sync the permission catalogue from code into the database."

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = 0

        for order, spec in enumerate(registry.all()):
            _, was_created = Permission.objects.update_or_create(
                code=spec.code,
                defaults={
                    "module": spec.module,
                    "label": spec.label,
                    "value_type": str(spec.value_type),
                    "is_dangerous": spec.is_dangerous,
                    "requires_feature": spec.requires_feature or "",
                    "sort_order": order,
                },
            )
            created += was_created
            updated += not was_created

        # A permission removed from the code is removed from every role too.
        stale = Permission.objects.exclude(code__in=registry.codes())
        stale_count = stale.count()
        stale.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Permissions synced: {created} new, {updated} updated, "
                f"{stale_count} removed."
            )
        )

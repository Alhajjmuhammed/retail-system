"""
Apply Postgres row-level security to every tenant-owned table.

This is the second lock. The managers in ``core.managers`` are the first and
do the real work; RLS is what still refuses if application code ever reaches
the database another way -- a raw cursor, a ``.extra()``, a hand-written
report query, a third-party package that does not know about tenants.

Any table with a ``tenant_id`` column is covered, so a new app is protected by
running this command rather than by remembering to add it to a list.

The policy allows unrestricted access when ``app.tenant_id`` is unset. That is
deliberate: migrations, management commands, Celery beat and the platform
admin all legitimately work across tenants, and they run with nothing bound.
``TenantMiddleware`` binds it on every tenant-facing request, which is where
the risk actually lives.

To harden further in production, give the web process its own database role
that is not the table owner and cannot ``SET app.tenant_id`` to empty, and let
only the platform admin connect as the owner.
"""

from django.core.management.base import BaseCommand
from django.db import connection

FIND_TENANT_TABLES = """
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_name = c.table_name AND t.table_schema = c.table_schema
    WHERE c.table_schema = 'public'
      AND c.column_name = 'tenant_id'
      AND t.table_type = 'BASE TABLE'
    ORDER BY c.table_name;
"""

POLICY_NAME = "tenant_isolation"

APPLY = """
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {policy} ON {table};
    CREATE POLICY {policy} ON {table}
        USING (
            NULLIF(current_setting('app.tenant_id', true), '') IS NULL
            OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
        )
        WITH CHECK (
            NULLIF(current_setting('app.tenant_id', true), '') IS NULL
            OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
        );
"""


class Command(BaseCommand):
    help = "Enable row-level security on every table with a tenant_id column."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the tables that would be covered and make no changes.",
        )

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(FIND_TENANT_TABLES)
            tables = [row[0] for row in cursor.fetchall()]

            if options["list"]:
                for table in tables:
                    self.stdout.write(f"  {table}")
                self.stdout.write(f"{len(tables)} tenant tables found.")
                return

            for table in tables:
                cursor.execute(
                    APPLY.format(table=f'"{table}"', policy=POLICY_NAME)
                )

        self.stdout.write(
            self.style.SUCCESS(f"Row-level security applied to {len(tables)} tables.")
        )

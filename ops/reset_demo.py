"""
Throw away the demo data and build a fresh demo shop.

Development only, and deliberately not a management command: it removes every
shop in the database, so it should be something you run on purpose rather than
something that sits next to `seed_demo` waiting to be typed by accident.

    .venv/bin/python manage.py shell < ops/reset_demo.py

Take a dump first if the database holds anything you would miss:

    podman exec retail-db pg_dump -U retail retail > before.sql
"""

from django.core.management import call_command

from apps.accounts.models import User
from apps.core.context import unscoped
from apps.tenancy.models import Tenant

with unscoped():
    shops = list(Tenant.objects.all())
    if any(not s.name for s in shops):
        raise SystemExit("Unnamed shop found; stopping rather than guessing.")

    demo_only = all(
        u.email.endswith("@demo.test") for u in User.objects.all()
    )
    if not demo_only:
        real = [u.email for u in User.objects.exclude(email__endswith="@demo.test")]
        raise SystemExit(
            "This database has accounts that are not demo accounts, so it is "
            f"not a demo database: {real[:5]}. Stopping."
        )

    print(f"removing {len(shops)} shop(s): {[s.name for s in shops]}")
    for shop in shops:
        # Sales point at users and variants with PROTECT, so the shop's own
        # rows have to go before the people do.
        shop.delete()
    removed = User.objects.all().delete()[0]
    print(f"removed {removed} account(s)")

call_command("sync_permissions")
call_command("seed_plans")
call_command("seed_demo")

"""
Throw away the demo data and build a fresh demo shop.

Development only, and deliberately not a management command: it empties every
shop in the database, so it should be something you run on purpose rather than
something sitting next to `seed_demo` waiting to be typed by accident.

    .venv/bin/python manage.py shell < ops/reset_demo.py

Take a dump first if the database holds anything you would miss:

    podman exec retail-db pg_dump -U retail retail > before.sql

Note that the platform's own "delete shop" is not this. That one refuses a
shop that has ever traded, on purpose: its records are somebody's answer to a
question later. This is for a development database full of nonsense.
"""

from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import transaction
from django.db.models import ProtectedError

from apps.accounts.models import User
from apps.core.context import unscoped
from apps.tenancy.models import Tenant


def tenant_models():
    """Every model that belongs to a shop, most dependent first-ish."""
    found = []
    for model in django_apps.get_models():
        if any(f.name == "tenant" for f in model._meta.fields):
            found.append(model)
    # Lines before the things they hang off, as a starting order.
    return sorted(found, key=lambda m: m.__name__, reverse=True)


def empty_the_shops():
    """
    Delete every shop's rows, letting the database decide the order.

    Half these tables protect each other -- a sale line protects the product
    it names, a membership protects the role it points at -- so rather than
    keeping a hand-written order in step with the models, this keeps sweeping
    until a pass removes nothing.
    """
    models = tenant_models()
    while True:
        removed_this_pass = 0
        for model in list(models):
            try:
                with transaction.atomic():
                    count = model._base_manager.all().delete()[0]
            except ProtectedError:
                continue            # something still points at these; later
            removed_this_pass += count
            if not model._base_manager.exists():
                models.remove(model)
        if not removed_this_pass:
            return models


with unscoped():
    if not User.objects.exists():
        print("nothing here yet")
    elif not all(u.email.endswith("@demo.test") for u in User.objects.all()):
        real = list(
            User.objects.exclude(email__endswith="@demo.test")
            .values_list("email", flat=True)[:5]
        )
        raise SystemExit(
            f"This database has accounts that are not demo accounts: {real}. "
            "That makes it somebody's real data, so this script stops here."
        )

    shops = list(Tenant.objects.values_list("name", flat=True))
    print(f"emptying {len(shops)} shop(s): {shops}")
    stuck = empty_the_shops()
    if stuck:
        raise SystemExit(f"Could not empty: {[m.__name__ for m in stuck]}")

    Tenant.objects.all().delete()
    gone = User.objects.all().delete()[0]
    print(f"removed every shop and {gone} account row(s)")

call_command("sync_permissions")
call_command("seed_plans")
call_command("seed_demo")

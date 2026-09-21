"""
A demo shop to click around in.

One business on the Business plan with two branches, somebody in every role,
and a shelf worth of stock: products priced and counted, barcodes to scan
(including a crate that sells 24 in one beep), tiles for the things that have
no barcode at all, a customer on account at wholesale prices, and a supplier.
Enough to see how the whole thing behaves without typing anything in first.

Development only. It refuses to run against a database that already has real
tenants unless --force is given.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Membership, Role, User
from apps.core.context import tenant_context, unscoped
from apps.org.models import Branch
from apps.tenancy.models import Plan, Tenant
from apps.tenancy.services import create_tenant

PASSWORD = "demo12345"

STAFF = [
    ("manager@demo.test", "Asha Mohamed", "Manager", "1234"),
    ("cashier@demo.test", "Juma Ally", "Cashier", ""),
    ("stock@demo.test", "Neema Said", "Stock clerk", ""),
]

# Roles a shop builds for itself, to show that roles are not a fixed list.
# Each is a job somebody actually does behind a counter.
EXTRA_ROLES = [
    ("Phone seller", "phone@demo.test", "Rehema Phone",
     # A ceiling with no method allowed is a person who can fill a basket and
     # then not take a shilling for it, so the methods come with it.
     {"pos.mobile_cart": None, "pos.mobile_payment": Decimal("200000"),
      "pos.mobile_methods": ["mpesa", "tigopesa", "airtelmoney"],
      "product.view": None}),
    ("Buyer", "buyer@demo.test", "Hamisi Buyer",
     {"po.manage": None, "po.approve": Decimal("1000000"), "supplier.manage": None,
      "supplier.pay": Decimal("1000000"), "product.view": None}),
    ("Bookkeeper", "books@demo.test", "Zainabu Books",
     {"expense.create": Decimal("100000"), "expense.approve": None,
      "cashup.approve": None, "credit.collect": None, "report.sales": None,
      "report.margin": None, "report.stock": None, "report.staff": None,
      "report.export": None, "product.view_cost": None}),
    # Somebody whose role has not been filled in yet, to show what they see.
    ("New role", "empty@demo.test", "Baraka New", {}),
]

# name, unit, price, cost, opening stock, barcode, crate barcode (sells 24)
SHELF = [
    ("Sukari 1kg", "kg", 3000, 2400, 60, "6001234567890", None),
    ("Mkate Mkubwa", "pc", 1500, 1100, 40, "6001111111111", None),
    ("Soda 500ml", "pc", 1000, 700, 120, "6002222222222", "6002222222229"),
    ("Mchele 5kg", "kg", 14000, 11500, 25, "6003333333333", None),
    ("Sabuni", "pc", 2500, 1900, 80, "6004444444444", None),
    ("Mafuta 1L", "l", 6500, 5400, 30, "6005555555555", None),
    # No barcode at all: these reach the basket by tile or by name, which is
    # most of what a small shop actually sells.
    ("Nyanya (kg)", "kg", 2000, 1400, 0, None, None),
    ("Mkaa (debe)", "pc", 12000, 9000, 0, None, None),
]


class Command(BaseCommand):
    help = "Create a demo shop with staff, for development."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        with unscoped():
            existing = Tenant.objects.count()
        if existing and not options["force"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{existing} tenant(s) already exist. Re-run with --force."
                )
            )
            return

        # The platform account is yours, not a shop's. It belongs to no tenant
        # and reaches /platform/ instead of the shop screens.
        from apps.accounts.models import PlatformRole

        staff, _ = User.objects.get_or_create(
            email="platform@demo.test",
            defaults={"name": "Platform Admin", "is_platform_staff": True,
                      "is_staff": True, "is_superuser": True},
        )
        staff.is_platform_staff = True
        staff.is_staff = True
        staff.is_superuser = True
        PlatformRole.ensure_defaults()
        staff.platform_role = PlatformRole.super_role()
        staff.set_password(PASSWORD)
        staff.save()

        # One team member per limited role, to see what each role is shown.
        for email, name, role_name in [
            ("support@demo.test", "Sam Support", "Support"),
            ("billing@demo.test", "Bella Billing", "Billing"),
        ]:
            member, _ = User.objects.get_or_create(email=email, defaults={"name": name})
            member.is_platform_staff = True
            member.platform_role = PlatformRole.objects.filter(name=role_name).first()
            member.set_password(PASSWORD)
            member.save()

        owner, _ = User.objects.get_or_create(
            email="owner@demo.test", defaults={"name": "Salma Juma"}
        )
        owner.set_password(PASSWORD)
        owner.save()

        tenant, _ = create_tenant(
            name="Duka la Salma",
            owner=owner,
            plan=Plan.objects.get(code="business"),
            branch_name="Stone Town",
        )

        with tenant_context(tenant, user=owner):
            nungwi = Branch.objects.create(name="Nungwi")
            stone_town = Branch.objects.get(name="Stone Town")

            for email, name, role_name, pin in STAFF:
                user, _ = User.objects.get_or_create(
                    email=email, defaults={"name": name}
                )
                user.set_password(PASSWORD)
                user.save()

                membership = Membership.objects.create(
                    tenant=tenant, user=user, role=Role.objects.get(name=role_name)
                )
                membership.branch_links.create(branch=stone_town)
                if role_name == "Manager":
                    membership.branch_links.create(branch=nungwi)
                if pin:
                    membership.set_pin(pin)

            self._stock_the_shop(tenant, stone_town, owner)
            self._extra_roles(tenant, stone_town)
            self._trade(tenant, stone_town, owner)

        self.stdout.write(self.style.SUCCESS(f"\n{tenant.name} created.\n"))
        self.stdout.write(f"  Password for everyone: {PASSWORD}\n")
        self.stdout.write("  owner@demo.test    Owner       full access")
        self.stdout.write("  manager@demo.test  Manager     PIN 1234, both branches")
        self.stdout.write("  cashier@demo.test  Cashier     5% discount cap")
        self.stdout.write("  stock@demo.test    Stock clerk no till access")
        self.stdout.write("  phone@demo.test    Phone seller sells from a phone")
        self.stdout.write("  buyer@demo.test    Buyer       orders and pays suppliers")
        self.stdout.write("  books@demo.test    Bookkeeper  expenses and reports")
        self.stdout.write("  empty@demo.test    New role    holds nothing yet")
        self.stdout.write(
            self.style.WARNING(
                "\n  platform@demo.test  Platform admin  /platform/ — yours, not a shop's\n"
            )
        )

    # -- the shop itself ---------------------------------------------------

    def _stock_the_shop(self, tenant, branch, owner):
        """Products, prices, barcodes, tiles, stock, a customer and a supplier."""
        from apps.catalog.models import (
            Barcode,
            Price,
            PriceList,
            PriceListKind,
            Product,
            QuickKey,
            TaxRate,
            Unit,
        )
        from apps.customers.models import Customer
        from apps.inventory.models import MovementReason, StockItem
        from apps.inventory.services import record_movement
        from apps.purchasing.models import Supplier

        vat = TaxRate.objects.get(is_default=True)
        retail = PriceList.objects.get(is_default=True)
        units = {u.code: u for u in Unit.objects.all()}
        wholesale = PriceList.objects.create(name="Wholesale", kind=PriceListKind.WHOLESALE)

        tiles = 0
        for name, unit_code, price, cost, qty, code, crate in SHELF:
            product = Product.objects.create(
                name=name, base_unit=units[unit_code], tax_rate=vat
            )
            variant = product.default_variant
            Price.objects.create(price_list=retail, variant=variant, amount=price)
            # Bulk buyers pay a little less; the till switches to this the
            # moment a wholesale customer is chosen.
            Price.objects.create(
                price_list=wholesale, variant=variant,
                amount=Decimal(price) * Decimal("0.95"),
            )
            if code:
                Barcode.objects.create(variant=variant, code=code, is_primary=True)
            if crate:
                Barcode.objects.create(
                    variant=variant, code=crate, pack_quantity=24, label="crate of 24"
                )
            if qty:
                record_movement(
                    variant=variant, qty_delta=qty, reason=MovementReason.OPENING,
                    branch=branch, unit_cost=cost, note="Opening stock", user=owner,
                )
                # A level to warn at, or "running low" can never fire and the
                # whole reorder feature is invisible in the demo. A third of
                # the opening count is about a week's cover for these lines.
                StockItem.objects.filter(variant=variant, branch=branch).update(
                    reorder_level=max(round(qty / 3), 5)
                )
            # Things without a barcode need a tile, or they cannot be sold.
            if code is None or tiles < 4:
                QuickKey.objects.create(variant=variant, position=tiles)
                tiles += 1

        Customer.objects.create(
            name="Hoteli ya Baharini", phone="0788112233", price_list=wholesale,
            credit_limit=Decimal("500000"),
            note="Buys in bulk every Friday. Pays at the end of the month.",
        )
        Customer.objects.create(name="Mama Neema", phone="0755998877",
                                credit_limit=Decimal("50000"))
        Supplier.objects.create(
            name="Azam Distributors", contact_name="Juma", phone="0712000111",
            payment_terms_days=14,
        )

    def _trade(self, tenant, branch, seller, days=30):
        """
        A month of trading behind the demo shop.

        Without it the dashboard opens on a flat line and every comparison
        reads "new", which teaches a first-time reader nothing about what the
        screen is for. The week has a shape -- Saturday busy, Sunday quiet --
        because a month of identical days is the one pattern no real shop has.
        """
        import random
        from datetime import datetime, time, timedelta

        from django.utils import timezone

        from apps.catalog.models import Product
        from apps.inventory.models import MovementReason, StockItem
        from apps.inventory.services import record_movement
        from apps.pos.models import PaymentMethod, Sale
        from apps.pos.services import add_to_cart, complete_sale, create_return, new_cart

        rng = random.Random(2026)
        costs = {name: Decimal(cost) for name, _, _, cost, *_ in SHELF}
        variants = [p.default_variant for p in Product.objects.all()]
        held = {
            row["variant_id"]: row["avg_cost"]
            for row in StockItem.objects.filter(branch=branch).values("variant_id", "avg_cost")
        }

        def cost_of(variant):
            """
            What it cost, in the order the shop itself would know it: what is
            on the shelf, then the opening price list, then a guess off the
            selling price. A flat guess for every line would make the margin
            figures on the dashboard fiction.
            """
            known = held.get(variant.pk) or costs.get(variant.product.name)
            if known:
                return known
            price = variant.price_for() or Decimal("1000")
            return (price * Decimal("0.65")).quantize(Decimal("0.01"))

        # A month of selling needs more on the shelf than the opening count,
        # or the demo spends its first minute in the red.
        for variant in variants:
            record_movement(
                variant=variant, qty_delta=700, reason=MovementReason.PURCHASE,
                branch=branch, unit_cost=cost_of(variant),
                note="Stocked up for the month", user=seller,
            )

        code = (branch.code or branch.name[:3]).upper()[:3]
        today = timezone.localdate()
        sales = []
        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            stem = f"{code}{day:%y%m%d}"
            # A day may already have sales on it -- this can be run against a
            # shop that has been clicked around in -- and two receipts with
            # one number is a database error, not a cosmetic one.
            taken = (Sale.objects_all.filter(tenant=tenant, number__startswith=stem)
                     .order_by("-number").values_list("number", flat=True).first())
            first = int(taken[len(stem):]) + 1 if taken else 1
            busy = {5: 15, 6: 5}.get(day.weekday(), 9)
            for counter in range(first, first + max(2, int(rng.gauss(busy, 2)))):
                when = timezone.make_aware(datetime.combine(
                    day, time(rng.randrange(8, 20), rng.randrange(60))))
                cart = new_cart(branch=branch)
                for variant in rng.sample(variants, rng.randint(1, 3)):
                    add_to_cart(cart, variant, qty=rng.randint(1, 4))
                method = PaymentMethod.CASH if rng.random() < 0.72 else PaymentMethod.MPESA
                sale = complete_sale(
                    cart, [{"method": method, "amount": cart.subtotal}],
                    user=seller, sold_at=when,
                )
                # The receipt number is built from the day it is rung up, and
                # these are being rung up out of time.
                Sale.objects.filter(pk=sale.pk).update(number=f"{stem}{counter:04d}")
                sales.append(sale)

        # Two things given back, so refunds are not a column of zeroes.
        for sale in rng.sample(sales[: len(sales) // 2], 2):
            line = sale.lines.first()
            if line is not None:
                # Money goes back the way it came, which is what the till
                # insists on for a real refund too.
                create_return(sale, {line.pk: 1}, reason="Wrong size",
                              method=sale.payments.first().method)

    def _extra_roles(self, tenant, branch):
        """Roles this shop invented for itself, and somebody in each."""
        from apps.accounts.models import Permission

        catalogue = {p.code: p for p in Permission.objects.all()}
        for role_name, email, person, grants in EXTRA_ROLES:
            role = Role.objects.create(tenant=tenant, name=role_name)
            for code, value in grants.items():
                permission = catalogue.get(code)
                if permission is None:
                    continue
                # A list is a set of options (which payment methods), a number
                # is a ceiling, None is a plain yes.
                options = value if isinstance(value, list) else []
                role.permissions.create(
                    permission=permission, granted=True,
                    limit_value=None if options else value,
                    set_value=options,
                )
            user, _ = User.objects.get_or_create(email=email, defaults={"name": person})
            user.set_password(PASSWORD)
            user.save()
            membership = Membership.objects.create(tenant=tenant, user=user, role=role)
            membership.branch_links.create(branch=branch)

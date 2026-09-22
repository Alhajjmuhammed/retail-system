"""
CSV import.

Built early and deliberately: two shops already running on other software have
to move their data across without retyping it, and that migration shapes the
format rather than being improvised later.

A bad row is reported and skipped. Stopping the whole import on row 400 of 900
is the behaviour that makes people give up and retype.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.catalog.models import (
    CATEGORY_DEPTH,
    Barcode,
    Category,
    Product,
    TaxRate,
    Unit,
)
from apps.catalog.services import attach_barcode, set_price
from apps.core.context import get_current_branch, get_current_tenant
from apps.core.features import LimitExceeded

COLUMNS = ["name", "sku", "barcode", "category", "unit", "price", "cost", "qty"]


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    # Columns left alone because the person importing may not set them.
    prices_skipped: int = 0
    stock_skipped: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_decimal(value, default=None):
    value = (value or "").strip().replace(",", "")
    if not value:
        return default
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a number") from exc


def import_products(file_obj, *, update_existing=True, branch=None,
                    may_price=True, may_stock=True) -> ImportResult:
    """
    Load products from a CSV.

    The file cannot do more than the person uploading it: prices need
    `product.set_price`, quantities need `stock.adjust`. Both used to be
    applied regardless, so a stock clerk could reprice the shop by upload.
    """
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        return ImportResult(errors=[(0, "The file is empty.")])

    headers = {name.strip().lower() for name in reader.fieldnames}
    if "name" not in headers:
        return ImportResult(
            errors=[(0, f"A 'name' column is required. Found: {', '.join(sorted(headers))}")]
        )

    result = ImportResult()
    branch = branch or get_current_branch()
    tenant = get_current_tenant()

    default_unit = Unit.objects.filter(code="pc").first() or Unit.objects.first()
    default_tax = TaxRate.objects.filter(is_default=True).first() or TaxRate.objects.first()

    for number, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("name", "")
        if not name:
            result.skipped += 1
            continue

        # Counted outside the transaction: a row that fails is rolled back in
        # the database, and its tally has to roll back with it.
        tally = ImportResult()
        try:
            with transaction.atomic():
                _import_row(
                    row, name, tenant, branch, default_unit, default_tax,
                    update_existing, tally, may_price=may_price, may_stock=may_stock,
                )
        except LimitExceeded as exc:
            # The plan limit is a hard stop, not a row error: everything after
            # this would fail the same way.
            result.errors.append((number, str(exc)))
            break
        except Exception as exc:
            result.errors.append((number, str(exc)))
            result.skipped += 1
        else:
            result.created += tally.created
            result.updated += tally.updated
            result.skipped += tally.skipped
            result.prices_skipped += tally.prices_skipped
            result.stock_skipped += tally.stock_skipped

    return result


def _import_row(row, name, tenant, branch, default_unit, default_tax,
                update_existing, result, *, may_price=True, may_stock=True):
    sku = row.get("sku", "")
    barcode_value = row.get("barcode", "")

    product = None
    if sku:
        product = Product.objects.filter(sku=sku).first()
    if product is None and barcode_value:
        existing = Barcode.objects.filter(code=barcode_value).first()
        if existing is not None:
            product = existing.variant.product
    if product is None:
        product = Product.objects.filter(name__iexact=name).first()

    if product is not None and not update_existing:
        result.skipped += 1
        return

    category = None
    if row.get("category"):
        category = _category_from_path(row["category"], tenant)

    unit = default_unit
    if row.get("unit"):
        unit = Unit.objects.filter(code__iexact=row["unit"]).first() or default_unit

    # VAT only from a "vat" column, or the default for a brand-new product.
    # It used to reset every existing product to the default rate.
    tax = None
    if row.get("vat"):
        tax = (TaxRate.objects.filter(name__iexact=row["vat"]).first()
               or TaxRate.objects.filter(rate=parse_decimal(row["vat"]) or -1).first())

    fields = {
        "name": name,
        "sku": sku,
        "category": category,
        "base_unit": unit,
        "tax_rate": tax,
    }

    created = product is None
    if product is None:
        product = Product.objects.create(**{**fields, "tax_rate": tax or default_tax})
        result.created += 1
    else:
        for key, value in fields.items():
            if value:
                setattr(product, key, value)
        product.save()
        result.updated += 1

    variant = product.default_variant

    price = parse_decimal(row.get("price"))
    if price is not None and price >= 0:
        if may_price:
            set_price(variant, price)
        else:
            result.prices_skipped += 1

    if barcode_value and not Barcode.objects.filter(code=barcode_value).exists():
        attach_barcode(variant, barcode_value)

    qty = parse_decimal(row.get("qty"))
    cost = parse_decimal(row.get("cost"))
    if qty and qty > 0 and branch is not None and product.track_stock:
        # `may_stock` may be a check per row: a flat yes let one CSV add any
        # amount of stock past the importer's adjustment limit.
        allowed = may_stock(variant, qty, cost) if callable(may_stock) else may_stock
        if not allowed:
            result.stock_skipped += 1
        else:
            _set_opening_stock(variant, qty, cost, branch, created=created)


def _category_from_path(text, tenant):
    """
    ``Drinks > Soda > Bottles`` from one cell of a spreadsheet.

    A shop exporting its products, adding a column in Excel and importing
    the file back is how most of them file things in bulk, so the path has
    to survive the round trip. A plain name is still a plain name.
    """
    names = [part.strip() for part in re.split(r"[>\u203a/]", text) if part.strip()]
    parent = None
    for name in names[:CATEGORY_DEPTH]:
        parent, _ = Category.objects.get_or_create(
            name=name[:80], parent=parent, defaults={"tenant": tenant}
        )
    return parent


def _set_opening_stock(variant, qty, cost, branch, *, created=False):
    """
    Opening stock, once.

    Re-importing a file must not double the shop's stock, so this sets a
    starting balance rather than adding to one -- and only for a product with
    no stock history at all here. One that has been sold or received used to
    get an arbitrary "opening" adjustment on top of real movements.
    """
    from apps.inventory.models import MovementReason, StockMovement
    from apps.inventory.services import get_stock_item, record_movement

    if StockMovement.objects.filter(variant=variant, branch=branch).exists():
        return

    item = get_stock_item(variant, branch)
    delta = qty - item.qty_on_hand
    if delta == 0:
        return

    record_movement(
        variant=variant,
        qty_delta=delta,
        reason=MovementReason.OPENING,
        branch=branch,
        unit_cost=cost,
        note="Imported",
        allow_negative=True,
    )


def safe_cell(text):
    """Text a spreadsheet will not treat as a formula."""
    text = "" if text is None else str(text)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def export_products(branch=None) -> str:
    """The same shape the importer reads, so a round trip is lossless."""
    from apps.catalog.services import category_tree
    from apps.inventory.services import quantity_of

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()

    # "Drinks > Soda > Bottles" in the cell, so what comes back in is filed
    # where it was. Built once: reading it off each product would walk back
    # up to the top shelf for every row.
    paths = {row.pk: row.path_label.replace(Category.SEPARATOR, " > ")
             for row in category_tree(include_inactive=True)}

    products = (
        Product.objects.select_related("category", "base_unit")
        .prefetch_related("variants__barcodes", "variants__prices")
        .filter(is_active=True)
    )
    for product in products:
        variant = product.default_variant
        if variant is None:
            continue
        barcode = variant.barcodes.filter(is_primary=True).first() or variant.barcodes.first()
        price = variant.price_for()
        writer.writerow({
            # A spreadsheet runs a cell starting with = + - @ as a formula.
            "name": safe_cell(product.name),
            "sku": safe_cell(product.sku),
            "barcode": safe_cell(barcode.code if barcode else ""),
            "category": safe_cell(paths.get(product.category_id, "")),
            "unit": product.base_unit.code,
            "price": price or "",
            "cost": "",
            "qty": quantity_of(variant, branch) if branch else "",
        })

    return buffer.getvalue()

"""
Finding a product.

Six ways in, one answer. Scanning is only one of them: a large share of what
these shops sell has no barcode at all, so tiles, short codes and search are
first-class routes to the same place, not fallbacks.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

from apps.catalog.models import Barcode, Price, PriceList, Product, QuickKey, Variant
from apps.core.context import get_current_tenant
from apps.pos.models import AddedVia


@dataclass
class Match:
    variant: Variant
    qty: Decimal
    added_via: str
    barcode: Barcode | None = None


def lookup(term: str, *, prefer_scan=True) -> Match | None:
    """
    Resolve one typed or scanned string to something sellable.

    Order matters: an exact barcode wins over a short code, which wins over an
    SKU. A scanner types fast and ends with Enter, so the common case has to
    be the first check.
    """
    term = (term or "").strip()
    if not term:
        return None

    if prefer_scan:
        barcode = Barcode.objects.select_related(
            "variant__product__tax_rate"
        ).filter(code=term).first()
        if barcode is not None:
            # A crate barcode adds 24, a bottle barcode adds 1. One scan
            # either way.
            return Match(
                variant=barcode.variant,
                qty=barcode.pack_quantity,
                added_via=AddedVia.SCAN,
                barcode=barcode,
            )

    quick = QuickKey.objects.select_related("variant__product").filter(
        plu_code=term
    ).first()
    if quick is not None:
        return Match(variant=quick.variant, qty=Decimal("1"), added_via=AddedVia.PLU)

    variant = Variant.objects.select_related("product__tax_rate").filter(
        Q(sku__iexact=term) | Q(product__sku__iexact=term), is_active=True
    ).first()
    if variant is not None:
        return Match(variant=variant, qty=Decimal("1"), added_via=AddedVia.SEARCH)

    return None


def search(term: str, *, limit=25, sellable_only=True):
    """Typo-forgiving enough for a cashier in a hurry, cheap enough for a till."""
    term = (term or "").strip()
    query = Variant.objects.select_related("product", "product__tax_rate").filter(
        is_active=True, product__is_active=True
    )
    if sellable_only:
        query = query.filter(product__sellable_at_pos=True)

    if term:
        query = query.filter(
            Q(product__name__icontains=term)
            | Q(name__icontains=term)
            | Q(sku__icontains=term)
            | Q(product__sku__icontains=term)
            | Q(barcodes__code__startswith=term)
        ).distinct()

    return query.order_by("product__name", "name")[:limit]


def attach_barcode(variant, code: str, *, pack_quantity=1, label="") -> Barcode:
    """
    Teach the system a code it did not know.

    An unknown barcode at the till must not be a wall -- a cashier with a
    customer waiting needs to attach it and carry on.
    """
    existing = Barcode.objects.filter(code=code).first()
    if existing is not None:
        raise BarcodeInUse(existing)
    return Barcode.objects.create(
        tenant=get_current_tenant(),
        variant=variant,
        code=code,
        pack_quantity=Decimal(str(pack_quantity)),
        label=label,
        is_primary=not variant.barcodes.exists(),
    )


class BarcodeInUse(Exception):
    def __init__(self, barcode):
        self.barcode = barcode
        super().__init__(f"{barcode.code} already belongs to {barcode.variant}.")


def set_price(variant, amount, *, price_list=None, min_qty=1) -> Price:
    price_list = price_list or PriceList.objects.filter(is_default=True).first()
    price, _ = Price.objects.update_or_create(
        price_list=price_list,
        variant=variant,
        min_qty=Decimal(str(min_qty)),
        defaults={"amount": Decimal(str(amount)), "tenant": get_current_tenant()},
    )
    return price


def create_product(*, name, price=None, cost=None, **fields) -> Product:
    """
    The short path.

    A shopkeeper adding stock at the counter should not have to visit four
    screens to get one product sellable.
    """
    from apps.catalog.models import TaxRate, Unit

    fields.setdefault("base_unit", Unit.objects.filter(code="pc").first())
    fields.setdefault("tax_rate", TaxRate.objects.filter(is_default=True).first())

    barcode = fields.pop("barcode", "")
    product = Product.objects.create(name=name, **fields)
    variant = product.default_variant

    if price is not None:
        set_price(variant, price)
    if barcode:
        attach_barcode(variant, barcode)

    return product


def tiles(branch=None, limit=60):
    """
    The touch grid.

    How tomatoes, bread and charcoal reach the cart, and the reason a shop
    with no barcodes at all can still use the till.
    """
    query = QuickKey.objects.select_related(
        "variant__product__category", "variant__product__base_unit",
        "variant__product__tax_rate",
    )
    if branch is not None:
        query = query.filter(Q(branch=branch) | Q(branch__isnull=True))
    keys = list(query.order_by("position")[:limit])

    # What is left on the shelf, for the corner of each tile. One query for
    # the lot: a cashier's grid should not cost sixty of them.
    from apps.inventory.models import StockItem

    on_hand = {}
    if branch is not None:
        on_hand = dict(
            StockItem.objects.filter(
                branch=branch, variant__in=[k.variant_id for k in keys]
            ).values_list("variant_id", "qty_on_hand")
        )
    for key in keys:
        product = key.variant.product
        # Services and one-off charges have no shelf to be short on.
        key.stock_left = on_hand.get(key.variant_id) if product.track_stock else None
    return keys


def tile_categories(tiles):
    """
    The categories the tiles actually fall into, in the order they appear.

    Taken from the tiles themselves rather than the category list: a category
    with nothing tappable in it is a tab that leads to an empty screen.
    """
    seen = {}
    for key in tiles:
        category = key.variant.product.category
        label = category.name if category else "Other"
        seen.setdefault(label, 0)
        seen[label] += 1
    return sorted(seen.items())

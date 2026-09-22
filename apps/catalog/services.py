"""
Finding a product.

Six ways in, one answer. Scanning is only one of them: a large share of what
these shops sell has no barcode at all, so tiles, short codes and search are
first-class routes to the same place, not fallbacks.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

from apps.catalog.models import (
    CATEGORY_DEPTH,
    Barcode,
    Category,
    Price,
    PriceList,
    Product,
    QuickKey,
    Variant,
)
from apps.core.context import get_current_tenant
from apps.pos.models import AddedVia


@dataclass
class TileGroup:
    """One tab over the touch grid, and the tabs inside it."""

    label: str
    count: int
    subs: list


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


# --------------------------------------------------------------------------
# Shelves inside shelves
# --------------------------------------------------------------------------

def category_tree(include_inactive=False):
    """
    Every category, a parent always before the things inside it.

    One query for the lot, then the nesting is worked out in memory: a shop
    has tens of categories, not thousands, and a picker that costs a query
    per level is a picker nobody opens twice.

    Each row comes back with its parent already attached, so ``level`` and
    ``path_label`` answer from memory instead of walking back up to the top
    shelf one query at a time.
    """
    rows = list(Category.objects.all() if include_inactive
                else Category.objects.filter(is_active=True))
    children = {}
    for row in rows:
        children.setdefault(row.parent_id, []).append(row)
    for group in children.values():
        group.sort(key=lambda row: row.name.lower())

    ordered = []

    def walk(parent, level):
        # Anything below the third level is left out rather than shown: the
        # forms refuse to make one, so a row down there is old or imported.
        if level > CATEGORY_DEPTH:
            return
        for row in children.get(parent.pk if parent else None, ()):
            # Assigning the instance fills Django's own cache for the field,
            # which is what stops `level` and `path_label` querying.
            row.parent = parent
            ordered.append(row)
            walk(row, level + 1)

    walk(None, 1)
    return ordered


def category_family(category_id, include_inactive=True):
    """
    A category and everything filed inside it, as ids.

    Picking "Drinks" in a filter has to find the soda and the bottles under
    it too, or a shop that files things properly sees an empty list.
    """
    category_id = int(category_id)
    rows = list(
        (Category.objects.all() if include_inactive
         else Category.objects.filter(is_active=True)).values_list("pk", "parent_id")
    )
    children = {}
    for pk, parent_id in rows:
        children.setdefault(parent_id, []).append(pk)

    family, queue = {category_id}, [category_id]
    while queue:
        for pk in children.get(queue.pop(), ()):
            if pk not in family:
                family.add(pk)
                queue.append(pk)
    return family


def category_parents(exclude=None, height=1):
    """
    The categories something may be put inside.

    ``height`` is how many levels the thing being moved is itself: moving a
    category that already has one inside it needs two levels of room, not
    one. A category may not be moved into itself or into one of its own --
    the move that turns a list into a ring nothing can draw.
    """
    family = category_family(exclude.pk) if exclude is not None else set()
    return [row for row in category_tree(include_inactive=True)
            if row.level + height <= CATEGORY_DEPTH and row.pk not in family]


def tiles(branch=None, limit=60):
    """
    The touch grid.

    How tomatoes, bread and charcoal reach the cart, and the reason a shop
    with no barcodes at all can still use the till.
    """
    query = QuickKey.objects.select_related(
        # Up the shelves as well, not just the shelf: the tabs read each
        # tile's ancestry, and one level of select_related made that a query
        # per tile the moment a shop filed anything two deep.
        "variant__product__category__parent__parent",
        "variant__product__base_unit",
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
    The tabs above the touch grid: top shelves, and what is inside each.

    Taken from the tiles themselves rather than the category list: a category
    with nothing tappable in it is a tab that leads to an empty screen.

    Two rows, not three. Each tile is also marked with the shelf and the
    sub-shelf it belongs to, so the grid can hide what the tabs exclude
    without a query per tile. A product filed three deep shows under its
    second-level shelf: the till has no room for a third row of tabs, and a
    cashier reaching for a Coke has made two taps by then already.
    """
    tops, inside = {}, {}
    for key in tiles:
        category = key.variant.product.category
        chain = category.ancestry if category else []
        top = chain[0].name if chain else "Other"
        sub = chain[1].name if len(chain) > 1 else ""
        key.tile_group, key.tile_sub = top, sub
        tops[top] = tops.get(top, 0) + 1
        if sub:
            inside.setdefault(top, {})
            inside[top][sub] = inside[top].get(sub, 0) + 1

    return [
        TileGroup(
            label=top,
            count=count,
            subs=[TileGroup(label=name, count=n, subs=[])
                  for name, n in sorted(inside.get(top, {}).items())],
        )
        for top, count in sorted(tops.items())
    ]

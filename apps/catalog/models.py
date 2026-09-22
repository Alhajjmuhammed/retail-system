"""
The product catalogue -- shared across every branch of a tenant.

Two decisions shape this whole module:

* Stock and prices hang off ``Variant``, never off ``Product``. Every product
  gets an automatic default variant, so there is one code path instead of two.
* Barcodes are their own table. One product legitimately has several: the
  bottle, the crate, and the replacement code after the supplier repackages.
"""

from decimal import Decimal

from django.db import models

from apps.core.features import LIMIT_PRODUCTS, WHOLESALE_PRICING
from apps.core.models import TenantModel

# Drinks > Soda > Bottles. Three is as deep as it goes: a shopkeeper filing
# stock does not think in a fourth level, and the till has room for two rows
# of tabs, not three.
CATEGORY_DEPTH = 3


class Category(TenantModel):
    """
    A shelf, and the shelves inside it.

    ``parent`` has been on this table from the start but nothing ever set it,
    so every shop had one flat list. Three levels is what a duka actually
    uses: Drinks, then Soda, then Bottles and Cans.
    """

    # Not ">" or "/": both turn up inside names a shop types.
    SEPARATOR = " \u203a "

    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name", "parent")]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    @property
    def ancestry(self):
        """
        This category and the ones it sits inside, outermost first.

        The loop counts what it has seen: a category that somehow ends up
        inside itself would otherwise hang the page rather than show it.
        """
        chain, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    @property
    def level(self):
        """1 for a top shelf, 3 for the deepest."""
        return len(self.ancestry)

    @property
    def path_label(self):
        """``Drinks > Soda > Bottles``, for a picker with room for it."""
        return self.SEPARATOR.join(node.name for node in self.ancestry)

    @property
    def top(self):
        """The shelf this sits under, which is the tab on the till."""
        return self.ancestry[0]


class Brand(TenantModel):
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return self.name


class Unit(TenantModel):
    """
    How a thing is measured.

    ``allows_decimal`` is what lets a shop sell 1.5 kg of sugar and refuses
    1.5 bottles.
    """

    name = models.CharField(max_length=40)
    code = models.CharField(max_length=10)
    allows_decimal = models.BooleanField(default=False)
    # A unit that has been used cannot be deleted without orphaning products,
    # so it is switched off instead and stops appearing on new ones.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "code")]

    def __str__(self):
        return self.code


class TaxRate(TenantModel):
    """
    A VAT rate a product can carry.

    Prices include VAT. That is how a shop here quotes -- the price on the
    shelf is what the customer hands over -- and the tax is worked out of it
    rather than added to it. There used to be a switch on this row and
    another on the business settings offering to change that; neither was
    ever read by the till, so both said something untrue.
    """

    name = models.CharField(max_length=40)
    rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # The code the revenue authority expects on a fiscal receipt.
    fiscal_code = models.CharField(max_length=10, blank=True)
    is_default = models.BooleanField(default=False)
    # A rate that has been charged is retired, never deleted: last year's
    # returns have to keep meaning what they meant.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return f"{self.name} ({self.rate}%)"


class Product(TenantModel):
    name = models.CharField(max_length=160, db_index=True)
    sku = models.CharField(max_length=40, blank=True, db_index=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="products/", blank=True)

    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    base_unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, related_name="products"
    )
    tax_rate = models.ForeignKey(
        TaxRate, on_delete=models.PROTECT, related_name="products"
    )

    # Services and one-off charges are sold without ever touching stock.
    track_stock = models.BooleanField(default=True)
    sellable_at_pos = models.BooleanField(default=True)
    discount_allowed = models.BooleanField(default=True)
    # A floor that stops staff discounting below cost.
    min_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    weight_kg = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["tenant", "name"]),
            models.Index(fields=["tenant", "sku"]),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        reviving = (
            not self._state.adding and self.is_active
            and type(self).objects_all.filter(pk=self.pk, is_active=False).exists()
        )
        if (self._state.adding and self.is_active) or reviving:
            from apps.core.context import get_current_tenant

            tenant = self.tenant if self.tenant_id else get_current_tenant()
            if tenant is not None:
                tenant.enforce_limit(LIMIT_PRODUCTS)
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating:
            # Every product gets a default variant so stock and prices always
            # have somewhere to hang. Nothing downstream special-cases it.
            Variant.objects.create(
                tenant=self.tenant, product=self, name="", sku=self.sku, is_default=True
            )

    @property
    def default_variant(self):
        # Lists prefetch it as `_default_variants`; one query per row otherwise.
        prefetched = getattr(self, "_default_variants", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.variants.filter(is_default=True).first()


class Variant(TenantModel):
    """
    A sellable thing. Size, colour, flavour -- or the single default one.

    Stock quantities and prices attach here, never to Product.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=80, blank=True)
    sku = models.CharField(max_length=40, blank=True, db_index=True)
    attributes = models.JSONField(
        default=dict, blank=True, help_text="Size, colour, flavour. Room to grow."
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__name", "name"]

    def __str__(self):
        if self.name:
            return f"{self.product.name} - {self.name}"
        return self.product.name

    def price_for(self, price_list=None, qty: Decimal = Decimal("1")):
        """
        The price this variant sells at.

        Falls back to the tenant's default list, and honours quantity breaks so
        wholesale tiers work without a second product.
        """
        if price_list is not None and not price_list.is_active:
            # A switched-off list prices nothing.
            return None
        cached = getattr(self, "_default_prices", None)
        if cached is not None and price_list is None:
            # Prefetched by a list: default-list prices, ordered by min_qty.
            fits = [p for p in cached if p.min_qty <= qty]
            row = fits[-1] if fits else (cached[0] if cached else None)
            return row.amount if row else None
        query = self.prices.select_related("price_list")
        if price_list is not None:
            query = query.filter(price_list=price_list)
        else:
            query = query.filter(price_list__is_default=True)
        row = query.filter(min_qty__lte=qty).order_by("-min_qty").first()
        if row is None and price_list is None:
            # The shop's own list: its smallest tier is the price, even for
            # a smaller quantity. A customer's list with only a bulk tier
            # falls back to the usual price instead of selling one at the
            # 50-unit rate.
            row = query.order_by("min_qty").first()
        return row.amount if row else None


class Barcode(TenantModel):
    """
    One variant, many codes.

    ``pack_quantity`` is what makes packaging work: scanning the crate adds 24,
    scanning the bottle adds 1, one scan either way.
    """

    variant = models.ForeignKey(Variant, on_delete=models.CASCADE, related_name="barcodes")
    code = models.CharField(max_length=64, db_index=True)
    pack_quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)
    label = models.CharField(max_length=40, blank=True, help_text="e.g. crate of 24")
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "code"]
        unique_together = [("tenant", "code")]

    def __str__(self):
        return self.code


class PriceListKind(models.TextChoices):
    RETAIL = "retail", "Retail"
    WHOLESALE = "wholesale", "Wholesale"


class PriceList(TenantModel):
    name = models.CharField(max_length=60)
    kind = models.CharField(
        max_length=12, choices=PriceListKind.choices, default=PriceListKind.RETAIL
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return self.name

    @property
    def requires_feature(self):
        return WHOLESALE_PRICING if self.kind == PriceListKind.WHOLESALE else None


class Price(TenantModel):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="prices")
    variant = models.ForeignKey(Variant, on_delete=models.CASCADE, related_name="prices")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Quantity breaks: 1+, 12+, 24+. Bulk pricing without a second product.
    min_qty = models.DecimalField(max_digits=10, decimal_places=3, default=1)

    class Meta:
        ordering = ["variant", "min_qty"]
        unique_together = [("price_list", "variant", "min_qty")]

    def __str__(self):
        return f"{self.variant}: {self.amount}"


class QuickKey(TenantModel):
    """
    Touch tiles and short PLU codes.

    Not decoration. A large share of what these shops sell has no barcode at
    all, and tiles are how tomatoes, bread and charcoal reach the cart.
    """

    branch = models.ForeignKey(
        "org.Branch", on_delete=models.CASCADE, related_name="quick_keys",
        null=True, blank=True, help_text="Blank means every branch.",
    )
    variant = models.ForeignKey(Variant, on_delete=models.CASCADE, related_name="quick_keys")
    plu_code = models.CharField(max_length=8, blank=True, db_index=True)
    position = models.PositiveIntegerField(default=0)
    colour = models.CharField(max_length=7, blank=True)

    class Meta:
        ordering = ["position"]

    def __str__(self):
        return self.plu_code or str(self.variant)

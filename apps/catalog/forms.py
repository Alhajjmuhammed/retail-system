from django import forms
from django.db import models

from apps.accounts.forms import TailwindMixin
from apps.catalog.models import Brand, Category, Product, TaxRate, Unit


class ProductForm(TailwindMixin, forms.ModelForm):
    """
    Everything needed to make one product sellable, on one screen.

    Price and barcode are not on the model -- they live on the variant -- but
    a shopkeeper does not think that way, so the form carries them.
    """

    price = forms.DecimalField(
        label="Selling price", max_digits=12, decimal_places=2, required=False, min_value=0
    )
    barcode = forms.CharField(label="Barcode", max_length=64, required=False)
    opening_qty = forms.DecimalField(
        label="Stock on hand", max_digits=14, decimal_places=3, required=False, min_value=0,
        help_text="Only on a new product. Adjust stock afterwards instead.",
    )
    cost = forms.DecimalField(
        label="Cost price", max_digits=12, decimal_places=2, required=False, min_value=0,
        help_text="What one unit cost you. Only used to value the opening stock.",
    )
    reorder_level = forms.DecimalField(
        label="Tell me when stock falls to", max_digits=14, decimal_places=3,
        required=False, min_value=0,
    )

    class Meta:
        model = Product
        fields = [
            "name", "sku", "category", "brand", "base_unit", "tax_rate",
            "description", "image", "track_stock", "sellable_at_pos",
            "discount_allowed", "min_price", "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Switched-off ones stay choosable for the product that already has
        # them; otherwise the field rendered blank and saving cleared it.
        self.fields["category"].queryset = Category.objects.filter(
            models.Q(is_active=True) | models.Q(pk=self.instance.category_id))
        self.fields["brand"].queryset = Brand.objects.filter(
            models.Q(is_active=True) | models.Q(pk=self.instance.brand_id))
        # Piece first, then the rest by name: nearly everything a shop adds is
        # sold by the piece, and alphabetical order put "Crate" at the top.
        self.fields["base_unit"].queryset = Unit.objects.filter(is_active=True).order_by(
            models.Case(
                models.When(code="pc", then=0), default=1,
                output_field=models.IntegerField(),
            ),
            "name",
        )
        self.fields["tax_rate"].queryset = TaxRate.objects.filter(is_active=True)
        self.fields["category"].required = False
        self.fields["brand"].required = False

        if not self.instance.pk:
            # Most things are sold by the piece at the shop's usual VAT; an
            # empty "Sold by" and "VAT" had to be picked on every product.
            self.fields["base_unit"].initial = (
                self.fields["base_unit"].queryset.filter(code="pc").first()
            )
            self.fields["tax_rate"].initial = TaxRate.objects.filter(
                is_active=True, is_default=True
            ).first()
            self.fields["base_unit"].empty_label = None
            self.fields["tax_rate"].empty_label = None

        if self.instance.pk:
            # Stock and its cost change through receiving and adjustments
            # once a product exists. A cost typed here used to be dropped
            # silently while the page said "updated".
            del self.fields["opening_qty"]
            del self.fields["cost"]
            # Codes are managed in the barcode box under the form. A second
            # copy here put a just-removed code straight back on save.
            del self.fields["barcode"]

    def clean(self):
        data = super().clean()
        price, floor = data.get("price"), data.get("min_price")
        if floor is not None and floor < 0:
            self.add_error("min_price", "Cannot be below zero.")
        elif price is not None and floor is not None and floor > price:
            self.add_error("min_price", "Higher than the selling price: nothing could be sold.")
        if data.get("opening_qty") and not data.get("track_stock"):
            self.add_error("opening_qty", "This product does not keep count of stock.")
        return data

    def clean_barcode(self):
        from apps.catalog.models import Barcode

        code = (self.cleaned_data.get("barcode") or "").strip()
        if not code:
            return ""
        clash = Barcode.objects.filter(code=code)
        if self.instance.pk:
            clash = clash.exclude(variant__product=self.instance)
        if clash.exists():
            raise forms.ValidationError(
                f"{code} already belongs to {clash.first().variant}."
            )
        return code


class CategoryForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "parent", "is_active"]


class TaxRateForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = TaxRate
        fields = ["name", "rate", "is_inclusive", "fiscal_code", "is_default"]


class UnitForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = Unit
        fields = ["name", "code", "allows_decimal"]


class ImportForm(TailwindMixin, forms.Form):
    """
    Bulk import.

    Shaped by the two shops already running on something else: their data has
    to arrive without retyping, and a bad row must not stop the good ones.
    """

    file = forms.FileField(
        label="CSV file",
        help_text="Columns: name, sku, barcode, category, unit, price, cost, qty",
    )
    update_existing = forms.BooleanField(
        label="Update products that already exist", required=False, initial=True
    )

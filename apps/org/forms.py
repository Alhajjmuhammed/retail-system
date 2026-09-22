from django import forms
from django.db import models

from apps.accounts.forms import TailwindMixin
from apps.org.models import Branch, Register, TenantSettings


class BranchForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["name", "code", "phone", "address", "is_default", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def clean_name(self):
        # `tenant` is not a form field, so Django's own unique check skipped
        # (tenant, name) and a duplicate reached the database as a 500.
        name = self.cleaned_data["name"].strip()
        clash = Branch.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("There is already a branch with this name.")
        return name

    def clean(self):
        data = super().clean()
        active = data.get("is_active", True)
        if not active and data.get("is_default"):
            self.add_error("is_active", "The default branch cannot be switched off.")
        elif self.instance.pk and self.instance.is_default and not data.get("is_default"):
            # Unticking the only default left the shop with none.
            self.add_error("is_default", "Make another branch the default instead.")
        elif not active and self.instance.pk:
            if self.instance.is_default:
                self.add_error(
                    "is_active", "Make another branch the default before switching this one off."
                )
            elif not Branch.objects.filter(is_active=True).exclude(pk=self.instance.pk).exists():
                self.add_error("is_active", "This is the only branch. The shop needs one.")
        return data


class RegisterForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = Register
        fields = ["branch", "name", "code", "is_active"]

    def __init__(self, *args, branches=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The branches this person runs. A till already in a closed branch
        # keeps its branch as a choice, or it could never be saved again.
        allowed = branches if branches is not None else Branch.objects.all()
        query = allowed.filter(is_active=True)
        if self.instance.pk:
            query = allowed.filter(models.Q(is_active=True) | models.Q(pk=self.instance.branch_id))
        self.fields["branch"].queryset = query


class BusinessForm(TailwindMixin, forms.ModelForm):
    """
    The settings a shop actually changes.

    ``cost_method`` is here rather than buried, because it decides every
    margin figure in the system and changing it later re-bases history.
    """

    class Meta:
        model = TenantSettings
        fields = [
            "cost_method", "default_tax_rate",
            "negative_stock_allowed", "low_stock_alerts", "expiry_warning_days",
            "receipt_header", "receipt_footer", "show_tin_on_receipt",
            "fiscal_provider",
        ]
        widgets = {
            "receipt_header": forms.Textarea(attrs={"rows": 2}),
            "receipt_footer": forms.Textarea(attrs={"rows": 2}),
        }

    # The labels a shopkeeper reads, over the names the code uses. "Weighted
    # average cost method" is an accounting term; the shop owner deciding it
    # has never heard it, and the setting is one they can get permanently
    # wrong. Said in their own words, it is a question they can answer.
    PLAIN = {
        "cost_method": (
            "What an item costs you",
            "Used to work out profit. Best chosen once: changing it later "
            "re-works the profit on everything you have already sold.",
        ),
        "default_tax_rate": (
            "VAT on a new product",
            "What a product is given when you add it. You can change it per product.",
        ),
        "negative_stock_allowed": (
            "Let a till sell something the system thinks is finished",
            "Leave this on. A till that is offline cannot always know what is "
            "left, and refusing the sale loses real money at the counter.",
        ),
        "low_stock_alerts": (
            "Tell me when something is nearly finished",
            "Uses the level you set against each product on the Stock page.",
        ),
        "expiry_warning_days": (
            "Warn me this many days before goods go off",
            "",
        ),
        "fiscal_provider": (
            "Tax machine (EFD/VFD)",
            "Leave blank until you have one. Receipts queue safely either way.",
        ),
        "receipt_header": ("Top of the receipt", ""),
        "receipt_footer": ("Bottom of the receipt", ""),
        "show_tin_on_receipt": ("Print your TIN on the receipt", ""),
    }

    COST_METHOD_WORDS = [
        ("weighted_average", "The average of what you have paid for it"),
        ("last_cost", "The last price you paid for it"),
    ]

    def __init__(self, *args, **kwargs):
        from apps.catalog.models import TaxRate

        super().__init__(*args, **kwargs)
        self.fields["default_tax_rate"].queryset = TaxRate.objects.filter(is_active=True)
        self.fields["default_tax_rate"].required = False

        self.fields["cost_method"].choices = self.COST_METHOD_WORDS
        for name, (label, hint) in self.PLAIN.items():
            if name in self.fields:
                self.fields[name].label = label
                self.fields[name].help_text = hint


class TenantProfileForm(TailwindMixin, forms.ModelForm):
    """Name, TIN and address -- what appears on a receipt."""

    class Meta:
        from apps.tenancy.models import Tenant

        model = Tenant
        fields = ["name", "legal_name", "tin", "vrn", "phone", "email", "address", "logo"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

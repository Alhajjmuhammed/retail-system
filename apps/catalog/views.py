"""
Catalogue screens.

Lists are HTMX-driven: the same view returns the whole page on a normal
request and just the rows on an HX-Request, so filtering feels instant
without a second implementation.
"""


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, OuterRef, Prefetch, Q, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.catalog.forms import ImportForm, ProductForm
from apps.catalog.imports import export_products, import_products
from apps.catalog.models import (
    CATEGORY_DEPTH,
    Barcode,
    Category,
    Price,
    Product,
    Variant,
)
from apps.catalog.services import (
    attach_barcode,
    category_family,
    category_parents,
    category_tree,
    set_price,
)
from apps.core import audit
from apps.core.decorators import requires
from apps.core.deletion import remove_or_archive
from apps.core.features import LimitExceeded
from apps.core.parsing import decimal_or_none, int_or
from apps.inventory.valuation import limit_value


@login_required
@requires("product.view")
def product_list(request):
    term = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    status = request.GET.get("status", "active")

    products = (
        Product.objects.select_related("category", "brand", "base_unit", "tax_rate")
        .prefetch_related(
            Prefetch(
                "variants",
                queryset=Variant.objects.filter(is_default=True).prefetch_related(
                    "barcodes",
                    Prefetch("prices", to_attr="_default_prices",
                             queryset=Price.objects.filter(price_list__is_default=True)
                             .order_by("min_qty")),
                ),
                to_attr="_default_variants",
            )
        )
        .annotate(variant_count=Count("variants"))
    )

    if status == "active":
        products = products.filter(is_active=True)
    elif status == "inactive":
        products = products.filter(is_active=False)

    if category:
        # Everything under the chosen shelf, not only what is filed directly
        # on it: picking Drinks has to find the soda and the bottles too.
        products = products.filter(category_id__in=category_family(int_or(category)))

    if term:
        products = products.filter(
            Q(name__icontains=term)
            | Q(sku__icontains=term)
            | Q(variants__barcodes__code__startswith=term)
        ).distinct()

    if request.branch is not None and request.membership.can("product.view_cost"):
        # Cost lives on StockItem, per branch. A subquery keeps this one query
        # instead of one per row.
        from apps.inventory.models import StockItem

        cost = StockItem.objects.filter(
            branch=request.branch, variant__product=OuterRef("pk"),
            variant__is_default=True,
        ).values("avg_cost")[:1]
        products = products.annotate(
            branch_cost=Subquery(cost, output_field=DecimalField())
        )

    from apps.core.listing import paginate

    context = {
        **paginate(request, products.order_by("name")),
        "q": term,
        "categories": category_tree(),
        "category": category,
        "status": status,
        "can_see_cost": request.membership.can("product.view_cost"),
    }

    if request.htmx:
        return render(request, "catalog/_product_rows.html", context)
    return render(request, "catalog/product_list.html", context)


def _product_form(request, form, *, product=None, variant=None, saved=""):
    """The product form: in the modal when opened from the list, else a page."""
    context = {
        "form": form, "product": product, "variant": variant, "saved": saved,
        "other_lists": _other_lists(request, variant, form),
        "modal": bool(request.htmx),
    }
    if variant is not None and request.membership.can("product.view_cost") and request.branch:
        from apps.inventory.services import get_stock_item

        item = get_stock_item(variant, request.branch, create=False)
        context["avg_cost"] = item.avg_cost if item else None
    template = "catalog/_product_form.html" if request.htmx else "catalog/product_form.html"
    return render(request, template, context)


def _other_lists(request, variant, form):
    """
    Prices on the lists that are not the default -- wholesale and the like.

    A second list could be created but there was nowhere to put a price on
    it, so every product fell back to the retail price.
    """
    from apps.catalog.models import Price, PriceList

    if not request.membership.can("product.set_price"):
        return []
    lists = list(PriceList.objects.filter(is_active=True, is_default=False).order_by("name"))
    current = {}
    if variant is not None:
        current = {
            p.price_list_id: p.amount
            for p in Price.objects.filter(variant=variant, price_list__in=lists, min_qty=1)
        }
    posted = form.data if form.is_bound else {}
    return [
        {"list": pl, "field": f"list_price:{pl.pk}",
         "value": posted.get(f"list_price:{pl.pk}", current.get(pl.pk, "")),
         "error": (form._list_errors or {}).get(pl.pk) if hasattr(form, "_list_errors") else None}
        for pl in lists
    ]


def _read_list_prices(request, form):
    """Check the other-list prices before anything is saved."""
    from apps.catalog.models import PriceList

    form._list_errors = {}
    wanted = {}
    if not request.membership.can("product.set_price"):
        return wanted
    for pl in PriceList.objects.filter(is_active=True, is_default=False):
        raw = (request.POST.get(f"list_price:{pl.pk}") or "").strip()
        if raw == "":
            wanted[pl] = None
            continue
        amount = decimal_or_none(raw)
        if amount is None or amount < 0:
            form._list_errors[pl.pk] = "A price of zero or more."
        else:
            wanted[pl] = amount
    return wanted


def _save_list_prices(variant, wanted):
    from apps.catalog.models import Price

    for pl, amount in wanted.items():
        if amount is None:
            Price.objects.filter(variant=variant, price_list=pl, min_qty=1).delete()
        else:
            set_price(variant, amount, price_list=pl)


@login_required
@requires("product.manage")
def product_create(request):
    from django.db import transaction

    from apps.core.listing import close_modal

    form = ProductForm(request.POST or None, request.FILES or None)
    _lock_price(request, form)
    if request.method == "POST":
        wanted = _read_list_prices(request, form)
        if form.is_valid() and not form._list_errors:
            try:
                with transaction.atomic():
                    product = _save_product(request, form, creating=True)
                    _save_list_prices(product.default_variant, wanted)
            except LimitExceeded as exc:
                form.add_error(None, str(exc))
            else:
                audit.record("product.created", obj=product, ip=audit.client_ip(request))
                if "save_and_new" in request.POST:
                    if request.htmx:
                        fresh = ProductForm()
                        _lock_price(request, fresh)
                        return _product_form(request, fresh, saved=product.name)
                    messages.success(request, f"{product.name} added.")
                    return redirect("catalog:product_create")
                messages.success(request, f"{product.name} added.")
                return close_modal(request, reverse("catalog:product_list"))

    return _product_form(request, form)


@login_required
@requires("product.manage")
def product_edit(request, pk):
    from django.db import transaction

    from apps.core.listing import close_modal

    product = get_object_or_404(Product, pk=pk)
    variant = product.default_variant
    barcode = variant.barcodes.filter(is_primary=True).first() if variant else None
    reorder = None
    if variant is not None and request.branch is not None:
        from apps.inventory.services import get_stock_item

        item = get_stock_item(variant, request.branch, create=False)
        reorder = item.reorder_level if item else None

    form = ProductForm(
        request.POST or None,
        request.FILES or None,
        instance=product,
        initial={
            "price": variant.price_for() if variant else None,
            "barcode": barcode.code if barcode else "",
            "reorder_level": reorder,
        },
    )

    _lock_price(request, form)

    if request.method == "POST":
        # Taken from the database before the form touches the instance:
        # is_valid() copies the new values in, so "before" equalled "after".
        before = _product_record(Product.objects.get(pk=product.pk))
        wanted = _read_list_prices(request, form)
        if form.is_valid() and not form._list_errors:
            with transaction.atomic():
                _save_product(request, form, creating=False)
                _save_list_prices(variant, wanted)
            audit.record(
                "product.updated", obj=product, before=before,
                after=_product_record(Product.objects.get(pk=product.pk)),
                ip=audit.client_ip(request),
            )
            messages.success(request, f"{product.name} updated.")
            return close_modal(request, reverse("catalog:product_list"))

    return _product_form(request, form, product=product, variant=variant)


def _product_record(product):
    """What an audit row keeps of a product: its fields and every price."""
    from apps.catalog.models import Price

    record = audit.snapshot(product)
    variant = product.default_variant
    if variant is not None:
        record["prices"] = {
            p.price_list.name: str(p.amount)
            for p in Price.objects.filter(variant=variant, min_qty=1).select_related("price_list")
        }
    return record


def _lock_price(request, form):
    """
    Show the price, but only let those who may set prices change it.

    Without `product.set_price` a typed price was silently ignored while the
    page still said "updated".
    """
    if not request.membership.can("product.set_price"):
        for name in ("price", "min_price"):
            if name in form.fields:
                form.fields[name].disabled = True
                form.fields[name].help_text = "Your role cannot change prices."


def _save_product(request, form, *, creating):
    product = form.save()
    variant = product.default_variant
    data = form.cleaned_data

    if data.get("price") is not None and request.membership.can("product.set_price"):
        set_price(variant, data["price"])

    code = data.get("barcode")
    if code and not variant.barcodes.filter(code=code).exists():
        attach_barcode(variant, code)

    if creating:
        _set_opening(request, variant, data)

    if data.get("reorder_level") is not None and request.branch is not None:
        from apps.inventory.services import get_stock_item

        item = get_stock_item(variant, request.branch)
        item.reorder_level = data["reorder_level"]
        item.save(update_fields=["reorder_level", "updated_at"])

    return product


def _set_opening(request, variant, data):
    qty = data.get("opening_qty")
    if not qty or not variant.product.track_stock or request.branch is None:
        return
    # Opening stock is stock appearing; it needs the same right, at the same
    # ceiling, as any other adjustment.
    if qty < 0:
        messages.warning(request, "Opening stock cannot be negative; none was added.")
        return
    # Valued like any adjustment (see inventory.valuation): with no cost and
    # no price it used to be worth nothing and pass every limit.
    value = limit_value(variant, qty, data.get("cost"), trust_cost=False)
    if not request.membership.can("stock.adjust", branch=request.branch, value=value):
        messages.warning(request, "Product saved without opening stock: your role cannot "
                                  "add that much stock. Ask a manager to adjust it.")
        return
    from apps.inventory.models import MovementReason
    from apps.inventory.services import record_movement

    record_movement(
        variant=variant,
        qty_delta=qty,
        reason=MovementReason.OPENING,
        branch=request.branch,
        unit_cost=data.get("cost"),
        note="Opening stock",
        allow_negative=True,
    )


def _barcode_box(request, variant, error=""):
    return render(request, "catalog/_barcodes.html", {"variant": variant, "error": error})


@login_required
@requires("product.manage")
@require_POST
def barcode_add(request, pk):
    """
    Attach a code the system did not know.

    Called from the till too, where a cashier with a customer waiting needs
    this to take two seconds.
    """
    from apps.catalog.services import BarcodeInUse

    variant = get_object_or_404(Variant, pk=pk)
    code = request.POST.get("code", "").strip()
    pack = decimal_or_none(request.POST.get("pack_quantity") or "1")

    def refuse(message, status=400):
        if request.htmx:
            return _barcode_box(request, variant, message)
        messages.error(request, message)
        return redirect("catalog:product_edit", pk=variant.product_id)

    if not code:
        return refuse("Scan or type the code first.")
    if len(code) > Barcode._meta.get_field("code").max_length:
        return refuse("That code is too long for a barcode.")
    # A pack of zero or less sold goods for nothing and put stock back up.
    if pack is None or pack <= 0 or pack > 100000:
        return refuse("Units in it: a number above zero, e.g. 1 for a single, 24 for a crate.")

    try:
        attach_barcode(variant, code, pack_quantity=pack)
    except BarcodeInUse as exc:
        return refuse(str(exc), status=409)
    audit.record("barcode.attached", obj=variant, after={"code": code, "pack": str(pack)},
                 ip=audit.client_ip(request))

    if request.htmx:
        return _barcode_box(request, variant)
    messages.success(request, f"{code} added.")
    return redirect("catalog:product_edit", pk=variant.product_id)


@login_required
@requires("product.manage")
def product_import(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    result = None

    if request.method == "POST" and form.is_valid():
        result = import_products(
            form.cleaned_data["file"],
            update_existing=form.cleaned_data["update_existing"],
            branch=request.branch,
            may_price=request.membership.can("product.set_price"),
            may_stock=lambda variant, qty, cost: request.membership.can(
                "stock.adjust", branch=request.branch,
                value=limit_value(variant, qty, cost, trust_cost=False)),
        )
        audit.record(
            "catalog.imported",
            after={"created": result.created, "updated": result.updated},
            ip=audit.client_ip(request),
        )
        if result.total:
            messages.success(
                request, f"{result.created} added, {result.updated} updated."
            )
        if result.prices_skipped or result.stock_skipped:
            messages.warning(
                request,
                f"Left unchanged because your role cannot set them: "
                f"{result.prices_skipped} prices, {result.stock_skipped} quantities.",
            )

    return render(
        request, "catalog/import.html", {"form": form, "result": result}
    )


@login_required
@requires("product.view")
def product_export(request):
    csv_text = export_products(branch=request.branch)
    response = HttpResponse(csv_text, content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="products.csv"'
    return response


@login_required
@requires("product.view")
def product_search(request):
    """Type-ahead for the till and for forms."""
    from apps.catalog.services import search

    results = search(request.GET.get("q", ""), limit=12)
    return render(request, "catalog/_search_results.html", {"results": results})


# --------------------------------------------------------------------------
# Catalogue settings
# --------------------------------------------------------------------------

@login_required
@requires("settings.edit")
def taxonomy(request):
    """
    Categories, brands, units and tax rates on one screen.

    Four small lists that a shop touches rarely. Giving each its own page
    would be four clicks to do one setup job.
    """
    from apps.catalog.models import Brand, TaxRate, Unit

    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind not in TAXONOMY_MODELS:
            messages.error(request, "Unknown list.")
            return redirect("catalog:taxonomy")
        label, model = _taxonomy_model(kind)

        if request.POST.get("action") == "restore":
            obj = get_object_or_404(model, pk=int_or(request.POST.get("pk")), is_active=False)
            obj.is_active = True
            obj.save(update_fields=["is_active", "updated_at"])
            audit.record(f"{kind}.restored", obj=obj, ip=audit.client_ip(request))
            messages.success(request, f"{obj.name} is back on.")
            return redirect("catalog:taxonomy")

        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "A name is required.")
            return redirect("catalog:taxonomy")
        # Lengths from the model: too long used to reach the database as a 500.
        limit = model._meta.get_field("name").max_length
        code = request.POST.get("code", "").strip()
        if len(name) > limit or (kind == "unit" and len(code) > 10):
            messages.error(request, f"Too long: a name is at most {limit} characters"
                                    + (", a unit code at most 10." if kind == "unit" else "."))
            return redirect("catalog:taxonomy")

        # Already there? Say so -- or switch it back on if it was retired.
        # "Added" used to show for a name that already existed, and a retired
        # one stayed hidden with no way back.
        parent = None
        if kind == "category" and request.POST.get("parent"):
            parent = Category.objects.filter(
                pk=int_or(request.POST.get("parent")), is_active=True).first()
            if parent is None:
                messages.error(request, "That category is no longer there.")
                return redirect("catalog:taxonomy")
            if parent.level >= CATEGORY_DEPTH:
                messages.error(
                    request,
                    f"A category goes three deep at most, like "
                    f"{parent.path_label}. Put this one further up.",
                )
                return redirect("catalog:taxonomy")

        if kind == "unit":
            code = code or name[:10].lower()
            clash = Unit.objects.filter(code__iexact=code).first()
        elif kind == "category":
            clash = Category.objects.filter(name__iexact=name, parent=parent).first()
        else:
            clash = model.objects.filter(name__iexact=name).first()
        if clash is not None:
            if clash.is_active:
                where = (f" inside {clash.parent.path_label}"
                         if kind == "category" and clash.parent_id else "")
                messages.error(
                    request, f"{label} {clash.name} is already on the list{where}.")
            else:
                clash.is_active = True
                clash.save(update_fields=["is_active", "updated_at"])
                audit.record(f"{kind}.restored", obj=clash, ip=audit.client_ip(request))
                messages.success(request, f"{clash.name} was switched off; it is back on.")
            return redirect("catalog:taxonomy")

        if kind == "category":
            obj = Category.objects.create(name=name, parent=parent)
        elif kind == "brand":
            obj = Brand.objects.create(name=name)
        elif kind == "unit":
            obj = Unit.objects.create(
                code=code, name=name,
                allows_decimal=request.POST.get("allows_decimal") == "on",
            )
        else:
            rate = decimal_or_none(request.POST.get("rate"))
            if rate is None or rate < 0 or rate > 100:
                messages.error(request, "A VAT rate is a percentage between 0 and 100.")
                return redirect("catalog:taxonomy")
            obj = TaxRate.objects.create(
                name=name, rate=rate,
                fiscal_code=request.POST.get("fiscal_code", "").strip()[:10],
            )
        audit.record(f"{kind}.created", obj=obj, after={"name": name},
                     ip=audit.client_ip(request))
        messages.success(
            request,
            f"{name} added inside {parent.path_label}." if parent else f"{name} added.",
        )
        return redirect("catalog:taxonomy")

    return render(
        request,
        "catalog/taxonomy.html",
        {
            "categories": category_tree(),
            "category_depth": CATEGORY_DEPTH,
            "brands": Brand.objects.filter(is_active=True).order_by("name"),
            "units": Unit.objects.filter(is_active=True).order_by("name"),
            "taxes": TaxRate.objects.filter(is_active=True).order_by("name"),
            "retired": [
                ("tax", "VAT rate", TaxRate.objects.filter(is_active=False).order_by("name")),
                ("unit", "Unit", Unit.objects.filter(is_active=False).order_by("name")),
                ("category", "Category", Category.objects.filter(is_active=False).order_by("name")),
                ("brand", "Brand", Brand.objects.filter(is_active=False).order_by("name")),
            ],
        },
    )


TAXONOMY_MODELS = {
    "category": ("Category", "catalog", "Category"),
    "brand": ("Brand", "catalog", "Brand"),
    "unit": ("Unit", "catalog", "Unit"),
    "tax": ("VAT rate", "catalog", "TaxRate"),
}


def _taxonomy_model(kind):
    from django.apps import apps as django_apps

    label, app_label, model_name = TAXONOMY_MODELS[kind]
    return label, django_apps.get_model(app_label, model_name)


@login_required
@requires("settings.edit")
def taxonomy_edit(request, kind, pk):
    """Rename a category, change a VAT rate, fix a unit typed wrongly."""
    if kind not in TAXONOMY_MODELS:
        raise Http404
    from apps.core.listing import close_modal

    label, model = _taxonomy_model(kind)
    obj = get_object_or_404(model, pk=pk)

    def form(error=""):
        return render(
            request, "catalog/taxonomy_edit.html" if not request.htmx else "catalog/_taxonomy_form.html",
            {"object": obj, "kind": kind, "label": label, "error": error,
             "modal": bool(request.htmx),
             "parents": (category_parents(exclude=obj, height=_category_height(obj))
                         if kind == "category" else [])},
        )

    if request.method == "POST":
        before = audit.snapshot(obj)
        name = request.POST.get("name", "").strip() or obj.name
        if len(name) > model._meta.get_field("name").max_length or \
                len(request.POST.get("code", "").strip()) > 10:
            return form("That is too long.")
        # Only categories have one, and a post that leaves the field out
        # entirely means "unchanged", not "move it to the top shelf".
        parent = obj.parent if kind == "category" else None
        if kind == "category" and "parent" in request.POST:
            chosen = request.POST.get("parent", "")
            parent = None
            if chosen:
                parent = Category.objects.filter(pk=int_or(chosen)).first()
                if parent is None or parent.pk in category_family(obj.pk):
                    # Into itself or into one of its own: the move that turns
                    # the list into a ring nothing can draw.
                    return form("A category cannot go inside itself.")
                if parent.level + _category_height(obj) > CATEGORY_DEPTH:
                    return form(
                        "That would go more than three deep. Move what is "
                        "inside this one first."
                    )

        clash = model.objects.filter(name__iexact=name).exclude(pk=obj.pk)
        if kind == "category":
            clash = clash.filter(parent=parent)
        if kind != "unit" and clash.exists():
            # A rename onto an existing name hit the unique index as a 500.
            return form(f"There is already a {label.lower()} called {name}.")
        obj.name = name
        if kind == "category":
            obj.parent = parent

        if kind == "unit":
            code = request.POST.get("code", "").strip() or obj.code
            if model.objects.filter(code__iexact=code).exclude(pk=obj.pk).exists():
                return form(f"The unit code {code} is already used.")
            obj.code = code
            obj.allows_decimal = request.POST.get("allows_decimal") == "on"
        elif kind == "tax":
            rate = decimal_or_none(request.POST.get("rate"))
            if rate is None or not (0 <= rate <= 100):
                return form("A VAT rate is a percentage between 0 and 100.")
            obj.rate = rate
            obj.fiscal_code = request.POST.get("fiscal_code", "").strip()[:10]
            if request.POST.get("is_default") == "on":
                model.objects.exclude(pk=obj.pk).update(is_default=False)
                obj.is_default = True

        obj.save()
        audit.record(f"{kind}.updated", obj=obj, before=before,
                     after=audit.snapshot(obj), ip=audit.client_ip(request))
        messages.success(request, f"{obj.name} saved.")
        return close_modal(request, reverse("catalog:taxonomy"))

    return form()


def _category_height(category):
    """How many levels this category is itself, counting what is inside it."""
    depth, level = 1, {category.pk: 1}
    for row in category_tree(include_inactive=True):
        if row.parent_id in level:
            level[row.pk] = level[row.parent_id] + 1
            depth = max(depth, level[row.pk])
    return depth


@login_required
@requires("settings.edit")
@require_POST
def taxonomy_delete(request, kind, pk):
    """
    Remove it if nothing uses it, switch it off if something does.

    A VAT rate that has been charged cannot vanish -- last year's returns
    have to keep meaning what they meant.
    """
    if kind not in TAXONOMY_MODELS:
        raise Http404
    label, model = _taxonomy_model(kind)
    obj = get_object_or_404(model, pk=pk)

    blockers = ()
    if kind == "tax" and getattr(obj, "is_default", False):
        blockers = ((lambda: True, "Choose another default VAT rate first."),)
    if kind == "category":
        # parent is SET_NULL, so removing Drinks would quietly tip Soda and
        # Water out onto the top level and nobody would be told.
        blockers = (
            (lambda: obj.children.exists(),
             f"Remove what is inside {obj.name} first, or move it elsewhere."),
        )

    outcome = remove_or_archive(obj, label=f"{label} {obj}", blockers=blockers)
    if not outcome.blocked:
        audit.record(f"{kind}.archived" if outcome.archived else f"{kind}.removed",
                     obj=obj, ip=audit.client_ip(request))

    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("catalog:taxonomy")


@login_required
@requires("product.manage")
@require_POST
def barcode_delete(request, pk):
    """
    Remove a code from a product.

    Happens whenever a supplier changes packaging and the old code starts
    appearing on somebody else's goods.
    """
    barcode = get_object_or_404(Barcode.objects.select_related("variant"), pk=pk)
    variant = barcode.variant
    code = barcode.code
    was_primary = barcode.is_primary
    barcode.delete()
    # The product form shows the primary code; without one it showed blank
    # and saving put the removed code straight back.
    if was_primary:
        nxt = variant.barcodes.order_by("pk").first()
        if nxt is not None:
            nxt.is_primary = True
            nxt.save(update_fields=["is_primary"])

    audit.record("barcode.removed", obj=variant, after={"code": code}, ip=audit.client_ip(request))
    if request.htmx:
        return _barcode_box(request, variant)
    messages.success(request, f"{code} removed.")
    return redirect("catalog:product_edit", pk=variant.product_id)


@login_required
@requires("product.manage")
@require_POST
def product_delete(request, pk):
    """Gone if it has never been sold, switched off if it has."""
    product = get_object_or_404(Product, pk=pk)
    name = product.name
    outcome = remove_or_archive(product, label=name)

    if not outcome.blocked:
        audit.record("product.removed", obj=product, ip=audit.client_ip(request))
    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("catalog:product_list")


@login_required
@requires("product.set_price")
def price_lists(request):
    """
    Retail and wholesale.

    A second list is how a shop sells the same product to a bulk buyer without
    duplicating it in the catalogue.
    """
    from apps.catalog.models import PriceList, PriceListKind

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            from apps.core.features import WHOLESALE_PRICING

            name = request.POST.get("name", "").strip()[:60]
            kind = request.POST.get("kind", PriceListKind.RETAIL)
            if kind not in PriceListKind.values:
                kind = PriceListKind.RETAIL
            if not name:
                messages.error(request, "A name is required.")
            elif PriceList.objects.filter(name__iexact=name).exists():
                messages.error(request, f"There is already a price list called {name}.")
            elif kind == PriceListKind.WHOLESALE and not request.tenant.has_feature(
                WHOLESALE_PRICING
            ):
                messages.error(request, "Wholesale pricing is not included in your plan.")
            elif not request.tenant.has_feature(WHOLESALE_PRICING):
                # The plan sells "a second price list for bulk buyers". Gating
                # only the wholesale *kind* left the door open: a shop without
                # the feature made a second "retail" list and charged bulk
                # prices off it. Every shop starts with one list; a second one
                # is the thing being paid for, whatever it is called.
                messages.error(
                    request, "A second price list is not included in your plan."
                )
            else:
                created = PriceList.objects.create(name=name, kind=kind)
                audit.record("pricelist.created", obj=created, ip=audit.client_ip(request))
                messages.success(request, f"{name} created. Put prices on it from each product's Edit.")
        elif action == "default":
            chosen = get_object_or_404(PriceList, pk=int_or(request.POST.get("price_list")),
                                       is_active=True)
            from apps.catalog.models import Price

            missing = (Variant.objects.filter(is_active=True, product__is_active=True,
                                              product__sellable_at_pos=True)
                       .exclude(pk__in=Price.objects.filter(price_list=chosen)
                                .values("variant")).count())
            if missing:
                # Every product without a price on the new default rang up
                # at 0 on the till the moment it switched.
                messages.error(request, f"{missing} product{'s' if missing != 1 else ''} "
                                        f"have no price on {chosen.name}. Give every product a "
                                        "price there first.")
                return redirect("catalog:price_lists")
            PriceList.objects.exclude(pk=chosen.pk).update(is_default=False)
            chosen.is_default = True
            chosen.save(update_fields=["is_default", "updated_at"])
            audit.record("pricelist.default", obj=chosen, ip=audit.client_ip(request))
            messages.success(request, f"{chosen.name} is now the default.")
        elif action == "rename":
            price_list = get_object_or_404(PriceList, pk=int_or(request.POST.get("price_list")))
            name = request.POST.get("name", "").strip()[:60]
            if not name:
                messages.error(request, "A name is required.")
            elif PriceList.objects.filter(name__iexact=name).exclude(pk=price_list.pk).exists():
                messages.error(request, f"There is already a price list called {name}.")
            else:
                before = price_list.name
                price_list.name = name
                price_list.save(update_fields=["name", "updated_at"])
                audit.record("pricelist.renamed", obj=price_list,
                             before={"name": before}, after={"name": name},
                             ip=audit.client_ip(request))
                messages.success(request, f"Renamed to {name}.")
        elif action == "restore":
            price_list = get_object_or_404(PriceList, pk=int_or(request.POST.get("price_list")),
                                           is_active=False)
            price_list.is_active = True
            price_list.save(update_fields=["is_active", "updated_at"])
            audit.record("pricelist.restored", obj=price_list, ip=audit.client_ip(request))
            messages.success(request, f"{price_list.name} is back on.")
        elif action == "delete":
            price_list = get_object_or_404(PriceList, pk=int_or(request.POST.get("price_list")))
            if price_list.is_default:
                messages.error(request, "Make another list the default first.")
            else:
                outcome = remove_or_archive(price_list, label=price_list.name)
                if not outcome.blocked:
                    audit.record("pricelist.removed", obj=price_list, ip=audit.client_ip(request))
                # Blocked deletes used to report success.
                (messages.success if outcome else messages.error)(request, outcome.message)
        return redirect("catalog:price_lists")

    return render(
        request, "catalog/price_lists.html",
        {"price_lists": PriceList.objects.annotate(items=Count("prices")).order_by("-is_default", "name")},
    )


@login_required
@requires("settings.edit")
def tiles(request):
    """
    The till's touch grid.

    Without this screen the till shows an empty state telling a shop to set up
    tiles and gives them no way to do it -- and tiles are how anything without
    a barcode gets sold.
    """
    from django.db.models import Max

    from apps.catalog.models import QuickKey
    from apps.catalog.services import tiles as till_tiles

    branches = request.membership.branches()
    mine = QuickKey.objects.filter(Q(branch__isnull=True) | Q(branch__in=branches))

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            variant = get_object_or_404(Variant, pk=int_or(request.POST.get("variant")),
                                        is_active=True)
            raw_branch = request.POST.get("branch", "")
            branch = None
            if raw_branch:
                branch = get_object_or_404(branches, pk=int_or(raw_branch))
            plu = request.POST.get("plu_code", "").strip()[:8]
            if plu and QuickKey.objects.filter(plu_code__iexact=plu).exists():
                # Typing a code at the till finds the first match only; a
                # second tile with the same code could never be reached.
                messages.error(request, f"The code {plu} is already on another tile.")
                return redirect("catalog:tiles")
            tile, created = QuickKey.objects.get_or_create(
                variant=variant, branch=branch,
                defaults={
                    "plu_code": plu,
                    "position": (QuickKey.objects.aggregate(m=Max("position"))["m"] or 0) + 1,
                },
            )
            if created:
                audit.record("tile.added", obj=tile, after={"variant": str(variant)},
                             ip=audit.client_ip(request))
                messages.success(request, f"{variant} added to the till.")
            else:
                messages.info(request, f"{variant} is already on the till.")
        elif action == "remove":
            tile = get_object_or_404(mine, pk=int_or(request.POST.get("tile")))
            audit.record("tile.removed", obj=tile, after={"variant": str(tile.variant)},
                         ip=audit.client_ip(request))
            tile.delete()
            messages.success(request, "Removed from the till.")
        return redirect("catalog:tiles")

    existing = mine.select_related("variant__product", "branch").order_by("position")
    term = request.GET.get("q", "").strip()
    candidates = (
        Variant.objects.select_related("product__category")
        .filter(is_active=True, product__is_active=True, product__sellable_at_pos=True)
        .exclude(pk__in=existing.filter(branch__isnull=True).values("variant"))
        .order_by("product__name")
    )
    if term:
        candidates = candidates.filter(
            Q(product__name__icontains=term) | Q(name__icontains=term) | Q(product__sku__icontains=term)
        )
    shown = len(till_tiles(branch=request.branch)) if request.branch else None
    return render(
        request,
        "catalog/tiles.html",
        {
            "tiles": existing,
            "candidates": candidates[:100],
            "more": candidates.count() > 100,
            "q": term,
            "branches": branches,
            "till_cap": 60,
            "shown": shown,
        },
    )

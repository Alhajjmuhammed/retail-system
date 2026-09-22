"""
The sidebar, worked out once and handed to the template as data.

Two hundred lines of repeated markup used to decide, in Django template
tags, three separate things at once: may this person see it, is it the page
they are on, and what is it called. That is why one entry could quietly
light up another's page for months.

Here each entry says those things plainly, and a shop that has never moved
stock between branches does not carry the words "Move stock" down its
sidebar every day: entries marked ``optional`` wait under "more" until the
shop uses them, and then stay.
"""

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    title: str
    icon: str
    route: str
    # What this person must hold to see it at all. Hiding is a courtesy; the
    # view's own decorator is the boundary.
    needs: str | None = None
    # url_names that mean "you are here". Matched against the resolved view.
    here: tuple[str, ...] = ()
    # ...narrowed to one app, where two apps share a url_name.
    here_app: str | None = None
    # An entry that waits under "more" until the shop has used it.
    optional: bool = False
    # Which key in apps.core.nav says whether it has been used.
    use_key: str | None = None

    def url(self) -> str:
        return reverse(self.route)


SECTIONS: list[tuple[str, tuple[Entry, ...]]] = [
    ("", (
        Entry("dashboard", "Dashboard", "Dashboard", "home", "core:dashboard",
              here=("dashboard",)),
        Entry("till", "Till", "Till", "cart", "pos:till",
              needs="pos.operate", here=("till",)),
        Entry("phone", "Sell on phone", "Sell on phone", "tag", "pos:phone",
              needs="pos.mobile_cart", here=("phone",)),
        Entry("sales", "Sales", "Sales", "receipt", "pos:sale_list",
              needs="report.sales", here=("sale_list", "sale_detail")),
    )),
    ("Inventory", (
        Entry("products", "Products", "Products", "tag", "catalog:product_list",
              needs="product.view",
              here=("product_list", "product_form", "product_create", "import")),
        Entry("categories", "Categories", "How your products are grouped", "tag",
              "catalog:categories", needs="settings.edit", here=("categories",),
              here_app="catalog"),
        Entry("stock", "Stock", "Stock", "box", "inventory:stock_list",
              needs="stock.view",
              here=("stock_list", "movements", "stock_adjust")),
        Entry("transfers", "Move stock", "Move stock between branches", "truck",
              "inventory:transfer_list", needs="stock.transfer",
              here_app="inventory", here=("transfer_list", "transfer_form", "transfer_detail"),
              optional=True, use_key="transfers"),
        Entry("counts", "Stock check", "Count the shelves", "clipboard",
              "inventory:count_list", needs="stock.count",
              here_app="inventory", here=("count_list", "count_detail", "count_create"),
              optional=True, use_key="counts"),
        Entry("expiry", "Expiry", "What is going off", "clock",
              "inventory:batch_list", needs="stock.batches", here=("batch_list",),
              optional=True, use_key="batches"),
    )),
    ("Buying", (
        Entry("deliveries", "Deliveries", "Deliveries that have arrived", "layers",
              "purchasing:receipt_list", needs="stock.receive",
              here_app="purchasing", here=("receipt_list", "receipt_detail", "receipt_create"),
              optional=True, use_key="receipts"),
        Entry("orders", "Supplier orders", "Orders to suppliers", "clipboard",
              "purchasing:order_list", needs="po.manage",
              here_app="purchasing", here=("order_list", "order_detail", "order_create"),
              optional=True, use_key="orders"),
        Entry("suppliers", "Suppliers", "Who you buy from", "building",
              "purchasing:supplier_list", needs="supplier.manage",
              here_app="purchasing", here=("supplier_list", "supplier_detail", "supplier_create"),
              optional=True, use_key="suppliers"),
    )),
    ("Money", (
        Entry("customers", "Customers", "Customers", "users",
              "customers:customer_list", needs="customer.manage", here_app="customers"),
        Entry("owed", "Who owes", "Who owes money", "wallet",
              "customers:statements", needs="credit.collect", here=("statements",)),
        Entry("expenses", "Expenses", "Expenses", "wallet",
              "finance:expense_list", needs="expense.create",
              here_app="finance", here=("expense_list", "expense_create"),
              optional=True, use_key="expenses"),
        Entry("cashups", "End of day", "Counting the drawer at the end of a shift",
              "credit-card", "finance:cashups", needs="cashup.approve", here=("cashups",),
              optional=True, use_key="cashups"),
    )),
    ("Reports", (
        Entry("rep_sales", "Sales", "Sales report", "chart", "reports:index",
              needs="report.sales", here_app="reports", here=("index",)),
        Entry("rep_profit", "Profit", "Profit", "trending", "reports:margin",
              needs="report.margin", here=("margin",)),
        Entry("rep_stock", "Stock value", "Stock value", "box", "reports:stock_value",
              needs="report.stock", here=("stock_value",)),
        Entry("rep_staff", "Who sold what", "Who sold what, and who was short",
              "activity", "reports:staff", needs="report.staff",
              here_app="reports", here=("staff",)),
        Entry("activity", "Activity", "Activity", "clipboard", "accounts:audit_log",
              needs="report.staff", here=("audit_log", "audit_entry")),
    )),
]

# The ten settings pages behind one door. Any of these permissions opens it.
SETTINGS = Entry(
    "settings", "Settings", "Settings", "settings", "org:settings_home",
)
SETTINGS_KEYS = ("user.manage", "role.manage", "register.manage",
                 "product.set_price", "settings.edit", "billing.manage")


def _is_here(entry, url_name, app) -> bool:
    if entry.here_app and app != entry.here_app:
        return False
    if entry.key == "dashboard":
        # The platform side has a page of the same name.
        return url_name == "dashboard" and app != "platform"
    if entry.key == "customers":
        return app == "customers"
    if entry.key == "products":
        return app == "catalog" and url_name in entry.here
    return url_name in entry.here


def build(request) -> dict:
    """
    The sidebar for this person, on this page, in this shop.

    Returns the sections to draw, and for each the entries shown now and the
    ones waiting under "more".
    """
    from apps.core import nav

    membership = getattr(request, "membership", None)
    if membership is None:
        return {"sections": [], "settings": None}

    match = request.resolver_match
    url_name = match.url_name if match else ""
    app = match.app_name if match else ""
    used = nav.in_use(request.tenant)
    branches = "branches" in used

    sections = []
    for label, entries in SECTIONS:
        shown, later = [], []
        for entry in entries:
            if entry.needs and not membership.can(entry.needs):
                continue
            # "Who owes" is the customers page for somebody who may collect
            # but not manage; with both, Customers covers it.
            if entry.key == "owed" and membership.can("customer.manage"):
                continue
            row = {"key": entry.key, "label": entry.label, "title": entry.title,
                   "icon": entry.icon, "url": entry.url(),
                   "active": _is_here(entry, url_name, app)}
            waiting = (entry.optional
                       and (entry.use_key not in used)
                       and not (entry.key == "transfers" and branches)
                       and not row["active"])
            (later if waiting else shown).append(row)
        if shown or later:
            sections.append({"label": label, "entries": shown, "more": later})

    settings = None
    if any(membership.can(code) for code in SETTINGS_KEYS):
        path = request.path
        # Every settings page lives under one of these two paths. The
        # activity log shares the first and belongs to Reports.
        here = (path.startswith(("/settings/", "/products/settings/"))
                and not url_name.startswith("audit"))
        settings = {"key": "settings", "label": SETTINGS.label, "title": SETTINGS.title,
                    "icon": SETTINGS.icon, "url": SETTINGS.url(), "active": here}
    return {"sections": sections, "settings": settings}

"""
The permission catalogue.

Each app owns its own declarations; they are collected here for the modules
that exist so far and imported at app-ready time. Adding a module means adding
its permissions.py and nothing else -- the role builder picks them up, existing
tenant roles keep working, and every new code defaults to denied.
"""

from apps.core.features import (
    BATCH_EXPIRY,
    CUSTOMER_CREDIT,
    FISCAL_RECEIPTS,
    MOBILE_SELLING,
    MULTI_BRANCH,
    PURCHASING,
    REPORT_EXPORT,
    STOCK_TRANSFERS,
)
from apps.core.permissions import PermissionSpec as P
from apps.core.permissions import ValueType as V
from apps.core.permissions import registry

SALES = "Sales"
PRODUCTS = "Products"
STOCK = "Stock"
BUYING = "Purchasing"
PEOPLE = "Customers"
MONEY = "Money"
REPORTS = "Reports"
ADMIN = "Administration"


registry.register_many([
    # -- Sales -------------------------------------------------------------
    P("pos.operate", SALES, "Use a till"),
    P("pos.sell", SALES, "Complete a sale"),
    P("pos.discount", SALES, "Give a discount", V.PERCENT, is_dangerous=True),
    P("pos.price_override", SALES, "Change a price at the till", is_dangerous=True),
    P("pos.void", SALES, "Void a sale", V.AMOUNT, is_dangerous=True),
    P("pos.refund", SALES, "Refund a sale", V.AMOUNT, is_dangerous=True),
    P("pos.open_item", SALES, "Sell an unlisted item", V.AMOUNT, is_dangerous=True),
    P("pos.reprint", SALES, "Reprint a receipt"),
    P("pos.mobile_cart", SALES, "Build a basket on a phone",
      requires_feature=MOBILE_SELLING),
    P("pos.mobile_payment", SALES, "Take payment on a phone", V.AMOUNT,
      is_dangerous=True, requires_feature=MOBILE_SELLING),
    P("pos.mobile_methods", SALES, "Payment methods allowed on a phone", V.SET,
      requires_feature=MOBILE_SELLING,
      options=("cash", "mpesa", "tigopesa", "airtelmoney", "card")),

    # -- Products ----------------------------------------------------------
    P("product.view", PRODUCTS, "See the product list"),
    P("product.view_cost", PRODUCTS, "See cost price and margin"),
    P("product.manage", PRODUCTS, "Create and edit products"),
    P("product.set_price", PRODUCTS, "Change selling prices", is_dangerous=True),

    # -- Stock -------------------------------------------------------------
    P("stock.view", STOCK, "See stock quantities"),
    # Value-limited: putting a delivery into stock also records what the
    # shop owes for it, from a cost the receiver types. Unlimited, a clerk
    # could create any debt at all.
    P("stock.receive", STOCK, "Receive goods", V.AMOUNT, is_dangerous=True),
    P("stock.adjust", STOCK, "Adjust stock", V.AMOUNT, is_dangerous=True),
    P("stock.wastage", STOCK, "Write off damage or expiry", V.AMOUNT, is_dangerous=True),
    P("stock.transfer", STOCK, "Transfer stock between branches",
      requires_feature=STOCK_TRANSFERS),
    P("stock.count", STOCK, "Run a stock count"),
    P("stock.batches", STOCK, "Manage batches and expiry", requires_feature=BATCH_EXPIRY),

    # -- Purchasing --------------------------------------------------------
    P("po.manage", BUYING, "Create purchase orders", requires_feature=PURCHASING),
    P("po.approve", BUYING, "Approve purchase orders", V.AMOUNT,
      is_dangerous=True, requires_feature=PURCHASING),
    P("supplier.manage", BUYING, "Manage suppliers", requires_feature=PURCHASING),
    P("supplier.pay", BUYING, "Record supplier payments", V.AMOUNT,
      is_dangerous=True, requires_feature=PURCHASING),

    # -- Customers ---------------------------------------------------------
    P("customer.manage", PEOPLE, "Manage customers"),
    P("credit.grant", PEOPLE, "Sell on credit", V.AMOUNT,
      is_dangerous=True, requires_feature=CUSTOMER_CREDIT),
    P("credit.collect", PEOPLE, "Record a customer payment",
      requires_feature=CUSTOMER_CREDIT),

    # -- Money -------------------------------------------------------------
    P("expense.create", MONEY, "Record an expense", V.AMOUNT),
    P("expense.approve", MONEY, "Approve an expense", is_dangerous=True),
    P("cashup.perform", MONEY, "Close a shift and count the drawer"),
    # Money out of the drawer that is not a sale. Unlimited, it hid any
    # shortfall: a 9,000,000 "pay out" left the cash-up looking perfect.
    P("cash.movement", MONEY, "Take money out of the till or bank it", V.AMOUNT,
      is_dangerous=True),
    P("cashup.approve", MONEY, "Approve a cash variance", is_dangerous=True),
    P("fiscal.manage", MONEY, "Manage fiscal receipts", requires_feature=FISCAL_RECEIPTS),

    # -- Reports -----------------------------------------------------------
    P("report.sales", REPORTS, "Sales reports"),
    P("report.margin", REPORTS, "Profit and margin reports"),
    P("report.stock", REPORTS, "Stock valuation and movement"),
    P("report.staff", REPORTS, "Staff performance and variance"),
    P("report.export", REPORTS, "Export reports", requires_feature=REPORT_EXPORT),

    # -- Administration ----------------------------------------------------
    P("user.manage", ADMIN, "Invite and manage staff"),
    P("role.manage", ADMIN, "Create roles and set permissions"),
    P("branch.manage", ADMIN, "Manage branches", requires_feature=MULTI_BRANCH),
    # Tills and phones exist on every plan: a one-shop owner has to be able
    # to add a till and switch off a stolen phone.
    P("register.manage", ADMIN, "Manage tills and devices"),
    P("settings.edit", ADMIN, "Change business settings"),
    P("billing.manage", ADMIN, "Manage the subscription and invoices"),
])


# --------------------------------------------------------------------------
# Starter roles
#
# Cloned into every new tenant, then fully theirs to edit. Owner is the
# exception: it always holds everything and the last one cannot be removed.
# --------------------------------------------------------------------------

STARTER_ROLES = {
    "Owner": {
        "description": "Full access to everything, including billing.",
        "is_owner_role": True,
        "permissions": {},
    },
    "Manager": {
        "description": "Runs the shop day to day.",
        "permissions": {
            "pos.operate": None, "pos.sell": None, "pos.reprint": None,
            "pos.discount": 20, "pos.void": 500000, "pos.refund": 500000,
            "pos.price_override": None, "pos.open_item": 100000,
            "pos.mobile_cart": None, "pos.mobile_payment": 500000,
            "pos.mobile_methods": ["cash", "mpesa", "tigopesa", "airtelmoney"],
            "product.view": None, "product.view_cost": None,
            "product.manage": None, "product.set_price": None,
            "stock.view": None, "stock.receive": None, "stock.adjust": 500000,
            "stock.wastage": 200000, "stock.transfer": None,
            "stock.count": None, "stock.batches": None,
            "po.manage": None, "po.approve": 2000000,
            "supplier.manage": None, "supplier.pay": 1000000,
            "customer.manage": None, "credit.grant": 500000, "credit.collect": None,
            "expense.create": 200000, "expense.approve": None,
            "cashup.perform": None, "cashup.approve": None, "cash.movement": 500000,
            "report.sales": None, "report.margin": None,
            "report.stock": None, "report.staff": None, "report.export": None,
            "user.manage": None, "settings.edit": None, "register.manage": None,
        },
    },
    "Cashier": {
        "description": "Sells at the till. No cost prices, no adjustments.",
        "permissions": {
            "pos.operate": None, "pos.sell": None, "pos.reprint": None,
            "pos.discount": 5,
            "product.view": None,
            "stock.view": None,
            "customer.manage": None,
            "cashup.perform": None, "cash.movement": 50000,
        },
    },
    "Stock clerk": {
        "description": "Receives and counts stock. Does not sell.",
        "permissions": {
            "product.view": None, "product.manage": None,
            "stock.view": None, "stock.receive": 2000000, "stock.count": None,
            "stock.adjust": 100000, "stock.wastage": 50000,
            "stock.transfer": None, "stock.batches": None,
            "po.manage": None, "supplier.manage": None,
            "report.stock": None,
        },
    },
}

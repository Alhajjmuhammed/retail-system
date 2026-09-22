"""
Which parts of the system this shop actually uses.

A duka that has never moved stock between branches, never counted a shelf
and never raised a purchase order should not carry those three words down
its sidebar every day. They stay one click away under "More", and the moment
the shop uses one it moves up and stays up.

The checks are existence queries only -- `EXISTS (SELECT 1 ...)` -- and the
answer is cached per shop, because a sidebar is rendered on every page load
and nobody starts using purchase orders twice a minute.
"""

from django.core.cache import cache

# Each key is a nav entry that has to earn its place. The callable answers
# "has this shop ever done this?" -- never "may this person see it", which
# stays a matter of permissions and is decided separately.
CACHE_SECONDS = 300


def _checks():
    from apps.catalog.models import PriceList, QuickKey
    from apps.customers.models import Customer
    from apps.finance.models import Expense
    from apps.inventory.models import Batch, StockCount, Transfer
    from apps.notifications.models import Message
    from apps.org.models import Branch
    from apps.pos.models import FiscalReceipt, Shift
    from apps.purchasing.models import GoodsReceipt, PurchaseOrder, Supplier

    return {
        "transfers": lambda: Transfer.objects.exists(),
        "counts": lambda: StockCount.objects.exists(),
        "batches": lambda: Batch.objects.exists(),
        "receipts": lambda: GoodsReceipt.objects.exists(),
        "orders": lambda: PurchaseOrder.objects.exists(),
        "suppliers": lambda: Supplier.objects.exists(),
        "customers": lambda: Customer.objects.exists(),
        "expenses": lambda: Expense.objects.exists(),
        "fiscal": lambda: FiscalReceipt.objects.exists(),
        "cashups": lambda: Shift.objects.filter(closed_at__isnull=False).exists(),
        "prices": lambda: PriceList.objects.filter(is_default=False).exists(),
        "messages": lambda: Message.objects.exists(),
        "tiles": lambda: QuickKey.objects.exists(),
        # More than one branch is what makes "which branch" a question at all.
        "branches": lambda: Branch.objects.count() > 1,
    }


def in_use(tenant) -> set:
    """
    The keys this shop has used at least once.

    A miss costs fourteen EXISTS queries, once every five minutes per shop.
    A shop that starts using something sees it move up within that.
    """
    key = f"nav-in-use:{tenant.pk}"
    found = cache.get(key)
    if found is None:
        found = {name for name, check in _checks().items() if check()}
        cache.set(key, found, CACHE_SECONDS)
    return found


def forget(tenant) -> None:
    """Drop the cached answer -- for tests, and for the seeder."""
    cache.delete(f"nav-in-use:{tenant.pk}")

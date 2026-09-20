"""
Domain events.

This is the extension seam. When something meaningful happens -- a sale
completes, stock crosses its reorder level, a subscription lapses -- the
module that owns it emits an event and moves on. Anything that needs to react
subscribes.

The rule this enforces: adding loyalty, SMS receipts, an accounting export or
an e-commerce sync must never mean reopening the checkout code. By the sixth
such feature, a view that everyone edits is a view nobody can change safely.

    from apps.core.events import events, SALE_COMPLETED

    @events.on(SALE_COMPLETED)
    def award_points(sale, **kwargs):
        ...
"""

import logging
from collections import defaultdict
from collections.abc import Callable

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Event names. Modules may declare their own; these are the shared ones.
# --------------------------------------------------------------------------

SALE_COMPLETED = "sale.completed"
SALE_VOIDED = "sale.voided"
SALE_RETURNED = "sale.returned"
SALE_SYNCED = "sale.synced"

STOCK_MOVED = "stock.moved"
STOCK_LOW = "stock.low"
STOCK_NEGATIVE = "stock.negative"
BATCH_EXPIRING = "batch.expiring"

GOODS_RECEIVED = "purchasing.goods_received"

SHIFT_OPENED = "shift.opened"
SHIFT_CLOSED = "shift.closed"
CASH_VARIANCE = "shift.variance"

CREDIT_EXTENDED = "customer.credit_extended"
CREDIT_LIMIT_HIT = "customer.credit_limit_hit"

TENANT_CREATED = "tenant.created"
SUBSCRIPTION_CHANGED = "subscription.changed"
SUBSCRIPTION_LAPSED = "subscription.lapsed"

PERMISSION_OVERRIDDEN = "permission.overridden"


class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, *, priority: int = 100):
        """Decorator. Lower priority numbers run first."""

        def wrapper(func: Callable) -> Callable:
            self._listeners[event].append((priority, func))
            self._listeners[event].sort(key=lambda pair: pair[0])
            return func

        return wrapper

    def subscribe(self, event: str, func: Callable, *, priority: int = 100) -> None:
        self._listeners[event].append((priority, func))
        self._listeners[event].sort(key=lambda pair: pair[0])

    def emit(self, event: str, **payload) -> None:
        """
        Fire an event.

        A failing listener is logged and skipped, never allowed to roll back
        the thing that happened. A sale that completed must not be undone
        because an SMS gateway was down.
        """
        for _priority, func in self._listeners.get(event, []):
            try:
                func(event=event, **payload)
            except Exception:
                logger.exception(
                    "Listener %s failed handling %s", func.__qualname__, event
                )

    def listeners(self, event: str) -> list[Callable]:
        return [func for _priority, func in self._listeners.get(event, [])]


events = EventBus()

"""
Money in more than one currency.

A shop keeps its books in one currency -- its own, ``Tenant.currency``. It may
also deal in others (a hotel billed in dollars, a tourist paying in euros),
each with a rate the owner keeps under Settings: how much of the shop's own
currency one unit buys.

Everything that lands in the books -- a customer's account, a sale, stock at
cost, a report -- is in the shop's own currency. A document written in another
currency carries the rate it was written with, frozen, and converts through it.
Rounding follows the currency of the figure: shillings to the shilling, dollars
to the cent.
"""

from decimal import ROUND_HALF_UP, Decimal

from apps.core.amounts import CURRENCIES

# The currencies a shop can pick from. The words for each live in
# core.amounts, so a total in words reads right in whichever is chosen.
CHOICES = [
    ("TZS", "Tanzanian shilling"),
    ("USD", "US dollar"),
    ("EUR", "Euro"),
    ("GBP", "British pound"),
    ("KES", "Kenyan shilling"),
    ("UGX", "Ugandan shilling"),
    ("AED", "UAE dirham"),
]
KNOWN = {code for code, _ in CHOICES} | set(CURRENCIES)
CENT = Decimal("0.01")
ONE = Decimal("1")


def main_currency(tenant=None) -> str:
    """The currency the shop keeps its books in."""
    if tenant is None:
        from apps.core.context import get_current_tenant

        tenant = get_current_tenant()
    return (getattr(tenant, "currency", None) or "TZS").upper()


def rates(tenant=None) -> dict:
    """Every other currency the shop deals in, code to rate, current ones only."""
    from apps.org.models import ExchangeRate

    rows = ExchangeRate.objects.filter(is_active=True)
    if tenant is not None:
        rows = ExchangeRate.objects_all.filter(tenant=tenant, is_active=True)
    main = main_currency(tenant)
    return {row.code: row.rate for row in rows if row.code != main}


def enabled(tenant=None) -> list:
    """The shop's own currency first, then the others it has a rate for."""
    return [main_currency(tenant), *sorted(rates(tenant))]


def rate_for(code, tenant=None) -> Decimal:
    """
    What one unit of ``code`` is worth in the shop's own currency today.
    Its own currency is worth exactly one; one with no rate is refused.
    """
    code = (code or "").upper()
    if not code or code == main_currency(tenant):
        return ONE
    found = rates(tenant).get(code)
    if found is None:
        raise ValueError(
            f"There is no rate for {code}. Add it under Settings, Currencies first."
        )
    return found


def quantum(code) -> Decimal:
    """The smallest coin a currency comes in."""
    from apps.pos.services import WHOLE_UNIT_CURRENCIES

    return ONE if (code or "").upper() in WHOLE_UNIT_CURRENCIES else CENT


def round_to(amount, code) -> Decimal:
    """An amount in ``code``, to its smallest coin, half up."""
    return Decimal(str(amount)).quantize(quantum(code), rounding=ROUND_HALF_UP)


def to_main(amount, rate, tenant=None) -> Decimal:
    """An amount in a document's currency, in the shop's own, to its coin."""
    return round_to(Decimal(str(amount)) * Decimal(rate), main_currency(tenant))


def from_main(amount, rate) -> Decimal:
    """
    A price in the shop's own currency, in another, to the cent. Prices keep
    their cents whatever the currency -- only amounts charged round to a coin.
    """
    if not rate:
        return Decimal(str(amount))
    return (Decimal(str(amount)) / Decimal(rate)).quantize(CENT, rounding=ROUND_HALF_UP)


def describe_rate(code, rate, tenant=None) -> str:
    """'1 USD = 2,650 TZS', for a document and the page that shows it."""
    figure = format(Decimal(rate).normalize(), ",f")
    return f"1 {code} = {figure} {main_currency(tenant)}"

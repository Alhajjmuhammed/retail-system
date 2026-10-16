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


# -- market rates ------------------------------------------------------------
#
# A free public service, updated daily, that needs no account and carries the
# shilling. Asked in dollars, because the figures it gives against a weak
# currency are rounded too short to invert: 0.00038 dollars a shilling is a
# rate of anywhere from 2,600 to 2,670.

MARKET_URL = "https://open.er-api.com/v6/latest/USD"
MARKET_TIMEOUT = 10


def fetch_market_table() -> dict:
    """
    Units of each currency one US dollar buys, today, from the rates service.
    Raises ValueError with a sentence a shopkeeper can read when it cannot.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(MARKET_URL, timeout=MARKET_TIMEOUT) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except Exception as exc:  # network, timeout, bad reply: all the same to the shop
        raise ValueError("The market rates could not be fetched just now. "
                         "Try again later, or type the rate yourself.") from exc
    table = data.get("rates") or {}
    if data.get("result") != "success" or "TZS" not in table:
        raise ValueError("The market rates service gave no usable answer. "
                         "Try again later, or type the rate yourself.")
    return {code: Decimal(str(value)) for code, value in table.items()}


def market_rate(code, main, table) -> Decimal:
    """What one ``code`` is worth in ``main``, from a dollar-based table."""
    code, main = code.upper(), main.upper()
    if code not in table or main not in table:
        raise ValueError(f"The market rates service does not carry {code if code not in table else main}.")
    return (table[main] / table[code]).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def refresh_market_rates(tenant=None, table=None) -> tuple:
    """
    Bring every market-following rate up to today's figure. Rates the owner
    typed are left alone. Returns (updated, skipped) counts.
    """
    from django.utils import timezone

    from apps.org.models import ExchangeRate

    table = table if table is not None else fetch_market_table()
    rows = ExchangeRate.objects.filter(is_active=True, source="market")
    if tenant is not None:
        rows = ExchangeRate.objects_all.filter(tenant=tenant, is_active=True, source="market")
    main = main_currency(tenant)
    updated = skipped = 0
    now = timezone.now()
    for row in rows:
        try:
            row.rate = market_rate(row.code, main, table)
        except ValueError:
            skipped += 1
            continue
        row.fetched_at = now
        row.save(update_fields=["rate", "fetched_at", "updated_at"])
        updated += 1
    return updated, skipped


def compare(amount_main, exclude=None, tenant=None) -> list:
    """
    An amount in the shop's own currency, shown in every other it deals in
    at today's rates: [("USD", 9.20), ("EUR", 8.41)]. For comparing, never
    for charging -- a document charges at its own frozen rate.
    """
    if amount_main is None:
        return []
    main = main_currency(tenant)
    amount_main = Decimal(str(amount_main))
    out = [] if exclude == main else [(main, round_to(amount_main, main))]
    for code, rate in sorted(rates(tenant).items()):
        if code != exclude:
            out.append((code, round_to(amount_main / rate, code)))
    return out

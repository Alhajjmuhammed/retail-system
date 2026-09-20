"""
Reading numbers typed into forms and query strings.

`Decimal("abc")` raises InvalidOperation, which is not a ValueError, so the
`except ValueError` around most views let it through as a 500 page. Every
view that reads a number from the request goes through these instead.
"""

from decimal import Decimal, InvalidOperation

# The smallest money column holds 12 digits with 2 decimals. Anything bigger
# is a typo that would otherwise reach the database as an error page.
LARGEST = Decimal("9999999999")


class BadInput(ValueError):
    """A value the person typed that cannot be used. The message is for them."""


def decimal_or_none(raw):
    """A Decimal, or None for blank or garbage. For filters and optional fields."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) > LARGEST:
        return None
    return value


def parse_decimal(raw, label="Amount", *, positive=False, minimum=None, default=None,
                  places=None):
    """
    A Decimal the view can trust, or BadInput with a message to show.

    `positive` refuses zero and below; `minimum` sets an inclusive floor;
    `places` refuses more decimals than the column keeps. Money is places=2:
    100.005 used to be stored as 100.01 on one row and 0.00 on another,
    leaving stray cents on accounts.
    """
    value = decimal_or_none(raw)
    if value is not None and places is not None and value != value.quantize(
            Decimal(1).scaleb(-places)):
        raise BadInput(f"{label} can have at most {places} decimal places.")
    if value is None:
        if default is not None:
            return Decimal(default)
        raise BadInput(f"{label} must be a number.")
    if positive and value <= 0:
        raise BadInput(f"{label} must be more than zero.")
    if minimum is not None and value < Decimal(minimum):
        raise BadInput(f"{label} cannot be less than {minimum}.")
    return value


def int_or(raw, default=0):
    """An int from a query string or form, or the default for blank or garbage."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def date_or(raw, default=None):
    """
    A date from a form, or `default` for blank, garbage or impossible dates.

    "yesterday" and 2026-02-30 both used to reach a DateField as a 500.
    """
    from django.utils.dateparse import parse_date

    try:
        return parse_date(str(raw or "").strip()) or default
    except ValueError:
        return default

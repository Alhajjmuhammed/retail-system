"""
Showing a shop's amounts in another currency, for comparison.

    {% load currency %}
    {{ price|in_currency:preview }}

``preview`` is ``(code, rate)`` -- a currency and what one unit of it is worth
in the shop's own -- or None, in which case the amount comes back unchanged.
"""

from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def in_currency(amount, preview):
    if amount in (None, "") or not preview:
        return amount
    from apps.core.currency import round_to

    code, rate = preview
    if not rate:
        return amount
    return round_to(Decimal(str(amount)) / Decimal(str(rate)), code)

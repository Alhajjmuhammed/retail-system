"""
Permission checks in templates.

Registered as a builtin, so no ``{% load %}`` is needed anywhere:

    {% if user|can:"product.view_cost" %}
      <td>{{ product.cost_price }}</td>
    {% endif %}

    {% if user|can_amount:"pos.void"|with_value:sale.total %}

Hiding a control the user cannot use is a courtesy, not a security boundary.
The view decorator is the boundary.
"""

from django import template

register = template.Library()


@register.filter(name="can")
def can(user, code: str) -> bool:
    membership = getattr(user, "_active_membership", None)
    if membership is None:
        return False
    return bool(membership.check_permission(code))


@register.simple_tag(takes_context=True)
def can_do(context, code: str, value=None, branch=None):
    membership = context.get("membership")
    if membership is None:
        return False
    return bool(membership.check_permission(code, branch=branch, value=value))


@register.simple_tag(takes_context=True)
def permission_limit(context, code: str):
    """The ceiling on a permission, for showing 'up to 50,000' in the UI."""
    membership = context.get("membership")
    if membership is None:
        return None
    return membership.check_permission(code).limit


@register.filter(name="get")
def get_item(mapping, key):
    """Look a key up in a dict from a template: {{ limits|get:key }}."""
    if hasattr(mapping, "get"):
        return mapping.get(key)
    return None


@register.filter
def ago(value):
    """
    "3 hours ago" rather than Django's "3 hours, 12 minutes".

    One unit is all a glance at a list needs, and anything under a minute
    reads better as "just now" than "0 minutes ago".
    """
    from django.utils import timezone
    from django.utils.timesince import timesince

    if not value:
        return ""
    if (timezone.now() - value).total_seconds() < 60:
        return "just now"
    return f"{timesince(value, depth=1)} ago"


@register.filter
def manageable_by(person, actor):
    """{% if row|manageable_by:request.user %} -- may they change this account?"""
    from apps.core.platform_perms import refusal_to_manage

    return refusal_to_manage(actor, person) is None


@register.filter
def join_if_list(value):
    """Lists read as "a, b, c"; empty ones as a dash. Anything else as itself."""
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{key} {val}" for key, val in value.items()) or "—"
    return value

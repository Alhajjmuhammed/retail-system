"""
Queuing messages, and the events that trigger them.

Listeners rather than calls inside the checkout: adding an SMS receipt must
never mean reopening the code that takes money. If the gateway is down the
sale still completes and the message sits in the queue.
"""

import logging

from apps.core.context import get_current_tenant
from apps.core.events import (
    CREDIT_EXTENDED,
    SALE_COMPLETED,
    STOCK_LOW,
    events,
)
from apps.core.features import SMS_NOTIFICATIONS
from apps.notifications.models import Channel, Message, MessageStatus, MessageTemplate

logger = logging.getLogger(__name__)

# Shipped with every tenant so SMS works the moment a shop switches it on.
DEFAULT_TEMPLATES = {
    "sale_receipt": (
        "Sale receipt",
        "{shop}: thank you. Receipt {receipt}, total {amount}.",
    ),
    "credit_taken": (
        "Sold on account",
        "{shop}: {amount} added to your account. Balance now {balance}.",
    ),
    "low_stock": (
        "Low stock",
        "{shop}: {item} is down to {quantity}.",
    ),
}


def queue(*, channel, to, body, template_key="", tenant=None):
    """
    Record the message now, send it later.

    Recorded even when there is nothing to send to, so a shop can see that a
    customer had no phone number rather than assuming they were told.
    """
    tenant = tenant or get_current_tenant()
    if tenant is None:
        return None

    if not to:
        return Message.objects.create(
            tenant=tenant, channel=channel, to="", body=body,
            template_key=template_key, status=MessageStatus.SKIPPED,
            error="No number to send to.",
        )

    return Message.objects.create(
        tenant=tenant, channel=channel, to=to, body=body,
        template_key=template_key, status=MessageStatus.QUEUED,
    )


def send_from_template(key, *, to, tenant=None, **context):
    tenant = tenant or get_current_tenant()
    if tenant is None or not tenant.has_feature(SMS_NOTIFICATIONS):
        return None

    template = MessageTemplate.objects.filter(key=key, is_active=True).first()
    if template is None:
        return None

    return queue(
        channel=template.channel,
        to=to,
        body=template.render(shop=tenant.name, **context),
        template_key=key,
        tenant=tenant,
    )


def install_default_templates(tenant):
    """Called when a shop is created, so the feature is usable immediately."""
    for key, (name, body) in DEFAULT_TEMPLATES.items():
        MessageTemplate.objects.get_or_create(
            tenant=tenant, key=key,
            defaults={"name": name, "body": body, "channel": Channel.SMS},
        )


# --------------------------------------------------------------------------
# Listeners. The whole point of the event bus: none of this touches checkout.
# --------------------------------------------------------------------------

@events.on(SALE_COMPLETED)
def sms_receipt(sale=None, **kwargs):
    if sale is None or sale.customer is None or not sale.customer.phone:
        return
    send_from_template(
        "sale_receipt", to=sale.customer.phone, tenant=sale.tenant,
        receipt=sale.number, amount=f"{sale.total:,.0f}",
        customer=sale.customer.name,
    )


@events.on(CREDIT_EXTENDED)
def sms_credit(customer=None, amount=None, **kwargs):
    if customer is None or not customer.phone:
        return
    send_from_template(
        "credit_taken", to=customer.phone, tenant=customer.tenant,
        customer=customer.name, amount=f"{amount:,.0f}",
        balance=f"{customer.balance:,.0f}",
    )


@events.on(STOCK_LOW)
def sms_low_stock(item=None, scheduled=False, **kwargs):
    # Only from the daily job. Firing on every movement would send a message
    # per sale once something dips below its reorder level.
    if item is None or not scheduled:
        return
    from apps.org.models import TenantSettings

    settings_row = TenantSettings.objects.filter(tenant=item.tenant).first()
    if settings_row is None:
        return

    owner_phone = item.tenant.phone
    send_from_template(
        "low_stock", to=owner_phone, tenant=item.tenant,
        item=str(item.variant), quantity=f"{item.qty_on_hand:,.0f}",
    )

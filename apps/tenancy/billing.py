"""
Billing the shops: raising invoices, taking payment, and what paying does.

Before this, nothing raised an invoice (a trial simply ended), paying one
extended nothing, and a paying shop whose period ended stayed "active" for
ever. Every screen and the nightly job go through these functions, so the
rules live in one place.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db import models, transaction
from django.utils import timezone

from apps.core.numbering import save_with_number
from apps.tenancy.models import (
    BillingCycle,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    SubscriptionStatus,
    TenantStatus,
)

# How long before a period ends its renewal invoice is raised.
INVOICE_LEAD_DAYS = 7


class BillingError(ValueError):
    """Refused, with a message for the person who asked."""


def price_for(subscription) -> Decimal:
    """One period of the plan, per branch in use -- at least one."""
    from apps.core.features import LIMIT_BRANCHES

    branches = max(subscription.tenant.usage_of(LIMIT_BRANCHES), 1)
    unit = (subscription.plan.price_annual if subscription.cycle == BillingCycle.ANNUAL
            else subscription.plan.price_monthly)
    return (unit or Decimal("0")) * branches


def period_after(subscription, start):
    """The period that begins at `start`: a month or a year, less a day."""
    step = relativedelta(years=1) if subscription.cycle == BillingCycle.ANNUAL else relativedelta(months=1)
    return start, start + step - timedelta(days=1)


def next_period_start(subscription):
    """
    Where the next paid period begins.

    After a trial, the day the trial ends -- not whenever the stored period
    happened to end. Otherwise the day after the paid-up date.
    """
    never_paid = not subscription.tenant.invoices.filter(status=InvoiceStatus.PAID).exists()
    if subscription.trial_ends_at and (
        subscription.status == SubscriptionStatus.TRIALING or never_paid
    ):
        return timezone.localtime(subscription.trial_ends_at).date()
    if subscription.period_end:
        return timezone.localtime(subscription.period_end).date() + timedelta(days=1)
    return timezone.localdate()


def next_number() -> str:
    year = timezone.localdate().year
    stem = f"INV{year}"
    last = (
        Invoice.objects.filter(number__startswith=stem)
        .order_by("-number").values_list("number", flat=True).first()
    )
    counter = int(last[len(stem):]) + 1 if last else 1
    return f"{stem}{counter:05d}"


@transaction.atomic
def raise_invoice(tenant, *, period_start, period_end, amount=None, due_date=None):
    subscription = tenant.active_subscription
    if subscription is None:
        raise BillingError(f"{tenant.name} has no subscription.")
    if period_end < period_start:
        raise BillingError("The period ends before it starts.")
    if amount is None:
        amount = price_for(subscription)
    if amount <= 0:
        # An invoice for nothing can never be paid off -- it has nothing left
        # to pay -- yet it would count as owed and hold the shop "past due".
        raise BillingError(f"Nothing to bill: {subscription.plan.name} costs nothing for this "
                           "period. Enter an amount if you agreed one.")
    clash = tenant.invoices.exclude(status=InvoiceStatus.VOID).filter(
        period_start__lte=period_end, period_end__gte=period_start
    ).first()
    if clash is not None:
        raise BillingError(f"{clash.number} already covers part of that period.")
    invoice = Invoice(
        tenant=tenant, period_start=period_start, period_end=period_end,
        amount=amount, total=amount, currency=tenant.currency,
        status=InvoiceStatus.OPEN, due_date=due_date or period_start,
    )
    # Numbered safely: two invoices raised at once used to collide on the
    # unique number and fail with a server error.
    return save_with_number(invoice, field="number", generate=next_number)


def paid_so_far(invoice) -> Decimal:
    return invoice.total - invoice.outstanding


@transaction.atomic
def record_payment(invoice, *, amount, method, reference=""):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in {InvoiceStatus.OPEN, InvoiceStatus.DRAFT}:
        raise BillingError(f"{invoice.number} is {invoice.get_status_display().lower()}; "
                           "nothing can be paid against it.")
    if amount <= 0:
        raise BillingError("A payment must be more than zero.")
    if amount > invoice.outstanding:
        raise BillingError(f"Only {invoice.outstanding:,.0f} is left to pay on {invoice.number}. "
                           "Record any extra as a separate invoice or a credit.")
    payment = Payment.objects.create(
        invoice=invoice, method=method, provider_ref=reference[:120],
        amount=amount, status=PaymentStatus.SUCCEEDED,
    )
    if invoice.outstanding <= 0:
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])
        _paid_up_to(invoice)
    return payment


@transaction.atomic
def reverse_payment(payment, *, reason):
    """
    Undo a payment recorded by mistake. Marked refunded, never deleted.

    If the invoice is no longer paid, the period it bought is taken back.
    """
    payment = Payment.objects.select_for_update().select_related("invoice").get(pk=payment.pk)
    if payment.status != PaymentStatus.SUCCEEDED:
        raise BillingError("That payment has already been reversed.")
    payment.status = PaymentStatus.REFUNDED
    payment.raw_payload = {**(payment.raw_payload or {}), "reversed_because": reason[:200]}
    payment.save(update_fields=["status", "raw_payload", "updated_at"])

    invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
    if invoice.status == InvoiceStatus.PAID and invoice.outstanding > 0:
        invoice.status = InvoiceStatus.OPEN
        invoice.paid_at = None
        invoice.save(update_fields=["status", "paid_at", "updated_at"])
        subscription = invoice.tenant.active_subscription
        paid_until = _end_of(invoice.period_end)
        if subscription and subscription.period_end and subscription.period_end >= paid_until:
            subscription.period_end = _end_of(invoice.period_start - timedelta(days=1))
            subscription.save(update_fields=["period_end", "updated_at"])
        _refresh_status(invoice.tenant)
    return payment


@transaction.atomic
def void_invoice(invoice):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == InvoiceStatus.VOID:
        raise BillingError(f"{invoice.number} is already void.")
    if invoice.payments.filter(status=PaymentStatus.SUCCEEDED).exists():
        raise BillingError(f"{invoice.number} has payments against it. Reverse those first.")
    invoice.status = InvoiceStatus.VOID
    invoice.save(update_fields=["status", "updated_at"])
    _refresh_status(invoice.tenant)
    return invoice


def status_now(tenant):
    """
    What a shop's subscription should say today.

    A trial not yet over is a trial. Owing means an unpaid invoice that is
    due -- a renewal raised a week early is not a debt yet.
    """
    subscription = tenant.subscription
    if subscription.trial_ends_at and subscription.trial_ends_at > timezone.now():
        return SubscriptionStatus.TRIALING
    today = timezone.localdate()
    owes = tenant.invoices.filter(
        models.Q(due_date__lte=today) | models.Q(due_date__isnull=True, period_start__lte=today),
        status__in=[InvoiceStatus.OPEN, InvoiceStatus.DRAFT],
    ).exists()
    return SubscriptionStatus.PAST_DUE if owes else SubscriptionStatus.ACTIVE


def _refresh_status(tenant):
    subscription = tenant.subscription
    if subscription.status in {SubscriptionStatus.CANCELLED}:
        return
    if tenant.status == TenantStatus.SUSPENDED:
        return  # suspended by hand: only a person lifts that
    wanted = status_now(tenant)
    if subscription.status != wanted and not (
        wanted == SubscriptionStatus.PAST_DUE
        and subscription.status in {SubscriptionStatus.GRACE, SubscriptionStatus.SUSPENDED}
    ):
        subscription.status = wanted
        subscription.save(update_fields=["status", "updated_at"])


def _end_of(day):
    return timezone.make_aware(datetime.combine(day, time.max))


def _paid_up_to(invoice):
    """
    A paid invoice buys its period: the shop's paid-up date moves on, and a
    shop held back for not paying can trade again.
    """
    tenant = invoice.tenant
    subscription = tenant.active_subscription
    if subscription is None:
        return
    paid_until = _end_of(invoice.period_end)
    fields = ["status", "updated_at"]
    if subscription.period_end is None or paid_until > subscription.period_end:
        subscription.period_start = timezone.make_aware(datetime.combine(invoice.period_start, time.min))
        subscription.period_end = paid_until
        fields += ["period_start", "period_end"]
    subscription.status = status_now(tenant)
    if subscription.status == SubscriptionStatus.TRIALING and invoice.total > 0:
        # Paying during a trial ends the trial: they are a customer now.
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.grace_ends_at = None
    fields.append("grace_ends_at")
    subscription.save(update_fields=fields)
    if tenant.status == TenantStatus.SUSPENDED and subscription.status == SubscriptionStatus.ACTIVE:
        # Suspended for not paying, and now nothing is owed: trading again.
        tenant.status = TenantStatus.ACTIVE
        tenant.save(update_fields=["status", "updated_at"])


def renew_due(now=None):
    """
    The nightly part: raise renewals ahead of time, and let lapsed ones lapse.

    Returns counts for the log.
    """
    from apps.tenancy.models import Subscription

    now = now or timezone.now()
    raised = extended = lapsed = 0
    upcoming = Subscription.objects.select_related("tenant", "plan").filter(
        # Past-due and grace too: a trial that ended with nothing billed
        # left the shop owing an invoice that did not exist.
        status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING,
                    SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE],
        period_end__isnull=False,
    ).filter(
        models.Q(period_end__lte=now + timedelta(days=INVOICE_LEAD_DAYS))
        | models.Q(trial_ends_at__lte=now + timedelta(days=INVOICE_LEAD_DAYS))
    )
    for subscription in upcoming:
        start, end = period_after(subscription, next_period_start(subscription))
        price = price_for(subscription)
        if price == 0:
            # A free plan renews itself when its period is actually over.
            if subscription.period_end <= now and subscription.status == SubscriptionStatus.ACTIVE:
                subscription.period_start = timezone.make_aware(datetime.combine(start, time.min))
                subscription.period_end = _end_of(end)
                subscription.save(update_fields=["period_start", "period_end", "updated_at"])
                extended += 1
            continue
        covered = subscription.tenant.invoices.exclude(status=InvoiceStatus.VOID).filter(
            period_start__lte=end, period_end__gte=start
        ).exists() or subscription.tenant.invoices.filter(
            status__in=[InvoiceStatus.OPEN, InvoiceStatus.DRAFT]
        ).exists()
        if not covered:
            try:
                raise_invoice(subscription.tenant, period_start=start, period_end=end,
                              amount=price, due_date=start)
                raised += 1
            except BillingError:
                pass
        if subscription.status == SubscriptionStatus.ACTIVE and subscription.period_end <= now:
            # Paid up to a date that has passed, and the next period unpaid.
            subscription.status = SubscriptionStatus.PAST_DUE
            subscription.save(update_fields=["status", "updated_at"])
            lapsed += 1
    return {"raised": raised, "extended": extended, "lapsed": lapsed}

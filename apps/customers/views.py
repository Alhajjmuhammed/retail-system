"""Customers, their account and what they owe."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.decorators import requires
from apps.core.deletion import remove_or_archive
from apps.core.parsing import BadInput, int_or, parse_decimal
from apps.customers.models import CreditKind, CreditTransaction, Customer

PAYMENT_METHODS = [("cash", "Cash"), ("mobile", "Mobile money"), ("bank", "Bank"),
                   ("card", "Card")]


@login_required
@requires("customer.manage")
def customer_list(request):
    from django.db.models import F

    from apps.core.listing import paginate

    term = request.GET.get("q", "").strip()

    # Annotated, not the `balance` property: that ran an aggregate per row,
    # so a shop with 200 customers made 200 extra queries to draw one page.
    everyone = Customer.objects.annotate(
        owed=Coalesce(Sum("credit_transactions__amount"), Decimal("0"))
    )
    active = everyone.filter(is_active=True)
    view = request.GET.get("view", "")
    if view == "owing":
        customers = active.filter(owed__gt=0)
    elif view == "over":
        customers = active.filter(owed__gt=0).filter(owed__gt=F("credit_limit"))
    elif view == "archived":
        customers = everyone.filter(is_active=False)
    else:
        view = ""
        customers = active
    if term:
        customers = customers.filter(Q(name__icontains=term) | Q(phone__icontains=term))

    owing = active.filter(owed__gt=0)
    context = {
        **paginate(request, customers.order_by("name")),
        "q": term,
        "view": view,
        "summary": {
            "count": active.count(),
            "owing": owing.count(),
            # Only what is owed to the shop; a customer in credit used to
            # reduce the total.
            "total_owed": sum((c.owed for c in owing), Decimal("0")),
            "over": owing.filter(owed__gt=F("credit_limit")).count(),
            "archived": everyone.filter(is_active=False).count(),
        },
    }
    if request.htmx:
        return render(request, "customers/_customer_rows.html", context)
    return render(request, "customers/customer_list.html", context)


@login_required
@requires("customer.manage")
def customer_form(request, pk=None):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from apps.catalog.models import PriceList
    from apps.core.listing import close_modal, modal_or_page

    customer = get_object_or_404(Customer, pk=pk) if pk else None
    errors, values = [], None
    price_lists = PriceList.objects.filter(is_active=True).order_by("-is_default", "name")

    if request.method == "POST":
        values = request.POST
        name = request.POST.get("name", "").strip()[:120]
        limit = Decimal("0")
        if not name:
            errors.append("A name is required.")
        try:
            limit = parse_decimal(request.POST.get("credit_limit"), "Credit limit",
                                  minimum=0, default=0)
        except BadInput as exc:
            errors.append(str(exc))
        # Raising a credit limit is a money decision with its own permission
        # and ceiling -- but only when it is actually being raised. Editing a
        # phone number used to need credit rights if a limit already existed.
        current = customer.credit_limit if customer else Decimal("0")
        if not errors and limit > current:
            decision = request.membership.check_permission("credit.grant", value=limit)
            if not decision.allowed:
                errors.append(decision.reason)
        email = request.POST.get("email", "").strip()[:254]
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors.append("That email address does not look right.")
        price_list = None
        if request.POST.get("price_list"):
            price_list = price_lists.filter(pk=int_or(request.POST.get("price_list"))).first()
        phone = request.POST.get("phone", "").strip()[:30]
        if phone and not errors:
            same = Customer.objects.filter(phone=phone, is_active=True)
            if customer is not None:
                same = same.exclude(pk=customer.pk)
            if same.exists():
                errors.append(f"{same.first().name} already has the number {phone}.")

        if not errors:
            before = audit.snapshot(customer) if customer else None
            fields = {
                "name": name, "phone": phone, "email": email,
                "tin": request.POST.get("tin", "").strip()[:30],
                "address": request.POST.get("address", "").strip()[:500],
                "note": request.POST.get("note", "").strip()[:1000],
                "credit_limit": limit,
                "price_list": price_list,
            }
            if customer is None:
                customer = Customer.objects.create(**fields)
            else:
                for key, value in fields.items():
                    setattr(customer, key, value)
                customer.save()
            audit.record("customer.saved", obj=customer, before=before,
                         after=audit.snapshot(customer), ip=audit.client_ip(request))
            messages.success(request, f"{customer.name} saved.")
            return close_modal(request, reverse("customers:customer_detail", args=[customer.pk]))

    return modal_or_page(
        request, "customers/_customer_form.html",
        {"customer": customer, "errors": errors, "values": values,
         "price_lists": price_lists},
        title=customer.name if customer else "New customer",
        back=reverse("customers:customer_list"),
    )


def _account_code(request, **kwargs):
    # Someone who only collects payments (a bookkeeper) reads the account
    # too; the statements page used to link them to a refused page.
    return "customer.manage" if request.membership.can("customer.manage") else "credit.collect"


@login_required
@requires(_account_code)
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    from apps.core.features import LOYALTY

    return render(
        request,
        "customers/customer_detail.html",
        {
            "customer": customer,
            "transactions": customer.credit_transactions.select_related("sale")[:100],
            "sales": customer.sales.select_related("branch").order_by("-sold_at")[:20],
            "sales_count": customer.sales.count(),
            "methods": PAYMENT_METHODS,
            "loyalty": customer.loyalty_transactions.select_related("sale")[:30],
            "has_loyalty": request.tenant.has_feature(LOYALTY),
            "may_manage": request.membership.can("customer.manage"),
        },
    )


@login_required
@requires("customer.manage")
@require_POST
def customer_redeem(request, pk):
    """Spend a customer's points."""
    from apps.core.features import LOYALTY
    from apps.customers.services import redeem

    customer = get_object_or_404(Customer, pk=pk)
    if not request.tenant.has_feature(LOYALTY):
        messages.error(request, "Loyalty points are not included in your plan.")
        return redirect("customers:customer_detail", pk=pk)
    from django.db import transaction as db

    try:
        with db.atomic():
            # Locked, so two taps cannot spend the same points twice.
            Customer.objects.select_for_update().filter(pk=customer.pk).first()
            redeem(
                customer,
                int_or(request.POST.get("points"), 0),
                note=request.POST.get("note", "").strip()[:200],
            )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Points redeemed for {customer.name}.")
    return redirect("customers:customer_detail", pk=pk)


@login_required
@requires("credit.collect")
def customer_payment(request, pk):
    """
    Record money coming in against an account.

    Stored as a negative line on the same ledger as the charges, so the
    balance is a plain sum and cannot drift.
    """
    from django.db import transaction as db

    customer = get_object_or_404(Customer, pk=pk)

    if request.method == "POST":
        method = request.POST.get("method", "cash")
        if method not in dict(PAYMENT_METHODS):
            method = "cash"
        try:
            amount = parse_decimal(request.POST.get("amount"), "The amount they paid",
                                   positive=True, places=2)
        except BadInput as exc:
            messages.error(request, str(exc))
            return redirect("customers:customer_detail", pk=pk)
        with db.atomic():
            # Locked: two payments at once both read the same balance and
            # wrote the same "balance after".
            Customer.objects.select_for_update().filter(pk=customer.pk).first()
            owed = customer.balance
            if amount > owed:
                messages.error(request, f"{customer.name} owes {max(owed, 0):,.0f}. "
                                        "Record no more than that.")
                return redirect("customers:customer_detail", pk=pk)
            entry = CreditTransaction.objects.create(
                customer=customer,
                kind=CreditKind.PAYMENT,
                amount=-amount,
                balance_after=owed - amount,
                method=method,
                reference=request.POST.get("reference", "").strip()[:60],
                note=request.POST.get("note", "").strip()[:200],
            )
            drawer = None
            if method == "cash":
                # Cash handed over at the counter goes in the drawer; without
                # this every account payment showed as the cashier being over.
                from apps.pos.models import CashMovementKind
                from apps.pos.services import record_cash_movement
                from apps.pos.views import _open_shift_for

                drawer = _open_shift_for(request)
                if drawer is not None:
                    entry.cash_movement = record_cash_movement(
                        drawer, kind=CashMovementKind.PAY_IN, amount=amount,
                        reason=f"Account payment: {customer.name}"[:200])
                    entry.save(update_fields=["cash_movement", "updated_at"])
        audit.record("credit.collected", obj=entry, ip=audit.client_ip(request))
        messages.success(
            request,
            f"{amount:,.0f} received from {customer.name}."
            + (" Added to your till drawer." if drawer is not None else
               " No till is open, so it is not in any drawer count." if method == "cash" else ""),
        )

    return redirect("customers:customer_detail", pk=pk)


@login_required
@requires("credit.collect")
@require_POST
def customer_payment_reverse(request, pk):
    """
    Undo a payment recorded by mistake.

    The ledger is never edited: an adjustment puts the amount back, and both
    lines stay on the account for anyone to read.
    """
    from django.db import transaction as db

    entry = get_object_or_404(
        CreditTransaction.objects.select_related("customer", "cash_movement__shift"), pk=pk,
        kind=CreditKind.PAYMENT)
    customer = entry.customer
    back = -entry.amount
    till_note = ""
    with db.atomic():
        # Locked, and checked again inside the lock: a double click used to
        # put the amount back on the account twice.
        Customer.objects.select_for_update().filter(pk=customer.pk).first()
        if CreditTransaction.objects.filter(customer=customer, kind=CreditKind.ADJUSTMENT,
                                            reference=f"undo:{entry.pk}").exists():
            messages.info(request, "That payment has already been undone.")
            return redirect("customers:customer_detail", pk=customer.pk)
        movement = entry.cash_movement
        if movement is not None and not request.membership.covers_branch(movement.shift.branch):
            # Checked before anything is written: returning from inside the
            # transaction does not undo what it has already done.
            messages.error(request, f"That payment went into a till at "
                                    f"{movement.shift.branch.name}. Somebody there undoes it.")
            return redirect("customers:customer_detail", pk=customer.pk)
        undo = CreditTransaction.objects.create(
            customer=customer, kind=CreditKind.ADJUSTMENT, amount=back,
            balance_after=customer.balance + back, reference=f"undo:{entry.pk}",
            note=f"Undoes payment of {back:,.0f} on {entry.created_at:%d %b}",
        )
        if movement is not None and movement.shift.closed_at is None:
            from apps.pos.models import CashMovementKind
            from apps.pos.services import record_cash_movement

            record_cash_movement(movement.shift, kind=CashMovementKind.PAY_OUT,
                                 amount=-movement.amount,
                                 reason=f"Undo account payment: {customer.name}"[:200])
            till_note = " Taken back out of the till's expected cash."
        elif movement is not None:
            till_note = " Its till is already counted; check that cash-up."
    audit.record("credit.payment_undone", obj=undo, ip=audit.client_ip(request))
    messages.success(request, f"Payment of {back:,.0f} undone.{till_note}")
    return redirect("customers:customer_detail", pk=customer.pk)


@login_required
@requires("credit.collect")
def customer_statement(request, pk):
    """The account on one printable page, to hand over or send."""
    from apps.org.models import TenantSettings

    customer = get_object_or_404(Customer, pk=pk)
    rows = customer.credit_transactions.select_related("sale").order_by("created_at", "pk")
    return render(request, "customers/statement_print.html", {
        "customer": customer, "rows": rows, "settings": TenantSettings.objects.first(),
    })


@login_required
@requires("customer.manage")
@require_POST
def customer_delete(request, pk):
    """
    Gone if they never bought anything, switched off if they did.

    Somebody who owes money is never removed: the balance has to stay visible.
    """
    customer = get_object_or_404(Customer, pk=pk)
    name = customer.name

    balance = customer.balance
    blockers = (
        (lambda: balance > 0, f"{name} still owes {balance:,.0f}. Settle the account first."),
        (lambda: balance < 0, f"You owe {name} {-balance:,.0f}. Settle it before removing them."),
    )
    if customer.sales.exists() or customer.credit_transactions.exists() \
            or customer.loyalty_transactions.exists():
        # Sales only point at a customer loosely, so deleting one quietly
        # unhooked every receipt and wiped the points. Switched off instead.
        for check, message in blockers:
            if check():
                messages.error(request, message)
                return redirect("customers:customer_list")
        customer.is_active = False
        customer.save(update_fields=["is_active", "updated_at"])
        audit.record("customer.archived", obj=customer, ip=audit.client_ip(request))
        messages.warning(request, f"{name} has history, so they were switched off rather "
                                  "than deleted. Bring them back from Removed.")
        return redirect("customers:customer_list")
    outcome = remove_or_archive(customer, label=name, blockers=blockers)

    if not outcome.blocked:
        audit.record("customer.removed", obj=customer, ip=audit.client_ip(request))
    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("customers:customer_list")


@login_required
@requires("customer.manage")
@require_POST
def customer_restore(request, pk):
    customer = get_object_or_404(Customer, pk=pk, is_active=False)
    if customer.phone and Customer.objects.filter(phone=customer.phone, is_active=True).exists():
        messages.error(request, f"Another customer now has {customer.phone}. Change one "
                                "of the numbers first.")
        return redirect("customers:customer_detail", pk=pk)
    customer.is_active = True
    customer.save(update_fields=["is_active", "updated_at"])
    audit.record("customer.restored", obj=customer, ip=audit.client_ip(request))
    messages.success(request, f"{customer.name} is back.")
    return redirect("customers:customer_detail", pk=pk)


@login_required
@requires("credit.collect")
def statements(request):
    """Who owes what, oldest first. The list an owner works through on a Friday."""
    customers = (
        Customer.objects.filter(is_active=True)
        .annotate(owed=Coalesce(Sum("credit_transactions__amount"), Decimal("0")),
                  last_paid=Max("credit_transactions__created_at",
                                filter=Q(credit_transactions__kind=CreditKind.PAYMENT)))
        .filter(owed__gt=0)
        .order_by("-owed")
    )
    return render(
        request,
        "customers/statements.html",
        {
            "customers": customers,
            "total": customers.aggregate(total=Coalesce(Sum("owed"), Decimal("0")))["total"],
        },
    )

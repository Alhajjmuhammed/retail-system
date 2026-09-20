"""Expenses and the daily cash position."""

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.deletion import remove_or_archive
from apps.core.parsing import BadInput, date_or, decimal_or_none, int_or, parse_decimal
from apps.finance.models import Expense, ExpenseCategory
from apps.pos.models import Shift, ShiftStatus

METHODS = [("cash", "Cash"), ("mpesa", "Mobile money"), ("bank", "Bank"), ("card", "Card")]
ATTACHMENT_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
ATTACHMENT_MAX = 5 * 1024 * 1024


@login_required
@requires("expense.create")
def expense_list(request):
    from django.db.models import Q

    from apps.core.listing import paginate

    branches = request.membership.branches(include_closed=True)
    expenses = (
        Expense.objects.select_related("category", "created_by", "approved_by", "branch")
        .filter(branch__in=branches)
        .order_by("-spent_at", "-pk")
    )
    f = {
        "branch": int_or(request.GET.get("branch")),
        "category": int_or(request.GET.get("category")),
        "method": request.GET.get("method", ""),
        "state": request.GET.get("state", ""),
        "from": date_or(request.GET.get("from")),
        "to": date_or(request.GET.get("to")),
        "q": request.GET.get("q", "").strip(),
    }
    if f["branch"]:
        expenses = expenses.filter(branch_id=f["branch"])
    if f["category"]:
        expenses = expenses.filter(category_id=f["category"])
    if f["method"] in dict(METHODS):
        expenses = expenses.filter(method=f["method"])
    if f["state"] == "waiting":
        expenses = expenses.filter(approved_by__isnull=True)
    elif f["state"] == "approved":
        expenses = expenses.filter(approved_by__isnull=False)
    if f["from"]:
        expenses = expenses.filter(spent_at__gte=f["from"])
    if f["to"]:
        expenses = expenses.filter(spent_at__lte=f["to"])
    if f["q"]:
        expenses = expenses.filter(Q(description__icontains=f["q"]) | Q(reference__icontains=f["q"]))

    month_start = timezone.localdate().replace(day=1)
    here = Expense.objects.filter(branch__in=branches)
    return render(
        request,
        "finance/expenses.html",
        {
            **paginate(request, expenses),
            "f": f,
            "branches": branches,
            "methods": METHODS,
            "categories": ExpenseCategory.objects.filter(is_active=True).order_by("name"),
            "shown_total": expenses.aggregate(t=Sum("amount"))["t"] or Decimal("0"),
            "month_total": here.filter(spent_at__gte=month_start)
            .aggregate(total=Sum("amount"))["total"] or Decimal("0"),
            "waiting": here.filter(approved_by__isnull=True).count(),
        },
    )


def _expense_form(request, expense=None, errors=(), values=None):
    from apps.core.listing import modal_or_page
    from apps.pos.views import _open_shift_for

    return modal_or_page(
        request, "finance/_expense_form.html",
        {"expense": expense, "errors": list(errors), "values": values,
         "categories": ExpenseCategory.objects.filter(is_active=True).order_by("name"),
         "methods": METHODS, "drawer": _open_shift_for(request) if expense is None else None,
         "today": timezone.localdate()},
        title="Edit expense" if expense else "Record an expense",
        back=reverse("finance:expense_list"),
    )


def _read_expense(request, errors):
    """Everything typed on the expense form, checked. Errors are appended."""
    import os

    out = {}
    try:
        out["amount"] = parse_decimal(request.POST.get("amount"), "Amount", positive=True, places=2)
    except BadInput as exc:
        errors.append(str(exc))
    name = request.POST.get("category_name", "").strip()[:80]
    if name:
        # Switched back on (if it was off) only when the expense is saved,
        # never as a side effect of a form that failed.
        category = ExpenseCategory.objects.filter(name__iexact=name).first()
        out["category"] = category or ExpenseCategory(name=name)
    else:
        out["category"] = ExpenseCategory.objects.filter(
            pk=int_or(request.POST.get("category")), is_active=True).first()
        if out["category"] is None:
            # A missing category used to be a bare 404 page.
            errors.append("Choose a category, or type a new one.")
    spent = date_or(request.POST.get("spent_at"), timezone.localdate())
    if spent > timezone.localdate():
        errors.append("The date is in the future.")
    out["spent_at"] = spent
    method = request.POST.get("method", "cash")
    out["method"] = method if method in dict(METHODS) else "cash"
    out["reference"] = request.POST.get("reference", "").strip()[:60]
    out["description"] = request.POST.get("description", "").strip()[:200]
    upload = request.FILES.get("attachment")
    if upload is not None:
        ext = os.path.splitext(upload.name)[1].lower()
        if ext not in ATTACHMENT_TYPES:
            errors.append("The receipt must be a photo (JPG, PNG, WebP) or a PDF.")
        elif upload.size > ATTACHMENT_MAX:
            errors.append("The receipt is larger than 5 MB.")
        else:
            out["attachment"] = upload
    return out


def _save_category(category):
    if category.pk is None:
        category.save()
    elif not category.is_active:
        category.is_active = True
        category.save(update_fields=["is_active", "updated_at"])


@login_required
@requires("expense.create", value=lambda r, **kw: decimal_or_none(r.POST.get("amount")) or 0)
def expense_create(request):
    from django.db import transaction

    from apps.core.listing import close_modal

    if request.branch is None:
        messages.error(request, "Choose a branch first.")
        return redirect("finance:expense_list")
    if request.method != "POST":
        return _expense_form(request)

    errors = []
    data = _read_expense(request, errors)
    drawer = None
    if not errors and data["method"] == "cash" and request.POST.get("from_drawer") == "on":
        from apps.pos.views import _open_shift_for

        drawer = _open_shift_for(request)
        if drawer is None:
            errors.append("No till of yours is open, so it cannot come out of a drawer.")
        elif data["amount"] > drawer.compute_expected_cash():
            errors.append(f"Your drawer should only hold {drawer.compute_expected_cash():,.0f}.")
    if errors:
        return _expense_form(request, errors=errors, values=request.POST)

    with transaction.atomic():
        _save_category(data["category"])
        expense = Expense.objects.create(branch=request.branch, **data)
        if drawer is not None:
            # Paid out of the till: the drawer's expected cash drops with it,
            # or the cashier looks short by exactly this amount.
            from apps.pos.models import CashMovementKind
            from apps.pos.services import record_cash_movement

            expense.cash_movement = record_cash_movement(
                drawer, kind=CashMovementKind.PAY_OUT, amount=-data["amount"],
                reason=f"Expense: {data['category'].name} {data['description']}".strip()[:200])
            expense.save(update_fields=["cash_movement", "updated_at"])
    audit.record("expense.recorded", obj=expense, ip=audit.client_ip(request))
    messages.success(request, f"{expense.amount:,.0f} for {expense.category} recorded"
                     + (" and taken from your till." if drawer else "."))
    return close_modal(request, reverse("finance:expense_list"))


@login_required
@requires("expense.create", value=lambda r, **kw: decimal_or_none(r.POST.get("amount")) or 0,
          branch=branch_of(Expense))
def expense_edit(request, pk):
    from apps.core.listing import close_modal

    expense = get_object_or_404(Expense, pk=pk)

    if expense.approved_by_id:
        # Approving a spend means approving *this* amount. Editing one
        # afterwards used to keep the approval on whatever it became.
        messages.error(request, "This expense has been approved and cannot be changed. "
                                "Remove it and enter it again if it is wrong.")
        return close_modal(request, reverse("finance:expense_list"))

    if request.method == "POST":
        errors = []
        data = _read_expense(request, errors)
        movement = expense.cash_movement
        if not errors and movement is not None and (
                data["amount"] != expense.amount or data["method"] != expense.method):
            # The drawer already paid this out; changing the figure here would
            # leave the cash-up wrong. Remove it and record it again instead.
            errors.append("This came out of a till, so its amount and how it was paid are "
                          "fixed. Remove it and record it again if they are wrong.")
        if errors:
            return _expense_form(request, expense, errors=errors, values=request.POST)
        before = audit.snapshot(expense)
        _save_category(data["category"])
        old_file = expense.attachment.name if "attachment" in data else ""
        for key, value in data.items():
            setattr(expense, key, value)
        expense.save()
        if old_file and old_file != expense.attachment.name:
            # A replaced receipt used to stay on disk for ever.
            expense.attachment.storage.delete(old_file)
        audit.record("expense.updated", obj=expense, before=before,
                     after=audit.snapshot(expense), ip=audit.client_ip(request))
        messages.success(request, "Expense updated.")
        return close_modal(request, reverse("finance:expense_list"))

    return _expense_form(request, expense)


@login_required
@requires("expense.create", branch=branch_of(Expense))
def expense_attachment(request, pk):
    """The receipt photo, only to people who may see this branch's expenses."""
    import mimetypes

    from django.http import FileResponse, Http404

    expense = get_object_or_404(Expense, pk=pk)
    if not expense.attachment:
        raise Http404
    try:
        handle = expense.attachment.open("rb")
    except FileNotFoundError:
        raise Http404 from None
    # Only the types we accept are shown in the browser. An older upload
    # named .html or .svg would otherwise run as a page on this site, with
    # the viewer's session.
    kind = mimetypes.guess_type(expense.attachment.name)[0] or "application/octet-stream"
    safe = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    inline = kind in safe
    response = FileResponse(handle, content_type=kind if inline else "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Content-Disposition"] = "inline" if inline else "attachment"
    return response


@login_required
@requires("expense.approve", branch=branch_of(Expense))
@require_POST
def expense_delete(request, pk):
    """
    Remove an expense entered by mistake.

    Gated on approval rather than entry: whoever can sign off a spend is who
    should be able to unsay one.
    """
    from django.db import transaction

    expense = get_object_or_404(Expense.objects.select_related("cash_movement__shift"), pk=pk)
    if expense.approved_by_id and not request.membership.role.is_owner_role:
        messages.error(request, "An approved expense can only be removed by an owner.")
        return redirect("finance:expense_list")
    audit.record("expense.deleted", obj=expense, before=audit.snapshot(expense),
                 ip=audit.client_ip(request))
    stored = expense.attachment.name
    with transaction.atomic():
        # Locked and re-read: two clicks both put the cash back, and the
        # drawer then expected it twice.
        locked = Expense.objects.select_for_update().filter(pk=pk).first()
        if locked is None:
            messages.info(request, "That expense has already been removed.")
            return redirect("finance:expense_list")
        movement = expense.cash_movement
        reversed_in_till = _undo_expense(expense, movement)
    if stored:
        Expense._meta.get_field("attachment").storage.delete(stored)
    messages.success(request, "Expense removed."
                     + (" Its cash is back in the till's expected figure." if reversed_in_till
                        else " It came out of a till that is already counted; check that cash-up."
                        if movement is not None else ""))
    return redirect("finance:expense_list")


def _undo_expense(expense, movement):
    """Put a till pay-out back, if its drawer is still open. Returns whether it was."""
    if movement is not None and movement.shift.closed_at is None:
        # The drawer is still open: put the cash back in its expected figure.
        from apps.pos.models import CashMovementKind
        from apps.pos.services import record_cash_movement

        record_cash_movement(movement.shift, kind=CashMovementKind.PAY_IN,
                             amount=-movement.amount,
                             reason=f"Expense removed: {movement.reason}"[:200])
        expense.delete()
        return True
    expense.delete()
    return False


@login_required
@requires("expense.approve")
@require_POST
def expense_category_delete(request, pk):
    category = get_object_or_404(ExpenseCategory, pk=pk)
    outcome = remove_or_archive(category, label=category.name)
    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("finance:expense_list")


@login_required
@requires("cashup.approve")
def cashups(request):
    """
    Every closed shift and what it was short by.

    Sorted by variance so the ones worth a conversation are at the top.
    """
    days = min(max(int_or(request.GET.get("days"), 14), 1), 3650)
    since = timezone.now() - timedelta(days=days)

    from apps.core.listing import paginate

    branches = request.membership.branches(include_closed=True)
    shifts = (
        Shift.objects.select_related("user", "register", "branch", "approved_by")
        .filter(closed_at__isnull=False, closed_at__gte=since, branch__in=branches)
        .order_by("-closed_at")
    )
    branch = int_or(request.GET.get("branch"))
    if branch:
        shifts = shifts.filter(branch_id=branch)
    state = request.GET.get("state", "")
    if state == "waiting":
        shifts = shifts.filter(approved_by__isnull=True)
    elif state == "short":
        shifts = shifts.filter(variance__lt=0)

    totals = shifts.aggregate(
        variance=Sum("variance"), counted=Sum("counted_cash"), expected=Sum("expected_cash")
    )

    return render(
        request,
        "finance/cashups.html",
        {
            **paginate(request, shifts),
            "totals": totals,
            "days": days,
            "branches": branches,
            "branch": branch,
            "state": state,
            "worst": shifts.exclude(variance=0).order_by("variance")[:5],
            # Open by the same flag the till uses to refuse a second shift on
            # a register, so this list and the till can never disagree about
            # which drawers are still out.
            "open_shifts": Shift.objects.select_related("user", "register", "branch")
            .filter(status=ShiftStatus.OPEN, branch__in=request.membership.branches())
            .order_by("opened_at"),
        },
    )


@login_required
@requires("expense.approve", branch=branch_of(Expense))
@require_POST
def expense_approve(request, pk):
    """
    Sign off a spend.

    `expense.approve` and the approved-by column existed with nothing that
    set them, so every expense stayed "unapproved" for ever.
    """
    expense = get_object_or_404(Expense, pk=pk)
    if expense.approved_by_id:
        messages.info(request, "Already approved.")
    elif (getattr(request, "authorised_by", None) or request.user).pk in {
        expense.created_by_id, *_editors(expense)
    }:
        # Rewriting somebody's expense and then approving it is approving
        # your own figure.
        messages.error(request, "Somebody else has to approve an expense you entered or changed.")
    else:
        expense.approved_by = getattr(request, "authorised_by", None) or request.user
        expense.approved_at = timezone.now()
        expense.save(update_fields=["approved_by", "approved_at", "updated_at"])
        audit.record("expense.approved", obj=expense, ip=audit.client_ip(request))
        messages.success(request, f"{expense.amount:,.0f} for {expense.category} approved.")
    return redirect("finance:expense_list")


@login_required
@requires("cashup.approve", branch=branch_of(Shift))
@require_POST
def cashup_approve(request, pk):
    """Accept a closed drawer's count, variance and all. Never your own."""
    shift = get_object_or_404(Shift, pk=pk)
    if shift.closed_at is None:
        messages.error(request, "Close the shift before approving its cash-up.")
    elif shift.approved_by_id:
        messages.info(request, "Already approved.")
    elif (getattr(request, "authorised_by", None) or request.user).pk == shift.user_id:
        messages.error(request, "Somebody else has to approve your own drawer.")
    else:
        shift.approved_by = getattr(request, "authorised_by", None) or request.user
        shift.save(update_fields=["approved_by", "updated_at"])
        audit.record("cashup.approved", obj=shift, after={"variance": str(shift.variance)},
                     ip=audit.client_ip(request))
        messages.success(request, f"Cash-up for {shift.user.name} approved.")
    return redirect("finance:cashups")


def _editors(expense):
    """
    Everyone who ever changed it. Only the last editor was checked, so one
    manager could change the amount, a second touch the description, and the
    first then approve their own figure.
    """
    from apps.accounts.models import AuditLog

    return set(
        AuditLog.objects.filter(action="expense.updated", object_type="Expense",
                                object_id=str(expense.pk))
        .values_list("user_id", flat=True)
    )

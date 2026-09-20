"""
View-level permission enforcement.

    @requires("pos.void", amount=lambda r, **kw: kw["sale"].total)
    def void_sale(request, sale): ...

A denial on a dangerous permission does not end in a wall. It renders the
approval prompt, a manager enters their PIN on the same screen, and the action
proceeds with both names on the audit row.
"""

import secrets
import time
from contextlib import suppress
from decimal import Decimal
from functools import wraps

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render


def requires(code_or_fn, *, value=None, branch=None):
    """
    Guard a view with a permission.

    The code itself may be a callable too, for a form that is one permission
    or another depending on what is ticked (an adjustment vs a write-off).

    ``value`` and ``branch`` may be callables taking the request and the
    view's keyword arguments, for limits that depend on what is being acted
    on -- the size of the discount, the value of the sale being voided.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            membership = getattr(request, "membership", None)
            if membership is None:
                raise PermissionDenied("No active membership.")

            code = code_or_fn(request, **kwargs) if callable(code_or_fn) else code_or_fn
            resolved_value = value(request, **kwargs) if callable(value) else value
            resolved_branch = branch(request, **kwargs) if callable(branch) else branch
            if resolved_branch is None:
                resolved_branch = getattr(request, "branch", None)

            decision = membership.check_permission(
                code, branch=resolved_branch, value=resolved_value
            )
            if decision.allowed:
                return view(request, *args, **kwargs)

            if not decision.can_override:
                if getattr(request, "htmx", False):
                    return _in_modal(render(request, "core/_denied_modal.html",
                                            {"decision": decision, "modal": True}))
                return render(request, "core/denied.html", {"decision": decision}, status=403)

            approval = _approval(request, code, resolved_branch, resolved_value)
            if isinstance(approval, HttpResponse):
                return approval
            if approval is not None:
                return _run_approved(view, request, approval, args, kwargs)

            return _ask_for_approval(request, decision, code)

        return wrapped

    return decorator


APPROVAL_FIELDS = {"override_email", "override_pin", "override_permission",
                   "override_method", "override_nonce", "csrfmiddlewaretoken"}
APPROVAL_SECONDS = 5 * 60
MAX_PIN_TRIES = 5


def _ask_for_approval(request, decision, code, error=""):
    """
    The approval screen, carrying the original form along.

    It used to post back only the PIN, so the void reason or the refund
    quantities were lost -- and nothing read the PIN anyway.
    """
    carried = [
        (key, item) for key in request.POST for item in request.POST.getlist(key)
        if key not in APPROVAL_FIELDS
    ]
    # One approval, one use. The PIN travels with the form, so without this
    # the same submission could be sent again -- the back button, a double
    # click -- and every copy was approved: three refunds for one PIN.
    nonce = secrets.token_urlsafe(16)
    pending = request.session.get("approval_nonces", {})
    now = time.time()
    pending = {k: v for k, v in pending.items() if v.get("expires", 0) > now}
    pending[nonce] = {"code": code, "path": request.path, "expires": now + APPROVAL_SECONDS}
    request.session["approval_nonces"] = pending
    context = {
        "decision": decision,
        "permission_code": code,
        "target_url": request.get_full_path(),
        "carried": carried,
        "original_method": request.POST.get("override_method") or request.method,
        "nonce": nonce,
        "error": error,
    }
    if getattr(request, "htmx", False):
        return _in_modal(render(request, "core/_approval_modal.html",
                                {**context, "modal": True, "has_files": bool(request.FILES)}))
    return render(request, "core/override_required.html", context, status=403)


def _in_modal(response):
    """
    Show this in the pop-up, whatever the form was aimed at. HTMX drops 4xx
    responses, so a refusal inside a modal used to look like nothing at all.
    """
    response["HX-Retarget"] = "#modal"
    response["HX-Reswap"] = "innerHTML"
    return response


def _approval(request, code, branch, value):
    """
    A manager standing at the till, or None.

    Returns the approving user, None if nobody has tried yet, or a response
    (the approval screen with an error) when the attempt failed.
    """
    from apps.accounts.models import Membership

    session_key = f"approval:{code}:{request.path}"
    granted = request.session.get(session_key)
    if granted and granted.get("expires", 0) > time.time():
        # Approved on this page's screen a moment ago: good for opening the
        # page and then submitting it once -- a refund used to need the
        # manager twice, for the page and again for the form on it.
        if request.method == "GET" and not granted.get("opened"):
            granted["opened"] = True
            request.session[session_key] = granted
            return Membership.objects.filter(
                pk=granted["approver"], is_active=True, user__is_active=True
            ).first()
        if request.method == "POST" and not request.POST.get("override_pin"):
            request.session.pop(session_key, None)
            approver = Membership.objects.select_related("role").filter(
                pk=granted["approver"], is_active=True, user__is_active=True
            ).first()
            # The manager approved opening the page. What is submitted on it
            # must still be within *their* own limit -- an adjustment has no
            # value until the quantity is typed, so this check is the one
            # that bounds it.
            # And no larger than what they approved, when that was known:
            # a PIN given for a small refund could otherwise be spent on a
            # big one, up to the manager's own ceiling.
            approved = granted.get("value")
            if approved is not None and value is not None and \
                    Decimal(str(value)) > Decimal(approved):
                return None
            if approver is not None and approver.check_permission(
                code, branch=branch, value=value
            ):
                return approver
            return None
    elif granted:
        request.session.pop(session_key, None)

    if request.method == "GET":
        return None

    email = (request.POST.get("override_email") or "").strip().lower()
    pin = request.POST.get("override_pin") or ""
    if not email or not pin:
        return None

    decision = request.membership.check_permission(code, branch=branch, value=value)
    pending = request.session.get("approval_nonces", {})
    ticket = pending.pop(request.POST.get("override_nonce", ""), None)
    request.session["approval_nonces"] = pending
    if not ticket or ticket["code"] != code or ticket["path"] != request.path \
            or ticket["expires"] < time.time():
        return _ask_for_approval(
            request, decision, code,
            "That approval was already used or has expired. Ask the manager again.",
        )

    # Keyed on the manager's own account, not on the spelling typed: the
    # lookup is case-insensitive, so "BOSS@x" and "boss@x" each had their
    # own budget.
    approver = (
        Membership.objects.select_related("user", "role")
        .filter(user__email__iexact=email, is_active=True, user__is_active=True)
        .first()
    )
    throttle_key = f"pin-fail:{request.tenant.pk}:{request.user.pk}"
    approver_key = (f"pin-fail-approver:{request.tenant.pk}:"
                    f"{approver.pk if approver else (email or '').lower()}")
    if _cache_get(throttle_key, 0) >= MAX_PIN_TRIES or \
            _cache_get(approver_key, 0) >= MAX_PIN_TRIES * 2:
        # Per person asking *and* per manager: several staff accounts
        # guessing one manager's four-digit PIN used to multiply the rate.
        return _ask_for_approval(request, decision, code,
                                 "Too many wrong PINs. Wait 15 minutes and try again.")

    problem = None
    if approver is None or not approver.check_pin(pin):
        problem = "That email and PIN do not match a manager here."
    elif approver.user_id == request.user.pk:
        problem = "Somebody else has to approve this."
    else:
        verdict = approver.check_permission(code, branch=branch, value=value)
        if not verdict:
            problem = f"{approver.user.name} cannot approve this either: {verdict.reason}"

    if problem:
        _cache_incr(throttle_key)
        _cache_incr(approver_key)
        return _ask_for_approval(request, decision, code, problem)

    _cache_delete(throttle_key)
    # A right PIN clears the manager's counter too, so two staff mistyping it
    # cannot leave them unable to approve anything for fifteen minutes.
    _cache_delete(approver_key)
    if request.POST.get("override_method") == "GET":
        request.session[session_key] = {
            "approver": approver.pk, "expires": time.time() + APPROVAL_SECONDS,
            "value": None if value is None else str(value),
        }
        return HttpResponseRedirect(request.get_full_path())
    return approver


def _run_approved(view, request, approver, args, kwargs):
    from apps.core import audit
    from apps.core.context import reset_current_approver, set_current_approver

    request.authorised_by = approver.user
    token = set_current_approver(approver.user)
    try:
        audit.record("approval.granted", after={"by": approver.user.email,
                                                 "for": request.path},
                     ip=audit.client_ip(request))
        return view(request, *args, **kwargs)
    finally:
        reset_current_approver(token)


def _cache_get(key, default):
    try:
        return cache.get(key, default)
    except Exception:
        return default


def _cache_incr(key):
    # A cache outage must not stop a sale; it only loses the counter.
    with suppress(Exception):
        if not cache.add(key, 1, timeout=15 * 60):
            cache.incr(key)


def _cache_delete(key):
    with suppress(Exception):
        cache.delete(key)


def branch_of(model, path="branch", key="pk"):
    """
    For ``requires(branch=...)``: the branch of the object in the URL.

    Without it every check ran against the branch the person happened to be
    signed in to, so a manager of one branch could act on another's records.
    ``path`` follows relations: "order.branch" for an order line,
    "from_branch" for a transfer's sender.
    """

    def resolve(request, **kwargs):
        obj = model.objects.filter(pk=kwargs.get(key)).first()
        for part in path.split("."):
            obj = getattr(obj, part, None) if obj is not None else None
        return obj

    return resolve


def not_support(view):
    """
    Refuse during a platform support session.

    Support signs in as a stand-in owner to see and fix a shop's settings.
    Creating accounts, setting passwords or PINs and sending invitations are
    different: they outlive the session, so support could leave itself a way
    back in that the shop cannot see. The shop's own people do these.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if getattr(getattr(request, "membership", None), "is_support", False):
            raise PermissionDenied(
                "A support session cannot add staff, set passwords or send invitations. "
                "The shop's owner does this."
            )
        return view(request, *args, **kwargs)

    return wrapped


def platform_staff_only(view):
    """Gate for /platform/. Sits outside tenant scoping entirely."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_platform_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def platform_perm(*codes):
    """
    Gate a platform view on the team member's role.

    Any one of the codes is enough. Implies platform_staff_only: a shop's
    staff hold no platform permissions at all.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if (
                user is None
                or not user.is_authenticated
                or not any(user.has_platform_perm(code) for code in codes)
            ):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator

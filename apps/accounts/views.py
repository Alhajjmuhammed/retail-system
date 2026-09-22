"""
Auth, staff and the role builder.

The role builder is the screen that sells this system to an owner, so it reads
in plain language grouped by area -- Sales, Stock, Money -- with the ceilings
inline, never raw permission codes.
"""

import uuid

from django.contrib import messages
from django.contrib.auth import login, password_validation, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts import guards, throttle
from apps.accounts.forms import RoleForm, SignupForm, StaffForm
from apps.accounts.models import (
    Invitation,
    Membership,
    OverrideEffect,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
)
from apps.core import audit
from apps.core.context import tenant_context, unscoped
from apps.core.decorators import not_support, requires
from apps.core.features import LimitExceeded
from apps.core.parsing import decimal_or_none
from apps.core.permissions import ValueType
from apps.core.permissions import registry as perms_registry
from apps.tenancy.services import create_tenant

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _refuse_branch_bound_role_editor(request):
    """
    Roles apply to the whole shop.

    A manager confined to one branch could not edit another branch's staff
    directly, but could edit the role they hold and change what they may do
    everywhere.
    """
    m = request.membership
    if not (m.role.is_owner_role or m.all_branches):
        raise PermissionDenied(
            "Roles apply to every branch, so they are changed by somebody who works "
            "across the whole shop. Ask an owner."
        )


def _canonical_email(raw):
    """
    The account's own address, whatever was typed.

    Django normalises unicode and the account lookup is case-insensitive, so
    a look-alike spelling and "SALMA@x" and " salma@x " are all one account
    -- and used to get one lockout budget each.
    """
    import unicodedata

    typed = unicodedata.normalize("NFKC", (raw or "")).strip().lower()
    if not typed:
        return typed
    found = User.objects.filter(email__iexact=typed).values_list("email", flat=True).first()
    return (found or typed).lower()


class LoginView(auth_views.LoginView):
    """
    Sign-in, with password guessing slowed down.

    A login form on the public internet is otherwise an open invitation to
    run a wordlist against a shop owner's email.
    """

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def get_form_kwargs(self):
        # Emails are stored lower-case; "Owner@Shop.co.tz" must still sign in.
        kwargs = super().get_form_kwargs()
        if "data" in kwargs:
            data = kwargs["data"].copy()
            data["username"] = data.get("username", "").strip().lower()
            kwargs["data"] = data
        return kwargs

    def post(self, request, *args, **kwargs):
        # The same key the sign-in uses: " Owner@..." must count against
        # owner@..., or the lockout is one space away from useless.
        email = _canonical_email(request.POST.get("username", ""))
        ip = audit.client_ip(request)

        if throttle.is_locked(email, ip):
            form = self.get_form()
            form.add_error(
                None,
                "Too many failed attempts. Wait fifteen minutes and try again.",
            )
            return self.form_invalid(form)

        response = super().post(request, *args, **kwargs)

        if request.user.is_authenticated:
            throttle.clear(email, ip)
        else:
            throttle.record_failure(email, ip)
            audit_login_failure(email, ip)

        return response


def audit_login_failure(email, ip):
    """
    Recorded outside any tenant, because a failed sign-in has no tenant yet.

    Logged rather than stored: an audit row needs a tenant, and repeated
    failures are an operational signal, not a shop's business.
    """
    import logging

    logging.getLogger("apps.accounts").warning(
        "Failed sign-in for %s from %s", email, ip
    )


class LogoutView(auth_views.LogoutView):
    pass


def signup(request):
    """Create the person, the business and the trial in one step."""
    if request.user.is_authenticated:
        return redirect("core:dashboard")

    form = SignupForm(request.POST or None)
    if request.method == "POST" and throttle.signups_exhausted(audit.client_ip(request)):
        form.add_error(None, "Too many shops opened from here just now. Try again later.")
        return render(request, "accounts/signup.html", {"form": form})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
                name=form.cleaned_data["name"],
                phone=form.cleaned_data["phone"],
            )
            tenant, _membership = create_tenant(
                name=form.cleaned_data["business_name"], owner=user
            )

        throttle.record_signup(audit.client_ip(request))
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["tenant_id"] = tenant.pk
        # A plan with no trial is not "a 0-day trial", which is what this
        # said to every shop that signed up, all of which land on Free.
        plan = tenant.subscription.plan
        messages.success(
            request,
            f"Welcome. {tenant.name} is on a {plan.trial_days}-day free trial."
            if plan.trial_days else
            f"Welcome. {tenant.name} is ready, on the {plan.name} plan.",
        )
        return redirect("core:dashboard")

    return render(request, "accounts/signup.html", {"form": form})


@login_required
def no_tenant(request):
    """Signed in but belonging to no business yet."""
    return render(request, "accounts/no_tenant.html")


@login_required
def switch_tenant(request):
    """
    For owners who run more than one business.

    One identity, many memberships, the active one held in the session -- never
    in the URL, because the URL is user input.
    """
    with unscoped():
        memberships = list(
            Membership.objects_all.select_related("tenant", "role")
            .filter(user=request.user, is_active=True)
            .order_by("tenant__name")
        )

    if request.method == "POST":
        wanted = request.POST.get("tenant_id") or ""
        chosen = next(
            (m for m in memberships if str(m.tenant_id) == wanted), None
        )
        if chosen is None:
            messages.error(request, "You do not belong to that business.")
            return redirect("accounts:switch")

        request.session["tenant_id"] = chosen.tenant_id
        request.session.pop("branch_id", None)
        request.user.last_tenant = chosen.tenant
        request.user.save(update_fields=["last_tenant", "updated_at"])
        return redirect("core:dashboard")

    return render(request, "accounts/switch.html", {"memberships": memberships})


@login_required
@require_POST
def switch_branch(request):
    """
    Move yourself to another of your branches.

    `active_branch` has always honoured a branch held in the session, but
    nothing ever put one there: whoever covered two shops was stuck in the
    default one. That stranded stock sent between branches, because a transfer
    is accepted by the branch it arrives at, and nobody could stand there.

    The branch is checked against this person's own branches, so a posted id
    cannot move somebody into a shop they do not work in.
    """
    if request.membership is None:
        raise PermissionDenied

    wanted = request.POST.get("branch_id") or ""
    chosen = next(
        (b for b in request.membership.branches() if str(b.pk) == wanted), None
    )
    if chosen is None:
        messages.error(request, "You do not work in that branch.")
    else:
        request.session["branch_id"] = chosen.pk
        messages.success(request, f"You are now working in {chosen.name}.")

    back = request.POST.get("next") or ""
    if back and url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(back)
    return redirect("core:dashboard")


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

@login_required
@requires("role.manage")
def roles(request):
    from django.db.models import Count, Q

    # Counted in the query, not per row; suspended holders shown apart.
    role_list = (
        Role.objects.annotate(
            active=Count("memberships", filter=Q(memberships__is_active=True), distinct=True),
            suspended=Count("memberships", filter=Q(memberships__is_active=False), distinct=True),
            perm_count=Count("permissions", filter=Q(permissions__granted=True), distinct=True),
        )
        .order_by("-is_owner_role", "name")
    )
    return render(request, "accounts/roles.html", {"roles": role_list})


@login_required
@requires("role.manage")
@not_support
def role_create(request):
    _refuse_branch_bound_role_editor(request)
    form = RoleForm(request.POST or None, tenant=request.tenant)
    if request.method == "POST" and form.is_valid():
        role = form.save(commit=False)
        role.tenant = request.tenant
        role.save()
        audit.record("role.created", obj=role, after=audit.snapshot(role))
        messages.success(request, f"{role.name} created. Now choose what it can do.")
        return redirect("accounts:role_edit", pk=role.pk)

    return render(request, "accounts/role_form.html", {"form": form})


@login_required
@requires("role.manage")
@not_support
def role_edit(request, pk):
    """
    The permission matrix.

    Owner is deliberately not editable: it holds everything by definition, and
    a tenant that could un-tick their own last owner would lock themselves out
    of their own business on a Saturday night.
    """
    _refuse_branch_bound_role_editor(request)
    role = get_object_or_404(Role, pk=pk)

    if role.is_owner_role:
        messages.info(
            request, "The Owner role always has full access and cannot be changed."
        )
        return redirect("accounts:roles")

    if request.membership.role_id == role.pk and not guards.is_owner(request.membership):
        messages.error(
            request, "You cannot change the role you hold yourself. Ask an owner."
        )
        return redirect("accounts:roles")

    if request.method == "POST":
        form = RoleForm(request.POST, instance=role, tenant=request.tenant)
        if not form.is_valid():
            return render(request, "accounts/role_form.html", {
                "role": role, "form": form,
                "groups": permission_groups(request.tenant, role=role),
                **_role_jobs(request.tenant, role),
            })
        # The name and description used to be shown here and then ignored.
        form.save()
        refused = _save_role_permissions(request, role, wanted=_wanted(request))
        if refused:
            messages.warning(
                request,
                "Left unchanged, because you do not hold them yourself: "
                + ", ".join(sorted(refused)) + ".",
            )
        messages.success(request, f"{role.name} updated.")
        return redirect("accounts:roles")

    return render(
        request,
        "accounts/role_form.html",
        {
            "role": role,
            "form": RoleForm(instance=role, tenant=request.tenant),
            "groups": permission_groups(request.tenant, role=role),
            **_role_jobs(request.tenant, role),
        },
    )


def _wanted(request):
    """
    What the form asked for, whichever of the two views sent it.

    The matrix speaks in permissions and is passed straight through. The
    plain view speaks in jobs, and is translated here -- once, on the way
    in, so everything after it is the same code.
    """
    from apps.accounts import role_jobs

    if request.POST.get("mode") != "simple":
        return None
    allowed = {
        permission.code for permission in Permission.objects.all()
        if not permission.requires_feature
        or request.tenant.has_feature(permission.requires_feature)
    }
    return role_jobs.expand(request.POST, available=allowed)


def _role_jobs(tenant, role=None):
    """
    The plain-language view of a role, and whether it can be trusted.

    A role assembled by hand in the full matrix may hold half of a job and
    two things no job speaks for. Flattening that into nine switches would
    quietly take permissions away, so such a role says so and opens in the
    matrix instead.
    """
    from apps.accounts import role_jobs

    granted = {}
    if role is not None:
        granted = {
            rp.permission.code: rp.limit_value
            for rp in role.permissions.select_related("permission")
        }
    jobs = role_jobs.read(granted)
    labels = {p.code: p.label for p in Permission.objects.all()}
    for row in jobs.values():
        row["limit"] = _plain(row["limit"]) if row["limit"] is not None else ""
        # What the switch actually grants, in the catalogue's own words, so
        # a coarse control is never a hidden one.
        row["includes"] = [labels.get(code, code) for code in row["job"].grants
                           if code in labels]
    by_hand = any(row["state"] == "some" for row in jobs.values())
    return {
        "jobs": [jobs[job.key] for job in role_jobs.JOBS],
        "set_by_hand": by_hand,
        "spare": role_jobs.unspoken(granted) if by_hand else [],
    }


def _save_role_permissions(request, role, wanted=None):
    """
    Write what the form asked for, within what the person asking may give.

    ``wanted`` maps permission code to {"limit", "options"} and is how the
    plain-language view says the same thing as the full matrix: both end up
    here, so the rules about what you may grant, what your plan includes and
    what gets written to the audit trail are written once.
    """
    def asked(code):
        if wanted is None:
            return (request.POST.get(f"grant:{code}") == "on",
                    (request.POST.get(f"limit:{code}") or "").strip(),
                    request.POST.getlist(f"options:{code}"))
        row = wanted.get(code)
        return (row is not None, (row or {}).get("limit", ""), (row or {}).get("options", []))

    existing = {rp.permission.code: rp for rp in role.permissions.select_related("permission")}
    permissions = {p.code: p for p in Permission.objects.all()}
    before = {code: str(rp.limit_value) for code, rp in existing.items()}

    keep = set()
    refused = set()
    actor = request.membership
    for code, permission in permissions.items():
        spec = perms_registry.get(code)
        if spec and spec.requires_feature and not role.tenant.has_feature(spec.requires_feature):
            # Shown greyed out, so never submitted: keep it exactly as it is.
            # It used to be deleted on every save, lost for when the shop
            # upgrades again.
            if code in existing:
                keep.add(code)
            continue
        wants, limit_raw, options = asked(code)
        had = code in existing
        # Compared as numbers: the form shows "20.00" for a stored 20, and
        # comparing text flagged every untouched limit as changed.
        changing = wants != had or (
            wants and (
                decimal_or_none(limit_raw) != existing[code].limit_value
                or sorted(options) != sorted(existing[code].set_value or [])
            )
        ) if had or wants else False
        bad_number = bool(limit_raw) and decimal_or_none(limit_raw) is None
        # Taking a permission away needs only that you hold it; giving one
        # (or changing its size) must fit inside what you hold.
        allowed = (guards.may_grant(actor, code, limit_raw, options) if wants
                   else guards.is_owner(actor) or guards.holds(actor, code))
        if changing and (bad_number or not allowed):
            # Not theirs to give or take: leave it exactly as it was.
            refused.add(permission.label if hasattr(permission, "label") else code)
            if had:
                keep.add(code)
            continue
        if not wants:
            continue
        keep.add(code)
        limit_value = decimal_or_none(limit_raw)
        RolePermission.objects.update_or_create(
            role=role,
            permission=permission,
            defaults={
                "granted": True,
                "limit_value": limit_value,
                "set_value": options,
            },
        )

    role.permissions.exclude(permission__code__in=keep).delete()
    role.bump_version()

    audit.record(
        "role.permissions_changed",
        obj=role,
        before=before,
        after={code: asked(code)[1] for code in keep},
        ip=audit.client_ip(request),
    )
    return refused


def _plain(value) -> str:
    """A stored limit as the form would send it back: 5.00 -> "5"."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@login_required
@requires("role.manage")
@require_POST
@not_support
def role_delete(request, pk):
    """
    Remove a role.

    Owner can never go: it is what administers the shop. A role somebody
    holds cannot go either -- silently moving people to another role is the
    kind of surprise that ends with a cashier voiding sales at midnight.
    """
    _refuse_branch_bound_role_editor(request)
    role = get_object_or_404(Role, pk=pk)
    holders = role.memberships.count()

    if role.is_owner_role:
        messages.error(request, "The Owner role cannot be deleted.")
    elif holders:
        messages.error(
            request,
            f"{holders} {'person holds' if holders == 1 else 'people hold'} "
            f"the {role.name} role. Move them to another role first.",
        )
    else:
        audit.record("role.deleted", obj=role, before=audit.snapshot(role),
                     ip=audit.client_ip(request))
        name = role.name
        role.delete()
        messages.success(request, f"{name} deleted.")

    return redirect("accounts:roles")


@login_required
@requires("role.manage")
@require_POST
@not_support
def role_duplicate(request, pk):
    """
    Copy a role, permissions and all.

    How a shop actually builds a second role: take the cashier, copy it, and
    change the two things that differ.
    """
    _refuse_branch_bound_role_editor(request)
    source = get_object_or_404(Role, pk=pk)
    if not guards.role_fits(request.membership, source):
        # Copying a role you could not have written is writing it.
        raise PermissionDenied(f"The {source.name} role can do things you cannot. Ask an owner.")

    name = f"{source.name} copy"
    suffix = 2
    while Role.objects.filter(name=name).exists():
        name = f"{source.name} copy {suffix}"
        suffix += 1

    copy = Role.objects.create(
        tenant=request.tenant,
        name=name,
        description=source.description,
    )
    # The owner role stores no rows -- it holds everything implicitly -- so a
    # copy of it starts empty and is an ordinary role.
    RolePermission.objects.bulk_create([
        RolePermission(
            role=copy, permission=rp.permission, granted=rp.granted,
            limit_value=rp.limit_value, set_value=rp.set_value,
        )
        for rp in source.permissions.all()
    ])

    audit.record("role.duplicated", obj=copy, ip=audit.client_ip(request))
    messages.success(request, f"{name} created. Change what differs.")
    return redirect("accounts:role_edit", pk=copy.pk)


def issue_invitation(request, email, role, *, branches=(), all_branches=True, pin=""):
    """
    Create (or renew) an invitation. Returns (invitation, problem).

    One place for every rule: only a role the inviter could hold, never a
    platform account, a fresh token each time -- re-inviting used to revive
    the old link -- and the branches and PIN chosen now applied on accept.
    """
    from datetime import timedelta

    from django.contrib.auth.hashers import make_password

    if role.is_owner_role and not guards.is_owner(request.membership):
        return None, "Only an owner can invite another owner."
    if not guards.role_fits(request.membership, role):
        return None, f"The {role.name} role can do things you cannot, so you cannot give it out."
    if not guards.branches_fit(request.membership, all_branches=all_branches, branches=branches):
        # An invitation from a manager of one branch is for that branch.
        all_branches = False
        branches = list(request.membership.branches())
        if not branches:
            # No open branch of their own: an empty list would mean "every
            # branch" once accepted.
            return None, "You do not work in an open branch, so you cannot invite anybody."
    with unscoped():
        platform_account = User.objects.filter(email__iexact=email, is_platform_staff=True).exists()
    if platform_account:
        return None, "This address cannot be invited to a shop."
    request.tenant.enforce_limit("users")
    invitation, _ = Invitation.objects.update_or_create(
        tenant=request.tenant,
        email=email.lower(),
        defaults={
            "role": role,
            "token": uuid.uuid4(),
            "expires_at": timezone.now() + timedelta(days=14),
            "accepted_at": None,
            "branch_ids": [] if all_branches else [b.pk for b in branches],
            "pin_hash": make_password(pin) if pin else "",
        },
    )
    audit.record("staff.invited", obj=invitation, ip=audit.client_ip(request))
    return invitation, None


def _cancel_open_invitations(tenant, email):
    """A pending link is a way in: it must not survive a change to the person."""
    Invitation.objects.filter(email__iexact=email, accepted_at__isnull=True).delete()


def _join(invitation, user):
    """Make the membership an invitation promised, with its branches and PIN."""
    from apps.org.models import Branch as BranchModel

    with tenant_context(invitation.tenant, user=user):
        membership = Membership.objects.filter(user=user).first()
        if membership is None:
            membership = Membership.objects.create(
                tenant=invitation.tenant, user=user, role=invitation.role,
                pin_hash=invitation.pin_hash, all_branches=not invitation.branch_ids,
            )
            for branch in BranchModel.objects.filter(pk__in=invitation.branch_ids):
                membership.branch_links.create(branch=branch)
        elif not membership.is_active:
            # Taken off the team earlier and invited back: they are back.
            membership.is_active = True
            membership.role = invitation.role
            membership.save(update_fields=["is_active", "role", "updated_at"])
        # Their permissions have just changed; the cached map must not stand.
        membership.invalidate_permissions()
    return membership


@login_required
@requires("user.manage")
@not_support
def staff_invite(request):
    """
    Invite somebody to set their own password.

    The alternative -- a manager typing a password and reading it out -- works
    but means the manager knows it. An invite link sent over WhatsApp lets the
    person choose their own, which is how a password should start.
    """

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        role = get_object_or_404(Role, pk=request.POST.get("role") or 0)

        if not email:
            messages.error(request, "An email address is required.")
        elif Membership.objects.filter(user__email=email).exists():
            messages.error(request, "This person is already on your team.")
        else:
            invitation, problem = issue_invitation(request, email, role)
            if problem:
                messages.error(request, problem)
            else:
                return redirect("accounts:staff_invite_sent", pk=invitation.pk)

    return render(
        request,
        "accounts/staff_invite.html",
        {
            "roles": Role.objects.exclude(is_owner_role=True).order_by("name"),
            "pending": [
                inv for inv in Invitation.objects.filter(
                    accepted_at__isnull=True, expires_at__gt=timezone.now()
                ).select_related("role")
                if guards.may_handle_invitation(request.membership, inv)
            ],
        },
    )


@login_required
@requires("user.manage")
@not_support
def staff_invite_sent(request, pk):
    """Show the link once, to copy into WhatsApp."""
    invitation = get_object_or_404(Invitation, pk=pk)
    if not guards.may_handle_invitation(request.membership, invitation):
        raise PermissionDenied("This invitation gives more than you hold. Ask an owner.")
    link = request.build_absolute_uri(
        reverse("accounts:accept_invitation", args=[invitation.token])
    )
    return render(
        request,
        "accounts/staff_invite_sent.html",
        {"invitation": invitation, "link": link},
    )


@login_required
@requires("user.manage")
@require_POST
@not_support
def staff_invite_cancel(request, pk):
    invitation = get_object_or_404(Invitation, pk=pk)
    if not guards.may_handle_invitation(request.membership, invitation):
        raise PermissionDenied("This invitation gives more than you hold. Ask an owner.")
    email = invitation.email
    audit.record("staff.invite_cancelled", after={"email": email}, ip=audit.client_ip(request))
    invitation.delete()
    messages.success(request, f"Invitation to {email} cancelled.")
    return redirect("accounts:staff")


def accept_invitation(request, token):
    """
    Where an invited person lands.

    Public by necessity: they have no account yet. The token is the only
    credential, so it expires and is single use.
    """
    # unscoped, not just objects_all: somebody already signed in to one shop
    # may be opening an invitation to another, and row-level security would
    # otherwise hide it and show them "this link no longer works". The token
    # is the credential here, and it is unguessable.
    with unscoped():
        invitation = (
            Invitation.objects_all.filter(token=token)
            .select_related("tenant", "role")
            .first()
        )

    if invitation is None or not invitation.is_valid:
        return render(request, "accounts/invitation_invalid.html", status=410)

    # Somebody who already has an account -- in another shop, or on the
    # platform -- proves it is them with the password they already have. The
    # link is shown to the shop owner to pass on, so letting it choose a new
    # password would let any owner take over any account by inviting it.
    with unscoped():
        existing = User.objects.filter(email=invitation.email).first()
    if existing is not None:
        return _accept_as_existing(request, invitation, existing)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")

        try:
            password_validation.validate_password(password)
        except ValidationError as exc:
            return render(
                request,
                "accounts/accept_invitation.html",
                {"invitation": invitation, "errors": exc.messages, "name": name},
            )

        if not name:
            return render(
                request,
                "accounts/accept_invitation.html",
                {"invitation": invitation, "errors": ["Your name is required."]},
            )

        if not invitation.tenant.within_limit("users"):
            return render(request, "accounts/accept_invitation.html", {
                "invitation": invitation, "name": name,
                "errors": [f"{invitation.tenant.name} has no room for more staff on its "
                           "current plan. Ask them to upgrade, then open this link again."],
            })

        with transaction.atomic():
            user, _ = User.objects.get_or_create(
                email=invitation.email, defaults={"name": name}
            )
            user.name = name
            user.set_password(password)
            user.save()

            _join(invitation, user)

            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["accepted_at", "updated_at"])

        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["tenant_id"] = invitation.tenant_id
        messages.success(request, f"Welcome to {invitation.tenant.name}.")
        return redirect("core:dashboard")

    return render(
        request, "accounts/accept_invitation.html", {"invitation": invitation}
    )


def _accept_as_existing(request, invitation, user):
    """Join a shop with the account you already have. Nothing about it changes."""
    signed_in_as_them = request.user.is_authenticated and request.user.pk == user.pk
    errors = []

    ip = audit.client_ip(request)
    if request.method == "POST":
        if user.is_platform_staff:
            # Never a way into a platform account, whoever sent the link.
            errors.append("This invitation cannot be accepted. Ask the shop to contact support.")
        elif not signed_in_as_them and throttle.is_locked(user.email, ip):
            # Same limit as the sign-in page: this form checks a password too,
            # and the link is in the hands of whoever sent the invitation.
            errors.append("Too many failed attempts. Wait fifteen minutes and try again.")
        elif not signed_in_as_them and not user.check_password(
            request.POST.get("password", "")
        ):
            throttle.record_failure(user.email, ip)
            errors.append("That is not the password for this account.")
        elif not user.is_active:
            errors.append("This account has been switched off. Ask the shop to contact support.")
        elif not invitation.tenant.within_limit("users"):
            errors.append(f"{invitation.tenant.name} has no room for more staff on its "
                          "current plan. Ask them to upgrade, then open this link again.")
        else:
            with transaction.atomic():
                _join(invitation, user)
                with unscoped():
                    invitation.accepted_at = timezone.now()
                    invitation.save(update_fields=["accepted_at", "updated_at"])
            throttle.clear(user.email, ip)

            if not signed_in_as_them:
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session["tenant_id"] = invitation.tenant_id
            messages.success(request, f"Welcome to {invitation.tenant.name}.")
            return redirect("core:dashboard")

    return render(request, "accounts/accept_invitation.html", {
        "invitation": invitation, "existing": user,
        "signed_in_as_them": signed_in_as_them, "errors": errors,
    })


@login_required
@requires("user.manage")
@require_POST
@not_support
def staff_remove(request, pk):
    """
    Take somebody off the team.

    Their sales, adjustments and audit rows stay exactly as they are -- the
    membership goes, the history does not.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("user", "role"), pk=pk
    )

    if membership.user_id == request.user.pk:
        messages.error(request, "You cannot remove yourself.")
    elif membership.role.is_owner_role and not guards.is_owner(request.membership):
        messages.error(request, "Only an owner can remove an owner.")
    elif guards.outranks(membership, request.membership):
        messages.error(request, f"{membership.user.name} can do things you cannot. Ask an owner.")
    elif membership.role.is_owner_role and not Membership.objects.filter(
        role__is_owner_role=True, is_active=True, user__is_active=True
    ).exclude(pk=membership.pk).exists():
        messages.error(
            request,
            "This is the only owner. Make somebody else an owner first, or "
            "nobody will be able to administer the shop.",
        )
    else:
        name = membership.user.name
        audit.record("staff.removed", obj=membership, ip=audit.client_ip(request))
        # Any open invitation to them would let them back in, with its role.
        _cancel_open_invitations(request.tenant, membership.user.email)
        membership.delete()
        messages.success(
            request, f"{name} removed. Everything they recorded stays."
        )

    return redirect("accounts:staff")


def permission_groups(tenant, role=None, membership=None):
    """
    The catalogue, grouped for a human.

    Permissions whose feature the plan excludes are still shown, marked
    unavailable, so an owner can see what an upgrade would buy rather than
    wondering why a checkbox is missing.
    """
    granted: dict[str, dict] = {}
    if role is not None:
        granted = {
            rp.permission.code: {"limit": rp.limit_value, "options": rp.set_value}
            for rp in role.permissions.select_related("permission")
        }

    overrides: dict[str, UserPermission] = {}
    if membership is not None:
        overrides = {
            up.permission.code: up
            for up in membership.overrides.select_related("permission")
        }

    groups: dict[str, list] = {}
    for permission in Permission.objects.all():
        available = (
            not permission.requires_feature
            or tenant.has_feature(permission.requires_feature)
        )
        entry = granted.get(permission.code)
        groups.setdefault(permission.module, []).append(
            {
                "permission": permission,
                "available": available,
                "granted": entry is not None,
                "limit": entry["limit"] if entry else None,
                "options": entry["options"] if entry else [],
                "takes_limit": permission.value_type
                in {ValueType.AMOUNT, ValueType.PERCENT},
                "takes_options": permission.value_type == ValueType.SET,
                "choices": list(permission.spec.options),
                "override": overrides.get(permission.code),
            }
        )
    return groups


# --------------------------------------------------------------------------
# Staff
# --------------------------------------------------------------------------

@login_required
@requires("user.manage")
def staff(request):
    from django.db.models import Count, Q
    from django.utils import timezone

    memberships = (
        Membership.objects.select_related("user", "role")
        .prefetch_related("branch_links__branch")
        .annotate(exceptions=Count("overrides", distinct=True))
        .order_by("user__name")
    )
    term = request.GET.get("q", "").strip()
    if term:
        memberships = memberships.filter(
            Q(user__name__icontains=term) | Q(user__email__icontains=term)
            | Q(role__name__icontains=term))
    status = request.GET.get("status", "")
    if status == "active":
        memberships = memberships.filter(is_active=True)
    elif status == "suspended":
        memberships = memberships.filter(is_active=False)
    else:
        status = ""
    pending = [
        inv for inv in Invitation.objects.select_related("role")
        .filter(accepted_at__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at")
        if guards.may_handle_invitation(request.membership, inv)
    ]
    memberships = list(memberships)
    for m in memberships:
        # Only offer what the server would allow: buttons on the owner that
        # then said "ask an owner" were a dead end.
        m.may_manage = m.user_id != request.user.pk and not guards.outranks(m, request.membership)
    return render(request, "accounts/staff.html", {
        "memberships": memberships, "q": term, "status": status, "pending": pending,
        "suspended": Membership.objects.filter(is_active=False).count(),
    })


@login_required
@requires("user.manage")
@require_POST
@not_support
def staff_toggle(request, pk):
    """
    Suspend somebody without removing them -- leave, a dispute, a lost phone.

    The same guards as removal: never yourself, never someone who can do
    more than you, never the last owner.
    """
    membership = get_object_or_404(Membership.objects.select_related("user", "role"), pk=pk)
    name = membership.user.name
    if membership.is_active:
        if membership.user_id == request.user.pk:
            messages.error(request, "You cannot suspend yourself.")
            return redirect("accounts:staff")
        if guards.outranks(membership, request.membership) or (
                membership.role.is_owner_role and not guards.is_owner(request.membership)):
            messages.error(request, f"{name} can do things you cannot. Ask an owner.")
            return redirect("accounts:staff")
        if membership.role.is_owner_role and not Membership.objects.filter(
                role__is_owner_role=True, is_active=True, user__is_active=True
        ).exclude(pk=membership.pk).exists():
            messages.error(request, "This is the only owner and cannot be suspended.")
            return redirect("accounts:staff")
    elif guards.outranks(membership, request.membership):
        messages.error(request, f"{name} can do things you cannot. Ask an owner.")
        return redirect("accounts:staff")
    if not membership.is_active:
        # Suspended people do not count against the plan; bringing one back
        # does. Without this a Free shop suspended, added and reactivated its
        # way past its user limit.
        try:
            request.tenant.enforce_limit("users")
        except LimitExceeded as exc:
            messages.error(request, str(exc))
            return redirect("accounts:staff")
    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active", "updated_at"])
    if not membership.is_active:
        # Suspended, with a pending link, meant "suspended until they click it".
        _cancel_open_invitations(request.tenant, membership.user.email)
    audit.record("staff.reactivated" if membership.is_active else "staff.suspended",
                 obj=membership, ip=audit.client_ip(request))
    messages.success(request, f"{name} can sign in again." if membership.is_active
                     else f"{name} is suspended and can no longer sign in here.")
    return redirect("accounts:staff")


@login_required
@requires("user.manage")
@not_support
def staff_create(request):
    form = StaffForm(request.POST or None, tenant=request.tenant, actor=request.membership)
    if request.method == "POST" and form.is_valid():
        request.tenant.enforce_limit("users")
        data = form.cleaned_data

        user = User.objects.filter(email__iexact=data["email"]).first()
        if user is not None:
            # Somebody who already has an account -- in another shop --
            # joins only by accepting with their own password. Attaching
            # them directly let one shop plant an account it knew the
            # password to in another shop's team.
            invitation, problem = issue_invitation(
                request, user.email, data["role"], branches=data["branches"],
                all_branches=data["all_branches"], pin=data["pin"],
            )
            if problem:
                messages.error(request, problem)
                return render(request, "accounts/staff_form.html", {"form": form})
            messages.info(
                request,
                f"{user.email} already has an account, so they join by accepting "
                "this invitation with their own password. Send them the link.",
            )
            return redirect("accounts:staff_invite_sent", pk=invitation.pk)

        user = User.objects.create_user(
            email=data["email"], password=data["password"],
            name=data["name"], phone=data["phone"],
        )
        membership = Membership.objects.create(
            tenant=request.tenant, user=user, role=data["role"]
        )
        membership.set_branches(data["branches"], all_branches=data["all_branches"])
        if data["pin"]:
            membership.set_pin(data["pin"])

        audit.record("staff.added", obj=membership, ip=audit.client_ip(request))
        messages.success(request, f"{user.name} added as {data['role'].name}.")
        return redirect("accounts:staff_edit", pk=membership.pk)

    return render(request, "accounts/staff_form.html", {"form": form})


@login_required
@requires("user.manage")
@not_support
def staff_edit(request, pk):
    """
    Role, branches, and the per-person exceptions.

    "Juma is a Cashier but he may also adjust stock" -- or the reverse. A deny
    always beats the role's grant, because withholding something has to be
    reliable or it is worthless.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("user", "role"), pk=pk
    )
    if membership.pk != request.membership.pk and guards.outranks(membership, request.membership):
        # The form opened and every save was then refused.
        raise PermissionDenied(f"{membership.user.name} can do things you cannot. Ask an owner.")

    if request.method == "POST":
        if request.POST.get("action") == "overrides":
            refusal = guards.refuse_staff_change(request.membership, membership)
            if refusal:
                messages.error(request, refusal)
                return redirect("accounts:staff_edit", pk=membership.pk)
            refused = _save_overrides(request, membership)
            if refused:
                messages.warning(
                    request,
                    "Left unchanged, because you do not hold them yourself: "
                    + ", ".join(sorted(refused)) + ".",
                )
            messages.success(request, f"Exceptions for {membership.user.name} saved.")
        else:
            form = StaffForm(request.POST, tenant=request.tenant, instance=membership,
                             actor=request.membership)
            if not form.is_valid():
                # Used to redirect here, silently throwing the errors away.
                return render(request, "accounts/staff_form.html", {
                    "membership": membership, "form": form,
                    "groups": permission_groups(
                        request.tenant, role=membership.role, membership=membership
                    ),
                })
            data = form.cleaned_data
            actor = request.membership
            role_changing = data["role"].pk != membership.role_id
            current_branches = set(
                membership.branch_links.filter(branch__is_active=True)
                .values_list("branch_id", flat=True)
            )
            branches_changing = (
                data["all_branches"] != membership.all_branches
                or {b.pk for b in data["branches"]} != current_branches
            )

            if membership.pk == actor.pk:
                refusal = ("You cannot change your own role or branches. Ask an owner."
                           if role_changing or branches_changing else None)
            else:
                # Any change to somebody else -- password, PIN, branches, not
                # only their role -- needs them to hold nothing you do not.
                refusal = guards.refuse_staff_change(
                    actor, membership, new_role=data["role"] if role_changing else None
                )
            if refusal:
                messages.error(request, refusal)
                return redirect("accounts:staff_edit", pk=membership.pk)

            if membership.user.shop_may_set_password(request.tenant):
                membership.user.name = data["name"]
                membership.user.phone = data["phone"]
                if data.get("password"):
                    membership.user.set_password(data["password"])
                membership.user.save(
                    update_fields=["name", "phone", "password", "updated_at"]
                )
            if role_changing:
                membership.role = data["role"]
                membership.save(update_fields=["role", "updated_at"])
            if branches_changing:
                membership.set_branches(data["branches"], all_branches=data["all_branches"])
            if data["pin"]:
                membership.set_pin(data["pin"])
            membership.invalidate_permissions()
            if membership.user.pk == request.user.pk and data.get("password"):
                update_session_auth_hash(request, membership.user)
            audit.record("staff.updated", obj=membership, ip=audit.client_ip(request),
                         after={"role": data["role"].name})
            messages.success(request, f"{membership.user.name} updated.")
            return redirect("accounts:staff_edit", pk=membership.pk)
        return redirect("accounts:staff_edit", pk=membership.pk)

    form = StaffForm(
        tenant=request.tenant,
        instance=membership,
        actor=request.membership,
        initial={
            "name": membership.user.name,
            "email": membership.user.email,
            "phone": membership.user.phone,
            "role": membership.role,
            "all_branches": membership.all_branches,
            "branches": [link.branch for link in membership.branch_links.all()],
        },
    )
    return render(
        request,
        "accounts/staff_form.html",
        {
            "membership": membership,
            "form": form,
            "groups": permission_groups(
                request.tenant, role=membership.role, membership=membership
            ),
        },
    )


def _save_overrides(request, membership):
    """
    Save per-person exceptions.

    A deny is always allowed -- taking something away cannot escalate. A
    grant is only allowed for what the person granting it holds themselves,
    at no bigger a limit; anything else is kept exactly as it was.
    """
    permissions = {p.code: p for p in Permission.objects.all()}
    existing = {o.permission.code: o for o in membership.overrides.select_related("permission")}
    actor = request.membership
    refused = set()
    wanted = {}

    for code in permissions:
        effect = request.POST.get(f"override:{code}")
        limit_raw = (request.POST.get(f"override_limit:{code}") or "").strip()
        if limit_raw and decimal_or_none(limit_raw) is None:
            refused.add(code)
            if code in existing:
                wanted[code] = None  # kept exactly as it was, deny or grant
            continue
        if effect == OverrideEffect.GRANT and not guards.may_grant(actor, code, limit_raw):
            refused.add(code)
            if code in existing:
                wanted[code] = None  # keep as it was
            continue
        if code in existing and existing[code].effect == OverrideEffect.GRANT \
                and effect != OverrideEffect.GRANT and not guards.may_grant(actor, code):
            # Removing a grant you could not have given is harmless, but
            # replacing it is not yours to decide either; leave it.
            wanted[code] = None
            continue
        if effect in {OverrideEffect.GRANT, OverrideEffect.DENY}:
            limit_value = decimal_or_none(limit_raw)
            wanted[code] = (effect, limit_value,
                            request.POST.get(f"override_reason:{code}", "")[:200])

    for code, override in existing.items():
        if wanted.get(code, "drop") is None:
            continue
        override.delete()
    for code, spec in wanted.items():
        if spec is None:
            continue
        effect, limit_value, reason = spec
        UserPermission.objects.create(
            membership=membership, permission=permissions[code],
            effect=effect, limit_value=limit_value, reason=reason,
        )

    membership.invalidate_permissions()
    audit.record(
        "staff.permissions_changed",
        obj=membership,
        ip=audit.client_ip(request),
    )
    return refused


# --------------------------------------------------------------------------
# Your own account
# --------------------------------------------------------------------------

@login_required
def profile(request):
    """
    What the shop knows about you, and the parts of it you may change.

    Everybody gets this, whatever their role holds -- a cashier who cannot
    reach a single settings page still has a name that might be spelled wrong
    and a phone number that changes. What they may not change is anything
    that would be a way around their own permissions: not their email, which
    is how they sign in and how an owner finds them, and not their role.

    The approval PIN lives here too, for the same reason a password does. A
    manager whose PIN was typed in for them by somebody else is a manager
    whose approvals are not theirs.
    """
    membership = request.membership
    if request.method == "POST":
        action = request.POST.get("action", "details")

        if action == "pin":
            if membership is None:
                raise PermissionDenied
            pin = (request.POST.get("pin") or "").strip()
            again = (request.POST.get("pin_again") or "").strip()
            if pin == "" and request.POST.get("remove"):
                membership.pin_hash = ""
                membership.save(update_fields=["pin_hash", "updated_at"])
                audit.record("membership.pin_removed", obj=membership,
                             ip=audit.client_ip(request))
                messages.success(request, "Your approval PIN has been removed.")
            elif not pin.isdigit() or not 4 <= len(pin) <= 8:
                messages.error(request, "A PIN is between four and eight numbers.")
            elif pin != again:
                messages.error(request, "The two PINs are not the same.")
            else:
                membership.set_pin(pin)
                audit.record("membership.pin_set", obj=membership,
                             ip=audit.client_ip(request))
                messages.success(request, "Your approval PIN has been changed.")
            return redirect("accounts:profile")

        name = (request.POST.get("name") or "").strip()[:120]
        phone = (request.POST.get("phone") or "").strip()[:30]
        if not name:
            messages.error(request, "A name is needed: it goes on every sale you make.")
            return redirect("accounts:profile")
        before = audit.snapshot(request.user)
        request.user.name = name
        request.user.phone = phone
        request.user.save(update_fields=["name", "phone", "updated_at"])
        audit.record("user.profile_changed", obj=request.user, before=before,
                     after=audit.snapshot(request.user), ip=audit.client_ip(request))
        messages.success(request, "Saved.")
        return redirect("accounts:profile")

    branches = []
    if membership is not None:
        branches = list(membership.branches(include_closed=True))
    return render(request, "accounts/profile.html", {
        "membership": membership,
        "branches": branches,
        "has_pin": bool(membership and membership.pin_hash),
        # Only worth offering to somebody whose approvals are ever asked for.
        "pin_matters": bool(membership and membership.role
                            and membership.role.permissions.filter(granted=True).exists()),
    })


@login_required
def my_permissions(request):
    """
    What your role actually allows you to do, in plain words.

    Read-only, and for everybody. A cashier who is refused something should
    be able to find out why without asking the owner to open a settings page
    they cannot reach themselves -- and an owner should be able to hand
    somebody this page instead of describing the role from memory.
    """
    membership = request.membership
    if membership is None:
        raise PermissionDenied

    groups = permission_groups(request.tenant, role=membership.role,
                               membership=membership)
    # Only what they hold. The full catalogue belongs on the role screen,
    # where somebody can act on it.
    mine: dict[str, list] = {}
    for module, items in groups.items():
        kept = []
        for item in items:
            override = item["override"]
            allowed = item["granted"]
            if override is not None:
                allowed = override.effect == "allow"
            if allowed and item["available"]:
                # "up to 5" and "up to 5%" are different permissions to a
                # cashier reading this to find out what they may do.
                kept.append({
                    **item,
                    "personal": override is not None,
                    "is_percent": item["permission"].value_type == ValueType.PERCENT,
                })
        if kept:
            mine[module] = kept

    return render(request, "accounts/my_permissions.html", {
        "membership": membership,
        "groups": mine,
        "held": sum(len(items) for items in mine.values()),
        "is_owner": guards.is_owner(membership),
    })


# --------------------------------------------------------------------------
# Your own password
# --------------------------------------------------------------------------

@login_required
def password_change(request):
    """
    Change your own password.

    There was no way to at all: a password a manager typed in for you stayed
    one the manager knew, forever.
    """
    from django.contrib.auth.forms import PasswordChangeForm

    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # stay signed in here
        messages.success(request, "Password changed. Other devices have been signed out.")
        return redirect("platform:dashboard" if user.is_platform_staff and not request.tenant
                        else "core:dashboard")
    for field in form.fields.values():
        field.widget.attrs.setdefault("class", "input")
    return render(request, "accounts/password_change.html", {"form": form})


class PasswordResetView(auth_views.PasswordResetView):
    """Forgotten password: a one-time link by email. Same answer whether or not the address exists."""

    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = "/accounts/password/reset/sent/"

    def form_valid(self, form):
        form.cleaned_data["email"] = form.cleaned_data["email"].strip().lower()
        return super().form_valid(form)


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = "/accounts/login/?reset=1"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "input input-lg")
        return form

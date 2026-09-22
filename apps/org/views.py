"""
Settings.

Everything a shop configures about itself: its branches and tills, its tax
rates and units, and the handful of choices that change how the rest of the
system behaves.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.decorators import branch_of, requires
from apps.core.deletion import remove_or_archive
from apps.core.features import LimitExceeded
from apps.org.forms import BranchForm, BusinessForm, RegisterForm, TenantProfileForm
from apps.org.models import Branch, Register, TenantSettings


def _branch_in_url(request, **kwargs):
    return Branch.objects.filter(pk=kwargs.get("pk")).first() if kwargs.get("pk") else None


def _covers_all(request):
    m = request.membership
    return m.role.is_owner_role or m.all_branches


def _settings_row(tenant):
    row, _ = TenantSettings.objects.get_or_create(tenant=tenant)
    return row


@login_required
def settings_home(request):
    """
    One door in front of the ten settings pages.

    Each one was a word the sidebar could not explain -- "Taxonomy",
    "Price lists", "Devices" -- so here each carries the sentence that says
    what it is for, and somebody who opens settings once a year can find the
    right page without guessing. No permission of its own: it shows only the
    pages this person may already open, and is empty for nobody, because
    anybody who can reach it can reach at least one.
    """
    can = request.membership.can
    everything = [
        ("user.manage", "accounts:staff", "user", "ink", "Your people",
         "Who works here, what they may do, and how to invite somebody."),
        ("role.manage", "accounts:roles", "shield", "ink", "Roles & permissions",
         "The jobs in your shop, and what each job is allowed to do."),
        ("register.manage", "org:branches", "building", "ink", "Branches & tills",
         "Your shops and the tills inside them."),
        ("register.manage", "org:devices", "cart", "ink", "Tills & phones",
         "Every till and phone that has sold here. Switch off a lost one."),
        ("product.set_price", "catalog:price_lists", "tag", "brand", "Special prices",
         "A second price for bulk buyers, attached to a customer."),
        ("settings.edit", "catalog:tiles", "layers", "brand", "Till tiles",
         "What shows on the till for goods with no barcode."),
        ("settings.edit", "catalog:taxonomy", "tag", "brand", "Units & VAT",
         "The units you sell in, your brands, and the tax rates you charge. "
         "Categories live with the products, under Inventory."),
        ("settings.edit", "notifications:message_log", "activity", "amber", "Messages",
         "What your SMS say, and every message that has been sent."),
        ("settings.edit", "org:business", "settings", "amber", "Business",
         "Your name and TIN, what prints on a receipt, and how the system behaves."),
        ("billing.manage", "tenancy:billing", "credit-card", "amber", "Subscription",
         "Your plan, what you are using, and your invoices."),
    ]
    sections = [
        {"url": reverse(route), "icon": icon, "tone": tone, "label": label, "text": text}
        for code, route, icon, tone, label, text in everything if can(code)
    ]
    return render(request, "org/settings_home.html", {"sections": sections})


@login_required
@requires("settings.edit")
def business(request):
    row = _settings_row(request.tenant)

    profile = TenantProfileForm(
        request.POST or None, request.FILES or None, instance=request.tenant
    )
    settings_form = BusinessForm(request.POST or None, instance=row)

    if request.method == "POST" and profile.is_valid() and settings_form.is_valid():
        before = audit.snapshot(row)
        profile.save()
        settings_form.save()
        audit.record(
            "settings.changed",
            obj=row,
            before=before,
            after=audit.snapshot(row),
            ip=audit.client_ip(request),
        )
        messages.success(request, "Settings saved.")
        return redirect("org:business")

    # Two shapes of field: the ones with a box to fill in, and the ones that
    # are a yes/no. The template lays each group out; the words are the
    # form's.
    boxes = ["cost_method", "default_tax_rate", "expiry_warning_days", "fiscal_provider"]
    switches = ["negative_stock_allowed",
                "low_stock_alerts", "show_tin_on_receipt"]
    return render(
        request,
        "org/business.html",
        {"profile": profile, "form": settings_form, "settings_row": row,
         "plain_fields": [settings_form[name] for name in boxes],
         "switch_fields": [settings_form[name] for name in switches]},
    )


@login_required
@requires("register.manage")
def branches(request):
    return render(
        request,
        "org/branches.html",
        {
            # Only the branches this person runs; a manager of one branch
            # could rename, close or delete another's.
            "branches": request.membership.branches(include_closed=True)
            .prefetch_related("registers").order_by("name"),
            "may_branches": request.membership.can("branch.manage"),
            "covers_all": _covers_all(request),
            "form": BranchForm(),
            "register_form": RegisterForm(),
            "allowed": request.tenant.limit_for("branches"),
            "used": request.tenant.usage_of("branches"),
        },
    )


def _settings_form(request, form, *, title, action):
    from apps.core.listing import modal_or_page

    return modal_or_page(request, "org/_model_form.html",
                         {"form": form, "title": title, "action": action},
                         title=title, back=reverse("org:branches"))


@login_required
@requires("branch.manage", branch=_branch_in_url)
def branch_form(request, pk=None):
    from apps.core.listing import close_modal

    branch = get_object_or_404(Branch, pk=pk) if pk else None
    if branch is None and not _covers_all(request):
        messages.error(request, "Only someone who works across every branch can open a new one.")
        return close_modal(request, reverse("org:branches"))
    form = BranchForm(request.POST or None, instance=branch)

    if request.method == "POST" and form.is_valid():
        try:
            saved = form.save()
        except LimitExceeded as exc:
            # Shown in the form: it used to close the page and say it on another.
            form.add_error(None, str(exc))
        else:
            if saved.is_default:
                # Exactly one default, or people land in the wrong shop.
                Branch.objects.exclude(pk=saved.pk).update(is_default=False)
            audit.record("branch.saved", obj=saved, ip=audit.client_ip(request))
            messages.success(request, f"{saved.name} saved.")
            return close_modal(request, reverse("org:branches"))

    return _settings_form(
        request, form, title=branch.name if branch else "New branch",
        action=reverse("org:branch_edit", args=[branch.pk]) if branch
        else reverse("org:branch_create"),
    )


@login_required
@requires("register.manage", branch=branch_of(Register))
def register_edit(request, pk):
    from apps.core.listing import close_modal

    register = get_object_or_404(Register, pk=pk)
    form = RegisterForm(request.POST or None, instance=register,
                        branches=request.membership.branches(include_closed=True))
    if request.method == "POST" and form.is_valid():
        before = audit.snapshot(register)
        form.save()
        audit.record("register.saved", obj=register, before=before,
                     after=audit.snapshot(register), ip=audit.client_ip(request))
        messages.success(request, f"{register.name} saved.")
        return close_modal(request, reverse("org:branches"))
    return _settings_form(request, form, title=f"Till {register.name}",
                          action=reverse("org:register_edit", args=[pk]))


@login_required
@requires("register.manage", branch=branch_of(Register))
@require_POST
def register_delete(request, pk):
    """
    Gone if it never sold anything, switched off if it did.

    A till with shifts behind it holds the cash history for those shifts.
    """
    register = get_object_or_404(Register, pk=pk)
    name = register.name
    outcome = remove_or_archive(register, label=f"Till {name}")

    if not outcome.blocked:
        audit.record("register.removed", obj=register, ip=audit.client_ip(request))
    if outcome.archived:
        messages.warning(request, outcome.message)
    elif outcome.blocked:
        messages.error(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("org:branches")


@login_required
@requires("branch.manage", branch=_branch_in_url)
@require_POST
def branch_delete(request, pk):
    """
    A branch with stock or sales is closed rather than deleted.

    Deleting it would orphan every movement recorded there.
    """
    branch = get_object_or_404(Branch, pk=pk)
    name = branch.name

    blockers = (
        (
            lambda: branch.is_default and Branch.objects.filter(is_active=True).count() > 1,
            "Make another branch the default first.",
        ),
        (
            lambda: Branch.objects.filter(is_active=True).count() == 1,
            "This is your only branch. A shop needs at least one.",
        ),
    )
    outcome = remove_or_archive(branch, label=name, blockers=blockers)

    if not outcome.blocked:
        audit.record("branch.removed", obj=branch, ip=audit.client_ip(request))
    if outcome.blocked:
        messages.error(request, outcome.message)
    elif outcome.archived:
        messages.warning(request, outcome.message)
    else:
        messages.success(request, outcome.message)
    return redirect("org:branches")


@login_required
@requires("register.manage")
def register_create(request):
    from apps.core.listing import close_modal
    from apps.core.parsing import int_or

    form = RegisterForm(request.POST or None,
                        initial={"branch": int_or(request.GET.get("branch")) or None},
                        branches=request.membership.branches())
    if request.method == "POST" and form.is_valid():
        register = form.save()
        audit.record("register.created", obj=register, ip=audit.client_ip(request))
        messages.success(request, f"{register.name} added to {register.branch.name}.")
        return close_modal(request, reverse("org:branches"))
    return _settings_form(request, form, title="New till", action=reverse("org:register_create"))


# --------------------------------------------------------------------------
# Tills and phones
# --------------------------------------------------------------------------

@login_required
@requires("register.manage")
def devices(request):
    """
    The shop's own tills and phones, and a way to switch off a lost one.

    Only the platform could see these before -- an owner with a stolen phone
    had no way to stop it taking payment.
    """
    from datetime import timedelta
    from urllib.parse import urlencode

    from django.core.paginator import Paginator
    from django.db.models import Q
    from django.utils import timezone

    from apps.org.models import Device

    mine = request.membership.branches(include_closed=True)
    rows = (
        Device.objects.select_related("branch")
        .filter(branch__in=mine, hidden=False)
        .order_by("-is_active", "branch__name", "label")
    )
    stale_before = timezone.now() - timedelta(hours=24)

    # A busy shop registers a device for every till and every phone that has
    # ever sold, and the page listed every one of them with no way to find
    # anything. The counts below are over all of them, not the page shown.
    counts = {
        "all": rows.count(),
        "silent": rows.filter(is_active=True).filter(
            Q(last_sync_at__lt=stale_before) | Q(last_sync_at__isnull=True)).count(),
        "off": rows.filter(is_active=False).count(),
    }

    term = request.GET.get("q", "").strip()[:60]
    view = request.GET.get("view", "")
    if term:
        rows = rows.filter(Q(label__icontains=term) | Q(branch__name__icontains=term))
    if view == "silent":
        rows = rows.filter(is_active=True).filter(
            Q(last_sync_at__lt=stale_before) | Q(last_sync_at__isnull=True))
    elif view == "off":
        rows = rows.filter(is_active=False)

    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return render(request, "org/devices.html", {
        "page": page, "devices": page.object_list, "counts": counts,
        "q": term, "view": view, "stale_before": stale_before,
        "keep": urlencode({k: v for k, v in (("q", term), ("view", view)) if v}),
    })


@login_required
@requires("register.manage")
@require_POST
def device_update(request, pk):
    from apps.org.models import Device

    device = get_object_or_404(Device, pk=pk,
                               branch__in=request.membership.branches(include_closed=True))
    action = request.POST.get("action")
    if action == "rename":
        device.label = request.POST.get("label", "").strip()[:60]
        device.save(update_fields=["label", "updated_at"])
        messages.success(request, f"Renamed to {device}.")
    elif action == "toggle":
        device.is_active = not device.is_active
        device.save(update_fields=["is_active", "updated_at"])
        messages.success(request, f"{device} {'is back in use' if device.is_active else 'is switched off: it can no longer send sales'}.")
    elif action == "forget" and not device.queued:
        # Forgetting also switches it off, and keeps the record that blocks it.
        device.is_active = False
        device.hidden = True
        device.save(update_fields=["is_active", "hidden", "updated_at"])
        messages.success(request, f"{device} forgotten. It stays blocked if it is ever used again.")
        audit.record("device.forgotten", obj=device, ip=audit.client_ip(request))
        return redirect("org:devices")
    else:
        messages.error(request, "Nothing changed.")
        return redirect("org:devices")
    audit.record(f"device.{action}", obj=device, ip=audit.client_ip(request))
    return redirect("org:devices")

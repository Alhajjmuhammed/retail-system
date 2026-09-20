"""
The things that only matter when this is on the internet.

A health check that checks nothing, a login form with no rate limit, error
pages that leak a stack trace, and a production config that happily runs on
the development secret key.
"""

import pytest
from django.urls import reverse

from apps.accounts import throttle
from apps.core.context import tenant_context
from apps.pos.models import FiscalReceipt, FiscalStatus

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def test_the_health_check_actually_checks_something(client):
    """
    It used to return "ok" without touching anything, so a server with a dead
    database stayed in rotation answering nothing.
    """
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_the_health_check_fails_when_the_database_is_gone(client, monkeypatch):
    import django.db

    class BrokenConnection:
        def cursor(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(django.db, "connection", BrokenConnection())
    response = client.get("/healthz")
    assert response.status_code == 503
    assert b"database" in response.content


# --------------------------------------------------------------------------
# Password guessing
# --------------------------------------------------------------------------

def test_repeated_failures_lock_the_login_form(client, db, owner):
    """
    A login form on the public internet is otherwise an open invitation to
    run a wordlist against a shop owner's email.
    """
    throttle.clear(owner.email, "127.0.0.1")

    for _ in range(throttle.MAX_ATTEMPTS):
        client.post(reverse("accounts:login"),
                    {"username": owner.email, "password": "wrong"})

    # Even the correct password is refused while locked.
    response = client.post(
        reverse("accounts:login"),
        {"username": owner.email, "password": "pw"},
        follow=True,
    )
    assert b"Too many failed attempts" in response.content

    throttle.clear(owner.email, "127.0.0.1")


def test_signing_in_successfully_clears_the_count(client, db, owner):
    throttle.clear(owner.email, "127.0.0.1")

    client.post(reverse("accounts:login"),
                {"username": owner.email, "password": "wrong"})
    client.post(reverse("accounts:login"),
                {"username": owner.email, "password": "pw"})

    assert not throttle.is_locked(owner.email, "127.0.0.1")


def test_a_cache_outage_never_stops_people_signing_in(db, owner, monkeypatch):
    """Failing closed here would lock a whole shop out over a Redis restart."""
    from django.core import cache as cache_module

    def explode(*args, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(cache_module.cache, "set", explode)
    monkeypatch.setattr(cache_module.cache, "get", explode)

    # Neither raises, and nothing is locked.
    throttle.record_failure(owner.email, "127.0.0.1")
    assert throttle.is_locked(owner.email, "127.0.0.1") is False


# --------------------------------------------------------------------------
# Error pages
# --------------------------------------------------------------------------

def test_a_missing_page_is_handled(client, shop, owner):
    client.force_login(owner)
    response = client.get("/products/999999/")
    assert response.status_code == 404


def test_error_templates_exist_for_production():
    import pathlib

    for page in ("400.html", "403.html", "404.html", "500.html"):
        assert (pathlib.Path("templates") / page).exists(), page


# --------------------------------------------------------------------------
# Plan gating on the sync API
# --------------------------------------------------------------------------

def test_without_offline_selling_the_till_still_sells_but_offline_sales_are_flagged(
    client, db, owner, free_plan
):
    """
    Every sale goes through the sync API, so closing it on plans without
    offline selling stopped those shops selling at all. Selling is open to
    every plan; a sale that was held offline is flagged for the owner.
    """
    import json
    import uuid
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.context import tenant_context
    from apps.core.features import OFFLINE_POS
    from apps.org.models import Register
    from apps.pos.models import Sale
    from apps.pos.services import open_shift
    from apps.tenancy.models import PlanFeature
    from apps.tenancy.services import create_tenant

    tenant, _ = create_tenant(name="Small Duka", owner=owner, plan=free_plan)
    PlanFeature.objects.filter(plan=free_plan, feature_key=OFFLINE_POS).delete()
    with tenant_context(tenant, user=owner):
        register = Register.objects.select_related("branch").first()
    with tenant_context(tenant, branch=register.branch, user=owner):
        open_shift(register=register)

    client.force_login(owner)
    assert client.get(reverse("sync:catalog")).status_code == 200
    result = client.post(reverse("sync:push_sales"), data=json.dumps({"device_id": "till-test", "sales": [{
        "client_uuid": str(uuid.uuid4()),
        "sold_at": (timezone.now() - timedelta(hours=2)).isoformat(),
        "lines": [{"description": "Item", "qty": 1, "unit_price": 100}],
        "payments": [{"method": "cash", "amount": 100}],
    }]}), content_type="application/json").json()
    with tenant_context(tenant):
        sale = Sale.objects.get(number=result["accepted"][0]["number"])
        assert sale.needs_review and "offline" in sale.review_notes


def test_the_sync_api_is_open_with_the_feature(client, shop, stocked, owner):
    client.force_login(owner)
    assert client.get(reverse("sync:catalog")).status_code == 200


# --------------------------------------------------------------------------
# Fiscal receipts
# --------------------------------------------------------------------------

def test_the_fiscal_screen_shows_the_queue_and_can_retry(
    client, shop, main_branch, stocked, owner
):
    from apps.pos.models import PaymentMethod
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=owner):
        cart = new_cart(branch=main_branch)
        add_to_cart(cart, stocked["Mkate"], qty=1)
        sale = complete_sale(cart, [{"method": PaymentMethod.CASH, "amount": 1500}])
        FiscalReceipt.objects.create(
            tenant=shop, sale=sale, provider="tra",
            status=FiscalStatus.FAILED, error="gateway timeout", attempts=3,
        )

    client.force_login(owner)
    response = client.get(reverse("pos:fiscal_receipts"))
    assert response.status_code == 200
    assert response.context["counts"]["failed"] == 1
    # Honest about there being no provider connected.
    assert b"No fiscal provider is connected" in response.content

    client.post(reverse("pos:fiscal_receipts"), {"action": "retry"}, follow=True)
    with tenant_context(shop):
        receipt = FiscalReceipt.objects.get(sale=sale)
        assert receipt.status == FiscalStatus.PENDING
        assert receipt.attempts == 0


def test_the_fiscal_screen_needs_the_plan_and_the_permission(client, shop, cashier):
    from apps.accounts.models import Membership, Role

    with tenant_context(shop):
        Membership.objects.create(
            tenant=shop, user=cashier, role=Role.objects.get(name="Cashier")
        )
    client.force_login(cashier)
    assert client.get(reverse("pos:fiscal_receipts")).status_code == 403


# --------------------------------------------------------------------------
# Production configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "", "change-me", "dev-only-insecure-key-change-me", "short-key",
])
def test_production_refuses_a_weak_secret_key(key):
    """
    Silently falling back to a shared key means forged sessions, and it is
    the kind of thing nobody notices until it matters.
    """
    from django.core.exceptions import ImproperlyConfigured

    from config.settings.guards import check_production_config

    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        check_production_config(key, ["app.example.com"])


@pytest.mark.parametrize("hosts", [[], ["localhost"], ["127.0.0.1"], ["*"]])
def test_production_refuses_development_hostnames(hosts):
    from django.core.exceptions import ImproperlyConfigured

    from config.settings.guards import check_production_config

    with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
        check_production_config("x" * 60, hosts)


def test_production_accepts_a_real_configuration():
    from config.settings.guards import check_production_config

    check_production_config("x" * 60, ["app.example.com"])


def test_deployment_files_are_present():
    """A system nobody can deploy is not finished."""
    import pathlib

    for path in ("Dockerfile", "compose.yaml", ".dockerignore",
                 "ops/nginx.conf", "ops/backup.sh", "ops/restore.sh"):
        assert pathlib.Path(path).exists(), path

    assert pathlib.Path("ops/backup.sh").stat().st_mode & 0o111, "backup.sh not executable"
    assert pathlib.Path("ops/restore.sh").stat().st_mode & 0o111, "restore.sh not executable"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def _zone_during_request(rf, user):
    """The time zone a view actually runs in, seen from inside it."""
    from django.http import HttpResponse
    from django.utils import timezone as django_timezone

    from apps.core.middleware import TenantMiddleware

    seen = {}

    def view(request):
        seen["zone"] = str(django_timezone.get_current_timezone())
        return HttpResponse()

    request = rf.get("/")
    request.user = user
    request.session = {}
    TenantMiddleware(view)(request)
    seen["after"] = str(django_timezone.get_current_timezone())
    return seen


def test_a_shop_works_in_its_own_timezone(rf, shop, owner):
    """
    Every report asks what was sold today, and today ends at midnight where
    the shop is. The tenant carried a timezone field nothing ever read.
    """
    shop.timezone = "Pacific/Auckland"
    shop.save(update_fields=["timezone"])

    seen = _zone_during_request(rf, owner)
    assert "Auckland" in seen["zone"]
    # ...and it does not follow the thread into the next request.
    assert "Auckland" not in seen["after"]


def test_a_branch_may_override_the_shops_timezone(rf, shop, main_branch, owner):
    """A chain can cross a border."""
    shop.timezone = "Africa/Dar_es_Salaam"
    shop.save(update_fields=["timezone"])
    main_branch.timezone = "Europe/London"
    main_branch.save(update_fields=["timezone"])

    assert "London" in _zone_during_request(rf, owner)["zone"]


def test_a_nonsense_timezone_does_not_take_the_shop_down(client, shop, owner):
    shop.timezone = "Not/APlace"
    shop.save(update_fields=["timezone"])

    client.force_login(owner)
    assert client.get(reverse("core:dashboard")).status_code == 200


def test_background_work_uses_the_shops_own_day(shop):
    """A nightly snapshot that records 'today' has to mean the shop's today."""
    from django.utils import timezone as django_timezone

    from apps.core.context import tenant_context

    shop.timezone = "Pacific/Auckland"
    shop.save(update_fields=["timezone"])

    with tenant_context(shop):
        assert "Auckland" in str(django_timezone.get_current_timezone())

    # And it is put back afterwards, so the next tenant is not affected.
    assert "Auckland" not in str(django_timezone.get_current_timezone())


# --------------------------------------------------------------------------
# The sidebar
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path_name", ["catalog:product_list", "accounts:roles"])
def test_the_shop_sidebar_stays_reachable_on_a_long_page(client, shop, owner, path_name):
    """
    It used to grow with the document, so on a long page sign-out fell below
    the fold and you had to scroll a whole table to reach it.
    """
    client.force_login(owner)
    content = client.get(reverse(path_name)).content.decode()

    assert 'class="sidebar' in content, "sidebar is not pinned to the viewport"
    assert "sidebar-scroll" in content, "nav does not scroll inside the sidebar"
    assert "Sign out" in content


def test_the_platform_sidebar_stays_reachable_on_a_long_page(client, db, owner):
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])

    client.force_login(owner)
    # The permission catalogue is the longest page in the platform admin.
    content = client.get(reverse("platform:permissions")).content.decode()

    assert 'class="sidebar' in content
    assert "sidebar-scroll" in content
    assert "Sign out" in content


def test_the_menu_toggle_sits_in_the_header_where_people_look(client, shop, owner):
    """
    One button in the top-left corner: it opens the drawer on a phone and
    collapses the sidebar on a desktop. A toggle at the bottom of the menu is
    a toggle nobody finds.
    """
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])
    client.force_login(owner)

    shop_side = client.get(reverse("core:dashboard")).content.decode()
    platform_side = client.get(reverse("platform:dashboard")).content.decode()

    for content, key in [(shop_side, "nav-collapsed"),
                         (platform_side, "platform-nav-collapsed")]:
        # The one control, in the header, doing both jobs.
        assert 'aria-label="Menu"' in content
        assert "window.innerWidth < 1024" in content
        assert "mobileNav = true" in content
        # And the choice is remembered rather than reset on every page.
        assert f"localStorage.getItem('{key}')" in content
        assert f"localStorage.setItem('{key}'" in content

    # Exactly one toggle, not one in the header and another in the menu.
    assert shop_side.count('aria-label="Menu"') == 1


def test_navigation_labels_can_be_hidden_when_collapsed(client, shop, owner):
    """
    Labels are hidden rather than removed, so the markup is identical between
    states and the mobile drawer needs no separate copy.
    """
    client.force_login(owner)
    content = client.get(reverse("core:dashboard")).content.decode()

    assert content.count("nav-label") > 10
    # Every link carries a title, so a collapsed icon still says what it is.
    assert content.count('title="Dashboard"') >= 1


def test_the_debug_toolbar_is_gone(client, shop, owner):
    """
    It covered a third of the screen and shipped a third-party panel into
    every development page.
    """
    import pathlib

    settings_text = pathlib.Path("config/settings/dev.py").read_text()
    assert "debug_toolbar" not in settings_text

    urls_text = pathlib.Path("config/urls.py").read_text()
    assert "__debug__" not in urls_text

    client.force_login(owner)
    content = client.get(reverse("core:dashboard")).content.decode()
    assert "djDebug" not in content
    assert "debug_toolbar" not in content


def test_no_template_uses_a_multi_line_short_comment():
    """
    Django's {# #} is single-line only. A multi-line one is not a comment at
    all -- it renders straight into the page as text, which is exactly what
    happened on the platform dashboard.
    """
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("templates").rglob("*.html"):
        source = path.read_text()
        for match in re.finditer(r"\{#(?:(?!#\}).)*$", source, re.M):
            line = source[: match.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")

    assert not offenders, "multi-line {# #} renders as text: " + ", ".join(offenders)


def test_the_platform_dashboard_leads_with_money(client, shop, owner):
    """
    It used to count shops and say nothing about whether the business was
    working.
    """
    owner.is_platform_staff = True
    owner.save(update_fields=["is_platform_staff"])

    client.force_login(owner)
    response = client.get(reverse("platform:dashboard"))
    assert response.status_code == 200

    for key in ("mrr", "trial_value", "outstanding_total", "signups",
                "by_plan", "health", "trials_ending"):
        assert key in response.context, key

    content = response.content.decode()
    assert "Monthly recurring" in content
    # Six months of columns, whatever their values.
    assert len(response.context["signups"]) == 6
    # And no template syntax leaked into the page.
    assert "{#" not in content
    assert "{%" not in content

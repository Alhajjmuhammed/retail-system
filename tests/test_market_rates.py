"""
Market exchange rates, a document moved into another currency while it is
still a draft, and amounts shown in other currencies for comparison.

The rates service is never called from here: ``fetch_market_table`` is
replaced with a fixed table, so the tests are the same tomorrow as today.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core import currency
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

# Dollar-based, as the service answers: 1 USD buys this many of each.
TABLE = {"USD": Decimal("1"), "TZS": Decimal("2500"), "EUR": Decimal("0.8"),
         "KES": Decimal("125")}


@pytest.fixture
def market(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return dict(TABLE)

    monkeypatch.setattr(currency, "fetch_market_table", fake)
    return calls


@pytest.fixture
def offline(monkeypatch):
    def broken():
        raise ValueError("The market rates could not be fetched just now. "
                         "Try again later, or type the rate yourself.")

    monkeypatch.setattr(currency, "fetch_market_table", broken)


def _rate(shop, code):
    from apps.org.models import ExchangeRate

    with tenant_context(shop):
        return ExchangeRate.objects.get(code=code)


# -- the arithmetic ----------------------------------------------------------

def test_a_market_rate_crosses_through_the_dollar():
    assert currency.market_rate("USD", "TZS", TABLE) == Decimal("2500")
    assert currency.market_rate("EUR", "TZS", TABLE) == Decimal("3125")     # 2500 / 0.8
    assert currency.market_rate("KES", "TZS", TABLE) == Decimal("20")
    with pytest.raises(ValueError, match="does not carry"):
        currency.market_rate("XYZ", "TZS", TABLE)


# -- settings ----------------------------------------------------------------

def test_adding_with_no_rate_follows_the_market(client, shop, owner, market):
    client.force_login(owner)
    client.post(reverse("org:currencies"), {"action": "save_rate", "code": "USD", "rate": ""})
    row = _rate(shop, "USD")
    assert row.rate == Decimal("2500")
    assert row.source == "market"
    assert row.fetched_at is not None


def test_a_typed_rate_is_yours_and_the_morning_leaves_it(client, shop, owner, market):
    client.force_login(owner)
    url = reverse("org:currencies")
    client.post(url, {"action": "save_rate", "code": "USD", "rate": ""})
    client.post(url, {"action": "save_rate", "code": "EUR", "rate": "3000"})
    assert _rate(shop, "EUR").source == "manual"
    TABLE_MOVED = {**TABLE, "TZS": Decimal("2600")}
    with tenant_context(shop):
        currency.refresh_market_rates(shop, TABLE_MOVED)
    assert _rate(shop, "USD").rate == Decimal("2600")      # market: followed
    assert _rate(shop, "EUR").rate == Decimal("3000")      # yours: left alone


def test_switching_back_to_the_market(client, shop, owner, market):
    client.force_login(owner)
    url = reverse("org:currencies")
    client.post(url, {"action": "save_rate", "code": "EUR", "rate": "3000"})
    client.post(url, {"action": "use_market", "code": "EUR"})
    row = _rate(shop, "EUR")
    assert row.source == "market" and row.rate == Decimal("3125")


def test_the_refresh_button(client, shop, owner, market):
    from apps.org.models import ExchangeRate

    with tenant_context(shop):
        ExchangeRate.objects.create(code="USD", rate=Decimal("1"), source="market")
    client.force_login(owner)
    page = client.post(reverse("org:currencies"), {"action": "refresh"}, follow=True)
    assert "brought up to date" in page.content.decode()
    assert _rate(shop, "USD").rate == Decimal("2500")


def test_when_the_service_is_down_the_shop_is_told(client, shop, owner, offline):
    from apps.org.models import ExchangeRate

    client.force_login(owner)
    page = client.post(reverse("org:currencies"),
                       {"action": "save_rate", "code": "USD", "rate": ""}, follow=True)
    assert "could not be fetched" in page.content.decode()
    with tenant_context(shop):
        assert not ExchangeRate.objects.exists()


def test_the_page_shows_where_each_rate_comes_from(client, shop, owner, market):
    client.force_login(owner)
    url = reverse("org:currencies")
    client.post(url, {"action": "save_rate", "code": "USD", "rate": ""})
    client.post(url, {"action": "save_rate", "code": "EUR", "rate": "3000"})
    html = client.get(url).content.decode()
    assert "Market" in html and "Yours" in html
    assert "Get today&#x27;s market rates now" in html or "Get today's market rates now" in html
    assert "Use the market rate" in html


# -- the morning job ---------------------------------------------------------

def test_the_morning_job_asks_once_for_every_shop(shop, owner, business_plan, market):
    from apps.accounts.models import User
    from apps.org.models import ExchangeRate
    from apps.org.tasks import refresh_exchange_rates
    from apps.tenancy.services import create_tenant

    other_owner = User.objects.create_user("pili@shop.test", "pw", name="Pili")
    other, _ = create_tenant(name="Duka la Pili", owner=other_owner, plan=business_plan)
    for tenant in (shop, other):
        with tenant_context(tenant):
            ExchangeRate.objects.create(code="USD", rate=Decimal("1"), source="market")
            ExchangeRate.objects.create(code="EUR", rate=Decimal("9"), source="manual")

    assert refresh_exchange_rates() == 2
    assert len(market) == 1                      # one request, both shops
    for tenant in (shop, other):
        assert _rate(tenant, "USD").rate == Decimal("2500")
        assert _rate(tenant, "EUR").rate == Decimal("9")


def test_the_morning_job_does_nothing_without_market_rates(shop, market):
    from apps.org.tasks import refresh_exchange_rates

    assert refresh_exchange_rates() == 0
    assert market == []                          # did not even ask


# -- a draft into another currency -------------------------------------------

@pytest.fixture
def usd(shop):
    from apps.org.models import ExchangeRate

    with tenant_context(shop):
        return ExchangeRate.objects.create(code="USD", rate=Decimal("2500"), source="manual")


@pytest.fixture
def proforma(shop, main_branch):
    from apps.customers.models import Customer
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        inv = Invoice.objects.create(
            reference="PRO260001", customer=Customer.objects.create(name="Hoteli"),
            branch=main_branch, issued_on=today, due_on=today + timedelta(days=14),
            currency="TZS")
        inv.lines.create(description="Mincer", qty=Decimal("2"), unit_price=Decimal("5000"),
                         position=1)
        return inv


def test_a_draft_moves_into_dollars_and_back(client, shop, main_branch, owner, usd, proforma):
    client.force_login(owner)
    url = reverse("selling:invoice_detail", args=[proforma.pk])
    html = client.get(url).content.decode()
    assert "change to" in html
    client.post(url, {"action": "currency", "currency": "USD"})
    with tenant_context(shop):
        proforma.refresh_from_db()
        assert proforma.currency == "USD" and proforma.exchange_rate == Decimal("2500")
        assert proforma.lines.get().unit_price == Decimal("2.00")
        assert proforma.total == Decimal("4.00")
        assert proforma.total_main == Decimal("10000")
    client.post(url, {"action": "currency", "currency": "TZS"})
    with tenant_context(shop):
        proforma.refresh_from_db()
        assert proforma.currency == "TZS"
        assert proforma.lines.get().unit_price == Decimal("5000")


def test_an_issued_invoice_keeps_its_currency(shop, main_branch, owner, usd, proforma):
    from apps.selling import services

    with tenant_context(shop, branch=main_branch, user=owner):
        services.issue(proforma)
        proforma.refresh_from_db()
        with pytest.raises(ValueError, match="has gone out"):
            services.change_currency(proforma, "USD")


def test_no_switch_for_a_shop_with_one_currency(client, shop, owner, proforma):
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_detail", args=[proforma.pk])).content.decode()
    assert "change to" not in html


# -- comparing ---------------------------------------------------------------

def test_a_document_shows_its_total_in_the_other_currencies(client, shop, owner, usd, proforma):
    from apps.org.models import ExchangeRate

    with tenant_context(shop):
        ExchangeRate.objects.create(code="EUR", rate=Decimal("3125"), source="manual")
    client.force_login(owner)
    html = client.get(reverse("selling:invoice_detail", args=[proforma.pk])).content.decode()
    # TZS 10,000 = USD 4 = EUR 3.20 at today's rates.
    assert "Today: EUR 3.20 &middot; USD 4" in html


def test_the_product_list_shows_prices_in_another_currency(client, shop, owner, usd, stocked):
    client.force_login(owner)
    html = client.get(reverse("catalog:product_list"), {"show": "USD"}).content.decode()
    assert "Prices in USD" in html
    assert "1.2" in html          # Sukari, TZS 3,000
    assert "0.6" in html          # Mkate, TZS 1,500


def test_the_product_page_shows_each_price_in_the_other_currencies(client, shop, owner, usd,
                                                                   stocked):
    client.force_login(owner)
    product = stocked["Mkate"].product
    html = client.get(reverse("catalog:product_detail", args=[product.pk])).content.decode()
    assert "USD 0.6" in html

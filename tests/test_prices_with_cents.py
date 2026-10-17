"""
A price can have cents.

A shop that buys a mincer at 2,170.60 typed it into a quotation and the
browser refused it: "the two nearest valid values are 2170 and 2171". The
price boxes stepped in whole shillings, and a step is also what the browser
accepts. Prices are kept to the cent by design (pos.services.price); only an
amount charged rounds to the smallest coin.
"""

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
PRICE_FIELDS = re.compile(r'<input[^>]*name="(unit_price|unit_cost|price|cost)"[^>]*>')


def test_no_price_box_refuses_cents():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        for match in PRICE_FIELDS.finditer(path.read_text(encoding="utf-8")):
            if 'step="1"' in match.group(0):
                offenders.append(f"{path.relative_to(TEMPLATES)}: {match.group(1)}")
    assert not offenders, offenders


def test_the_product_form_takes_a_price_with_cents(client, shop, owner):
    client.force_login(owner)
    page = client.get(reverse("catalog:product_create")).content.decode()
    price_box = re.search(r'<input[^>]*name="price"[^>]*>', page).group(0)
    cost_box = re.search(r'<input[^>]*name="cost"[^>]*>', page).group(0)
    assert 'step="any"' in price_box
    assert 'step="any"' in cost_box


@pytest.fixture
def buyer(shop):
    from apps.customers.models import Customer

    with tenant_context(shop):
        return Customer.objects.create(name="Hoteli ya Baharini")


def test_a_quotation_line_keeps_its_cents(client, shop, main_branch, owner, buyer):
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14))

    client.force_login(owner)
    client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "description": "Mincer", "qty": "2", "unit_price": "2170.60",
    })
    with tenant_context(shop, branch=main_branch):
        line = quotation.lines.get()
        assert line.unit_price == Decimal("2170.60")


def test_an_invoice_line_keeps_its_cents(client, shop, main_branch, owner, buyer):
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        invoice = Invoice.objects.create(
            reference="PRO260001", customer=buyer, branch=main_branch,
            issued_on=today, due_on=today + timedelta(days=14))

    client.force_login(owner)
    client.post(reverse("selling:invoice_detail", args=[invoice.pk]), {
        "action": "add_line", "description": "Mincer", "qty": "2", "unit_price": "2170.60",
    })
    with tenant_context(shop, branch=main_branch):
        assert invoice.lines.get().unit_price == Decimal("2170.60")


def test_more_than_two_decimals_is_refused_not_rounded(client, shop, main_branch, owner, buyer):
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14))

    client.force_login(owner)
    page = client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "add_line", "description": "Mincer", "qty": "2", "unit_price": "2170.605",
    }, follow=True)
    assert "at most 2 decimal places" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not quotation.lines.exists()


def test_a_price_shows_as_it_was_set_not_rounded(client, shop, owner, stocked):
    """6.80 was shown as 7 on the product list; it is shown as set."""
    from apps.catalog.models import Price

    with tenant_context(shop):
        Price.objects.filter(variant=stocked["Mkate"]).update(amount=Decimal("6.80"))
    client.force_login(owner)
    html = client.get(reverse("catalog:product_list")).content.decode()
    assert "6.80" in html
    # A whole price stays without noise: 3,000 not 3,000.00.
    assert "3,000" in html and "3,000.00" not in html


def test_the_till_and_phone_have_a_price_formatter():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for js, page in [("static/js/till.js", "templates/pos/till.html"),
                     ("static/js/phone.js", "templates/pos/phone.html")]:
        assert "price(value)" in (root / js).read_text(encoding="utf-8")
        assert 'x-text="price(' in (root / page).read_text(encoding="utf-8")

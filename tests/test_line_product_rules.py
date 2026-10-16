"""
A product on a quotation or invoice keeps the rules it has at the POS, the
same product twice is one line, and its details are a tap away.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.selling import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def quotation(shop, main_branch):
    from apps.customers.models import Customer
    from apps.selling.models import Quotation

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        return Quotation.objects.create(
            reference="QUO260001", customer=Customer.objects.create(name="Hoteli"),
            branch=main_branch, issued_on=today, valid_until=today + timedelta(days=14))


@pytest.fixture
def proforma(shop, main_branch):
    from apps.customers.models import Customer
    from apps.selling.models import Invoice

    today = timezone.localdate()
    with tenant_context(shop, branch=main_branch):
        return Invoice.objects.create(
            reference="PRO260001", customer=Customer.objects.create(name="Hoteli"),
            branch=main_branch, issued_on=today, due_on=today + timedelta(days=14))


@pytest.fixture
def biskuti(shop, stocked):
    """Mkate at 1,500 a loaf, never sold below 1,400."""
    with tenant_context(shop):
        product = stocked["Mkate"].product
        product.min_price = Decimal("1400")
        product.save()
    return stocked["Mkate"]


def _add(client, doc, url, **data):
    return client.post(reverse(url, args=[doc.pk]), {"action": "add_line", **data}, follow=True)


DOCS = [("selling:quotation_detail", "quotation"), ("selling:invoice_detail", "proforma")]


# -- never sell below --------------------------------------------------------

@pytest.mark.parametrize("url,name", DOCS)
def test_a_price_under_the_floor_is_refused(client, request, shop, main_branch, owner,
                                            biskuti, url, name):
    doc = request.getfixturevalue(name)
    client.force_login(owner)
    page = _add(client, doc, url, variant=biskuti.pk, qty="5", unit_price="1300")
    assert "never sold below 1,400.00" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        assert not doc.lines.exists()


def test_a_discount_cannot_walk_the_price_under_the_floor(
        client, shop, main_branch, owner, biskuti, quotation):
    client.force_login(owner)
    # 1,500 less 10% is 1,350: under 1,400.
    page = _add(client, quotation, "selling:quotation_detail", variant=biskuti.pk,
                qty="1", unit_price="1500", discount_percent="10")
    assert "This comes to 1,350.00 each" in page.content.decode()


def test_the_whole_document_discount_counts_against_the_floor(
        client, shop, main_branch, owner, biskuti, quotation):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", variant=biskuti.pk,
         qty="1", unit_price="1500")
    page = client.post(reverse("selling:quotation_detail", args=[quotation.pk]), {
        "action": "header", "discount_percent": "10",
        "valid_until": (timezone.localdate() + timedelta(days=14)).isoformat(),
    }, follow=True)
    assert "never sold below" in page.content.decode()
    with tenant_context(shop):
        quotation.refresh_from_db()
        assert quotation.discount_percent == 0


def test_at_or_above_the_floor_is_fine(client, shop, main_branch, owner, biskuti, quotation):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", variant=biskuti.pk,
         qty="1", unit_price="1400")
    with tenant_context(shop):
        assert quotation.lines.get().unit_price == Decimal("1400")


def test_an_old_line_under_the_floor_cannot_be_issued(shop, main_branch, owner, biskuti,
                                                      proforma):
    with tenant_context(shop, branch=main_branch, user=owner):
        proforma.lines.create(variant=biskuti, description="Mkate", qty=Decimal("1"),
                              unit_price=Decimal("900"), position=10)
        with pytest.raises(ValueError, match="never sold below"):
            services.issue(proforma)


# -- can be discounted -------------------------------------------------------

def test_a_product_that_takes_no_discount(client, shop, main_branch, owner, stocked, quotation):
    sukari = stocked["Sukari 1kg"]
    with tenant_context(shop):
        product = sukari.product
        product.discount_allowed = False
        product.save()
    client.force_login(owner)
    page = _add(client, quotation, "selling:quotation_detail", variant=sukari.pk,
                qty="1", unit_price="3000", discount_percent="5")
    assert "cannot be discounted" in page.content.decode()
    # Without a discount it goes in.
    _add(client, quotation, "selling:quotation_detail", variant=sukari.pk,
         qty="1", unit_price="3000")
    with tenant_context(shop):
        assert quotation.lines.count() == 1


# -- the same product twice --------------------------------------------------

@pytest.mark.parametrize("url,name", DOCS)
def test_the_same_product_again_adds_to_its_line(client, request, shop, main_branch, owner,
                                                 stocked, url, name):
    doc = request.getfixturevalue(name)
    client.force_login(owner)
    _add(client, doc, url, variant=stocked["Mkate"].pk, qty="5", unit_price="1500")
    page = _add(client, doc, url, variant=stocked["Mkate"].pk, qty="2", unit_price="1500")
    assert "now 7" in page.content.decode()
    with tenant_context(shop, branch=main_branch):
        line = doc.lines.get()
        assert line.qty == Decimal("7")
        assert line.line_total == Decimal("10500")


def test_a_different_price_is_a_different_line(client, shop, main_branch, owner, stocked,
                                               quotation):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", variant=stocked["Mkate"].pk,
         qty="5", unit_price="1500")
    _add(client, quotation, "selling:quotation_detail", variant=stocked["Mkate"].pk,
         qty="2", unit_price="1600")
    with tenant_context(shop):
        assert quotation.lines.count() == 2


def test_typed_lines_are_never_merged(client, shop, owner, quotation):
    client.force_login(owner)
    for _ in range(2):
        _add(client, quotation, "selling:quotation_detail",
             description="Delivery", qty="1", unit_price="5000")
    with tenant_context(shop):
        assert quotation.lines.count() == 2


# -- product details ---------------------------------------------------------

def test_a_product_line_shows_its_details_and_a_link(client, shop, main_branch, owner,
                                                     biskuti, quotation):
    client.force_login(owner)
    _add(client, quotation, "selling:quotation_detail", variant=biskuti.pk,
         qty="5", unit_price="1500")
    html = client.get(reverse("selling:quotation_detail", args=[quotation.pk])).content.decode()
    assert "Product details" in html
    assert "Never below" in html
    assert "In stock here" in html
    assert reverse("catalog:product_edit", args=[biskuti.product.pk]) in html


def test_no_link_for_someone_who_cannot_open_products(client, shop, main_branch, biskuti,
                                                      proforma):
    from apps.accounts.models import Membership, Permission, Role, RolePermission, User

    with tenant_context(shop, branch=main_branch):
        proforma.lines.create(variant=biskuti, description="Mkate", qty=Decimal("1"),
                              unit_price=Decimal("1500"), position=10)
        role = Role.objects.create(tenant=shop, name="Viewer")
        RolePermission.objects.create(role=role,
                                      permission=Permission.objects.get(code="invoice.view"))
        person = User.objects.create_user("viewer@shop.test", "pw", name="Viewer")
        Membership.objects.create(tenant=shop, user=person, role=role)
    client.force_login(person)
    html = client.get(reverse("selling:invoice_detail", args=[proforma.pk])).content.decode()
    assert "Product details" in html
    assert reverse("catalog:product_edit", args=[biskuti.product.pk]) not in html

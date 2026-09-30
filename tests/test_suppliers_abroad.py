"""
Suppliers you cannot ring, and knowing which goods come from whom.

A factory in Guangzhou is a different animal from a wholesaler in Stone
Town: its tax number is not a TIN, it bills in dollars, and the bank will
not send it money without a SWIFT code. All of that lived in somebody's
WhatsApp.
"""

import pytest
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


ABROAD = {
    "name": "Guangzhou Paper Co. Ltd",
    "contact_name": "Li Wei",
    "phone": "+86 20 1234 5678",
    "email": "sales@gzpaper.cn",
    "city": "Guangzhou",
    "country": "China",
    "website": "www.gzpaper.cn",
    "currency": "usd",
    "vat_number": "91440101MA5XYZ",
    "bank_name": "Bank of China",
    "bank_account": "6217 0000 1111 2222",
    "swift_code": "bkchcnbj",
    "iban": "cn12 3456 7890",
    "payment_terms_days": "45",
    "address": "88 Baiyun Road",
}


def test_a_supplier_abroad_can_be_written_down(client, shop, main_branch, owner):
    from apps.purchasing.models import Supplier

    client.force_login(owner)
    client.post(reverse("purchasing:supplier_create"), ABROAD, follow=True)

    with tenant_context(shop):
        supplier = Supplier.objects.get(name="Guangzhou Paper Co. Ltd")
        assert supplier.country == "China"
        assert supplier.city == "Guangzhou"
        assert supplier.website == "www.gzpaper.cn"
        assert supplier.vat_number == "91440101MA5XYZ"
        assert supplier.payment_terms_days == 45
        # Codes are shouted in upper case on every bank form there is.
        assert supplier.currency == "USD"
        assert supplier.swift_code == "BKCHCNBJ"
        assert supplier.iban == "CN12 3456 7890"


def test_the_form_asks_for_what_a_foreign_supplier_needs(client, shop, owner):
    client.force_login(owner)
    body = client.get(reverse("purchasing:supplier_create")).content.decode()
    for field in ("country", "city", "website", "currency", "vat_number",
                  "bank_name", "bank_account", "swift_code", "iban"):
        assert f'name="{field}"' in body, field


def test_a_local_supplier_needs_none_of_it(client, shop, owner):
    """A wholesaler down the road is still a name and a phone number."""
    from apps.purchasing.models import Supplier

    client.force_login(owner)
    client.post(reverse("purchasing:supplier_create"),
                {"name": "Mzee Juma Wholesale", "phone": "0777123456",
                 "payment_terms_days": "0"}, follow=True)
    with tenant_context(shop):
        supplier = Supplier.objects.get(name="Mzee Juma Wholesale")
        assert supplier.country == ""
        assert supplier.swift_code == ""


def test_the_supplier_page_lists_what_you_buy_from_them(
        client, shop, main_branch, owner, stocked):
    from apps.purchasing.models import Supplier

    with tenant_context(shop):
        supplier = Supplier.objects.create(name="Azam Ltd")
        product = stocked["Mkate"].product
        product.supplier = supplier
        product.sku = "BR-001"
        product.save(update_fields=["supplier", "sku"])

    client.force_login(owner)
    body = client.get(
        reverse("purchasing:supplier_detail", args=[supplier.pk])).content.decode()
    assert "What you buy from them" in body
    assert product.name in body
    assert "BR-001" in body


def test_a_supplier_with_no_products_says_so(client, shop, main_branch, owner):
    from apps.purchasing.models import Supplier

    with tenant_context(shop):
        supplier = Supplier.objects.create(name="Nobody's Supplier")

    client.force_login(owner)
    body = client.get(
        reverse("purchasing:supplier_detail", args=[supplier.pk])).content.decode()
    assert "No products from them yet" in body


def test_everything_from_one_factory_is_one_click(
        client, shop, main_branch, owner, stocked):
    """The link existed in the data and there was no way to use it."""
    from apps.purchasing.models import Supplier

    with tenant_context(shop):
        azam = Supplier.objects.create(name="Azam Ltd")
        other = Supplier.objects.create(name="Somebody Else")
        theirs = stocked["Mkate"].product
        theirs.supplier = azam
        theirs.save(update_fields=["supplier"])
        not_theirs = stocked["Sukari 1kg"].product
        not_theirs.supplier = other
        not_theirs.save(update_fields=["supplier"])

    client.force_login(owner)
    body = client.get(reverse("catalog:product_list"),
                      {"supplier": azam.pk}).content.decode()
    assert theirs.name in body
    assert not_theirs.name not in body
    # And the page says whose goods these are, with a way back out.
    assert "Azam Ltd" in body
    assert "Show every product" in body


def test_searching_within_one_supplier_keeps_the_supplier(
        client, shop, main_branch, owner, stocked):
    from apps.purchasing.models import Supplier

    with tenant_context(shop):
        azam = Supplier.objects.create(name="Azam Ltd")
        for variant in stocked.values():
            variant.product.supplier = azam
            variant.product.save(update_fields=["supplier"])

    client.force_login(owner)
    body = client.get(reverse("catalog:product_list"),
                      {"supplier": azam.pk}).content.decode()
    assert f'name="supplier" value="{azam.pk}"' in body, \
        "typing a search would lose the supplier filter"

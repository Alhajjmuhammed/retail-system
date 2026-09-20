"""
Scanning a barcode at the till.

The till and the phone both mount an Alpine component whose ``init()`` Alpine
calls by itself. Adding ``x-init="init()"`` as well ran it twice, which bound
the scanner's keydown listener twice: every scanned character arrived doubled
("6001..." became "66000011...") and no barcode ever matched. Selling by
scanner was broken while every server-side test passed, because the tests push
sales through the sync API and never touch the keyboard.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.catalog.models import Barcode
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def _template(name):
    for root in settings.TEMPLATES[0]["DIRS"]:
        path = Path(root) / name
        if path.exists():
            return path.read_text()
    raise AssertionError(f"{name} not found")


@pytest.mark.parametrize("name", ["pos/till.html", "pos/phone.html"])
def test_the_selling_screens_are_only_started_once(name):
    body = _template(name)
    mounts = re.findall(r'x-data="(\w+)\([^"]*\)"[^>]*', body)
    assert mounts, f"{name} no longer mounts an Alpine component"
    assert 'x-init="init()"' not in body, (
        f"{name} calls init() through x-init as well as letting Alpine call it. "
        "Everything in init() then happens twice, including binding the "
        "scanner's keydown listener, which doubles every scanned character."
    )


def test_the_catalogue_carries_the_barcodes_the_till_scans(client, shop, owner, stocked):
    """The till matches scans against this feed, so each code must arrive with
    the quantity one scan stands for."""
    with tenant_context(shop):
        Barcode.objects.create(variant=stocked["Soda 500ml"], code="6002222222222")
        Barcode.objects.create(variant=stocked["Soda 500ml"], code="6002222222229",
                               pack_quantity=24, label="crate of 24")
    client.force_login(owner)
    data = client.get(reverse("sync:catalog")).json()
    soda = next(v for v in data["variants"] if v["id"] == stocked["Soda 500ml"].pk)
    codes = {b["code"]: b["qty"] for b in soda["barcodes"]}
    assert codes["6002222222222"] == "1.000"
    assert codes["6002222222229"] == "24.000"    # scanning the crate sells 24

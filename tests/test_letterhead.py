"""
The shop's own mark on the paper it hands out.

A duka that uploads its logo expects to see it on the quotation, the
invoice, the delivery note and the receipt -- not only in the corner of a
screen its customers never look at.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db

# The smallest valid PNG: one transparent pixel.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001[]".replace("[]", "0d0a2db4")
    + "0000000049454e44ae426082"
)


@pytest.fixture
def shop_with_logo(shop):
    with tenant_context(shop):
        shop.logo = SimpleUploadedFile("duka.png", PIXEL, content_type="image/png")
        shop.save(update_fields=["logo"])
    return shop


def test_no_logo_leaves_no_gap(client, shop, main_branch, owner):
    """Most dukas have none, and an empty box is worse than none at all."""
    client.force_login(owner)
    page = client.get(reverse("customers:statements"))
    assert page.status_code == 200
    assert "letterhead_mark" not in page.content.decode()


def test_the_logo_reaches_the_paperwork(client, shop_with_logo, main_branch, owner):
    from apps.customers.models import Customer
    from apps.selling.models import Quotation, QuotationStatus
    from django.utils import timezone
    from datetime import timedelta

    today = timezone.localdate()
    with tenant_context(shop_with_logo, branch=main_branch):
        buyer = Customer.objects.create(name="Hoteli ya Baharini", phone="0788112233")
        quotation = Quotation.objects.create(
            reference="QUO260001", customer=buyer, branch=main_branch,
            issued_on=today, valid_until=today + timedelta(days=14),
            status=QuotationStatus.SENT,
        )

    client.force_login(owner)
    body = client.get(
        reverse("selling:quotation_print", args=[quotation.pk])).content.decode()
    assert shop_with_logo.logo.url in body
    assert "<img" in body

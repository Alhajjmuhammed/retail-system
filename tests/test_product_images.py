"""
Product photographs.

A shopkeeper photographs a bag of sugar with their phone and uploads four
megabytes of it, four thousand pixels tall. That picture is then sent to every
till in the shop on every catalogue sync, over the connection this system
exists to survive without. So nothing is stored as it arrived.
"""

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.catalog.images import MAX_UPLOAD_BYTES, BadImage, shrink
from apps.catalog.models import Product
from apps.core.context import tenant_context

pytestmark = pytest.mark.django_db


def _photo(width=3024, height=4032, fmt="JPEG", name="IMG_4021.jpg"):
    """Something shaped like what a phone hands over."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (210, 180, 140)).save(buffer, format=fmt)
    buffer.seek(0)
    kind = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    return SimpleUploadedFile(name, buffer.read(), content_type=kind)


def test_a_phone_photograph_is_squared_off_and_shrunk():
    small = shrink(_photo())
    reopened = Image.open(small)
    assert reopened.size == (512, 512)
    assert reopened.format == "WEBP"


def test_the_stored_picture_is_small_enough_to_send_to_a_till():
    original = _photo()
    small = shrink(original)
    assert small.size < original.size
    assert small.size < 300 * 1024


def test_the_camera_metadata_does_not_come_with_it():
    """A phone photo carries where it was taken. A shop's shelf is not public."""
    buffer = BytesIO()
    image = Image.new("RGB", (800, 600), "white")
    exif = image.getexif()
    exif[0x010F] = "ACME Phones"          # Make
    exif[0x0110] = "Model X"              # Model
    exif[0x9003] = "2026:09:21 08:15:00"  # when, and so roughly where they were
    image.save(buffer, format="JPEG", exif=exif)
    buffer.seek(0)
    upload = SimpleUploadedFile("IMG.jpg", buffer.read(), content_type="image/jpeg")

    assert Image.open(BytesIO(upload.read())).getexif(), "the fixture should carry metadata"
    upload.seek(0)

    stored = Image.open(shrink(upload))
    assert not stored.getexif()


def test_something_that_is_not_a_picture_is_refused():
    upload = SimpleUploadedFile("notes.txt", b"this is not a photograph",
                                content_type="image/jpeg")
    with pytest.raises(BadImage):
        shrink(upload)


def test_an_enormous_upload_is_refused_before_it_is_opened():
    upload = SimpleUploadedFile("huge.jpg", b"x" * 16, content_type="image/jpeg")
    upload.size = MAX_UPLOAD_BYTES + 1
    with pytest.raises(BadImage) as refused:
        shrink(upload)
    assert "MB" in str(refused.value)


def test_uploading_through_the_form_stores_the_small_one(client, shop, owner, stocked):
    client.force_login(owner)
    with tenant_context(shop):
        product = Product.objects.get(name="Sukari 1kg")
    client.post(reverse("catalog:product_edit", args=[product.pk]), {
        "name": product.name, "base_unit": product.base_unit_id,
        "tax_rate": product.tax_rate_id, "is_active": "on",
        "track_stock": "on", "sellable_at_pos": "on", "discount_allowed": "on",
        "image": _photo(),
    })
    with tenant_context(shop):
        product.refresh_from_db()
        assert product.image
        assert product.image.name.endswith(".webp")
        assert (product.image.width, product.image.height) == (512, 512)


def test_the_till_is_told_where_the_picture_is(client, shop, owner, stocked):
    """The catalogue feed carries it, so an offline till can keep a copy."""
    with tenant_context(shop):
        product = Product.objects.get(name="Sukari 1kg")
        product.image = shrink(_photo())
        product.save(update_fields=["image"])
    client.force_login(owner)
    feed = client.get(reverse("sync:catalog")).json()
    sugar = next(v for v in feed["variants"] if v["name"].startswith("Sukari"))
    assert sugar["image"] and sugar["image"].startswith("/media/products/")
    bread = next(v for v in feed["variants"] if v["name"].startswith("Mkate"))
    assert bread["image"] is None      # no photograph, and that is not an error


def test_the_service_worker_keeps_the_pictures_for_offline():
    """A tile with a hole where its picture was is worse than one with a name."""
    from pathlib import Path

    from django.conf import settings

    worker = (Path(settings.BASE_DIR) / "static" / "js" / "sw.js").read_text()
    assert "/media/products/" in worker
    assert "PICTURES" in worker

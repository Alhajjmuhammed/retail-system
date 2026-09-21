"""
Product pictures, cut down to what a till can actually carry.

A shopkeeper photographs a bag of sugar with their phone and uploads four
megabytes of it. That photo is then sent to every till in the shop, on every
catalogue sync, over the connection this system is built to survive without.
So the original is never kept: it is squared off, shrunk to a size that looks
right on a tile, stripped of its camera metadata and stored as WebP.

Stripping the metadata is not only about size. A phone photo carries the GPS
position it was taken at, and a shop's pictures should not quietly publish
where the owner lives.
"""

from io import BytesIO

from django.core.files.uploadedfile import InMemoryUploadedFile

# Big enough to look sharp on a tile at twice the pixel density, small enough
# that a hundred of them are not a download.
EDGE = 512
QUALITY = 82
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
ACCEPTED = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


class BadImage(Exception):
    """The upload is not a picture we can use."""


def shrink(upload):
    """
    Return the upload as a small, square, metadata-free WebP.

    Square because tiles are a grid: letting each picture set its own shape
    makes the till look like a jumble sale. The crop takes the middle, which
    is where somebody photographing a product puts the product.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if upload.size and upload.size > MAX_UPLOAD_BYTES:
        raise BadImage(
            f"That picture is {upload.size // (1024 * 1024)} MB. "
            f"Please use one under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise BadImage("That file is not a picture we can read.") from exc

    # Phones record the orientation rather than rotating the pixels.
    image = ImageOps.exif_transpose(image)
    image = ImageOps.fit(image.convert("RGB"), (EDGE, EDGE), method=Image.LANCZOS)

    out = BytesIO()
    image.save(out, format="WEBP", quality=QUALITY, method=4)
    out.seek(0)

    name = (upload.name.rsplit(".", 1)[0] or "product")[:40] + ".webp"
    return InMemoryUploadedFile(
        out, "ImageField", name, "image/webp", out.getbuffer().nbytes, None
    )

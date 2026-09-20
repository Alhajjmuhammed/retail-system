"""
Files that must never be public.

MEDIA_ROOT is served straight by nginx to anyone with the link, which is fine
for product photos and wrong for receipts. Private files live under
PRIVATE_MEDIA_ROOT, which nothing serves: a view checks permission and
streams them.
"""

import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateStorage(FileSystemStorage):
    # Read on every use rather than frozen at import, so settings (and tests)
    # decide where it lives.
    @property
    def base_location(self):
        return settings.PRIVATE_MEDIA_ROOT

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        raise ValueError("Private files have no public address; serve them through a view.")

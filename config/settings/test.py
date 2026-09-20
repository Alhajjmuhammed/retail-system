"""
Tests: the dev settings, with a private in-memory cache.

The dev Redis was shared with the running dev server, so a test that locked
out an IP locked the developer out too, and state leaked between tests.
"""

from .dev import *

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

"""
A rebuilt stylesheet must arrive under a URL nothing has cached.

The till's service worker serves styles and scripts from its cache first, on
purpose, so the till still opens with no connection. In development the files
keep their names from one build to the next, so after a rebuild the worker
handed back the previous stylesheet and the page rendered new markup in old
styles -- looking broken while still returning 200.

Production hashes the filenames and needs none of this.
"""

import os
import time
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template
from django.test import override_settings

CSS = Path(settings.BASE_DIR) / "static" / "css" / "app.css"


def _render(path="css/app.css"):
    return Template(
        "{% load assets %}{% versioned_static '" + path + "' %}"
    ).render(Context({}))


@override_settings(DEBUG=True)
def test_the_url_carries_the_files_own_timestamp():
    url = _render()
    assert url.startswith("/static/css/app.css?v=")
    stamp = url.split("?v=")[1]
    assert int(stamp, 16) == int(os.path.getmtime(CSS))


@override_settings(DEBUG=True)
def test_rebuilding_changes_the_url():
    before = _render()
    original = os.stat(CSS)
    try:
        os.utime(CSS, (original.st_atime, time.time() + 5))
        assert _render() != before
    finally:
        os.utime(CSS, (original.st_atime, original.st_mtime))


@override_settings(DEBUG=False)
def test_production_is_left_alone():
    """The filename already carries a content hash there."""
    assert "?v=" not in _render()


@override_settings(DEBUG=True)
def test_a_missing_file_does_not_break_the_page():
    assert _render("css/not-here.css").endswith("css/not-here.css")


@pytest.mark.django_db
def test_the_till_and_the_layout_both_use_it():
    """These are the two the worker caches, so these are the two that matter."""
    base = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text()
    till = (Path(settings.BASE_DIR) / "templates" / "pos" / "till.html").read_text()
    assert "versioned_static 'css/app.css'" in base
    assert "versioned_static 'js/till.js'" in till

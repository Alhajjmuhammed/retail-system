"""
What the till does with a barcode reader's keystrokes.

A reader is a keyboard that types fast and ends with a terminator, so none of
this can be caught by a server-side test: the keystrokes never reach Django.
Three things were wrong and are pinned here.

  * Readers that send Tab instead of Enter did nothing at all.
  * A code shorter than four characters was ignored, so a shop using short
    internal codes could not sell by scanning.
  * A scan made while the cursor sat in the search box left the barcode in the
    box with stale results under it.
"""

from pathlib import Path

import pytest
from django.conf import settings

SOURCE = Path(settings.BASE_DIR) / "static" / "js" / "till.js"


@pytest.fixture(scope="module")
def till_js():
    return SOURCE.read_text()


def test_both_terminators_finish_a_scan(till_js):
    """The shop does not always know which terminator its reader sends."""
    assert 'event.key === "Enter" || event.key === "Tab"' in till_js


def test_a_short_code_is_still_looked_up(till_js):
    """The catalogue decides whether a code exists, not its length."""
    assert "code.length >= 2" in till_js
    # ...but two characters a person typed must not raise "Unknown barcode".
    assert "quiet: code.length < 4" in till_js


def test_a_scan_clears_the_search_box(till_js):
    scan = till_js[till_js.index("async scan(code"):]
    scan = scan[: scan.index("async search()")]
    assert 'this.query = ""' in scan
    assert "this.results = []" in scan


def test_an_unknown_code_is_named_unless_it_was_too_short_to_mean_anything(till_js):
    scan = till_js[till_js.index("async scan(code"):]
    scan = scan[: scan.index("async search()")]
    assert "if (!quiet) this.status = \"Unknown barcode \" + code;" in scan


def test_one_scan_sells_what_the_code_stands_for(till_js):
    """Scanning a crate sells the crate, not one bottle."""
    scan = till_js[till_js.index("async scan(code"):]
    scan = scan[: scan.index("async search()")]
    assert "hit.qty || 1" in scan


def test_a_stray_scan_cannot_change_a_basket_being_paid_for(till_js):
    keys = till_js[till_js.index("onKey(event)"):]
    keys = keys[: keys.index("async scan(code")]
    assert "if (this.showPayment || this.dialog)" in keys


def test_typing_slowly_is_not_a_scan(till_js):
    """The buffer clears itself between human keystrokes."""
    assert "this.scanTimer = setTimeout(() => (this.scanBuffer = \"\"), 120);" in till_js

"""
The compiled stylesheet has to match the source.

Tailwind fails loudly and then carries on doing nothing: `@apply group` or
`@apply tnum` stops the build, the old app.css stays exactly where it was, and
the next page load is the new markup wearing yesterday's styles. It happened
twice in one afternoon, and both times the page still returned 200 -- the till
just looked broken, which is worse than an error.

This compares the component classes the source defines with what came out.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

SOURCE = Path(settings.BASE_DIR) / "assets" / "tailwind" / "input.css"
BUILT = Path(settings.BASE_DIR) / "static" / "css" / "app.css"


@pytest.fixture(scope="module")
def defined():
    """Every `.name {` the source declares, ignoring element and state rules."""
    text = SOURCE.read_text()
    names = set(re.findall(r"^\s*\.([a-z][a-z0-9-]+)\s*[,{]", text, re.MULTILINE))
    assert names, "no component classes found; has input.css moved?"
    return names


def test_the_stylesheet_was_built_from_the_current_source(defined):
    built = BUILT.read_text()
    missing = sorted(name for name in defined if f".{name}" not in built)
    assert not missing, (
        "These classes are in assets/tailwind/input.css but not in the built "
        "static/css/app.css, which means the Tailwind build failed and left "
        "the previous stylesheet in place. Run it again and read its output:\n"
        "  ./tailwindcss -i assets/tailwind/input.css -o static/css/app.css --minify\n"
        "  missing: " + ", ".join(missing)
    )


def test_the_till_tiles_are_styled(defined):
    """The one screen where unstyled markup is money on the floor."""
    built = BUILT.read_text()
    for name in ("tile", "tile-pic", "tile-body", "tile-name", "tile-price"):
        assert name in defined, f"{name} is no longer defined in the source"
        assert f".{name}" in built, f"{name} never reached the built stylesheet"

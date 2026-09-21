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
TILL = Path(settings.BASE_DIR) / "templates" / "pos" / "till.html"


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


def test_the_till_is_styled(defined):
    """
    The one screen where unstyled markup is money on the floor.

    The list is what the till's own markup asks for, so a class renamed in
    the template without being renamed in the stylesheet fails here rather
    than in front of a queue.
    """
    built = BUILT.read_text()
    wanted = set(re.findall(r'class="([^"]+)"', TILL.read_text()))
    used = {token for group in wanted for token in group.split()
            if token in defined}
    assert {"tile", "tile-pic", "tile-name", "tile-price", "tab", "deck-panel"} <= used, (
        "the till stopped using the classes this test exists to protect: " + str(sorted(used))
    )
    missing = sorted(name for name in used if f".{name}" not in built)
    assert not missing, f"never reached the built stylesheet: {missing}"

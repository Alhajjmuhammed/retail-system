"""
Django's {# #} comment is one line only.

Spread over two, the tag never closes and the "comment" is rendered to the
page as ordinary text. It has happened twice: once in the user menu, where a
note about branch switching appeared under somebody's name, and once on the
sign-in page, where a note to myself sat above the form. Both times the page
still returned 200, so nothing caught it but a pair of eyes.

Multi-line notes belong in {% comment %} ... {% endcomment %}.
"""

from pathlib import Path

from django.conf import settings

TEMPLATE_DIRS = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]] + [
    Path(settings.BASE_DIR) / "apps"
]


def _templates():
    for root in TEMPLATE_DIRS:
        yield from root.rglob("*.html")


def test_no_comment_is_left_open_across_lines():
    leaking = []
    for path in _templates():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "{#" in line and "#}" not in line:
                leaking.append(f"{path.relative_to(settings.BASE_DIR)}:{number}")
    assert not leaking, (
        "These {# #} comments do not close on their own line, so Django renders "
        "them to the page as text. Use {% comment %} instead:\n  "
        + "\n  ".join(leaking)
    )

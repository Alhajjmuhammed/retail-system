"""
A stamp on every static file, so a changed one is never served stale.

In production the files carry a content hash in their names and this does
nothing. In development they do not: `app.css` keeps its name from one build
to the next, so the till's service worker -- which serves assets from its
cache first, on purpose, to keep working with no connection -- hands back the
previous stylesheet after a rebuild. The page then renders new markup in old
styles, looks broken, and still returns 200.

Appending the file's modification time gives each build its own URL, which no
cache has seen before.
"""

import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def versioned_static(path):
    url = static(path)
    if not settings.DEBUG:
        # Hashed names already; a query string would only defeat caching.
        return url
    # Through the finders, because in development the files are served from
    # their source directories and nothing has been collected yet.
    #
    # Read every time rather than remembered: the whole point is to notice a
    # rebuild, and nobody should have to restart to see their own stylesheet.
    found = finders.find(path)
    if not found:
        return url
    try:
        stamp = int(os.path.getmtime(found))
    except OSError:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={stamp:x}"

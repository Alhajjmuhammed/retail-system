"""
Inline SVG icons.

Inline rather than a font or a sprite sheet, because the till has to render
correctly with no connection and a missing icon font would leave a screen of
empty boxes in front of a queue.

    {% icon "cart" %}            {% icon "cart" class="h-5 w-5" %}
"""

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Paths only; the wrapper supplies the sizing and stroke. 24x24 grid.
PATHS = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>',
    "cart": '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>'
            '<path d="M2 3h2.5l2.2 11.2a2 2 0 0 0 2 1.6h8.4a2 2 0 0 0 2-1.6L21 7H5.2"/>',
    "receipt": '<path d="M5 3v18l2.5-1.5L10 21l2-1.5L14 21l2.5-1.5L19 21V3z"/>'
               '<path d="M9 8h6M9 12h6"/>',
    "box": '<path d="M12 3 3 7.5v9L12 21l9-4.5v-9z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/>',
    "layers": '<path d="M12 3 2 8l10 5 10-5z"/><path d="m2 16 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "truck": '<path d="M2 6h11v11H2z"/><path d="M13 9h4l4 4v4h-8z"/>'
             '<circle cx="6.5" cy="18.5" r="1.8"/><circle cx="17" cy="18.5" r="1.8"/>',
    "clipboard": '<rect x="7" y="4" width="10" height="3.5" rx="1"/>'
                 '<path d="M8.5 5.5H6a2 2 0 0 0-2 2V19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5a2 2 0 0 0-2-2h-2.5"/>'
                 '<path d="M8.5 12h7M8.5 16h4"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.2 2"/>',
    "mail": '<rect x="2.5" y="5" width="19" height="14" rx="2"/>'
            '<path d="m3 7 8.1 5.6a1.6 1.6 0 0 0 1.8 0L21 7"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/>'
           '<circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M10.6 6.2A8.6 8.6 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-3.2 3.8"/>'
               '<path d="M6.4 7.6A16 16 0 0 0 2.5 12S6 18 12 18a8.8 8.8 0 0 0 3.4-.66"/>'
               '<path d="m9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="m3 3 18 18"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/>'
            '<path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
             '<path d="M16 5.2a3.5 3.5 0 0 1 0 6.6M17.5 14.5A6.5 6.5 0 0 1 21.5 20"/>',
    "user": '<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
    "shield": '<path d="M12 3 4.5 6v6c0 4.4 3.1 7.9 7.5 9 4.4-1.1 7.5-4.6 7.5-9V6z"/>'
              '<path d="m9.2 12 2 2 3.6-3.8"/>',
    "wallet": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H18v2.5"/>'
              '<path d="M3 7.5V18a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2H5"/>'
              '<circle cx="16.5" cy="14" r="1.2"/>',
    "chart": '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 20v-6M13 20v-10M18 20v-4"/>',
    "trending": '<path d="m3 16 5.5-5.5 4 4L21 6"/><path d="M15 6h6v6"/>',
    "tag": '<path d="M3 11.5V4.5A1.5 1.5 0 0 1 4.5 3h7l9 9-8 8z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 14.5a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.4 6.6l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 2.9-1.2V2a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.2a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.6 1.5z"/>',
    "credit-card": '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 10h19"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m5 13 4.5 4.5L19 7"/>',
    "alert": '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4.5M12 17.5h.01"/>',
    "download": '<path d="M12 3v12"/><path d="m7.5 11 4.5 4.5 4.5-4.5"/><path d="M4 20h16"/>',
    "upload": '<path d="M12 16V4"/><path d="m7.5 8 4.5-4.5L16.5 8"/><path d="M4 20h16"/>',
    "logout": '<path d="M14 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h8"/><path d="M17 8.5 20.5 12 17 15.5"/><path d="M20 12H10"/>',
    "switch": '<path d="M7 4 3.5 7.5 7 11"/><path d="M3.5 7.5H17a3.5 3.5 0 0 1 0 7h-1"/>'
              '<path d="m17 20 3.5-3.5L17 13"/>',
    "building": '<path d="M4 21V5.5A1.5 1.5 0 0 1 5.5 4h7A1.5 1.5 0 0 1 14 5.5V21"/>'
                '<path d="M14 10h4.5A1.5 1.5 0 0 1 20 11.5V21"/><path d="M2.5 21h19"/>'
                '<path d="M7.5 8h3M7.5 12h3M7.5 16h3"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="16" rx="2"/><path d="M3.5 10h17"/>'
                '<path d="M8 3v4M16 3v4"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    # Row actions. Small, unambiguous shapes -- at 16px anything fussier
    # turns to mush.
    "key": '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.7-8.7"/><path d="m16.5 6.5 2.5 2.5"/><path d="m14 9 2 2"/>',
    "pencil": '<path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="m14.5 6.5 3 3"/>',
    "trash": '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/>'
             '<path d="M6 7l1 13h10l1-13"/><path d="M9 7V4.5h6V7"/>',
    "enter": '<path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/>'
             '<path d="M14.5 8.5 18 12l-3.5 3.5"/><path d="M18 12H9"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    # Suspend and restore. Two bare vertical strokes read as "||" at 16px,
    # so suspension gets the universally understood barred circle instead.
    "suspend": '<circle cx="12" cy="12" r="8.5"/><path d="m6.2 6.2 11.6 11.6"/>',
    "restore": '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/>'
               '<path d="M3.5 4v4.5H8"/>',
    "dots": '<circle cx="12" cy="5.5" r="1.4"/><circle cx="12" cy="12" r="1.4"/>'
            '<circle cx="12" cy="18.5" r="1.4"/>',
    "filter": '<path d="M3.5 5h17l-6.5 7.5V19l-4 2v-8.5z"/>',
}


@register.simple_tag
def icon(name, **attrs):
    path = PATHS.get(name)
    if path is None:
        return ""
    classes = attrs.pop("class", "h-4 w-4")
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return mark_safe(
        f'<svg class="{classes}" {extra} viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )

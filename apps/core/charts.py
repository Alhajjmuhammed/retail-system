"""
Charts, drawn on the server.

The till works with no network and the dashboard is read on a phone over a
patchy connection, so a charting library downloaded from a CDN is the wrong
shape for this system: the page would render with four empty rectangles
exactly when the shop most needs to see its figures. Every chart here is
plain SVG geometry worked out in Python and handed to the template as
numbers, which also means a test can assert on the shape of a line rather
than on a screenshot of one.

Coordinates are in a fixed viewBox and the SVG scales to its box, so none of
this depends on the width of the reader's screen.
"""

from decimal import Decimal

# One viewBox for every chart of a kind, so a sparkline in the first card and
# a sparkline in the fourth are the same size whatever numbers they hold.
SPARK = (100, 28)
CURVE = (600, 180)


def _floats(values):
    return [float(v or 0) for v in values]


def spark(values, width=SPARK[0], height=SPARK[1]):
    """
    The little line under a headline figure.

    Returns the polyline points, the matching filled area, and where the last
    point sits so the template can put a dot on it. A flat series is drawn
    along the middle rather than along the floor: a shop that took the same
    money three days running has not fallen to zero.
    """
    nums = _floats(values)
    # A line through a row of zeroes says nothing at all, and drawn along the
    # middle it actively misleads. Nothing happened: show nothing.
    if len(nums) < 2 or not any(nums):
        return None

    low, high = min(nums), max(nums)
    span = high - low
    pad = 2.0  # keeps the stroke and the dot inside the box
    step = width / (len(nums) - 1)

    def y(v):
        if span == 0:
            return height / 2
        return height - pad - (v - low) / span * (height - pad * 2)

    points = [(i * step, y(v)) for i, v in enumerate(nums)]
    line = " ".join(f"{x:.1f},{p:.1f}" for x, p in points)
    area = (f"M0,{height:.1f} L" + " L".join(f"{x:.1f},{p:.1f}" for x, p in points)
            + f" L{width:.1f},{height:.1f} Z")
    last_x, last_y = points[-1]
    return {"line": line, "area": area, "last_x": round(last_x, 1),
            "last_y": round(last_y, 1), "width": width, "height": height}


def curve(rows, width=CURVE[0], height=CURVE[1]):
    """
    The big line chart: one point per row, each row a dict with a label and a
    value.

    Unlike the sparkline this one is anchored at zero, because it carries a
    scale down the side and a line that starts halfway up an axis reading
    zero is a lie. Returns the geometry plus the gridlines and the handful of
    labels that fit along the bottom.
    """
    nums = _floats(r["value"] for r in rows)
    if not nums or not any(nums):
        return None

    high = max(nums)
    top = high if high > 0 else 1.0
    pad = 6.0
    step = width / (len(nums) - 1) if len(nums) > 1 else 0.0

    def y(v):
        return height - pad - (v / top) * (height - pad * 2)

    points = [(i * step, y(v)) for i, v in enumerate(nums)]
    line = " ".join(f"{x:.1f},{p:.1f}" for x, p in points)
    area = (f"M0,{height:.1f} L" + " L".join(f"{x:.1f},{p:.1f}" for x, p in points)
            + f" L{points[-1][0]:.1f},{height:.1f} Z")

    # Three bands, top down, so a template can drop them straight into a
    # column beside the plot. More than three and the gridlines start
    # competing with the line they are there to serve.
    grid = [{"pct": round((height - pad - fraction * (height - pad * 2)) / height * 100, 2),
             "value": Decimal(str(top * fraction))}
            for fraction in (1, 0.5, 0)]

    # Only ever six labels along the foot, whichever period is being shown:
    # thirty dates in that space is a grey smear. Positions come back as
    # percentages so the labels can be laid out in HTML, where they stay the
    # right shape -- text inside a stretched SVG does not.
    every = max(1, len(rows) // 6)
    marks = [{"pct": round(x / width * 100, 2),
              "label": rows[i].get("short") or rows[i]["label"]}
             for i, (x, _) in enumerate(points)
             if i % every == 0 or i == len(rows) - 1]

    return {
        "line": line, "area": area, "width": width, "height": height,
        "grid": grid, "marks": marks, "peak": Decimal(str(top)),
    }


def change(now, before):
    """
    How the period compares with the one before it, as a percentage.

    ``None`` when there is nothing to compare against: a first week in
    business is not "up 100%", it is simply the first week, and saying
    otherwise turns the whole row of figures into noise.
    """
    now, before = float(now or 0), float(before or 0)
    if before == 0:
        return None
    return round((now - before) / before * 100, 1)

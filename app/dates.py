"""Centralised date formatting.

Every UI-facing date string should go through one of the helpers here
(or the matching Jinja filters registered in ``templating.py``).

Styles
------
``short``   – "16 Apr"         (day + abbreviated month, no year)
``medium``  – "16 Apr 2026"    (default; day + abbreviated month + year)
``long``    – "16 April 2026"  (day + full month + year)
``month``   – "April 2026"     (full month + year)
``iso``     – "2026-04-16"     (ISO-8601, used for <input type="date"> values & URLs)

For datetimes an extra ``datetime`` style is available:
``datetime`` – "16 Apr 2026, 14:30"
"""

from __future__ import annotations

from datetime import date, datetime


_STYLES = {
    "short": "%d %b",
    "medium": "%d %b %Y",
    "long": "%d %B %Y",
    "month": "%B %Y",
    "month_short": "%b %Y",
    "month_abbr": "%b %y",
    "weekday": "%a",
    "iso": "%Y-%m-%d",
}

_DATETIME_FMT = "%d %b %Y, %H:%M"


def fmt_date(value: date | datetime | None, style: str = "medium") -> str:
    """Format a ``date`` or ``datetime`` for display.

    Returns ``""`` for *None* so templates can use it unconditionally.
    """
    if value is None:
        return ""
    if isinstance(value, datetime) and style == "datetime":
        return value.strftime(_DATETIME_FMT)
    pattern = _STYLES.get(style)
    if pattern is None:
        raise ValueError(f"Unknown date style {style!r}")
    return value.strftime(pattern).lstrip("0")


def fmt_month(value: date | datetime | None) -> str:
    """Shorthand for ``fmt_date(value, "month")``."""
    return fmt_date(value, "month")


def fmt_iso(value: date | datetime | None) -> str:
    """Shorthand for ``fmt_date(value, "iso")``."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()

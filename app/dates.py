"""Centralised date formatting and parsing.

Every UI-facing date string should go through one of the helpers here
(or the matching Jinja filters registered in ``templating.py``). Every
*parsing* of a date/datetime string from a query parameter, form field,
import file, or external feed should also go through the parsers here so
we have one place to fix bugs, normalise edge cases, and reason about
input shape.

Formatting styles
-----------------
``short``   – "16 Apr"         (day + abbreviated month, no year)
``medium``  – "16 Apr 2026"    (default; day + abbreviated month + year)
``long``    – "16 April 2026"  (day + full month + year)
``month``   – "April 2026"     (full month + year)
``iso``     – "2026-04-16"     (ISO-8601, used for <input type="date"> values & URLs)

For datetimes an extra ``datetime`` style is available:
``datetime`` – "16 Apr 2026, 14:30"

Parsers
-------
``parse_iso_date``         – strict ISO-8601 date (``YYYY-MM-DD``); raises ``ValueError``.
``parse_iso_date_or_none`` – like above but ``None``/empty string → ``None``.
``parse_iso_date_or``      – like above but malformed/empty → caller-supplied fallback.
``parse_iso_datetime``     – strict ISO-8601 datetime, accepting both ``Z`` and ``+00:00`` suffixes.
``parse_iso_datetime_or_none`` – like above but malformed/empty → ``None``.
``parse_date_with_formats``    – try a list of ``strptime`` formats in order.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import TypeVar, overload


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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

T = TypeVar("T")


def parse_iso_date(raw: str) -> date:
    """Parse a strict ISO-8601 date string (``YYYY-MM-DD``).

    Raises ``ValueError`` on malformed input or non-string values. Use this
    for inputs you control end-to-end (e.g. hidden form fields you also
    emit, or values you've already validated).
    """
    if not isinstance(raw, str):
        raise ValueError(f"Expected ISO-8601 date string, got {type(raw).__name__}")
    return date.fromisoformat(raw)


@overload
def parse_iso_date_or_none(raw: None) -> None: ...
@overload
def parse_iso_date_or_none(raw: str) -> date | None: ...
def parse_iso_date_or_none(raw: str | None) -> date | None:
    """Parse a strict ISO-8601 date string, returning ``None`` when input is
    missing or blank.

    Malformed (non-empty) input still raises ``ValueError`` so it surfaces
    as a 4xx via FastAPI rather than being silently swallowed.
    """
    if raw is None:
        return None
    s = raw.strip() if isinstance(raw, str) else raw
    if s == "" or s is None:
        return None
    return parse_iso_date(s)


def parse_iso_date_or(raw: str | None, fallback: T) -> date | T:
    """Parse a strict ISO-8601 date string, returning ``fallback`` for any
    missing/blank/malformed input.

    Use this at the *outer edge* of the app (query strings, optional
    form fields) where it's reasonable to fall back to "today" or a
    sensible default rather than 400-ing the request.
    """
    if raw is None:
        return fallback
    s = raw.strip() if isinstance(raw, str) else raw
    if s == "":
        return fallback
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return fallback


def parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO-8601 datetime string.

    Tolerates the trailing ``Z`` UTC marker (Python's ``fromisoformat``
    accepts ``+00:00`` but historically not ``Z``); both produce the same
    timezone-aware ``datetime``.
    """
    if not isinstance(raw, str):
        raise ValueError(f"Expected ISO-8601 datetime string, got {type(raw).__name__}")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def parse_iso_datetime_or_none(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning ``None`` for missing,
    blank, or malformed input.

    Used for tolerant ingestion of external feed timestamps (e.g. Akahu)
    where a missing/garbled timestamp is recoverable.
    """
    if raw is None:
        return None
    s = raw.strip() if isinstance(raw, str) else raw
    if not s:
        return None
    try:
        return parse_iso_datetime(s)
    except (ValueError, TypeError, AttributeError):
        return None


def parse_iso_datetime_date_or(raw: str | None, fallback: T) -> date | T:
    """Parse an ISO-8601 datetime and return its date portion. ``fallback``
    on missing/malformed input.

    Convenience for places that have a UTC timestamp string but only want
    the calendar date (e.g. Akahu posted-date filters).
    """
    parsed = parse_iso_datetime_or_none(raw)
    if parsed is None:
        return fallback
    return parsed.date()


def parse_date_with_formats(raw: str, formats: Iterable[str]) -> date:
    """Try each ``strptime`` format in order; return the date of the first
    that succeeds. Raises ``ValueError`` if none match.

    Used by the CSV importer where the user picks a primary format and we
    fall back through a small list of common locale shapes.
    """
    if not isinstance(raw, str):
        raise ValueError(f"Expected date string, got {type(raw).__name__}")
    s = raw.strip()
    last_error: ValueError | None = None
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError as e:
            last_error = e
            continue
    raise last_error or ValueError(f"No format matched: {raw!r}")

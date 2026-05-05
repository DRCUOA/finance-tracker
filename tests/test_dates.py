"""Tests for the centralised date utilities in ``app.dates``.

These cover both the formatting helpers (``fmt_date``/``fmt_iso``/``fmt_month``)
and the parse helpers (``parse_iso_date`` and friends). Date handling lives in
exactly one place; these tests are that place's contract.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.dates import (
    fmt_date,
    fmt_iso,
    fmt_month,
    parse_date_with_formats,
    parse_iso_date,
    parse_iso_date_or,
    parse_iso_date_or_none,
    parse_iso_datetime,
    parse_iso_datetime_date_or,
    parse_iso_datetime_or_none,
)


# ---------------------------------------------------------------------------
# Formatting (existing behaviour — locks in current contracts)
# ---------------------------------------------------------------------------

class TestFmtDate:
    def test_short_strips_leading_zero(self):
        assert fmt_date(date(2026, 3, 5), "short") == "5 Mar"

    def test_medium_default(self):
        assert fmt_date(date(2026, 3, 31)) == "31 Mar 2026"

    def test_long_full_month(self):
        assert fmt_date(date(2026, 3, 31), "long") == "31 March 2026"

    def test_month_only(self):
        assert fmt_month(date(2026, 3, 1)) == "March 2026"

    def test_iso(self):
        assert fmt_iso(date(2026, 3, 31)) == "2026-03-31"

    def test_iso_from_datetime_drops_time(self):
        assert fmt_iso(datetime(2026, 3, 31, 14, 30)) == "2026-03-31"

    def test_none_renders_blank(self):
        assert fmt_date(None) == ""
        assert fmt_iso(None) == ""

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError):
            fmt_date(date(2026, 3, 31), "no-such-style")

    def test_datetime_style_only_works_for_datetime(self):
        assert fmt_date(datetime(2026, 3, 31, 14, 30), "datetime") == "31 Mar 2026, 14:30"


# ---------------------------------------------------------------------------
# Strict ISO date parsing
# ---------------------------------------------------------------------------

class TestParseIsoDateStrict:
    def test_happy_path(self):
        assert parse_iso_date("2026-03-31") == date(2026, 3, 31)

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            parse_iso_date("not-a-date")

    def test_empty_string_raises(self):
        # Empty is malformed for the strict parser — caller must use the
        # ``_or_none`` variant to opt out of the raise.
        with pytest.raises(ValueError):
            parse_iso_date("")

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            parse_iso_date(20260331)  # type: ignore[arg-type]


class TestParseIsoDateOrNone:
    def test_happy_path(self):
        assert parse_iso_date_or_none("2026-03-31") == date(2026, 3, 31)

    def test_none_input(self):
        assert parse_iso_date_or_none(None) is None

    def test_blank_input(self):
        assert parse_iso_date_or_none("") is None
        assert parse_iso_date_or_none("   ") is None

    def test_malformed_still_raises(self):
        # Non-empty + malformed ⇒ ValueError. The point of "_or_none" is to
        # tolerate *missing* input, not garbage input.
        with pytest.raises(ValueError):
            parse_iso_date_or_none("garbage")


class TestParseIsoDateOr:
    FALLBACK = date(2000, 1, 1)

    def test_happy_path(self):
        assert parse_iso_date_or("2026-03-31", self.FALLBACK) == date(2026, 3, 31)

    def test_none_falls_back(self):
        assert parse_iso_date_or(None, self.FALLBACK) == self.FALLBACK

    def test_blank_falls_back(self):
        assert parse_iso_date_or("", self.FALLBACK) == self.FALLBACK
        assert parse_iso_date_or("   ", self.FALLBACK) == self.FALLBACK

    def test_malformed_falls_back(self):
        assert parse_iso_date_or("not-a-date", self.FALLBACK) == self.FALLBACK

    def test_fallback_can_be_anything(self):
        # The helper is useful with a None fallback too (Akahu range parsing).
        assert parse_iso_date_or("garbage", None) is None


# ---------------------------------------------------------------------------
# ISO datetime parsing (Akahu / API ingest)
# ---------------------------------------------------------------------------

class TestParseIsoDatetime:
    def test_z_suffix(self):
        assert parse_iso_datetime("2026-04-20T09:30:00.000Z") == datetime(
            2026, 4, 20, 9, 30, tzinfo=timezone.utc,
        )

    def test_explicit_offset(self):
        assert parse_iso_datetime("2026-04-20T09:30:00+00:00") == datetime(
            2026, 4, 20, 9, 30, tzinfo=timezone.utc,
        )

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            parse_iso_datetime("nope")


class TestParseIsoDatetimeOrNone:
    def test_happy_path(self):
        assert parse_iso_datetime_or_none("2026-04-20T09:30:00Z") == datetime(
            2026, 4, 20, 9, 30, tzinfo=timezone.utc,
        )

    def test_none_or_blank(self):
        assert parse_iso_datetime_or_none(None) is None
        assert parse_iso_datetime_or_none("") is None

    def test_malformed_returns_none(self):
        # Tolerant variant — used for external feeds where a bad timestamp
        # shouldn't crash the sync.
        assert parse_iso_datetime_or_none("garbage") is None


class TestParseIsoDatetimeDateOr:
    def test_happy_path(self):
        assert parse_iso_datetime_date_or("2026-04-20T09:30:00Z", None) == date(2026, 4, 20)

    def test_falls_back_on_missing(self):
        assert parse_iso_datetime_date_or(None, date(2000, 1, 1)) == date(2000, 1, 1)
        assert parse_iso_datetime_date_or("", date(2000, 1, 1)) == date(2000, 1, 1)

    def test_falls_back_on_malformed(self):
        assert parse_iso_datetime_date_or("garbage", None) is None


# ---------------------------------------------------------------------------
# Multi-format parsing (CSV importer)
# ---------------------------------------------------------------------------

class TestParseDateWithFormats:
    def test_first_format_wins(self):
        assert parse_date_with_formats(
            "05/31/2026", ("%m/%d/%Y", "%d/%m/%Y"),
        ) == date(2026, 5, 31)

    def test_falls_through_to_second_format(self):
        # The string isn't a valid m/d/Y (no month 31), so the parser should
        # try the next format in the list.
        assert parse_date_with_formats(
            "31/05/2026", ("%m/%d/%Y", "%d/%m/%Y"),
        ) == date(2026, 5, 31)

    def test_iso_format_in_list(self):
        assert parse_date_with_formats(
            "2026-05-31", ("%m/%d/%Y", "%Y-%m-%d"),
        ) == date(2026, 5, 31)

    def test_strips_surrounding_whitespace(self):
        # CSV cells often have stray whitespace; the helper handles it so
        # callers don't have to remember.
        assert parse_date_with_formats(
            "  2026-05-31  ", ("%Y-%m-%d",),
        ) == date(2026, 5, 31)

    def test_no_match_raises(self):
        with pytest.raises(ValueError):
            parse_date_with_formats("not a date", ("%Y-%m-%d", "%d/%m/%Y"))

    def test_empty_format_list_raises(self):
        with pytest.raises(ValueError):
            parse_date_with_formats("2026-05-31", ())

"""Behaviour tests for the date / interval helpers in cratebuilder.util.

These pin the exact contracts the single-file app relied on before the helpers
were lifted into the tested package (lenient parsing: bad input is returned
unchanged or mapped to a sentinel, never raised).
"""
import time
from datetime import date, datetime, timedelta

from cratebuilder import util


# ── days_ago_yyyymmdd ─────────────────────────────────────────────────────────
def test_days_ago_zero_is_today():
    assert util.days_ago_yyyymmdd(0) == util.today_yyyymmdd()


def test_days_ago_counts_back():
    expected = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
    assert util.days_ago_yyyymmdd(7) == expected


def test_days_ago_coerces_str_int():
    assert util.days_ago_yyyymmdd("3") == (date.today() - timedelta(days=3)).strftime("%Y%m%d")


# ── subtract_days_from_yyyymmdd ───────────────────────────────────────────────
def test_subtract_days_crosses_month_boundary():
    assert util.subtract_days_from_yyyymmdd("20260310", 10) == "20260228"


def test_subtract_days_invalid_date_returned_unchanged():
    assert util.subtract_days_from_yyyymmdd("notadate", 5) == "notadate"


def test_subtract_days_invalid_count_returned_unchanged():
    assert util.subtract_days_from_yyyymmdd("20260310", "x") == "20260310"


# ── format_yyyymmdd_readable ──────────────────────────────────────────────────
def test_format_readable_valid():
    assert util.format_yyyymmdd_readable("20260310") == "March 10, 2026"


def test_format_readable_invalid_returns_string_of_input():
    assert util.format_yyyymmdd_readable("abc") == "abc"
    assert util.format_yyyymmdd_readable(None) == "None"


# ── format_timestamp_relative ─────────────────────────────────────────────────
def test_relative_never_for_falsy():
    assert util.format_timestamp_relative(0) == "Never"
    assert util.format_timestamp_relative(None) == "Never"


def test_relative_just_now():
    assert util.format_timestamp_relative(time.time()) == "Just now"


def test_relative_singular_vs_plural_minutes():
    assert util.format_timestamp_relative(time.time() - 65) == "1 minute ago"
    assert util.format_timestamp_relative(time.time() - 305) == "5 minutes ago"


def test_relative_hours_and_days():
    assert util.format_timestamp_relative(time.time() - 3700) == "1 hour ago"
    assert util.format_timestamp_relative(time.time() - 86400 * 2 - 100) == "2 days ago"


def test_relative_unparseable_is_unknown():
    assert util.format_timestamp_relative("notanumber") == "Unknown"


# ── interval_label_to_seconds ─────────────────────────────────────────────────
def test_interval_off_and_blank_are_none():
    assert util.interval_label_to_seconds("Off") is None
    assert util.interval_label_to_seconds("  off ") is None
    assert util.interval_label_to_seconds("") is None
    assert util.interval_label_to_seconds(None) is None


def test_interval_parses_hours_days_weeks():
    assert util.interval_label_to_seconds("6 hours") == 6 * 3600
    assert util.interval_label_to_seconds("12 hours") == 12 * 3600
    assert util.interval_label_to_seconds("1 day") == 86400
    assert util.interval_label_to_seconds("2 days") == 2 * 86400
    assert util.interval_label_to_seconds("1 week") == 7 * 86400


def test_interval_garbage_is_none():
    assert util.interval_label_to_seconds("garbage") is None
    assert util.interval_label_to_seconds("5 fortnights") is None

import time

from cratebuilder.util import (
    BUSY_RETRY_MS, SCAN_SETTLE_MAX_POLLS, SCAN_SETTLE_POLL_MS,
    interval_label_to_seconds, next_run_delay_ms, next_run_label,
    scan_settle_verdict,
)

DAY = 86400
HOUR = 3600


def test_interval_label_to_seconds(cb_mod):
    f = cb_mod.interval_label_to_seconds
    assert f("Off") is None
    assert f("6 hours") == 6 * 3600
    assert f("12 hours") == 12 * 3600
    assert f("1 day") == 86400
    assert f("2 days") == 2 * 86400
    assert f("3 days") == 3 * 86400
    assert f("1 week") == 7 * 86400
    assert f("nonsense") is None
    # Real runtime inputs from an unset/blank StringVar must be safe.
    assert f("") is None
    assert f(None) is None


# ── next_run_delay_ms ─────────────────────────────────────────────────────────
def test_no_interval_disarms_the_timer():
    # 'Off' parses to None, and None must reach the caller as "no next run"
    # rather than as a delay of zero, which would fire immediately forever.
    assert next_run_delay_ms(None, 1_000_000, 1_000_000) is None
    assert next_run_delay_ms(interval_label_to_seconds("Off"), 0, 0) is None


def test_a_full_interval_remains_when_the_last_run_was_just_now():
    delay_ms, next_ts = next_run_delay_ms(6 * HOUR, 1_000_000, 1_000_000)
    assert delay_ms == 6 * HOUR * 1000
    assert next_ts == 1_000_000 + 6 * HOUR


def test_a_partly_elapsed_interval_only_waits_out_the_remainder():
    # Two hours into a six-hour interval: four hours left, not six. This is what
    # stops a restart from resetting the clock and postponing the run forever.
    delay_ms, next_ts = next_run_delay_ms(6 * HOUR, 1_000_000,
                                          1_000_000 + 2 * HOUR)
    assert delay_ms == 4 * HOUR * 1000
    assert next_ts == 1_000_000 + 6 * HOUR


def test_an_overdue_run_is_scheduled_one_second_out_not_immediately():
    # Exactly due, and long overdue, both land on 1s. Zero would mean firing
    # from inside the arming call and re-entering the scheduler.
    assert next_run_delay_ms(HOUR, 1_000_000, 1_000_000 + HOUR)[0] == 1000
    assert next_run_delay_ms(HOUR, 1_000_000, 1_000_000 + 99 * DAY)[0] == 1000


def test_never_having_run_is_overdue():
    # A falsy anchor counts from the epoch, so a first launch with an interval
    # set runs almost at once instead of waiting a whole interval.
    for anchor in (0, None, ""):
        assert next_run_delay_ms(DAY, anchor, 1_000_000)[0] == 1000


def test_the_reported_next_timestamp_matches_the_delay_it_returns():
    # The label reads next_ts while after() reads delay_ms; if they disagree the
    # user is told a time the timer will not honour.
    for elapsed in (0, 61, 3599, 6 * HOUR):
        delay_ms, next_ts = next_run_delay_ms(6 * HOUR, 1_000_000,
                                              1_000_000 + elapsed)
        assert next_ts == 1_000_000 + elapsed + delay_ms // 1000


def test_a_float_now_does_not_leak_into_a_float_delay():
    # time.time() is a float; after() and the label both want whole numbers.
    delay_ms, next_ts = next_run_delay_ms(HOUR, 1_000_000.0, 1_000_000.75)
    assert isinstance(delay_ms, int) and isinstance(next_ts, int)


# ── scan_settle_verdict ───────────────────────────────────────────────────────
def test_settled_scans_proceed_immediately():
    assert scan_settle_verdict(0, 0) == "proceed"
    # Even at the cap: nothing left to wait for beats the poll budget.
    assert scan_settle_verdict(0, SCAN_SETTLE_MAX_POLLS + 50) == "proceed"


def test_active_scans_are_waited_out_up_to_the_cap():
    assert scan_settle_verdict(3, 0) == "wait"
    assert scan_settle_verdict(1, SCAN_SETTLE_MAX_POLLS - 1) == "wait"


def test_a_wedged_scan_is_given_up_on_rather_than_polled_forever():
    assert scan_settle_verdict(1, SCAN_SETTLE_MAX_POLLS) == "give_up"
    assert scan_settle_verdict(1, SCAN_SETTLE_MAX_POLLS + 1) == "give_up"


def test_the_poll_budget_is_about_five_minutes():
    # The cap exists to bound the wait, so the bound itself is the contract.
    total_s = SCAN_SETTLE_MAX_POLLS * SCAN_SETTLE_POLL_MS / 1000
    assert 240 <= total_s <= 360


def test_the_busy_retry_is_a_minute():
    # Long enough not to fight the user's own scan, short enough that a
    # scheduled run still happens soon after they finish.
    assert BUSY_RETRY_MS == 60_000


# ── next_run_label ────────────────────────────────────────────────────────────
def test_no_next_run_reads_off():
    for ts in (None, 0, ""):
        assert next_run_label(ts) == "⏰  Next auto-download:  Off"


def test_a_timestamp_renders_as_a_local_date_and_12_hour_time():
    ts = time.mktime((2026, 3, 9, 14, 5, 0, 0, 0, -1))
    text = next_run_label(ts)
    assert text.startswith("⏰  Next auto-download:  Mon Mar 9, 2026")
    # Leading zero stripped by hand — '%-I' is not portable to Windows.
    assert text.endswith("2:05 PM")


def test_midnight_reads_as_twelve_not_zero():
    ts = time.mktime((2026, 3, 9, 0, 30, 0, 0, 0, -1))
    assert next_run_label(ts).endswith("12:30 AM")


def test_noon_reads_as_twelve_pm():
    ts = time.mktime((2026, 3, 9, 12, 0, 0, 0, 0, -1))
    assert next_run_label(ts).endswith("12:00 PM")


def test_an_unrenderable_timestamp_degrades_to_a_dash():
    # A corrupt stored anchor must not raise out of a label refresh.
    assert next_run_label(10 ** 20) == "⏰  Next auto-download:  —"


def test_the_prefix_is_the_callers():
    assert next_run_label(0, prefix="Next check: ") == "Next check: Off"

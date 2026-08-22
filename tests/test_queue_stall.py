"""The Queue's connect-wait counter: the seconds a track spends trying to start.

A track that yt-dlp cannot reach sits there looking identical to one that is
downloading fine. These cover the counter that tells them apart — the pure
render, the column it shares with the terminal verdict, and the once-a-second
ticker that must repaint a row without ever scrolling the queue.
"""
import time

import pytest


# ── The pure render ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("elapsed, grace, expected", [
    (0, 3, ""),
    (2.9, 3, ""),
    (3, 3, "⏳ 3s"),
    (3.9, 3, "⏳ 3s"),
    (12, 3, "⏳ 12s"),
    (99, 3, "⏳ 99s"),
    (99.9, 3, "⏳ 99s"),
    (100, 3, "⏳ 1:40"),
    (105, 3, "⏳ 1:45"),
    (3599, 3, "⏳ 59:59"),
    (3600, 3, "⏳ 60:00"),
])
def test_connect_wait_text_reads_the_way_a_stopwatch_does(cb_mod, elapsed,
                                                          grace, expected):
    assert cb_mod.MP3DownloaderApp.connect_wait_text(elapsed, grace) == expected


def test_connect_wait_text_never_outgrows_the_status_column(cb_mod):
    """The column is 14 characters wide; a longer string would push the whole
    fixed-width line out of shape."""
    render = cb_mod.MP3DownloaderApp.connect_wait_text
    for elapsed in (0, 1, 3, 99, 100, 599, 3600, 86400, 10 ** 6, 10 ** 12,
                    float("1e18"), -5):
        assert len(render(elapsed, 3)) <= 14, elapsed


def test_a_grace_of_zero_still_starts_counting_from_the_first_second(cb_mod):
    assert cb_mod.MP3DownloaderApp.connect_wait_text(0, 0) == "⏳ 0s"


# ── The line it lands in ──────────────────────────────────────────────────────
def _width(line):
    return len(line.rstrip("\n"))


def test_the_line_width_is_unchanged_with_and_without_a_tick(app):
    plain = app._format_queue_line(0, "◉", "Deonite - Light")
    ticked = app._format_queue_line(0, "◉", "Deonite - Light",
                                    stall="⏳ 12s")
    both = app._format_queue_line(0, "◉", "Deonite - Light",
                                  bitrate="160k → 192k", note="✓ done",
                                  stall="⏳ 12s")
    long_title = app._format_queue_line(9, "◉", "x" * 200,
                                        stall="⏳ 99:59+")
    for line in (plain, ticked, both, long_title):
        assert _width(line) == 89
        assert line.endswith("\n")


def test_a_tick_can_never_widen_a_line(app, cb_mod):
    """The width guarantee the counter is actually responsible for.

    Some notes are still wider than the 14-wide status column — "format
    unavailable" is 18 — so the line is not 89 characters unconditionally. But
    a tick only renders into a column a verdict has left empty, and
    connect_wait_text is bounded, so no tick can push a line past the width
    that same line already had, whatever the note does."""
    render = cb_mod.MP3DownloaderApp.connect_wait_text
    ticks = [render(e, 3) for e in (3, 12, 99, 100, 3599, 6000)]
    for note in ("", "skipped", "✓ done", "x" * 60):
        without = app._format_queue_line(0, "◉", "T", note=note)
        for tick in ticks:
            with_tick = app._format_queue_line(0, "◉", "T", note=note,
                                               stall=tick)
            assert _width(with_tick) == _width(without), (note, tick)


def test_a_real_failure_reason_renders_inside_the_designed_width(app):
    """The two halves joined up: what the classifier hands back has to fit the
    line the queue draws it into.

    These three messages were reaching the column verbatim — one ran to 135
    columns in a Text widget that neither wraps nor scrolls sideways, so its
    tail was off the edge of the window with no way to reach it."""
    from cratebuilder.download import classify_download_failure

    raw = [
        r"[Errno 13] Permission denied: 'C:\WINDOWS\System32\tmpr1yl51yu.tmp'",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: \r[download] Got error: HTTPSConnectionPool("
        "host='rr4---sn-p5qlsn7d.googlevideo.com', port=443): "
        "Read timed out. (read timeout=20.0)",
    ]
    for text in raw:
        reason = classify_download_failure(text).reason
        line = app._format_queue_line(0, "✗", "Deonite - Light", note=reason)
        assert _width(line) == 89, (reason, _width(line))
        assert reason in line
        assert "\n" not in line.rstrip("\n")


def test_a_terminal_verdict_beats_a_tick_in_the_shared_column(app):
    line = app._format_queue_line(0, "✓", "Deonite - Light",
                                  note="✓ done", stall="⏳ 12s")
    assert "✓ done" in line
    assert "⏳" not in line


def test_a_tick_shows_when_there_is_no_verdict_yet(app):
    line = app._format_queue_line(0, "◉", "Deonite - Light",
                                  stall="⏳ 12s")
    assert line.rstrip("\n").endswith("⏳ 12s")


# ── The ticker ────────────────────────────────────────────────────────────────
class Timers:
    """Captures app.after(...) instead of scheduling it, so the self-arming
    ticker can be stepped one tick at a time."""

    def __init__(self, app, monkeypatch):
        self.calls = []
        monkeypatch.setattr(
            app, "after",
            lambda ms, fn=None, *args: self.calls.append((ms, fn, args))
            or "after#0")

    @property
    def delays(self):
        return [ms for ms, _fn, _a in self.calls]


@pytest.fixture
def running(cb_mod, app, monkeypatch):
    """One active row in a two-row queue, throttle off, a batch 'in flight'.

    Returns (timers, row) where row is the live dict for queue row 0.
    """
    app._sleep_enabled.set(False)
    app._build_queue_ui([{"title": "Deonite - Light"}, {"title": "Next Up"}],
                        "track")
    app._set_row_state(0, cb_mod.ST_ACTIVE)
    app._downloading = True
    timers = Timers(app, monkeypatch)
    return timers, app._queue[0]


def _row_text(app, idx=0):
    return app._qtxt.get(f"{idx+1}.0", f"{idx+1}.end")


def test_the_ticker_paints_a_tick_then_clears_it_once_bytes_arrive(cb_mod, app,
                                                                   running):
    timers, row = running
    app._stall = (0, "connecting", time.monotonic() - 12)
    app._stall_tick()
    assert row["stall"] == "⏳ 12s"
    assert "⏳ 12s" in _row_text(app)

    app._stall = (0, "flowing", time.monotonic())
    app._stall_tick()
    assert row["stall"] == ""
    assert "⏳" not in _row_text(app)


def test_the_ticker_stays_silent_inside_the_grace(cb_mod, app, running):
    timers, row = running
    app._stall = (0, "connecting", time.monotonic() - 1)
    app._stall_tick()
    assert row["stall"] == ""
    assert "⏳" not in _row_text(app)


def test_conversion_gets_no_counter(cb_mod, app, running):
    """Nothing reports in between 'finished' and FFmpeg completing, so a
    counter there would flag a perfectly healthy track."""
    timers, row = running
    app._stall = (0, "converting", time.monotonic() - 300)
    app._stall_tick()
    assert row["stall"] == ""


def test_the_ticker_never_scrolls_the_queue(cb_mod, app, running,
                                            monkeypatch):
    """The load-bearing one: a once-a-second repaint that yanked the viewport
    would make the queue unreadable for the whole download."""
    scrolls = []
    monkeypatch.setattr(app, "_scroll_to_row", lambda i: scrolls.append(i))
    timers, row = running
    for ago in (12, 13, 14):
        app._stall = (0, "connecting", time.monotonic() - ago)
        app._stall_tick()
    assert row["stall"] == "⏳ 14s"
    assert scrolls == []
    assert 80 not in timers.delays


def test_the_ticker_rearms_itself_every_second(cb_mod, app, running):
    timers, _row = running
    app._stall = (0, "connecting", time.monotonic())
    app._stall_tick()
    assert timers.delays == [1000]
    assert app._stall_after_id == "after#0"


def test_the_ticker_stops_dead_when_the_batch_is_over(cb_mod, app, running):
    timers, _row = running
    app._downloading = False
    app._stall = (0, "connecting", time.monotonic() - 12)
    app._stall_tick()
    assert timers.calls == []
    assert app._stall_after_id is None


def test_a_repeated_tick_repaints_only_when_the_string_changes(cb_mod, app,
                                                               running,
                                                               monkeypatch):
    """A healthy download must cost one comparison a second, not a Text rewrite."""
    painted = []
    real_render = app._render_row
    monkeypatch.setattr(app, "_render_row",
                        lambda i: (painted.append(i), real_render(i))[1])
    timers, _row = running
    since = time.monotonic() - 12
    app._stall = (0, "connecting", since)
    app._stall_tick()
    app._stall_tick()
    app._stall_tick()
    assert painted == [0]


@pytest.mark.parametrize("terminal, note", [
    ("ST_DONE", "✓ done"),
    ("ST_SKIPPED", "skipped"),
    ("ST_ERROR", "HTTP 403"),
])
def test_a_settled_row_is_left_alone(cb_mod, app, running, monkeypatch,
                                     terminal, note):
    timers, row = running
    app._set_row_state(0, getattr(cb_mod, terminal), note)
    row["stall"] = "SENTINEL"
    painted = []
    monkeypatch.setattr(app, "_render_row", lambda i: painted.append(i))
    app._stall = (0, "connecting", time.monotonic() - 90)
    app._stall_tick()
    assert row["stall"] == "SENTINEL"
    assert painted == []
    assert note in _row_text(app)


def test_a_beat_past_the_end_of_a_rebuilt_queue_is_a_no_op(cb_mod, app,
                                                           running):
    timers, _row = running
    app._stall = (7, "connecting", time.monotonic() - 90)
    app._stall_tick()
    assert [r["stall"] for r in app._queue] == ["", ""]


def test_settling_a_row_drops_its_tick(cb_mod, app, running):
    timers, row = running
    app._stall = (0, "connecting", time.monotonic() - 12)
    app._stall_tick()
    assert row["stall"] == "⏳ 12s"
    app._set_row_state(0, cb_mod.ST_ERROR, "")
    assert row["stall"] == ""
    assert "⏳" not in _row_text(app)


def test_the_bitrate_column_does_not_erase_the_tick(cb_mod, app, running):
    """_set_row_bitrate re-renders the whole line; the tick lives on the row
    dict precisely so that re-render cannot lose it."""
    timers, row = running
    app._stall = (0, "connecting", time.monotonic() - 12)
    app._stall_tick()
    app._set_row_bitrate(0, "160k → 192k")
    assert row["stall"] == "⏳ 12s"
    line = _row_text(app)
    assert "160k → 192k" in line
    assert "⏳ 12s" in line


# ── The grace ─────────────────────────────────────────────────────────────────
def test_the_throttle_sleep_is_absorbed_into_the_grace(cb_mod, app):
    """A configured 3–8 s delay between tracks is not a stall; without this
    term every throttled download would report a wait we asked for ourselves."""
    app._sleep_enabled.set(False)
    assert app._stall_grace() == cb_mod.STALL_GRACE_SECONDS

    app._sleep_enabled.set(True)
    app._sleep_mode.set("Manual")
    app._sleep_min.set(3)
    app._sleep_max.set(11)
    assert app._stall_grace() == cb_mod.STALL_GRACE_SECONDS + 11


def test_a_throttled_track_stays_quiet_until_past_its_own_delay(cb_mod, app,
                                                                running):
    timers, row = running
    app._sleep_enabled.set(True)
    app._sleep_mode.set("Manual")
    app._sleep_min.set(1)
    app._sleep_max.set(20)

    app._stall = (0, "connecting", time.monotonic() - 12)
    app._stall_tick()
    assert row["stall"] == ""

    app._stall = (0, "connecting", time.monotonic() - 25)
    app._stall_tick()
    assert row["stall"] == "⏳ 25s"


# ── Session wiring ────────────────────────────────────────────────────────────
def test_a_new_batch_arms_the_ticker_and_finishing_disarms_it(cb_mod, app,
                                                              monkeypatch):
    cancelled = []
    monkeypatch.setattr(app, "after_cancel", lambda i: cancelled.append(i))
    timers = Timers(app, monkeypatch)
    app._begin_download_session("Preparing batch…")
    assert app._stall_after_id == "after#0"
    assert 1000 in timers.delays

    app._stall = (0, "connecting", time.monotonic())
    app._finish()
    assert app._stall_after_id is None
    assert app._stall is None
    assert cancelled == ["after#0"]


def test_quitting_cancels_the_ticker(cb_mod, app, monkeypatch):
    cancelled = []
    monkeypatch.setattr(app, "after_cancel", lambda i: cancelled.append(i))
    monkeypatch.setattr(app, "destroy", lambda: None)
    app._auto_dl_after_id = None
    app._stall_after_id = "tick#1"
    app._quit_app()
    assert "tick#1" in cancelled


# ── What the first adversarial pass got past ──────────────────────────────────
def test_a_beat_does_not_outlive_the_queue_it_indexes(cb_mod, app, monkeypatch):
    """The one that bit. A batch clears the queue between every URL and row
    indices restart at 0, so a beat left over from the previous URL's last
    failed track would paint its multi-minute wait onto the next URL's first
    row — a false stall on a download that had only just started."""
    app._sleep_enabled.set(False)
    app._build_queue_ui([{"title": "URL A track"}], "track")
    app._set_row_state(0, cb_mod.ST_ACTIVE)
    app._downloading = True
    app._stall = (0, "connecting", time.monotonic() - 300)
    app._set_row_state(0, cb_mod.ST_ERROR, "HTTP 403")

    app._build_queue_ui([{"title": "URL B track"}], "track")
    app._set_row_state(0, cb_mod.ST_ACTIVE)
    assert app._stall is None

    Timers(app, monkeypatch)
    app._stall_tick()
    assert app._queue[0]["stall"] == ""
    assert "⏳" not in _row_text(app)


def test_arming_a_second_time_does_not_leak_the_first_tick(app, monkeypatch):
    """Two sessions can overlap — _start opens a modal between its
    _downloading check and _begin_download_session, and the auto-download
    timer can fire in that gap. A second arm must not orphan the first
    after id, which nothing would ever cancel."""
    timers = Timers(app, monkeypatch)
    app._stall_start()
    app._stall_start()
    app._stall_start()
    assert timers.delays == [1000]


def test_the_row_colour_survives_the_render_extraction(cb_mod, app):
    """_render_row owns the colour tag for every writer now, and a row whose
    tag never changed would be silently uniform grey."""
    app._build_queue_ui([{"title": f"T{i}"} for i in range(5)], "track")
    expected = {
        cb_mod.ST_PENDING: "q_pending",
        cb_mod.ST_ACTIVE:  "q_active",
        cb_mod.ST_DONE:    "q_done",
        cb_mod.ST_SKIPPED: "q_skipped",
        cb_mod.ST_ERROR:   "q_error",
    }
    for idx, (state, tag) in enumerate(expected.items()):
        app._set_row_state(idx, state, "")
        assert tag in app._qtxt.tag_names(f"{idx+1}.0"), (state, tag)
    # A bitrate update repaints the same row and must keep its tag.
    app._set_row_bitrate(1, "160k → 192k")
    assert expected[cb_mod.ST_ACTIVE] in app._qtxt.tag_names("2.0")


@pytest.mark.parametrize("elapsed, expected", [
    (5999, "⏳ 99:59"),
    (6000, "⏳ 99:59+"),
    (6060, "⏳ 99:59+"),
])
def test_the_counter_saturates_rather_than_widening(cb_mod, elapsed, expected):
    assert cb_mod.MP3DownloaderApp.connect_wait_text(elapsed, 3) == expected


def test_a_negative_row_index_is_rejected(cb_mod, app, running, monkeypatch):
    timers, _row = running
    painted = []
    monkeypatch.setattr(app, "_render_row", lambda i: painted.append(i))
    app._stall = (-1, "connecting", time.monotonic() - 90)
    app._stall_tick()
    assert painted == []


@pytest.mark.parametrize("call", [
    lambda app, cb_mod: app._set_row_state(4, cb_mod.ST_DONE, "✓ done"),
    lambda app, cb_mod: app._set_row_bitrate(4, "160k → 192k"),
])
def test_a_late_callback_cannot_write_past_the_end_of_the_queue(cb_mod, app,
                                                                call):
    """after(0) callbacks from URL A's tracks can land once URL B's shorter
    queue is already up; the bounds guards are what stops them."""
    app._build_queue_ui([{"title": "Only one"}], "track")
    before = app._qtxt.get("1.0", "end")
    call(app, cb_mod)
    assert app._qtxt.get("1.0", "end") == before
    assert len(app._queue) == 1


def test_a_line_break_in_a_title_cannot_grow_the_queue(cb_mod, app, running):
    """SoundCloud titles are free-form user text. A repaint deletes one logical
    line and inserts what it built, so a title carrying its own newline used to
    add a line per repaint — once a second, for the whole connect wait."""
    timers, row = running
    lines = app._qtxt.index("end")
    row["title"] = "Bad\nTitle\r\nWorse"
    for ago in (12, 13, 14, 15):
        app._stall = (0, "connecting", time.monotonic() - ago)
        app._stall_tick()
    assert app._qtxt.index("end") == lines      # four repaints, no new lines
    assert "Bad Title Worse" in _row_text(app)
    assert "⏳ 15s" in _row_text(app)
    assert "Next Up" in _row_text(app, 1)


def test_an_abandoned_row_does_not_keep_a_frozen_counter(cb_mod, app, running):
    """A batch broken out of mid-track leaves its row ACTIVE and unsettled;
    without the wipe the last number painted sits there forever."""
    timers, row = running
    app._stall = (0, "connecting", time.monotonic() - 42)
    app._stall_tick()
    assert row["stall"] == "⏳ 42s"
    app._stall_stop()
    assert row["stall"] == ""
    assert "⏳" not in _row_text(app)


def test_a_raise_inside_one_tick_does_not_kill_the_rest(app, monkeypatch):
    """The ticker is the only self-arming loop on the class that writes to a
    widget; a raise must cost one tick, not every remaining one."""
    app._downloading = True
    timers = Timers(app, monkeypatch)
    monkeypatch.setattr(app, "_stall_paint",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    app._stall_tick()
    assert timers.delays == [1000]
    assert app._stall_after_id == "after#0"

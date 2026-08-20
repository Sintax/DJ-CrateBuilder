"""The Tk end of a TrackDownloader's Sink: every widget write marshalled.

_TkDownloadSink is called from the batch worker thread and — for progress and
title_corrected — from yt-dlp's own thread inside the progress hook. A direct
widget touch from there is a real crash, so every test here first asserts that
the call changed nothing at all, then runs the callbacks the sink handed to
`after(0, …)` and asserts what they did.
"""
import pytest


class Deferred:
    """Captures app.after(...) instead of scheduling it.

    Only the 0-delay callbacks are replayed: _set_row_state schedules its own
    after(80, scroll) and that is the app's business, not the sink's.
    """

    def __init__(self, app, monkeypatch):
        self.calls = []
        monkeypatch.setattr(
            app, "after",
            lambda ms, fn=None, *args: self.calls.append((ms, fn, args)))

    @property
    def scheduled(self):
        return [c for c in self.calls if c[0] == 0]

    def run(self):
        pending, self.calls = self.calls, []
        for ms, fn, args in pending:
            if ms == 0 and fn is not None:
                fn(*args)


@pytest.fixture
def wired(cb_mod, app, monkeypatch):
    """A sink over row 0 of a two-row queue, with after() captured.

    Returns (sink, deferred). The current-track label is pre-set to a sentinel
    so "nothing happened yet" is an assertable state.
    """
    app._build_queue_ui([{"title": "Placeholder"}, {"title": "Other"}], "track")
    app._cur_lbl.config(text="sentinel")
    app._speed_lbl.config(text="sentinel")
    app._bitrate_lbl.config(text="sentinel")
    app._vid_progress.config(value=17)
    deferred = Deferred(app, monkeypatch)
    return cb_mod._TkDownloadSink(app, 0), deferred


def test_started_names_the_track_and_zeroes_the_bar(wired, app):
    sink, deferred = wired
    sink.started("Real Title")
    assert app._cur_lbl.cget("text") == "sentinel"
    deferred.run()
    assert app._cur_lbl.cget("text") == "Real Title"
    assert app._vid_progress.cget("value") == 0


def test_bitrate_detected_labels_both_the_header_and_the_row(wired, app):
    sink, deferred = wired
    sink.bitrate_detected(160, "192")
    assert app._bitrate_lbl.cget("text") == "sentinel"
    deferred.run()
    assert app._bitrate_lbl.cget("text") == "src 160k → 192k"
    assert app._queue[0]["bitrate"] == "160k → 192k"


def test_progress_moves_the_bar_and_the_speed_label(wired, app):
    sink, deferred = wired
    sink.progress(percent=42.5, speed_text="1.20MiB/s  00:09")
    assert app._vid_progress.cget("value") == 17
    deferred.run()
    assert app._vid_progress.cget("value") == pytest.approx(42.5)
    assert app._speed_lbl.cget("text") == "1.20MiB/s  00:09"


def test_progress_with_nothing_to_say_schedules_nothing(wired, app):
    sink, deferred = wired
    sink.progress(percent=None, speed_text="")
    assert deferred.scheduled == []
    # …but the hook firing at all is still a sign of life for the queue's
    # connect-wait counter, and recording it costs no callback.
    assert app._stall[1] == "flowing"


def test_progress_reports_zero_percent_rather_than_swallowing_it(wired, app):
    """0.0 is a real percent — the falsy-value bug this guards against would
    leave the bar showing the previous track's position."""
    sink, deferred = wired
    sink.progress(percent=0.0, speed_text="")
    deferred.run()
    assert app._vid_progress.cget("value") == 0


def test_finished_shows_the_conversion_step(wired, app):
    sink, deferred = wired
    sink.finished()
    deferred.run()
    assert app._vid_progress.cget("value") == 95
    assert app._speed_lbl.cget("text") == "converting…"


def test_title_corrected_is_the_only_writer_of_the_queue_title(wired, app,
                                                               cb_mod):
    """The queue row title used to be assigned straight from the yt-dlp thread.
    Now it changes here, on the main thread, together with the row re-render and
    the current-track label."""
    sink, deferred = wired
    sink.title_corrected("The Real File Name")
    assert app._queue[0]["title"] == "Placeholder"
    deferred.run()
    assert app._queue[0]["title"] == "The Real File Name"
    assert app._cur_lbl.cget("text") == "The Real File Name"
    assert app._queue[0]["state"] == cb_mod.ST_ACTIVE
    assert app._queue[1]["title"] == "Other"


def test_title_corrected_past_the_end_of_the_queue_is_a_no_op(cb_mod, app,
                                                              monkeypatch):
    """A queue rebuilt shorter mid-run must not raise on the worker thread."""
    app._build_queue_ui([{"title": "Only"}], "track")
    deferred = Deferred(app, monkeypatch)
    cb_mod._TkDownloadSink(app, 7).title_corrected("nope")
    deferred.run()
    assert [row["title"] for row in app._queue] == ["Only"]


# ── The connect-wait beat ─────────────────────────────────────────────────────
# _beat is the one write here that deliberately does NOT go through after(0):
# a single store of an immutable tuple cannot tear, and marshalling it would
# queue a callback per progress chunk. These pin that, and the phase each
# event reports.
@pytest.mark.parametrize("event, args, phase", [
    ("started", ("Real Title",), "connecting"),
    ("bitrate_detected", (160, "192"), "flowing"),
    ("progress", (42.5, "1.20MiB/s"), "flowing"),
    ("title_corrected", ("The Real File Name",), "flowing"),
    ("finished", (), "converting"),
])
def test_every_event_beats_its_phase_without_being_marshalled(wired, app,
                                                              event, args,
                                                              phase):
    sink, deferred = wired
    app._stall = None
    getattr(sink, event)(*args)
    assert app._stall is not None, "the beat has to land before after(0) runs"
    idx, seen, since = app._stall
    assert (idx, seen) == (0, phase)
    assert isinstance(since, float)


def test_the_beat_uses_a_monotonic_clock_not_the_wall_clock(wired, app):
    """A clock change mid-download must not be able to invent an hour-long
    wait, so the beat is stamped with monotonic()."""
    import time
    before = time.monotonic()
    sink, _deferred = wired
    sink.started("Real Title")
    after = time.monotonic()
    assert before <= app._stall[2] <= after


def test_a_later_event_overwrites_the_earlier_beat(wired, app):
    sink, _deferred = wired
    sink.started("Real Title")
    assert app._stall[1] == "connecting"
    sink.progress(percent=1.0)
    assert app._stall[1] == "flowing"
    sink.finished()
    assert app._stall[1] == "converting"

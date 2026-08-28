"""The `notification` event: what the host announces, and in what shape.

The web bell (design 3n) and the Overview's Recent activity are both fed
entirely by this event, so a run that ends without emitting one is invisible
to a client that is not watching the screen it ran on.
"""
import time

import pytest

from cratebuilder.batchrun import BatchRunner
from cratebuilder.settings import Settings
from cratebuilder.ydl import YdlPermanent

from tests.test_watchrun import FakeSession, Harness


def notes(recorder):
    return recorder.of("notification")


def check_shape(note):
    """The contract's shape: {level, title, body, at} plus the job it came
    from, which is what the bell's jump link is derived from."""
    assert set(note) >= {"level", "title", "body", "at", "job"}
    assert note["level"] in ("info", "warn", "error")
    assert note["title"] and note["body"]
    assert "T" in note["at"]                  # an ISO timestamp, not a float


# ── Watch List ───────────────────────────────────────────────────────────────

def test_a_scan_announces_what_it_found(tmp_path):
    session = FakeSession(listing=[
        {"id": "a1", "title": "One", "url": "https://y/1", "upload_date": "20260101"},
        {"id": "a2", "title": "Two", "url": "https://y/2", "upload_date": "20260102"},
    ])
    h = Harness(tmp_path, session=session)
    cid = h.add_channel()
    h.ops.run_scan([cid])
    found = notes(h.emit)
    assert len(found) == 1
    check_shape(found[0])
    assert found[0]["job"] == "watchlist"
    assert found[0]["title"] == "Watch List scan"
    assert "2 new" in found[0]["body"]
    assert "1 channel" in found[0]["body"]


def test_a_scan_that_found_nothing_still_announces(tmp_path):
    """Silence is indistinguishable from a scan that never ran."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    h.ops.run_scan([h.add_channel()])
    found = notes(h.emit)
    assert len(found) == 1
    assert "0 new" in found[0]["body"]
    assert found[0]["level"] == "info"          # a clean scan is routine


def test_a_scan_whose_channels_errored_asks_to_be_looked_at(tmp_path):
    """"0 new across 0 channels" at info is indistinguishable from a quiet
    morning, and it is exactly what a scan that could not read a single channel
    would otherwise report — the same escalation run_download and
    BatchRunner._finish make."""
    h = Harness(tmp_path, session=FakeSession(error=YdlPermanent(
        "channel is gone", intent="list_channel", target="https://y/c")))
    h.ops.run_scan([h.add_channel()])
    found = notes(h.emit)
    assert len(found) == 1
    assert found[0]["level"] == "warn"
    assert "1 channel failed" in found[0]["body"]


def test_a_partly_failed_scan_reports_both_halves(tmp_path):
    """What it managed AND what it lost — a run that only reported its failures
    would hide the tracks the good channels found."""
    good = FakeSession(listing=[
        {"id": "d1", "title": "One", "url": "https://y/1", "upload_date": "20260101"},
    ])
    h = Harness(tmp_path, session=good)
    ok = h.add_channel(url="https://www.youtube.com/channel/UCok/videos",
                       name="Fine", channel_id="UCok")
    bad = h.add_channel(url="https://www.youtube.com/channel/UCbad/videos",
                        name="Broken", channel_id="UCbad")

    real = good.list_channel

    def flaky(url, ignore_no_formats=False):
        if "UCbad" in url:
            raise YdlPermanent("gone", intent="list_channel", target=url)
        return real(url, ignore_no_formats)

    good.list_channel = flaky
    h.ops.run_scan([ok, bad])
    note = notes(h.emit)[0]
    assert note["level"] == "warn"
    assert note["body"] == "1 new across 1 channel, 1 channel failed"


def test_a_channel_that_only_needs_its_link_fixed_is_not_a_failed_scan(tmp_path):
    """An unresolved channel is a gap the card already shows a Fix Link button
    for, not the run failing — escalating on it would make every Watch List
    with one unresolved entry cry wolf on every scan."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    cid = h.db.add_watchlist_channel(
        url="unresolved://Garage Archive", display_name="Garage Archive",
        platform="YouTube", genre="House")
    h.ops.run_scan([cid])
    note = notes(h.emit)[0]
    assert note["level"] == "info"
    assert "failed" not in note["body"]


def test_the_failure_tally_does_not_leak_into_the_next_run(tmp_path):
    """_begin resets it, so a clean scan after a broken one reports clean."""
    session = FakeSession(error=YdlPermanent(
        "gone", intent="list_channel", target="https://y/c"))
    h = Harness(tmp_path, session=session)
    cid = h.add_channel()
    h.ops.run_scan([cid])
    assert notes(h.emit)[-1]["level"] == "warn"
    session.error = None
    h.emit.events.clear()
    h.ops.run_scan([cid])
    again = notes(h.emit)[0]
    assert again["level"] == "info"
    assert "failed" not in again["body"]


def test_a_watch_list_download_announces_its_tally(tmp_path):
    session = FakeSession(listing=[
        {"id": "b1", "title": "Track One", "url": "https://y/1",
         "upload_date": "20260101"},
    ])
    h = Harness(tmp_path, session=session)
    cid = h.add_channel()
    h.ops.run_scan([cid])
    h.emit.events.clear()
    h.ops.run_download([cid])
    found = notes(h.emit)
    assert len(found) == 1
    check_shape(found[0])
    assert found[0]["title"] == "Watch List download"
    assert "1 track downloaded" in found[0]["body"]


def test_a_watch_list_download_that_lost_channels_asks_to_be_looked_at(tmp_path):
    """`BatchRunner._finish` escalates a batch with failures to `warn`; a Watch
    List run that lost whole channels is the same kind of outcome and must not
    report as routine."""
    session = FakeSession(listing=[
        {"id": "b1", "title": "Track One", "url": "https://y/1",
         "upload_date": "20260101"},
    ])
    h = Harness(tmp_path, session=session)
    cid = h.add_channel()
    h.ops.run_scan([cid])
    h.emit.events.clear()

    def boom(*_a, **_k):
        raise RuntimeError("channel folder is gone")

    h.ops._download_channel = boom
    h.ops.run_download([cid])
    found = notes(h.emit)
    assert len(found) == 1
    assert found[0]["level"] == "warn"
    assert "1 channel failed" in found[0]["body"]


def test_the_announcement_lands_after_the_run_s_closing_log_line(tmp_path):
    """The pinned scan log's DONE line and the announcement say the same
    thing; the log line is the live one, so it must not arrive second."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    h.ops.run_scan([h.add_channel()])
    kinds = [t for t, _ in h.emit.events if t in ("scan.line", "notification")]
    assert kinds[-2:] == ["scan.line", "notification"]


# ── a crashed run must not announce a routine completion (M5) ───────────────

def _explode(_self, *_a, **_k):
    raise RuntimeError("the run itself came apart")


def test_a_scan_that_crashes_outright_announces_nothing(tmp_path):
    """`_start_job` publishes the failure at `error` level. A completion
    announcement from the same run would arrive first and read as success —
    "0 new across 0 channels" is indistinguishable from a quiet morning."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    cid = h.add_channel()
    h.ops._end = lambda: _explode(h.ops)
    with pytest.raises(RuntimeError):
        h.ops.run_scan([cid])
    assert notes(h.emit) == []


def test_a_download_run_that_crashes_outright_announces_nothing(tmp_path):
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    cid = h.add_channel()
    h.ops._end = lambda: _explode(h.ops)
    with pytest.raises(RuntimeError):
        h.ops.run_download([cid])
    assert notes(h.emit) == []


def test_a_cancelled_run_is_not_a_crashed_one_and_still_announces(tmp_path):
    """Cancel returns through the normal path, so what the run managed before
    it stopped is still worth reporting."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    cid = h.add_channel()
    h.ops.cancel_all()
    h.ops.run_scan([cid])
    found = notes(h.emit)
    assert len(found) == 1
    assert found[0]["title"] == "Watch List scan"


# ── Main-tab batch ───────────────────────────────────────────────────────────

class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, type, payload):
        self.events.append((type, payload))

    def of(self, type):
        return [p for t, p in self.events if t == type]


def _runner(tmp_path, emit):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return BatchRunner(settings, None, emit)


def test_a_finished_batch_announces_its_tally(tmp_path):
    emit = _Sink()
    runner = _runner(tmp_path, emit)
    runner._downloaded, runner._skipped, runner._errors = 4, 1, 0
    runner._finish()
    found = emit.of("notification")
    assert len(found) == 1
    check_shape(found[0])
    assert found[0]["job"] == "batch"
    assert found[0]["level"] == "info"
    assert found[0]["title"] == "Batch complete"
    assert found[0]["body"] == "4 downloaded, 1 skipped, 0 failed"


def test_a_batch_with_failures_asks_to_be_looked_at(tmp_path):
    emit = _Sink()
    runner = _runner(tmp_path, emit)
    runner._downloaded, runner._skipped, runner._errors = 2, 0, 3
    runner._finish()
    assert emit.of("notification")[0]["level"] == "warn"


def test_a_cancelled_batch_says_so(tmp_path):
    emit = _Sink()
    runner = _runner(tmp_path, emit)
    runner._downloaded = 1
    runner.cancel()
    runner._finish()
    note = emit.of("notification")[0]
    assert note["title"] == "Batch cancelled"
    assert note["level"] == "warn"


def test_the_batch_announcement_follows_its_own_terminal_event(tmp_path):
    """`batch.finished` is the tally the Downloads screen reads; the
    announcement is the same tally for everyone not looking at it. Ordering
    matters only in that the display event is never late."""
    emit = _Sink()
    runner = _runner(tmp_path, emit)
    runner._finish()
    kinds = [t for t, _ in emit.events]
    assert kinds.index("batch.finished") < kinds.index("notification")


def test_a_watch_list_channel_s_own_runner_does_not_announce(tmp_path):
    """WatchlistOps drives a BatchRunner per channel through run_tracks, which
    never reaches _finish — one Watch List run is ONE announcement, not one
    per channel."""
    session = FakeSession(listing=[
        {"id": "c1", "title": "T1", "url": "https://y/1", "upload_date": "20260101"},
    ])
    h = Harness(tmp_path, session=session)
    cids = [h.add_channel(url="https://www.youtube.com/channel/UCa/videos",
                          name="A", channel_id="UCa"),
            h.add_channel(url="https://www.youtube.com/channel/UCb/videos",
                          name="B", channel_id="UCb")]
    h.ops.run_scan(cids)
    h.emit.events.clear()
    h.ops.run_download(cids)
    assert len(notes(h.emit)) == 1


# ── a crashed job still announces (Task 10's path, guarded here too) ─────────

def test_a_job_that_raises_announces_at_error_level(tmp_path):
    from cratebuilder.service import CrateBuilderService

    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    svc = CrateBuilderService(transport="local", settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"),
                              log_path=str(tmp_path / "activity.log"),
                              debug_log_path=str(tmp_path / "debug.log"))
    seen = []
    svc.events.subscribe(lambda t, p: seen.append((t, p)))

    def boom():
        raise RuntimeError("the wheels came off")

    svc._start_job("batch", boom)
    for _ in range(200):
        if any(t == "job.finished" for t, _ in seen):
            break
        time.sleep(0.02)
    svc._emit.flush()
    found = [p for t, p in seen if t == "notification"]
    assert found
    check_shape(found[0])
    assert found[0]["level"] == "error"
    assert "wheels came off" in found[0]["body"]

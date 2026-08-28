"""The `notification` event: what the host announces, and in what shape.

The web bell (design 3n) and the Overview's Recent activity are both fed
entirely by this event, so a run that ends without emitting one is invisible
to a client that is not watching the screen it ran on.
"""
import time

from cratebuilder.batchrun import BatchRunner
from cratebuilder.download import Outcome
from cratebuilder.settings import Settings

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


def test_the_announcement_lands_after_the_run_s_closing_log_line(tmp_path):
    """The pinned scan log's DONE line and the announcement say the same
    thing; the log line is the live one, so it must not arrive second."""
    h = Harness(tmp_path, session=FakeSession(listing=[]))
    h.ops.run_scan([h.add_channel()])
    kinds = [t for t, _ in h.emit.events if t in ("scan.line", "notification")]
    assert kinds[-2:] == ["scan.line", "notification"]


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

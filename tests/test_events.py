"""EventBus, Coalescer, and the CrateBuilderService job registry."""

import threading

import pytest

from cratebuilder.events import Coalescer, EventBus
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


class FakeClock:
    """A controllable clock so Coalescer tests never need to sleep."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ── EventBus ─────────────────────────────────────────────────────────────────

def test_subscriber_receives_emit():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append((t, p)))
    bus.emit("progress.current", {"pct": 10})
    assert received == [("progress.current", {"pct": 10})]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    received = []
    unsubscribe = bus.subscribe(lambda t, p: received.append((t, p)))
    unsubscribe()
    bus.emit("x", {})
    assert received == []


def test_raising_subscriber_does_not_break_others():
    bus = EventBus()
    received = []

    def bad(t, p):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda t, p: received.append((t, p)))
    bus.emit("x", {"a": 1})
    assert received == [("x", {"a": 1})]


def test_multiple_subscribers_all_receive():
    bus = EventBus()
    a, b = [], []
    bus.subscribe(lambda t, p: a.append(p))
    bus.subscribe(lambda t, p: b.append(p))
    bus.emit("x", 1)
    assert a == [1] and b == [1]


# ── Coalescer ────────────────────────────────────────────────────────────────

def test_coalescer_passes_first_event_immediately():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append((t, p)))
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=FakeClock())
    coalescer.emit("progress.current", {"pct": 1})
    assert received == [("progress.current", {"pct": 1})]


def test_coalescer_swallows_intermediate_events():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append(p))
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=FakeClock())
    coalescer.emit("progress.current", {"pct": 1})
    coalescer.emit("progress.current", {"pct": 2})
    coalescer.emit("progress.current", {"pct": 3})
    assert received == [{"pct": 1}]


def test_coalescer_flush_delivers_the_last_pending_payload():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append(p))
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=FakeClock())
    coalescer.emit("progress.current", {"pct": 1})
    coalescer.emit("progress.current", {"pct": 2})
    coalescer.emit("progress.current", {"pct": 3})
    coalescer.flush()
    assert received == [{"pct": 1}, {"pct": 3}]


def test_coalescer_flush_with_nothing_pending_is_a_noop():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append(p))
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=FakeClock())
    coalescer.emit("progress.current", {"pct": 1})
    coalescer.flush()
    assert received == [{"pct": 1}]


def test_coalescer_forwards_again_once_interval_elapses():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append(p))
    clock = FakeClock()
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=clock)
    coalescer.emit("progress.current", {"pct": 1})
    clock.advance(0.3)
    coalescer.emit("progress.current", {"pct": 2})
    assert received == [{"pct": 1}, {"pct": 2}]


def test_non_coalesced_types_bypass_the_interval():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append(p))
    coalescer = Coalescer(bus, coalesced_types=("progress.current",),
                          interval=0.25, now=FakeClock())
    coalescer.emit("job.done", {"ok": True})
    coalescer.emit("job.done", {"ok": True})
    assert received == [{"ok": True}, {"ok": True}]


def test_coalescer_defaults_cover_progress_current_and_overall():
    bus = EventBus()
    received = []
    bus.subscribe(lambda t, p: received.append((t, p)))
    coalescer = Coalescer(bus, now=FakeClock())
    coalescer.emit("progress.current", 1)
    coalescer.emit("progress.current", 2)
    coalescer.emit("progress.overall", 1)
    coalescer.emit("progress.overall", 2)
    assert received == [("progress.current", 1), ("progress.overall", 1)]


# ── job registry ─────────────────────────────────────────────────────────────

@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def test_start_job_returns_an_id(service):
    done = threading.Event()
    job_id = service._start_job("batch", done.wait)
    try:
        assert job_id is not None
    finally:
        done.set()


def test_double_start_same_category_raises_user_facing_cberror(service):
    started = threading.Event()
    release = threading.Event()

    def target():
        started.set()
        release.wait(2)

    service._start_job("batch", target)
    assert started.wait(2)
    with pytest.raises(CBError, match="already running"):
        service._start_job("batch", target)
    release.set()


def test_different_categories_run_concurrently(service):
    started_a = threading.Event()
    started_b = threading.Event()
    release = threading.Event()

    def target(started):
        started.set()
        release.wait(2)

    service._start_job("batch", target, started_a)
    service._start_job("watchlist", target, started_b)
    assert started_a.wait(2)
    assert started_b.wait(2)
    assert service._job_running("batch")
    assert service._job_running("watchlist")
    assert not service._job_running("maintenance")
    release.set()


def test_finished_job_frees_the_category(service):
    job_id = service._start_job("batch", lambda: None)
    assert job_id is not None
    for _ in range(100):
        if not service._job_running("batch"):
            break
        threading.Event().wait(0.01)
    assert not service._job_running("batch")


def test_snapshot_running_reflects_the_job_registry(service):
    assert service.snapshot()["running"] == {
        "batch": False, "watchlist": False, "maintenance": False}
    release = threading.Event()
    started = threading.Event()

    def target():
        started.set()
        release.wait(2)

    service._start_job("watchlist", target)
    assert started.wait(2)
    assert service.snapshot()["running"] == {
        "batch": False, "watchlist": True, "maintenance": False}
    release.set()

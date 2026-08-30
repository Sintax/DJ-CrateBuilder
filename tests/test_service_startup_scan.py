"""CrateBuilderService.start_startup_scan: the launch scan the Watch List owes.

The monolith arms this in __init__ (after(2200, _watchlist_startup_scan)) and
the web port never inherited it — the setting persisted and nothing read it.
Nothing here may sleep or touch the network: the real cold-boot guard is a
~90 s window, so both delays are patched to zero and the reachability probe is
fed a fixed sequence of answers.
"""

import threading

import pytest

from cratebuilder import service as service_mod
from cratebuilder import ydl as cb_ydl
from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import REMOTE, CBError, CrateBuilderService
from cratebuilder.settings import Settings

SCANNING = "🚀 Startup check: scanning all channels…"
WAITING = "🌐 Waiting for the network before the startup scan…"
SKIPPED = "Startup scan skipped — no network detected."


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    monkeypatch.setattr(service_mod, "WATCHLIST_STARTUP_DELAY", 0)
    monkeypatch.setattr(service_mod, "WATCHLIST_STARTUP_NET_DELAY", 0)


def _build(tmp_path, transport="local", channels=1, **config):
    """A service pointed entirely at tmp_path, with *channels* watched rows.

    channels=0 leaves the database file absent, which is also the "no
    database yet" case _watchlist_rows has to tolerate.
    """
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    for key, value in config.items():
        settings.set(key, value)
    db_path = str(tmp_path / "cratebuilder.db")
    for index in range(channels):
        DownloadsDatabase(db_path).add_watchlist_channel(
            url=f"https://yt/c{index}", display_name=f"Channel {index}",
            platform="YouTube", genre="House")
    return CrateBuilderService(
        transport=transport, settings=settings, db_path=db_path,
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"))


def _probe(monkeypatch, answers):
    """Feed network_is_reachable a fixed sequence; the last answer repeats."""
    remaining = list(answers)
    calls = []

    def probe(timeout=2.0):
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        calls.append(answer)
        return answer

    monkeypatch.setattr(cb_ydl, "network_is_reachable", probe)
    return calls


def _record_scan(service, monkeypatch, raises=None):
    scans = []

    def scan_all():
        scans.append(1)
        if raises is not None:
            raise raises
        return {"job_id": 1}

    monkeypatch.setattr(service, "watchlist_scan_all", scan_all)
    return scans


def _run(service):
    """Arm the scan and wait for its thread, rather than sleeping on it."""
    thread = service.start_startup_scan()
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive()
    return thread


def _log(tmp_path):
    path = tmp_path / "activity.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── what arms it, and what doesn't ───────────────────────────────────────────

def test_the_setting_gates_the_scan(tmp_path, monkeypatch):
    service = _build(tmp_path, watchlist_scan_on_startup=False)
    calls = _probe(monkeypatch, [True])
    scans = _record_scan(service, monkeypatch)

    assert service.start_startup_scan() is None
    assert (calls, scans) == ([], [])


def test_an_empty_watch_list_arms_nothing(tmp_path, monkeypatch):
    """The monolith's get_all_watchlist_channels gate — with no rows there is
    nothing to scan, and with no database at all nothing to read."""
    service = _build(tmp_path, channels=0)
    scans = _record_scan(service, monkeypatch)

    assert service.start_startup_scan() is None
    assert scans == []


def test_a_remote_mount_never_makes_the_host_scan(tmp_path, monkeypatch):
    """LOCAL only, the rule _arm_update_timer follows: a headless remote mount
    must not start work on the host just by being constructed."""
    service = _build(tmp_path, transport=REMOTE)
    scans = _record_scan(service, monkeypatch)

    assert service.start_startup_scan() is None
    assert scans == []


# ── the network wait ─────────────────────────────────────────────────────────

def test_it_scans_as_soon_as_the_network_answers(tmp_path, monkeypatch):
    service = _build(tmp_path)
    _probe(monkeypatch, [True])
    scans = _record_scan(service, monkeypatch)

    _run(service)

    assert scans == [1]
    log = _log(tmp_path)
    assert SCANNING in log
    assert WAITING not in log


def test_it_waits_for_the_network_and_says_so_once(tmp_path, monkeypatch):
    service = _build(tmp_path)
    calls = _probe(monkeypatch, [False, False, True])
    scans = _record_scan(service, monkeypatch)

    _run(service)

    assert calls == [False, False, True]
    assert scans == [1]
    assert _log(tmp_path).count(WAITING) == 1


def test_the_budget_runs_out_without_scanning(tmp_path, monkeypatch):
    """A cold boot that never comes online gives up quietly; the channels keep
    their links and a manual scan still works."""
    monkeypatch.setattr(service_mod, "WATCHLIST_STARTUP_NET_TRIES", 3)
    service = _build(tmp_path)
    calls = _probe(monkeypatch, [False])
    scans = _record_scan(service, monkeypatch)

    _run(service)

    assert calls == [False, False, False]
    assert scans == []
    assert SKIPPED in _log(tmp_path)
    assert SCANNING not in _log(tmp_path)


# ── giving way, and shutting down ────────────────────────────────────────────

def test_a_refusal_from_a_manual_run_is_swallowed(tmp_path, monkeypatch):
    """watchlist_scan_all raises CBError when the job slot is taken — a manual
    scan or download started while this waited. That is the startup scan
    giving way, not a failure, and it must not take the thread down."""
    service = _build(tmp_path)
    _probe(monkeypatch, [True])
    scans = _record_scan(service, monkeypatch,
                         raises=CBError("A watchlist job is already running."))
    caught = []
    monkeypatch.setattr(threading, "excepthook", lambda args: caught.append(args))

    _run(service)

    assert scans == [1]
    assert caught == []


def test_a_running_batch_download_holds_the_scan_back(tmp_path, monkeypatch):
    """The monolith's `self._downloading` half of the busy gate.

    A plain batch download holds no Watch List slot, so watchlist_scan_all
    would refuse nothing — and minutes can pass in the network wait, in which
    the user is not idle. The monolith skips its startup scan outright while a
    download runs; so does this."""
    service = _build(tmp_path)
    _probe(monkeypatch, [True])
    scans = _record_scan(service, monkeypatch)
    with service._lock:
        service._jobs["batch"] = 1

    try:
        _run(service)
    finally:
        with service._lock:
            service._jobs.pop("batch", None)

    assert scans == []
    assert SCANNING not in _log(tmp_path)


def test_closing_during_the_wait_stops_the_scan(tmp_path, monkeypatch):
    """close() while the thread is waiting for the network ends it, rather
    than scanning into a half-torn-down service."""
    service = _build(tmp_path)
    scans = _record_scan(service, monkeypatch)

    def probe(timeout=2.0):
        service.close()          # the window closed while we waited
        return True

    monkeypatch.setattr(cb_ydl, "network_is_reachable", probe)

    _run(service)

    assert scans == []
    assert SCANNING not in _log(tmp_path)


def test_a_closed_service_probes_nothing(tmp_path, monkeypatch):
    service = _build(tmp_path)
    calls = _probe(monkeypatch, [True])
    scans = _record_scan(service, monkeypatch)
    service.close()

    _run(service)

    assert (calls, scans) == ([], [])

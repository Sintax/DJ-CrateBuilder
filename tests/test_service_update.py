"""cratebuilder.service: the update.* methods and the local auto-check timer.

Everything network- or process-shaped is monkeypatched onto
cratebuilder.updater_core / subprocess — no test here reaches the network,
spawns a real updater, or writes outside tmp_path.
"""
import os
import threading
import time

import pytest

from cratebuilder import service as service_mod
from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import (LOCAL, REMOTE, CBError, CrateBuilderService,
                                  UPDATE_JOB)
from cratebuilder.settings import Settings

MANIFEST = {
    "build": 99, "url": "https://example.invalid/build-99.zip",
    "sha256": "a" * 64, "notes": "test build",
}


class _Waiter:
    """Collects every emitted event and can block for one job.finished."""

    def __init__(self, service):
        self.events = []
        self._done = threading.Event()
        service.events.subscribe(self._on)

    def _on(self, type_, payload):
        self.events.append((type_, payload))
        if type_ == "job.finished":
            self._done.set()

    def wait(self, timeout=5):
        assert self._done.wait(timeout), "job.finished never arrived"

    def of_type(self, type_):
        return [p for (t, p) in self.events if t == type_]


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A LOCAL service pointed entirely at tmp_path, with the update
    workspace redirected under tmp_path too — ucore.default_workspace()
    otherwise points at the developer's real LOCALAPPDATA."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    ws = tmp_path / "update-ws"
    monkeypatch.setattr(service_mod.ucore, "default_workspace",
                        lambda: str(ws))
    monkeypatch.setattr(service_mod.ucore, "install_dir",
                        lambda: str(tmp_path / "install"))
    yield svc
    svc.close()


def _fake_download(_unused=None, contents=b"zip-bytes"):
    def download(url, dest, progress_cb=None, timeout=30.0, _opener=None):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(contents)
        if progress_cb:
            progress_cb(len(contents) // 2, len(contents))
            progress_cb(len(contents), len(contents))
        return dest
    return download


# ── update.check ─────────────────────────────────────────────────────────────

def test_check_reports_available_when_manifest_is_newer(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 50})
    result = service.update_check()
    assert result["reachable"] is True
    assert result["valid"] is True
    assert result["available"] is True
    assert result["latest_build"] == 99
    assert result["current_build"] == 50
    assert result["notes"] == "test build"


def test_check_reports_current_when_not_newer(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {**MANIFEST, "build": 5})
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 50})
    result = service.update_check()
    assert result["reachable"] is True
    assert result["valid"] is True
    assert result["available"] is False
    # latest_build is reported whenever the manifest is reachable+valid, not
    # only when it's newer — the status line wants to say what build is live
    # either way. notes stays available-only (nothing to caption otherwise).
    assert result["latest_build"] == 5
    assert result["notes"] is None


def test_check_reports_unreachable(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: None)
    result = service.update_check()
    assert result == {
        "reachable": False, "valid": False, "available": False,
        "current_build": result["current_build"], "latest_build": None,
        "notes": None, "can_self_update": result["can_self_update"],
    }


def test_check_reports_invalid_manifest(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {"nope": "not a manifest"})
    result = service.update_check()
    assert result["reachable"] is True
    assert result["valid"] is False
    assert result["available"] is False


def test_check_persists_last_update_check(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: None)
    before = service._settings.get("last_update_check")
    service.update_check()
    after = service._settings.get("last_update_check")
    assert after not in (None, before)
    assert after <= time.time() + 1


def test_check_reports_unreachable_when_manifest_url_cannot_be_read(
        service, monkeypatch):
    """HIGH-3: an unparseable/missing monolith source means
    _update_manifest_url() returns None. update_check must report that as
    the ordinary "unreachable" result, never raise a bare TypeError out of
    fetch_manifest(None)."""
    monkeypatch.setattr(service_mod, "_manifest_urls", lambda: {})
    result = service.update_check()
    assert result["reachable"] is False
    assert result["valid"] is False
    assert result["available"] is False


def test_apply_raises_cberror_when_manifest_url_cannot_be_read(
        service, monkeypatch):
    """HIGH-3, the apply half: a CBError the frontend can toast, not a bare
    TypeError out of the RPC."""
    monkeypatch.setattr(service_mod, "_manifest_urls", lambda: {})
    with pytest.raises(CBError):
        service.update_apply()


# ── update.apply refusals ────────────────────────────────────────────────────

def test_apply_refuses_from_source(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: False)
    with pytest.raises(CBError, match="running from source"):
        service.update_apply()


def test_apply_refuses_on_linux(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: True)
    with pytest.raises(CBError, match="linux-v1.3"):
        service.update_apply()


def test_apply_refuses_no_update_available(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {**MANIFEST, "build": 1})
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 50})
    with pytest.raises(CBError, match="latest build"):
        service.update_apply()


def test_apply_refuses_unreachable_manifest(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: None)
    with pytest.raises(CBError, match="update server"):
        service.update_apply()


def test_remote_transport_refuses_update_methods(tmp_path):
    remote = CrateBuilderService(transport=REMOTE,
                                 settings=Settings(path=str(tmp_path / "c.json")),
                                 db_path=str(tmp_path / "db.sqlite"))
    try:
        for method in ("update.check", "update.apply", "update.status",
                      "update.set_interval"):
            with pytest.raises(CBError):
                remote.call(method)
    finally:
        remote.close()


# ── cross-job exclusion, both directions ─────────────────────────────────────

def test_apply_refuses_while_batch_running(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    with service._lock:
        service._jobs["batch"] = 1
    try:
        with pytest.raises(CBError, match="running"):
            service.update_apply()
    finally:
        with service._lock:
            service._jobs.pop("batch", None)


def test_batch_refuses_while_update_running(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    with service._lock:
        service._jobs[UPDATE_JOB] = 1
    try:
        with pytest.raises(CBError, match="restart"):
            service._require_idle_for_download()
    finally:
        with service._lock:
            service._jobs.pop(UPDATE_JOB, None)


def test_claim_tag_writes_refuses_while_update_runs(service):
    """CRITICAL-1, path 1: a Watch List genre-move retag moves the channel
    folder and rewrites its MP3s' ID3 frames with mutagen — exactly the
    write an update's restart-mid-swap must not land on top of."""
    with service._lock:
        service._jobs[UPDATE_JOB] = 1
    try:
        assert service.claim_tag_writes() is False
    finally:
        with service._lock:
            service._jobs.pop(UPDATE_JOB, None)


def test_repair_tags_refuses_while_update_runs(service):
    """CRITICAL-1, path 2: db.repair_tags is deliberately excluded from
    _MAINTENANCE_NEEDS_IDLE (it doesn't collide with a download on the
    downloads table), but it still saves ID3 frames in place with mutagen —
    the same write an update's file-swap-and-restart must not race."""
    with service._lock:
        service._jobs[UPDATE_JOB] = 1
    try:
        with pytest.raises(CBError, match="restart"):
            service.maintenance_start("db.repair_tags")
    finally:
        with service._lock:
            service._jobs.pop(UPDATE_JOB, None)


def test_watchlist_scan_refuses_while_update_runs(service):
    """CRITICAL-1, path 3: a scan shares WATCHLIST_JOB with a download, which
    only refuses another SAME-category claim — UPDATE_JOB is a different
    key, so scans need their own guard."""
    db = DownloadsDatabase(service._db_path)
    cid = db.add_watchlist_channel(url="https://example.test/@chan",
                                   display_name="Chan", platform="YouTube",
                                   genre="House")
    with service._lock:
        service._jobs[UPDATE_JOB] = 1
    try:
        with pytest.raises(CBError, match="restart"):
            service.watchlist_scan(cid)
        with pytest.raises(CBError, match="restart"):
            service.watchlist_scan_all()
    finally:
        with service._lock:
            service._jobs.pop(UPDATE_JOB, None)
    # The slot was never actually claimed by either refused call.
    assert not service._job_running(service_mod.WATCHLIST_JOB)


def test_repair_tags_still_refuses_a_live_retag(service):
    """The pre-existing retag-vs-repair exclusion must survive the reorder
    that moved the UPDATE_JOB check above the _MAINTENANCE_NEEDS_IDLE early
    return in _require_idle_library."""
    service._retags = 1
    try:
        with pytest.raises(CBError, match="tag"):
            service.maintenance_start("db.repair_tags")
    finally:
        service._retags = 0


def test_rebuild_unaffected_by_the_reorder(service):
    """db.rebuild IS in _MAINTENANCE_NEEDS_IDLE — a batch job still refuses
    it exactly as before the UPDATE_JOB check moved above that gate."""
    with service._lock:
        service._jobs["batch"] = 1
    try:
        with pytest.raises(CBError, match="download"):
            service.maintenance_start("db.rebuild")
    finally:
        with service._lock:
            service._jobs.pop("batch", None)


# ── update.apply happy path ──────────────────────────────────────────────────

def test_apply_happy_path(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: True)

    def fake_extract(zip_path, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "marker.txt"), "w") as fh:
            fh.write("staged")
        return dest_dir
    monkeypatch.setattr(service_mod.ucore, "extract_zip", fake_extract)

    popen_calls = []

    class _FakePopen:
        def __init__(self, cmd, **kw):
            popen_calls.append((cmd, kw))
    monkeypatch.setattr(service_mod.subprocess, "Popen", _FakePopen)

    restarted = []
    service.on_update_restart = lambda: restarted.append(True)

    waiter = _Waiter(service)
    result = service.update_apply()
    assert result["build"] == 99
    waiter.wait()

    finished = waiter.of_type("job.finished")
    assert finished and finished[-1]["ok"] is True
    assert finished[-1]["job"] == UPDATE_JOB

    # update.progress is coalesced (cratebuilder/events.py's
    # DEFAULT_COALESCED_TYPES): the first frame sends immediately, and any
    # in-between ones (the second download tick, "verify") that land inside
    # the coalescer's 0.25s window are superseded — the worker's own
    # flush() guarantees the final pending frame ("stage") always arrives
    # right before update.restarting. How many (if any) middle frames slip
    # through is a timing detail, not a guarantee this test should assert
    # exactly: a slow/loaded machine could see fake_download's second tick
    # or "verify" land as their own events if 250ms elapses first, so only
    # the two edges — and their order — are checked.
    progress_phases = [p["phase"] for p in waiter.of_type("update.progress")]
    assert progress_phases[0] == "download"
    assert progress_phases[-1] == "stage"
    assert waiter.of_type("update.restarting") == [{"build": 99}]
    assert restarted == [True]

    assert len(popen_calls) == 1
    cmd, kw = popen_calls[0]
    assert "--pid" in cmd
    assert str(os.getpid()) == cmd[cmd.index("--pid") + 1]
    staged_dir = cmd[cmd.index("--src") + 1]
    assert os.path.isfile(os.path.join(staged_dir, "marker.txt"))

    # The slot is free again once job.finished has landed.
    assert not service._job_running(UPDATE_JOB)


def test_restart_callback_raising_does_not_purge_the_handoff(service, monkeypatch):
    """HIGH-2: once Popen has returned, the staged payload belongs to the
    separate updater process — a failing restart callback (window already
    gone, a pywebview backend error, ...) must not delete it out from under
    that process, and the job must still report ok=True since the handoff
    itself succeeded."""
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: True)

    def fake_extract(zip_path, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "marker.txt"), "w") as fh:
            fh.write("staged")
        return dest_dir
    monkeypatch.setattr(service_mod.ucore, "extract_zip", fake_extract)

    launched = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            launched["cmd"] = cmd
            launched["staged_present"] = os.path.isfile(
                os.path.join(cmd[cmd.index("--src") + 1], "marker.txt"))
    monkeypatch.setattr(service_mod.subprocess, "Popen", _FakePopen)

    def boom():
        raise RuntimeError("window already destroyed")
    service.on_update_restart = boom

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert launched.get("staged_present") is True
    finished = waiter.of_type("job.finished")
    assert finished[-1]["ok"] is True
    assert waiter.of_type("notification") == []   # no error surfaced

    staged_dir = launched["cmd"][launched["cmd"].index("--src") + 1]
    ws_dir = os.path.dirname(staged_dir)
    assert os.path.isfile(os.path.join(staged_dir, "marker.txt"))
    assert os.path.isdir(ws_dir)


def test_apply_checksum_mismatch_purges_workspace(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: False)

    ws_holder = {}
    real_default_ws = service_mod.ucore.default_workspace
    def tracking_ws():
        path = real_default_ws()
        ws_holder["path"] = path
        return path
    monkeypatch.setattr(service_mod.ucore, "default_workspace", tracking_ws)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    finished = waiter.of_type("job.finished")
    assert finished[-1]["ok"] is False
    notes = waiter.of_type("notification")
    assert notes and "checksum mismatch" in notes[-1]["body"]
    assert not os.path.exists(ws_holder["path"])


# ── update.status / update.set_interval ──────────────────────────────────────

def test_set_interval_validates(service):
    with pytest.raises(CBError):
        service.update_set_interval("2 fortnights")


def test_set_interval_persists_and_rearms(service):
    before_next = service.update_status()["next_check"]
    status = service.update_set_interval("1 hour")
    assert status["interval"] == "1 hour"
    assert service._settings.get("update_check_interval") == "1 hour"
    assert status["next_check"] != before_next
    assert service._update_timer is not None


# ── auto-check timer ─────────────────────────────────────────────────────────

def test_construct_arms_no_timer(tmp_path, monkeypatch):
    """HIGH-1: a plain-constructed LOCAL service must not arm a Timer just
    from being built — every test and most tooling only ever needs one
    snapshot. start_update_timer() is the explicit opt-in web_window.py uses."""
    settings = Settings(path=str(tmp_path / "config.json"))
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    try:
        assert svc._update_timer is None
    finally:
        svc.close()


def test_start_update_timer_arms_it(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    try:
        assert svc._update_timer is None
        svc.start_update_timer()
        assert svc._update_timer is not None
    finally:
        svc.close()


def test_start_update_timer_noop_on_remote(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    svc = CrateBuilderService(transport=REMOTE, settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    try:
        svc.start_update_timer()
        assert svc._update_timer is None
    finally:
        svc.close()


def test_closed_service_cannot_be_rearmed_by_a_fire_in_flight(service):
    """MEDIUM-1: _update_timer_fire clears _update_timer under the lock, runs
    the check OUTSIDE the lock, then unconditionally re-arms. A close()
    landing in that window must not be undone by the fire's own re-arm.

    Reproduced without ever starting a real Timer (that would just leak one
    at whatever interval is configured, independent of what this asserts):
    _update_timer_fire's own re-arm is exactly the call _arm_update_timer()
    makes here, on a service already close()d — the _closed flag it checks
    under the same lock is the fix, and this is the direct check of it."""
    service.close()
    service._arm_update_timer()        # what _update_timer_fire's re-arm does
    assert service._update_timer is None
    assert service._closed is True


def test_timer_fire_emits_available_only_when_newer(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 1})
    waiter = _Waiter(service)
    service._update_timer_fire()
    available = waiter.of_type("update.available")
    assert available == [{
        "build": 99, "current_build": 1, "notes": "test build",
        "can_self_update": available[0]["can_self_update"],
    }]
    # Re-armed for the next interval.
    assert service._update_timer is not None


def test_timer_fire_silent_when_current(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {**MANIFEST, "build": 1})
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "1.3", "build": 50})
    waiter = _Waiter(service)
    service._update_timer_fire()
    assert waiter.of_type("update.available") == []


def test_timer_skips_fire_while_a_job_runs(service, monkeypatch):
    called = []
    monkeypatch.setattr(service, "update_check", lambda: called.append(1))
    with service._lock:
        service._jobs["batch"] = 1
    try:
        service._update_timer_fire()
    finally:
        with service._lock:
            service._jobs.pop("batch", None)
    assert called == []
    # Still re-armed — a skipped fire is not a disarmed timer.
    assert service._update_timer is not None


def test_remote_service_arms_no_timer(tmp_path):
    remote = CrateBuilderService(transport=REMOTE,
                                 settings=Settings(path=str(tmp_path / "c.json")),
                                 db_path=str(tmp_path / "db.sqlite"))
    try:
        assert remote._update_timer is None
    finally:
        remote.close()


def test_close_cancels_the_timer(service):
    service.start_update_timer()
    assert service._update_timer is not None
    service.close()
    assert service._update_timer is None

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
from cratebuilder import util
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
    def download(url, dest, progress_cb=None, timeout=30.0, _opener=None,
                 cancel=None):
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
                        lambda script_path=None: {"version": "2.0", "build": 50})
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
                        lambda script_path=None: {"version": "2.0", "build": 50})
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
        "notes": None, "notice": None, "components": None,
        "can_self_update": result["can_self_update"],
        "checked_at": result["checked_at"],
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
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: False)
    with pytest.raises(CBError, match="running from source"):
        service.update_apply()


def test_apply_refuses_on_linux(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: True)
    with pytest.raises(CBError, match="linux-v2.0"):
        service.update_apply()


def test_apply_refuses_no_update_available(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {**MANIFEST, "build": 1})
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 50})
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
                        lambda script_path=None: {"version": "2.0", "build": 1})
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
                        lambda script_path=None: {"version": "2.0", "build": 1})
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
                        lambda script_path=None: {"version": "2.0", "build": 1})
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


def _happy_apply(service, monkeypatch, manifest, tmp_path):
    service._log_path = str(tmp_path / "activity.log")
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: manifest)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: True)

    def fake_extract(zip_path, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        return dest_dir
    monkeypatch.setattr(service_mod.ucore, "extract_zip", fake_extract)

    class _FakePopen:
        def __init__(self, cmd, **kw):
            pass
    monkeypatch.setattr(service_mod.subprocess, "Popen", _FakePopen)
    service.on_update_restart = None


def _activity_lines(service):
    if not os.path.isfile(service._log_path):
        return []
    with open(service._log_path, encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh]


def test_apply_logs_the_components_the_build_changes(service, monkeypatch, tmp_path):
    """A successful handoff leaves one UPDATED line in activity.log naming
    the build jump and every bundled component the new build replaces."""
    service._installed_components_cache = {
        "python": "3.14.5", "ffmpeg": "9.0.1", "yt-dlp": "2026.8.19",
        "certifi": "2026.7.22",
    }
    manifest = dict(MANIFEST, components={
        "python": "3.14.5", "ffmpeg": "9.0.1", "yt-dlp": "2026.9.2",
        "certifi": "2026.9.1",
    })
    _happy_apply(service, monkeypatch, manifest, tmp_path)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    lines = [ln for ln in _activity_lines(service) if "UPDATED" in ln]
    assert len(lines) == 1
    assert "| UPDATED     | Build: 1 -> 99 | Components: " in lines[0]
    assert "yt-dlp 2026.8.19 -> 2026.9.2" in lines[0]
    assert "(certifi) 2026.7.22 -> 2026.9.1" in lines[0]
    assert "Python" not in lines[0]
    assert "FFmpeg" not in lines[0]


def test_apply_logs_not_listed_for_a_manifest_without_the_block(service, monkeypatch, tmp_path):
    service._installed_components_cache = {"python": "3.14.5"}
    _happy_apply(service, monkeypatch, MANIFEST, tmp_path)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    lines = [ln for ln in _activity_lines(service) if "UPDATED" in ln]
    assert len(lines) == 1
    assert lines[0].endswith("| Components: not listed")


def test_failed_apply_writes_no_updated_line(service, monkeypatch, tmp_path):
    """The line records an update that is really going to happen: a
    download that fails verification never reaches the handoff."""
    _happy_apply(service, monkeypatch, MANIFEST, tmp_path)
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: False)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert not any("UPDATED" in ln for ln in _activity_lines(service))


def test_restart_callback_raising_does_not_purge_the_handoff(service, monkeypatch):
    """HIGH-2: once Popen has returned, the staged payload belongs to the
    separate updater process — a failing restart callback (window already
    gone, a pywebview backend error, ...) must not delete it out from under
    that process, and the job must still report ok=True since the handoff
    itself succeeded."""
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
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
                        lambda script_path=None: {"version": "2.0", "build": 1})
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


# ── update.cancel ────────────────────────────────────────────────────────────
# The dialog's Cancel button. Honoured through download / verify / stage; a
# cancel is the user's own choice, so it must never surface as the error
# notification and ok=False that a worker which raises would produce.

def test_cancel_with_no_job_is_a_quiet_no(service):
    assert service.call("update.cancel") == {"cancelled": False}
    assert not service._update_cancel.is_set()


def test_cancel_during_download_purges_and_says_so(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)

    # A download that blocks until the test has cancelled, then behaves like
    # the real one: notices the event between chunks and raises.
    release = threading.Event()
    seen = {}

    def slow_download(url, dest, progress_cb=None, timeout=30.0,
                      _opener=None, cancel=None):
        seen["cancel"] = cancel
        assert release.wait(5), "the test never cancelled"
        if cancel is not None and cancel.is_set():
            raise service_mod.ucore.UpdateCancelled("cancelled")
        raise AssertionError("download ran on without a cancel")
    monkeypatch.setattr(service_mod.ucore, "download", slow_download)
    monkeypatch.setattr(service_mod.ucore, "verify_sha256",
                        lambda path, sha: pytest.fail("verify ran after a cancel"))

    ws_holder = {}
    real_default_ws = service_mod.ucore.default_workspace
    def tracking_ws():
        path = real_default_ws()
        ws_holder["path"] = path
        return path
    monkeypatch.setattr(service_mod.ucore, "default_workspace", tracking_ws)

    waiter = _Waiter(service)
    service.update_apply()
    assert service.call("update.cancel") == {"cancelled": True}
    release.set()
    waiter.wait()

    assert seen["cancel"] is service._update_cancel
    finished = waiter.of_type("job.finished")
    assert finished[-1]["ok"] is True
    assert finished[-1]["error"] is None
    notes = waiter.of_type("notification")
    assert [n["level"] for n in notes] == ["info"]
    assert notes[0]["title"] == "Update"
    assert notes[0]["body"] == "Update cancelled — still on build 1."
    assert waiter.of_type("update.cancelled") == [{"build": 1}]
    assert waiter.of_type("update.restarting") == []
    assert not os.path.exists(ws_holder["path"])
    assert not service._job_running(UPDATE_JOB)


def test_cancel_after_stage_still_stops_before_the_handoff(service, monkeypatch):
    """The event is checked after verify and after extract too — a cancel
    that lands while the zip is being unpacked must not reach Popen."""
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: True)

    def cancelling_extract(zip_path, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        service.call("update.cancel")
        return dest_dir
    monkeypatch.setattr(service_mod.ucore, "extract_zip", cancelling_extract)
    monkeypatch.setattr(service_mod.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen ran after a cancel"))
    restarted = []
    service.on_update_restart = lambda: restarted.append(True)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert waiter.of_type("update.restarting") == []
    assert waiter.of_type("update.cancelled") == [{"build": 1}]
    assert waiter.of_type("job.finished")[-1]["ok"] is True
    assert restarted == []


def test_cancel_before_the_job_exists_then_again_once_it_does(service, monkeypatch):
    """The page's Cancel is live while update.apply is still fetching the
    manifest — before _start_job has claimed the slot. That first cancel
    finds no job (and must not arm the event: the guard's clear would wipe
    it anyway); the page asks again once update.apply has returned, and
    that one lands."""
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    manifest_gate = threading.Event()
    fetching = threading.Event()

    def slow_fetch(url):
        fetching.set()
        assert manifest_gate.wait(5)
        return MANIFEST
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", slow_fetch)
    download_gate = threading.Event()

    def slow_download(url, dest, progress_cb=None, timeout=30.0,
                      _opener=None, cancel=None):
        assert download_gate.wait(5)
        if cancel is not None and cancel.is_set():
            raise service_mod.ucore.UpdateCancelled("cancelled")
        raise AssertionError("download ran on without a cancel")
    monkeypatch.setattr(service_mod.ucore, "download", slow_download)

    waiter = _Waiter(service)
    applier = threading.Thread(target=service.update_apply, daemon=True)
    applier.start()
    assert fetching.wait(5)
    assert service.call("update.cancel") == {"cancelled": False}   # no job yet
    manifest_gate.set()
    applier.join(5)
    assert service._job_running(UPDATE_JOB)
    assert service.call("update.cancel") == {"cancelled": True}    # the retry
    download_gate.set()
    waiter.wait()

    assert waiter.of_type("update.cancelled") == [{"build": 1}]
    assert waiter.of_type("job.finished")[-1]["ok"] is True


def test_a_second_apply_cannot_clear_a_live_jobs_cancel(service, monkeypatch):
    """The clear lives in _start_job's guard, which never runs while the
    slot is held: a refused second apply leaves the first one's cancel."""
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    gate = threading.Event()

    def slow_download(url, dest, progress_cb=None, timeout=30.0,
                      _opener=None, cancel=None):
        assert gate.wait(5)
        raise service_mod.ucore.UpdateCancelled("cancelled")
    monkeypatch.setattr(service_mod.ucore, "download", slow_download)

    waiter = _Waiter(service)
    service.update_apply()
    service.call("update.cancel")
    with pytest.raises(CBError):
        service.update_apply()
    assert service._update_cancel.is_set()
    gate.set()
    waiter.wait()


def test_a_stale_cancel_does_not_carry_into_the_next_apply(service, monkeypatch):
    """The event is cleared by _start_job's guard for the update job, so a
    cancel from an earlier run cannot abort the next one before it starts."""
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))
    monkeypatch.setattr(service_mod.ucore, "verify_sha256", lambda path, sha: True)
    monkeypatch.setattr(service_mod.ucore, "extract_zip",
                        lambda z, d: os.makedirs(d, exist_ok=True) or d)
    monkeypatch.setattr(service_mod.subprocess, "Popen", lambda *a, **k: None)
    service._update_cancel.set()

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert waiter.of_type("update.cancelled") == []
    assert waiter.of_type("update.restarting") == [{"build": 99}]


def test_remote_transport_refuses_update_cancel(tmp_path):
    remote = CrateBuilderService(transport=REMOTE,
                                 settings=Settings(path=str(tmp_path / "c.json")),
                                 db_path=str(tmp_path / "db.sqlite"))
    try:
        with pytest.raises(CBError):
            remote.call("update.cancel")
    finally:
        remote.close()


# ── app.quit ─────────────────────────────────────────────────────────────────
# The page's close dialog answered "close". Host-only by name: a paired
# browser must never be able to shut the desktop app down.

def test_app_quit_calls_the_hook_on_local(service):
    quits = []
    service.on_quit = lambda: quits.append(True)
    assert service.call("app.quit", transport=LOCAL) == {"ok": True}
    assert quits == [True]


def test_app_quit_without_a_hook_does_nothing(service):
    assert service.call("app.quit") == {"ok": False}


def test_app_quit_is_refused_over_remote(tmp_path):
    remote = CrateBuilderService(transport=REMOTE,
                                 settings=Settings(path=str(tmp_path / "c.json")),
                                 db_path=str(tmp_path / "db.sqlite"))
    quits = []
    remote.on_quit = lambda: quits.append(True)
    seen = []
    remote.on_close_seen = lambda: seen.append(True)
    try:
        for method in ("app.quit", "app.close_seen"):
            with pytest.raises(CBError):
                remote.call(method)
            with pytest.raises(CBError):
                remote.call(method, transport=REMOTE)
    finally:
        remote.close()
    assert quits == [] and seen == []


def test_app_close_seen_is_the_pages_receipt(service):
    assert service.call("app.close_seen") == {"ok": True}    # no hook: fine
    seen = []
    service.on_close_seen = lambda: seen.append(True)
    assert service.call("app.close_seen", transport=LOCAL) == {"ok": True}
    assert seen == [True]


def test_page_ready_flips_on_the_first_local_snapshot(service, tmp_path):
    assert service.page_ready is False
    service.call("state.snapshot", transport=LOCAL)
    assert service.page_ready is True

    remote = CrateBuilderService(transport=REMOTE,
                                 settings=Settings(path=str(tmp_path / "c.json")),
                                 db_path=str(tmp_path / "db.sqlite"))
    try:
        remote.call("state.snapshot", transport=REMOTE)
        assert remote.page_ready is False
    finally:
        remote.close()


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
                        lambda script_path=None: {"version": "2.0", "build": 1})
    waiter = _Waiter(service)
    service._update_timer_fire()
    available = waiter.of_type("update.available")
    assert available == [{
        "build": 99, "current_build": 1, "notes": "test build", "notice": None,
        "can_self_update": available[0]["can_self_update"],
        "checked_at": available[0]["checked_at"],
    }]
    # Re-armed for the next interval.
    assert service._update_timer is not None


def test_timer_fire_silent_when_current(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest",
                        lambda url: {**MANIFEST, "build": 1})
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 50})
    waiter = _Waiter(service)
    service._update_timer_fire()
    assert waiter.of_type("update.available") == []
    # ...but the verdict itself is still pushed, so the Overview's Update
    # card can turn "not checked yet" into "up to date" without a reload.
    checked = waiter.of_type("update.checked")
    assert len(checked) == 1
    assert checked[0]["available"] is False and checked[0]["latest_build"] == 1
    assert checked[0]["current_build"] == 50 and checked[0]["checked_at"] > 0


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


def test_startup_check_arms_a_short_timer(service):
    """The launch check fires seconds after the window is up, not a whole
    interval later."""
    service.start_startup_update_check()
    assert service._update_timer is not None
    assert service._next_update_check_ts - time.time() <= \
        service_mod.STARTUP_UPDATE_CHECK_DELAY
    service.close()


def test_startup_check_noop_on_remote(tmp_path):
    svc = CrateBuilderService(transport=REMOTE,
                              settings=Settings(path=str(tmp_path / "c.json")),
                              db_path=str(tmp_path / "db.sqlite"))
    try:
        svc.start_startup_update_check()
        assert svc._update_timer is None
    finally:
        svc.close()


def test_startup_fire_checks_even_while_a_job_runs(service, monkeypatch):
    """The launch check only reads the manifest and announces; it never
    installs. So a startup scan still running at the three-second mark is
    no reason to hold it back — waiting meant no check at all until the
    scan was stopped."""
    called = []
    monkeypatch.setattr(service, "update_check", lambda: called.append(1))
    with service._lock:
        service._jobs["watchlist"] = 1
    try:
        service._startup_update_check_fire()
    finally:
        with service._lock:
            service._jobs.pop("watchlist", None)
    assert called == [1]
    secs = util.interval_label_to_seconds(
        service._settings.get("update_check_interval"))
    assert service._next_update_check_ts - time.time() > secs - 5


def test_startup_fire_checks_then_hands_over_to_the_interval(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    waiter = _Waiter(service)
    service._startup_update_check_fire()
    assert [a["build"] for a in waiter.of_type("update.available")] == [99]
    secs = util.interval_label_to_seconds(
        service._settings.get("update_check_interval"))
    assert service._next_update_check_ts - time.time() > secs - 5


def test_close_cancels_the_timer(service):
    service.start_update_timer()
    assert service._update_timer is not None
    service.close()
    assert service._update_timer is None


# ── the Overview's Update card reads the last verdict off the snapshot ───────

def test_snapshot_carries_nothing_until_a_check_has_run(service):
    assert service.call("state.snapshot", {})["update"] is None


def test_snapshot_carries_the_last_check_result(service, monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: None)
    result = service.update_check()
    assert service.call("state.snapshot", {})["update"] == result
    assert result["checked_at"] > 0


# ── the manifest's styled split of the notes ─────────────────────────────────

def test_check_prefers_the_manifests_changes_and_notice_fields(service, monkeypatch):
    """release.py writes the legacy "notes" blob (shouted notice + changes)
    for old builds AND the clean "changes" / "notice" pair; the web dialog
    draws the pair, so the check reports those when they exist."""
    manifest = dict(MANIFEST, build=99,
                    notes="*** STOP SCANS ***\n\nFixed a thing.",
                    changes="Fixed a thing.",
                    notice="Important: stop all Watch List scans first.")
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: manifest)
    result = service.update_check()
    assert result["notes"] == "Fixed a thing."
    assert result["notice"] == "Important: stop all Watch List scans first."


def test_check_reads_a_manifest_without_the_split_the_old_way(service, monkeypatch):
    manifest = dict(MANIFEST, build=99, notes="Fixed a thing.")
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: manifest)
    result = service.update_check()
    assert result["notes"] == "Fixed a thing." and result["notice"] is None



# ── the components table (update.status → components) ───────────────────────

def _fake_installed(monkeypatch, versions):
    monkeypatch.setattr(service_mod.components, "installed_versions",
                        lambda ffmpeg_dir=None, **kw: dict(versions))


def test_status_lists_what_is_installed_before_any_check(service, monkeypatch):
    _fake_installed(monkeypatch, {"python": "3.14.5", "yt-dlp": "2026.8.19"})
    comp = service.update_status()["components"]
    assert comp["build"] is None and comp["available"] is False
    by_key = {r["key"]: r for r in comp["rows"]}
    assert by_key["python"] == {"key": "python", "label": "Python",
                                "installed": "3.14.5", "offered": None,
                                "state": "unknown"}
    assert by_key["yt-dlp"]["state"] == "unknown"
    assert by_key["ffmpeg"]["state"] == "missing"


def test_a_check_carries_the_manifests_components_into_the_table(service, monkeypatch):
    _fake_installed(monkeypatch, {"python": "3.14.5", "yt-dlp": "2026.8.19",
                                  "ffmpeg": "9.0.1+aa"})
    manifest = dict(MANIFEST, components={"python": "3.14.5", "yt_dlp": "2026.9.1",
                                          "ffmpeg": "9.0.1+aa"})
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: manifest)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})

    result = service.update_check()
    assert result["components"] == {"python": "3.14.5", "yt-dlp": "2026.9.1",
                                    "ffmpeg": "9.0.1+aa"}
    comp = service.update_status()["components"]
    assert comp["build"] == 99 and comp["available"] is True
    by_key = {r["key"]: r for r in comp["rows"]}
    assert by_key["yt-dlp"]["state"] == "newer"
    assert by_key["yt-dlp"]["offered"] == "2026.9.1"
    assert by_key["python"]["state"] == "same"
    assert by_key["ffmpeg"]["state"] == "same"
    assert by_key["pillow"]["state"] == "missing"


def test_a_manifest_before_the_block_leaves_every_row_unknown(service, monkeypatch):
    """Build 82's manifest has no components block: the table shows what
    is installed and says the build didn't list its own."""
    _fake_installed(monkeypatch, {"python": "3.14.5", "yt-dlp": "2026.8.19"})
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    assert service.update_check()["components"] is None
    comp = service.update_status()["components"]
    assert comp["build"] == 99
    assert {r["state"] for r in comp["rows"] if r["installed"]} == {"unknown"}


def test_installed_versions_are_read_once_per_service(service, monkeypatch):
    reads = []
    monkeypatch.setattr(service_mod.components, "installed_versions",
                        lambda ffmpeg_dir=None, **kw: reads.append(1) or {"python": "3"})
    service.update_status(); service.update_status()
    assert reads == [1]


def test_an_unreachable_check_keeps_a_stale_comparison_out(service, monkeypatch):
    """After a failed check nothing is 'live': the table falls back to the
    installed-only view rather than comparing against a stale build."""
    _fake_installed(monkeypatch, {"python": "3.14.5"})
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: None)
    service.update_check()
    comp = service.update_status()["components"]
    assert comp["build"] is None
    assert {r["state"] for r in comp["rows"] if r["installed"]} == {"unknown"}

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
    assert result["latest_build"] is None


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

    progress_phases = [p["phase"] for p in waiter.of_type("update.progress")]
    assert progress_phases == ["download", "download", "verify", "stage"]
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
    assert service._update_timer is not None
    service.close()
    assert service._update_timer is None

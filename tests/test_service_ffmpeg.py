"""cratebuilder.service: the bundled-FFmpeg update the web app's update check
carries — the monolith's _maybe_update_ffmpeg, which the web app shipped
without. The decision itself (ucore.ffmpeg_update_action) is covered in
test_ffmpeg_update.py / test_ffmpeg_marker.py; these pin the service wiring."""
import threading

import pytest

from cratebuilder import service as svc_mod
from cratebuilder import updater_core as ucore
from cratebuilder.service import CrateBuilderService, CBError
from cratebuilder.settings import Settings

OFFER = "9.0.2-essentials_build-www.gyan.dev+3256173f"
MANIFEST = {"version": "2.1", "build": 101, "url": "https://x/app.zip",
            "sha256": "a" * 64,
            "ffmpeg": {"version": OFFER, "url": "https://x/ffmpeg.zip",
                       "sha256": "b" * 64}}


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    s = CrateBuilderService(settings=settings,
                            db_path=str(tmp_path / "cratebuilder.db"))
    yield s
    s.close()


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """A packaged Windows install whose bundled FFmpeg is 9.0.1, and a swap
    that records what it was asked to do instead of touching the network."""
    install = tmp_path / "app"
    install.mkdir()
    (install / "ffmpeg.version").write_text(
        "9.0.1-essentials_build-www.gyan.dev+72a489ec", encoding="utf-8")
    monkeypatch.setattr(ucore, "is_frozen", lambda: True)
    monkeypatch.setattr(ucore, "is_linux", lambda: False)
    monkeypatch.setattr(svc_mod, "bundled_ffmpeg_dir", lambda: str(install))
    monkeypatch.setattr(ucore, "probe_ffmpeg_build",
                        lambda d: "9.0.1-essentials_build-www.gyan.dev")
    monkeypatch.setattr(ucore, "default_workspace",
                        lambda: str(tmp_path / "ws" / "update"))
    calls = {"download": [], "install": [], "done": threading.Event()}

    def download(url, dest, **kw):
        calls["download"].append(url)
        open(dest, "wb").close()

    def install_zip(zip_path, sha, install_dir, staged, backup, version):
        calls["install"].append((sha, install_dir, version, staged))
        ucore.write_ffmpeg_version(install_dir, version)
        calls["done"].set()
        return True

    monkeypatch.setattr(ucore, "download", download)
    monkeypatch.setattr(ucore, "install_ffmpeg_from_zip", install_zip)
    calls["dir"] = str(install)
    return calls


def _wait_idle(service, timeout=5):
    for _ in range(int(timeout * 100)):
        with service._lock:
            if not service._ffmpeg_swapping:
                return
        threading.Event().wait(0.01)
    raise AssertionError("the FFmpeg swap never finished")


def test_an_update_check_installs_the_offered_ffmpeg(service, frozen, monkeypatch):
    """The bug: the web app's check announced the app build and nothing else,
    so installs sat on the installer's FFmpeg forever."""
    monkeypatch.setattr(ucore, "fetch_manifest", lambda url, **kw: MANIFEST)
    monkeypatch.setattr(service, "_update_manifest_url", lambda: "https://x/u.json")
    service.update_check()
    assert frozen["done"].wait(5)
    _wait_idle(service)
    assert frozen["download"] == ["https://x/ffmpeg.zip"]
    sha, install_dir, version, staged = frozen["install"][0]
    assert (sha, install_dir, version) == ("b" * 64, frozen["dir"], OFFER)
    assert ucore.read_ffmpeg_version(frozen["dir"]) == OFFER
    # Its own workspace, beside the app update's — never inside it.
    assert staged.startswith(service._ffmpeg_workspace())
    assert not staged.startswith(ucore.default_workspace())


def test_the_swap_announces_itself_and_drops_the_stale_components_read(service, frozen):
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    service._installed_components_cache = {"ffmpeg": "old"}
    assert service._maybe_update_ffmpeg(MANIFEST) == "update"
    assert frozen["done"].wait(5)
    _wait_idle(service)
    service._emit.flush()
    notes = [p for t, p in seen if t == "notification"]
    assert notes and notes[-1]["body"] == "FFmpeg updated to 9.0.2."
    assert service._installed_components_cache is None


def _swap_states(seen):
    return [p["state"] for t, p in seen if t == "update.ffmpeg"]


def test_the_update_page_hears_the_swap_start_and_finish(service, frozen):
    """The bug: the swap finished after the check had answered, so the
    Update page kept the old FFmpeg until the app was restarted."""
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    assert service.update_status()["ffmpeg"] is None
    service._maybe_update_ffmpeg(MANIFEST)
    assert frozen["done"].wait(5)
    _wait_idle(service)
    service._emit.flush()
    assert _swap_states(seen) == ["updating", "updated"]
    assert service.update_status()["ffmpeg"] == {"state": "updated", "version": OFFER}


def test_a_busy_app_says_it_is_waiting(service, frozen, monkeypatch):
    monkeypatch.setattr(service, "FFMPEG_RETRY_SECONDS", 30)
    service._jobs["batch"] = 1
    service._maybe_update_ffmpeg(MANIFEST)
    service._ffmpeg_retry.cancel()
    assert service.update_status()["ffmpeg"]["state"] == "waiting"


def test_a_failed_swap_says_so(service, frozen, monkeypatch):
    def boom(*a, **kw):
        raise OSError("network down")
    monkeypatch.setattr(ucore, "download", boom)
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    service._maybe_update_ffmpeg(MANIFEST)
    _wait_idle(service)
    service._emit.flush()
    assert _swap_states(seen) == ["updating", "failed"]
    assert service.update_status()["ffmpeg"]["state"] == "failed"


def test_a_busy_app_defers_and_retries_instead_of_swapping(service, frozen, monkeypatch):
    monkeypatch.setattr(service, "FFMPEG_RETRY_SECONDS", 30)
    service._jobs["batch"] = 1
    assert service._maybe_update_ffmpeg(MANIFEST) == "deferred"
    assert frozen["download"] == []
    assert service._ffmpeg_retry is not None
    assert service._ffmpeg_retry_manifest is MANIFEST
    # Idle again: the retry's own fire does the swap and disarms.
    del service._jobs["batch"]
    service._ffmpeg_retry.cancel()
    service._ffmpeg_retry_fire()
    assert frozen["done"].wait(5)
    _wait_idle(service)
    assert service._ffmpeg_retry is None


def test_nothing_happens_when_the_install_already_has_the_offer(service, frozen, monkeypatch):
    ucore.write_ffmpeg_version(frozen["dir"], OFFER)
    monkeypatch.setattr(ucore, "probe_ffmpeg_build",
                        lambda d: "9.0.2-essentials_build-www.gyan.dev")
    assert service._maybe_update_ffmpeg(MANIFEST) == "none"
    assert frozen["download"] == []


def test_from_source_or_on_linux_it_never_touches_ffmpeg(service, monkeypatch):
    monkeypatch.setattr(ucore, "is_frozen", lambda: False)
    assert service._maybe_update_ffmpeg(MANIFEST) is None
    monkeypatch.setattr(ucore, "is_frozen", lambda: True)
    monkeypatch.setattr(ucore, "is_linux", lambda: True)
    assert service._maybe_update_ffmpeg(MANIFEST) is None


def test_a_failed_swap_is_dropped_and_frees_the_slot(service, frozen, monkeypatch):
    def boom(*a, **kw):
        raise OSError("network down")
    monkeypatch.setattr(ucore, "download", boom)
    assert service._maybe_update_ffmpeg(MANIFEST) == "update"
    _wait_idle(service)
    assert ucore.read_ffmpeg_version(frozen["dir"]).startswith("9.0.1")


def test_an_app_update_waits_for_a_running_ffmpeg_swap(service):
    """The updater relaunches the app; a swap cut off mid-copy would leave a
    half-replaced FFmpeg behind."""
    service._ffmpeg_swapping = True
    with pytest.raises(CBError):
        service._require_idle_for_update()


def test_an_ffmpeg_hiccup_never_costs_the_check_its_answer(service, monkeypatch):
    monkeypatch.setattr(ucore, "fetch_manifest", lambda url, **kw: MANIFEST)
    monkeypatch.setattr(service, "_update_manifest_url", lambda: "https://x/u.json")

    def broken(manifest):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(service, "_maybe_update_ffmpeg", broken)
    result = service.update_check()
    assert result["reachable"] and result["latest_build"] == 101


def test_close_disarms_a_pending_retry(service, frozen):
    service._jobs["batch"] = 1
    service._maybe_update_ffmpeg(MANIFEST)
    timer = service._ffmpeg_retry
    service.close()
    assert service._ffmpeg_retry is None
    assert timer.finished.is_set()

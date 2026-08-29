"""cratebuilder.service: frozen (PyInstaller) awareness.

Frozen, cratebuilder/service.py itself resolves inside PyInstaller's
_internal/ — fine for locating this file, but the wrong root for both user
data (which must land beside the installed exe, where every existing 1.3
install already keeps cratebuilder.db) and the bundled monolith (which
--add-data places at sys._MEIPASS). These guard that every reader of either
agrees with that, and that from-source behavior is unchanged.
"""
import os

from cratebuilder import service as service_mod
# app_dir is bound directly (not via service_mod.app_dir) because tests/conftest.py's
# autouse _isolate_service_paths fixture monkeypatches cratebuilder.service.app_dir
# to a tmp-path stub for every non-GUI test, to keep the developer's real config/DB
# out of the suite. This name, captured at collection time, still reaches the real
# implementation — which is what these tests exist to exercise.
from cratebuilder.service import (MAIN_SCRIPT, about_info, app_dir,
                                  bundled_ffmpeg_dir, version_info)


def _unset_frozen(monkeypatch):
    monkeypatch.delattr(service_mod.sys, "frozen", raising=False)
    monkeypatch.delattr(service_mod.sys, "_MEIPASS", raising=False)


def _plant_monolith(tmp_path, build=999):
    script = tmp_path / MAIN_SCRIPT
    script.write_text(
        'APP_VERSION = "2.0"\n'
        f'APP_BUILD = {build}\n'
        'ABOUT_CREATED_BY = "Someone"\n'
        'UPDATE_MANIFEST_URL = "https://example.com/update.json"\n',
        encoding="utf-8")
    return script


# ── app_dir() ────────────────────────────────────────────────────────────────

def test_app_dir_from_source_is_unchanged(monkeypatch):
    _unset_frozen(monkeypatch)
    expected = service_mod.util.runtime_data_dir(
        os.path.join(service_mod.repo_root(), MAIN_SCRIPT))
    assert app_dir() == expected


def test_app_dir_frozen_resolves_beside_the_exe(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "executable",
                        str(install / "DJ-CrateBuilder.exe"))
    assert os.path.realpath(app_dir()) == os.path.realpath(str(install))


def test_app_dir_frozen_falls_back_when_the_install_dir_is_not_writable(
        tmp_path, monkeypatch):
    """util.runtime_data_dir's not-writable fallback still applies frozen —
    the same protection a system-wide install would need."""
    install = tmp_path / "install"
    install.mkdir()
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "executable",
                        str(install / "DJ-CrateBuilder.exe"))
    monkeypatch.setattr(service_mod.util.os, "access", lambda path, mode: False)
    monkeypatch.setenv("LOCALAPPDATA", str(fallback))
    result = app_dir()
    assert os.path.realpath(result).startswith(os.path.realpath(str(fallback)))


# ── _monolith_path() / version_info() / about_info() / _manifest_urls() ──────

def test_monolith_path_from_source_is_the_repo_checkout(monkeypatch):
    _unset_frozen(monkeypatch)
    assert (service_mod._monolith_path()
           == os.path.join(service_mod.repo_root(), MAIN_SCRIPT))


def test_monolith_path_frozen_is_meipass(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert service_mod._monolith_path() == os.path.join(str(tmp_path), MAIN_SCRIPT)


def test_version_info_reads_the_bundled_copy_when_frozen(tmp_path, monkeypatch):
    _plant_monolith(tmp_path, build=4242)
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert version_info() == {"version": "2.0", "build": 4242}


def test_about_info_reads_the_bundled_copy_when_frozen(tmp_path, monkeypatch):
    _plant_monolith(tmp_path)
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert about_info()["created_by"] == "Someone"


def test_manifest_urls_reads_the_bundled_copy_when_frozen(tmp_path, monkeypatch):
    _plant_monolith(tmp_path)
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "_MEIPASS", str(tmp_path), raising=False)
    urls = service_mod._manifest_urls()
    assert urls["UPDATE_MANIFEST_URL"] == "https://example.com/update.json"


# ── bundled_ffmpeg_dir() ───────────────────────────────────────────────────

def test_bundled_ffmpeg_dir_is_none_from_source(monkeypatch):
    _unset_frozen(monkeypatch)
    assert bundled_ffmpeg_dir() is None


def test_bundled_ffmpeg_dir_frozen_finds_ffmpeg_beside_the_exe(tmp_path,
                                                                monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    (install / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "executable",
                        str(install / "DJ-CrateBuilder.exe"))
    monkeypatch.delattr(service_mod.sys, "_MEIPASS", raising=False)
    assert (os.path.realpath(bundled_ffmpeg_dir())
           == os.path.realpath(str(install)))


def test_bundled_ffmpeg_dir_frozen_falls_back_to_meipass(tmp_path, monkeypatch):
    install = tmp_path / "install"
    meipass = tmp_path / "meipass"
    install.mkdir()
    meipass.mkdir()
    (meipass / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "executable",
                        str(install / "DJ-CrateBuilder.exe"))
    monkeypatch.setattr(service_mod.sys, "_MEIPASS", str(meipass), raising=False)
    assert (os.path.realpath(bundled_ffmpeg_dir())
           == os.path.realpath(str(meipass)))


def test_bundled_ffmpeg_dir_frozen_but_missing_is_none(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(service_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_mod.sys, "executable",
                        str(install / "DJ-CrateBuilder.exe"))
    monkeypatch.delattr(service_mod.sys, "_MEIPASS", raising=False)
    assert bundled_ffmpeg_dir() is None


# ── ffmpeg_dir wired into every construction site ──────────────────────────

def _service(tmp_path):
    from cratebuilder.settings import Settings
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return service_mod.CrateBuilderService(
        settings=settings, db_path=str(tmp_path / "cratebuilder.db"))


class _CapturingStub:
    """Stands in for BatchRunner/WatchlistOps/MaintenanceOps: records the
    kwargs it was built with rather than doing any real work."""
    captured = None

    def __init__(self, *args, **kwargs):
        type(self).captured = kwargs


def test_batch_runner_gets_the_frozen_ffmpeg_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "bundled_ffmpeg_dir", lambda: r"C:\install")

    class StubRunner(_CapturingStub):
        def run(self, rows):
            return {"downloaded": 0, "skipped": 0, "errors": 0,
                    "cancelled": False}

    monkeypatch.setattr(service_mod, "BatchRunner", StubRunner)
    svc = _service(tmp_path)
    svc.batch_add("https://example.com/a")
    svc.download_start()
    assert StubRunner.captured.get("ffmpeg_dir") == r"C:\install"


def test_watchlist_ops_gets_the_frozen_ffmpeg_dir(tmp_path, monkeypatch):
    import cratebuilder.watchrun as watchrun_mod

    class StubOps(_CapturingStub):
        pass

    monkeypatch.setattr(service_mod, "bundled_ffmpeg_dir", lambda: r"C:\install")
    monkeypatch.setattr(watchrun_mod, "WatchlistOps", StubOps)
    svc = _service(tmp_path)
    svc._watchlist    # trigger the lazy build
    assert StubOps.captured.get("ffmpeg_dir") == r"C:\install"


def test_maintenance_ops_gets_the_frozen_ffmpeg_dir(tmp_path, monkeypatch):
    import cratebuilder.maintenance as maintenance_mod

    class StubOps(_CapturingStub):
        pass

    monkeypatch.setattr(service_mod, "bundled_ffmpeg_dir", lambda: r"C:\install")
    monkeypatch.setattr(maintenance_mod, "MaintenanceOps", StubOps)
    svc = _service(tmp_path)
    svc._maintenance    # trigger the lazy build
    assert StubOps.captured.get("ffmpeg_dir") == r"C:\install"

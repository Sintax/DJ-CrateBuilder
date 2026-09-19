"""Tests for the self-update core (manifest, integrity, file swap).

All pure / filesystem logic — no network and no tkinter. Network functions
(fetch_manifest, download) are exercised with an injected fake opener.
"""
import hashlib
import io
import json
import os
import sys

import pytest

from cratebuilder import updater_core as uc


# ── is_update_available ───────────────────────────────────────────────────────
def test_newer_build_is_available():
    assert uc.is_update_available({"build": 8}, 7) is True


def test_same_or_older_build_not_available():
    assert uc.is_update_available({"build": 7}, 7) is False
    assert uc.is_update_available({"build": 6}, 7) is False


def test_malformed_manifest_never_available():
    assert uc.is_update_available(None, 7) is False
    assert uc.is_update_available({}, 7) is False
    assert uc.is_update_available({"build": "not-a-number"}, 7) is False
    assert uc.is_update_available("nope", 7) is False


def test_string_build_numbers_compare_numerically():
    # A manifest authored with a quoted build must still compare as an int.
    assert uc.is_update_available({"build": "10"}, 9) is True


# ── validate_manifest ─────────────────────────────────────────────────────────
def _good_manifest():
    return {"build": 8, "url": "https://example/app.zip", "sha256": "a" * 64}


def test_valid_manifest_passes():
    ok, reason = uc.validate_manifest(_good_manifest())
    assert ok is True and reason == ""


def test_manifest_missing_field_fails():
    m = _good_manifest(); del m["url"]
    ok, reason = uc.validate_manifest(m)
    assert ok is False and "url" in reason


def test_manifest_bad_sha_fails():
    m = _good_manifest(); m["sha256"] = "tooshort"
    ok, reason = uc.validate_manifest(m)
    assert ok is False and "sha256" in reason


def test_manifest_non_int_build_fails():
    m = _good_manifest(); m["build"] = "x"
    ok, reason = uc.validate_manifest(m)
    assert ok is False and "build" in reason


# ── sha256 helpers ────────────────────────────────────────────────────────────
def test_sha256_and_verify_roundtrip(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(b"crate builder bytes")
    expected = hashlib.sha256(b"crate builder bytes").hexdigest()
    assert uc.sha256_file(str(p)) == expected
    assert uc.verify_sha256(str(p), expected) is True
    assert uc.verify_sha256(str(p), expected.upper()) is True   # case-insensitive
    assert uc.verify_sha256(str(p), "b" * 64) is False
    assert uc.verify_sha256(str(p), "") is False


# ── fetch_manifest (injected opener, no network) ──────────────────────────────
class _FakeResp:
    def __init__(self, body):
        self._buf = io.BytesIO(body)
        self.headers = {}
    def read(self, *a):
        return self._buf.read(*a)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_fetch_manifest_parses_json():
    payload = json.dumps({"build": 9, "url": "u", "sha256": "a" * 64}).encode()
    captured = {}
    def opener(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(payload)
    out = uc.fetch_manifest("https://host/update.json", _opener=opener)
    assert out["build"] == 9
    # Cache-buster appended.
    assert "t=" in captured["url"]


def test_fetch_manifest_returns_none_on_error():
    def boom(req, timeout=None):
        raise OSError("offline")
    assert uc.fetch_manifest("https://host/update.json", _opener=boom) is None


def test_fetch_manifest_returns_none_on_bad_json():
    def opener(req, timeout=None):
        return _FakeResp(b"<html>not json</html>")
    assert uc.fetch_manifest("https://host/update.json", _opener=opener) is None


# ── download (injected opener) ────────────────────────────────────────────────
class _FakeDownloadResp(_FakeResp):
    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}


def test_download_streams_and_reports_progress(tmp_path):
    body = b"x" * (70000)   # > one 64KiB chunk so progress fires twice
    def opener(req, timeout=None):
        return _FakeDownloadResp(body)
    seen = []
    dest = tmp_path / "out" / "app.zip"
    uc.download("https://host/app.zip", str(dest),
                progress_cb=lambda d, t: seen.append((d, t)), _opener=opener)
    assert dest.read_bytes() == body
    assert seen[-1] == (len(body), len(body))     # finished, total known
    assert not (tmp_path / "out" / "app.zip.part").exists()   # .part renamed away


class _Flag:
    """The one method download() needs of a threading.Event."""
    def __init__(self, on=False):
        self.on = on
    def is_set(self):
        return self.on


def test_download_with_a_preset_cancel_raises_and_leaves_no_part(tmp_path):
    opened = []
    def opener(req, timeout=None):
        opened.append(req.full_url)
        return _FakeDownloadResp(b"x" * 10)
    dest = tmp_path / "out" / "app.zip"

    with pytest.raises(uc.UpdateCancelled):
        uc.download("https://host/app.zip", str(dest), _opener=opener,
                    cancel=_Flag(on=True))

    assert opened == []                       # never even asked the server
    assert not dest.exists()
    assert not (tmp_path / "out" / "app.zip.part").exists()


def test_download_cancelled_between_chunks_deletes_the_part_file(tmp_path):
    body = b"x" * (70000)   # > one 64KiB chunk, so there IS a between-chunks
    flag = _Flag()
    def opener(req, timeout=None):
        return _FakeDownloadResp(body)
    dest = tmp_path / "out" / "app.zip"
    seen = []

    def progress(done, total):
        seen.append(done)
        flag.on = True                        # cancel after the first chunk

    with pytest.raises(uc.UpdateCancelled):
        uc.download("https://host/app.zip", str(dest), progress_cb=progress,
                    _opener=opener, cancel=flag)

    assert seen == [65536]                    # stopped reading after chunk one
    assert not dest.exists()
    assert not (tmp_path / "out" / "app.zip.part").exists()


def test_download_without_a_cancel_is_unchanged(tmp_path):
    body = b"y" * 10
    def opener(req, timeout=None):
        return _FakeDownloadResp(body)
    dest = tmp_path / "app.zip"
    uc.download("https://host/app.zip", str(dest), _opener=opener, cancel=None)
    assert dest.read_bytes() == body


# ── apply_update (the file swap + rollback) ───────────────────────────────────
def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


def test_apply_update_replaces_and_backs_up(tmp_path):
    app = tmp_path / "app"; staged = tmp_path / "staged"; backup = tmp_path / "bak"
    _write(str(app / "DJ-CrateBuilder.exe"), "OLD-exe")
    _write(str(app / "keep.dat"), "untouched")          # not in staged -> left alone
    _write(str(staged / "DJ-CrateBuilder.exe"), "NEW-exe")
    _write(str(staged / "sub" / "lib.dll"), "NEW-lib")   # brand-new nested file

    ok = uc.apply_update(str(staged), str(app), str(backup))

    assert ok is True
    assert (app / "DJ-CrateBuilder.exe").read_text() == "NEW-exe"
    assert (app / "sub" / "lib.dll").read_text() == "NEW-lib"
    assert (app / "keep.dat").read_text() == "untouched"
    # The replaced original was preserved in the backup tree.
    assert (backup / "DJ-CrateBuilder.exe").read_text() == "OLD-exe"


def test_apply_update_rolls_back_on_failure(tmp_path):
    app = tmp_path / "app"; staged = tmp_path / "staged"; backup = tmp_path / "bak"
    _write(str(app / "a.txt"), "OLD-a")
    _write(str(app / "b.txt"), "OLD-b")
    _write(str(staged / "a.txt"), "NEW-a")
    _write(str(staged / "b.txt"), "NEW-b")

    # Fail on the second file copied, mid-swap.
    calls = {"n": 0}
    def flaky_copy(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        import shutil
        shutil.copy2(src, dst)

    with pytest.raises(OSError):
        uc.apply_update(str(staged), str(app), str(backup), _copyfn=flaky_copy)

    # Both originals must be restored exactly as they were.
    assert (app / "a.txt").read_text() == "OLD-a"
    assert (app / "b.txt").read_text() == "OLD-b"


# A package bump ships the new `<name>-<ver>.dist-info` folder; the old one
# is not in the payload, so the additive overlay left it in place. With two
# side by side, importlib.metadata answers with whichever sorts first — the
# older — and the Update page reported build 89's uvicorn as still 0.52.4.
def test_apply_update_retires_the_older_dist_info_of_a_bumped_package(tmp_path):
    app = tmp_path / "app"; staged = tmp_path / "staged"; backup = tmp_path / "bak"
    old = app / "_internal" / "uvicorn-0.52.4.dist-info"
    _write(str(old / "METADATA"), "Version: 0.52.4")
    _write(str(old / "RECORD"), "old")
    _write(str(app / "_internal" / "certifi-2026.7.22.dist-info" / "METADATA"),
           "Version: 2026.7.22")                          # untouched package
    # A different package that merely shares the prefix must not be retired.
    _write(str(app / "_internal" / "uvicorn_worker-1.0.dist-info" / "METADATA"),
           "Version: 1.0")
    _write(str(staged / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"),
           "Version: 0.53.0")

    assert uc.apply_update(str(staged), str(app), str(backup)) is True

    assert not old.exists()
    assert (app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA").exists()
    assert (app / "_internal" / "certifi-2026.7.22.dist-info" / "METADATA").exists()
    assert (app / "_internal" / "uvicorn_worker-1.0.dist-info" / "METADATA").exists()
    # Retired, not destroyed: it sits in the backup tree for rollback.
    assert (backup / "_internal" / "uvicorn-0.52.4.dist-info" / "METADATA").read_text() \
        == "Version: 0.52.4"


def test_apply_update_a_bump_of_the_same_version_keeps_its_own_folder(tmp_path):
    app = tmp_path / "app"; staged = tmp_path / "staged"; backup = tmp_path / "bak"
    _write(str(app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"), "old")
    _write(str(app / "_internal" / "uvicorn-0.53.0.dist-info" / "RECORD"), "keep")
    _write(str(staged / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"), "new")

    assert uc.apply_update(str(staged), str(app), str(backup)) is True

    assert (app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA").read_text() == "new"
    assert (app / "_internal" / "uvicorn-0.53.0.dist-info" / "RECORD").read_text() == "keep"


def test_apply_update_rollback_restores_a_retired_dist_info(tmp_path):
    app = tmp_path / "app"; staged = tmp_path / "staged"; backup = tmp_path / "bak"
    old = app / "_internal" / "uvicorn-0.52.4.dist-info"
    _write(str(old / "METADATA"), "Version: 0.52.4")
    _write(str(staged / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"),
           "Version: 0.53.0")
    _write(str(staged / "z.txt"), "NEW-z")

    calls = {"n": 0}
    def flaky_copy(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        import shutil
        shutil.copy2(src, dst)

    with pytest.raises(OSError):
        uc.apply_update(str(staged), str(app), str(backup), _copyfn=flaky_copy)

    assert (old / "METADATA").read_text() == "Version: 0.52.4"
    assert not (app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA").exists()


# ── retire_duplicate_dist_infos (the launch-time sweep) ──────────────────────
# The retirement above only runs inside an update, and the update that first
# ships it is still applied by the previous build's updater.exe — so an install
# that already carries two folders keeps both. A delta never re-ships an
# unchanged dist-info either. The app therefore sweeps its own install folder
# once per launch: same package, several dist-info folders → keep the newest.
def test_retire_duplicate_dist_infos_keeps_only_the_newest_version(tmp_path):
    app = tmp_path / "app"
    internal = app / "_internal"
    _write(str(internal / "uvicorn-0.52.4.dist-info" / "METADATA"), "Version: 0.52.4")
    _write(str(internal / "uvicorn-0.53.0.dist-info" / "METADATA"), "Version: 0.53.0")
    # Sorts above 0.53.0 as text; must lose to it as a version.
    _write(str(internal / "uvicorn-0.9.9.dist-info" / "METADATA"), "Version: 0.9.9")
    _write(str(internal / "certifi-2026.7.22.dist-info" / "METADATA"), "keep")
    _write(str(internal / "uvicorn_worker-1.0.dist-info" / "METADATA"), "keep")
    _write(str(internal / "uvicorn" / "__init__.py"), "keep")   # the package itself

    removed = uc.retire_duplicate_dist_infos(str(app))

    assert sorted(removed) == sorted([
        str(internal / "uvicorn-0.52.4.dist-info"),
        str(internal / "uvicorn-0.9.9.dist-info"),
    ])
    assert not (internal / "uvicorn-0.52.4.dist-info").exists()
    assert not (internal / "uvicorn-0.9.9.dist-info").exists()
    assert (internal / "uvicorn-0.53.0.dist-info" / "METADATA").exists()
    assert (internal / "certifi-2026.7.22.dist-info" / "METADATA").exists()
    assert (internal / "uvicorn_worker-1.0.dist-info" / "METADATA").exists()
    assert (internal / "uvicorn" / "__init__.py").exists()


def test_retire_duplicate_dist_infos_also_sweeps_the_install_root(tmp_path):
    # PyInstaller < 6 laid the metadata beside the exe rather than in _internal.
    app = tmp_path / "app"
    _write(str(app / "yt_dlp-2026.1.1.dist-info" / "METADATA"), "old")
    _write(str(app / "yt_dlp-2026.2.2.dist-info" / "METADATA"), "new")

    removed = uc.retire_duplicate_dist_infos(str(app))

    assert removed == [str(app / "yt_dlp-2026.1.1.dist-info")]
    assert (app / "yt_dlp-2026.2.2.dist-info" / "METADATA").exists()


def test_retire_duplicate_dist_infos_with_nothing_to_do(tmp_path):
    app = tmp_path / "app"
    _write(str(app / "_internal" / "certifi-2026.7.22.dist-info" / "METADATA"), "x")
    _write(str(app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"), "x")

    assert uc.retire_duplicate_dist_infos(str(app)) == []
    assert (app / "_internal" / "certifi-2026.7.22.dist-info" / "METADATA").exists()
    assert (app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA").exists()


def test_retire_duplicate_dist_infos_never_raises(tmp_path):
    # A missing install folder (or one the process can't list) is a no-op:
    # this runs on every launch and must never keep the window from opening.
    assert uc.retire_duplicate_dist_infos(str(tmp_path / "nope")) == []
    # str.isdigit accepts characters int() rejects (superscript two); a folder
    # named that way must not turn the launch-time sweep into a crash.
    app = tmp_path / "app"
    _write(str(app / "foo-1.².dist-info" / "METADATA"), "x")
    _write(str(app / "foo-1.0.dist-info" / "METADATA"), "x")
    uc.retire_duplicate_dist_infos(str(app))          # must not raise


def test_retire_duplicate_dist_infos_only_looks_at_directories_one_level_deep(tmp_path):
    app = tmp_path / "app"
    _write(str(app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"), "x")
    _write(str(app / "_internal" / "uvicorn-0.1.0.dist-info"), "a file, not a folder")
    _write(str(app / "_internal" / "deeper" / "uvicorn-0.2.0.dist-info" / "METADATA"), "x")

    assert uc.retire_duplicate_dist_infos(str(app)) == []
    assert (app / "_internal" / "uvicorn-0.1.0.dist-info").is_file()
    assert (app / "_internal" / "deeper" / "uvicorn-0.2.0.dist-info" / "METADATA").exists()


def test_retire_duplicate_dist_infos_groups_by_normalised_name(tmp_path):
    # PEP 503: `foo.bar`, `foo_bar` and `Foo-Bar` are one distribution.
    app = tmp_path / "app"
    _write(str(app / "_internal" / "foo.bar-1.0.dist-info" / "METADATA"), "x")
    _write(str(app / "_internal" / "foo_bar-2.0.dist-info" / "METADATA"), "x")

    removed = uc.retire_duplicate_dist_infos(str(app))

    assert removed == [str(app / "_internal" / "foo.bar-1.0.dist-info")]
    assert (app / "_internal" / "foo_bar-2.0.dist-info" / "METADATA").exists()


def test_retire_duplicate_dist_infos_renames_before_deleting(tmp_path, monkeypatch):
    # A locked file can stop rmtree half way. The loser is moved out of the
    # dist-info namespace first, so importlib.metadata never meets a folder
    # with its METADATA gone — and the leftover is swept on the next launch.
    app = tmp_path / "app"
    old = app / "_internal" / "uvicorn-0.52.4.dist-info"
    _write(str(old / "METADATA"), "x")
    _write(str(app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA"), "x")
    monkeypatch.setattr(uc.shutil, "rmtree", lambda p, ignore_errors=False: None)

    removed = uc.retire_duplicate_dist_infos(str(app))

    assert removed == [str(old)]
    assert not old.exists()
    assert (app / "_internal" / "uvicorn-0.52.4.dist-info.retired" / "METADATA").exists()

    monkeypatch.undo()
    assert uc.retire_duplicate_dist_infos(str(app)) == []
    assert not (app / "_internal" / "uvicorn-0.52.4.dist-info.retired").exists()
    assert (app / "_internal" / "uvicorn-0.53.0.dist-info" / "METADATA").exists()


# ── launch_updater_command ──────────────────────────────────────────────────

def test_launch_updater_command_prefers_updater_exe(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "updater.exe").write_text("stub")
    cmd = uc.launch_updater_command(
        1234, str(tmp_path / "staged"), str(app_dir),
        str(app_dir / "DJ-CrateBuilder.exe"), str(tmp_path / "backup"),
        str(tmp_path / "update.log"))
    assert cmd[0] == str(app_dir / "updater.exe")
    assert "--pid" in cmd and cmd[cmd.index("--pid") + 1] == "1234"
    assert cmd[cmd.index("--src") + 1] == str(tmp_path / "staged")
    assert cmd[cmd.index("--dst") + 1] == str(app_dir)
    assert cmd[cmd.index("--relaunch") + 1] == str(app_dir / "DJ-CrateBuilder.exe")
    assert cmd[cmd.index("--backup") + 1] == str(tmp_path / "backup")
    assert cmd[cmd.index("--log") + 1] == str(tmp_path / "update.log")


def test_launch_updater_command_falls_back_to_source(tmp_path):
    # No updater.exe in app_dir: dev fallback drives updater.py with Python.
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    cmd = uc.launch_updater_command(
        99, str(tmp_path / "staged"), str(app_dir), "relaunch.exe",
        str(tmp_path / "backup"), str(tmp_path / "update.log"))
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("updater.py")
    assert os.path.isfile(cmd[1])

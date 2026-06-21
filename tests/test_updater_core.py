"""Tests for the self-update core (manifest, integrity, file swap).

All pure / filesystem logic — no network and no tkinter. Network functions
(fetch_manifest, download) are exercised with an injected fake opener.
"""
import hashlib
import io
import json
import os

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

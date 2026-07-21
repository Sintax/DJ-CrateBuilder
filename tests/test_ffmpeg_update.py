"""FFmpeg self-update: manifest block validation, version marker, action, install."""
import os
import zipfile

import pytest

from cratebuilder import updater_core as uc
from cratebuilder.updater_core import sha256_file


# ── Task 1: block validation + version marker ────────────────────────────────
def test_validate_ffmpeg_block_accepts_good_block():
    ok, reason = uc.validate_ffmpeg_block(
        {"version": "7.0.2+a1b2c3d4", "url": "http://x/z.zip", "sha256": "a" * 64})
    assert ok and reason == ""


def test_validate_ffmpeg_block_rejects_bad():
    assert uc.validate_ffmpeg_block(None)[0] is False
    assert uc.validate_ffmpeg_block({})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "", "url": "u", "sha256": "a" * 64})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "v", "url": "", "sha256": "a" * 64})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "v", "url": "u", "sha256": "xyz"})[0] is False


def test_version_marker_roundtrip(tmp_path):
    assert uc.read_ffmpeg_version(str(tmp_path)) is None
    uc.write_ffmpeg_version(str(tmp_path), "7.0.2+a1b2c3d4")
    assert uc.read_ffmpeg_version(str(tmp_path)) == "7.0.2+a1b2c3d4"
    # blank file reads as None
    open(os.path.join(str(tmp_path), uc.FFMPEG_VERSION_FILE), "w").close()
    assert uc.read_ffmpeg_version(str(tmp_path)) is None


# ── Task 2: update-action classifier ─────────────────────────────────────────
def _manifest(ver):
    return {"build": 40, "url": "u", "sha256": "a" * 64,
            "ffmpeg": {"version": ver, "url": "http://x/z.zip", "sha256": "b" * 64}}


def test_action_none_without_block():
    assert uc.ffmpeg_update_action({"build": 40}, None) == "none"
    assert uc.ffmpeg_update_action("nope", "7.0") == "none"


def test_action_adopt_when_no_marker():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+aa"), None) == "adopt"


def test_action_update_when_differs():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+bb"), "7.0.2+aa") == "update"


def test_action_none_when_matches():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+aa"), "7.0.2+aa") == "none"


# ── Task 3: install from verified zip ────────────────────────────────────────
def _make_ffmpeg_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ffmpeg.exe", b"NEW-FFMPEG")
        zf.writestr("ffprobe.exe", b"NEW-FFPROBE")
    return path


def test_install_ffmpeg_swaps_and_marks(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    (install / "ffmpeg.exe").write_bytes(b"OLD")
    (install / "ffprobe.exe").write_bytes(b"OLD")
    zip_path = _make_ffmpeg_zip(str(tmp_path / "f.zip"))
    sha = sha256_file(zip_path)

    uc.install_ffmpeg_from_zip(
        zip_path, sha, str(install),
        str(tmp_path / "staged"), str(tmp_path / "backup"), "7.0.2+aa")

    assert (install / "ffmpeg.exe").read_bytes() == b"NEW-FFMPEG"
    assert (install / "ffprobe.exe").read_bytes() == b"NEW-FFPROBE"
    assert uc.read_ffmpeg_version(str(install)) == "7.0.2+aa"


def test_install_ffmpeg_rejects_bad_checksum(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    zip_path = _make_ffmpeg_zip(str(tmp_path / "f.zip"))
    with pytest.raises(ValueError):
        uc.install_ffmpeg_from_zip(
            zip_path, "0" * 64, str(install),
            str(tmp_path / "staged"), str(tmp_path / "backup"), "7.0.2+aa")
    # marker not advanced on failure
    assert uc.read_ffmpeg_version(str(install)) is None

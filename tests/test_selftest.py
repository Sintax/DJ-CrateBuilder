"""cratebuilder.selftest: the built app's own smoke test — packages load,
the bundled FFmpeg encodes, yt-dlp can read a public video. Runs inside the
frozen exe via `--self-test <report>` and reports through that file only."""
import json
import os
import subprocess

import pytest

from cratebuilder import components as comp
from cratebuilder import selftest


def _all_present():
    return {key: "1.0" for key, _l, _d in comp.COMPONENTS}


def test_components_check_names_what_is_missing():
    have = _all_present()
    have["yt-dlp"] = None
    have["pillow"] = None
    out = selftest.check_components(have)
    assert out["ok"] is False
    assert "yt-dlp" in out["detail"] and "pillow" in out["detail"]


def test_components_check_ignores_python_and_ffmpeg_rows():
    """Those two aren't pip packages; a source run reports FFmpeg as None
    and that is not a missing-metadata failure."""
    have = _all_present()
    have["ffmpeg"] = None
    assert selftest.check_components(have)["ok"] is True


def test_ffmpeg_check_fails_without_a_binary(tmp_path):
    out = selftest.check_ffmpeg(None, str(tmp_path))
    assert out["ok"] is False and "not found" in out["detail"]


def test_ffmpeg_check_fails_when_the_encode_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    out = selftest.check_ffmpeg("C:/x/ffmpeg.exe", str(tmp_path))
    assert out["ok"] is False and "no output" in out["detail"]


def test_ffmpeg_check_fails_when_the_binary_errors(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "ffmpeg")
    monkeypatch.setattr(subprocess, "run", boom)
    out = selftest.check_ffmpeg("C:/x/ffmpeg.exe", str(tmp_path))
    assert out["ok"] is False and "encode failed" in out["detail"]


def test_ffmpeg_check_passes_on_a_real_mp3(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        assert "libmp3lame" in cmd
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\xff\xfb" * 100)
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = selftest.check_ffmpeg("C:/x/ffmpeg.exe", str(tmp_path))
    assert out["ok"] is True


def test_probe_check_wants_a_title_and_an_id():
    ok = selftest.check_probe("u", lambda u: {"title": "Me at the zoo", "id": "j"})
    assert ok["ok"] is True and "Me at the zoo" in ok["detail"]
    assert selftest.check_probe("u", lambda u: {"title": ""})["ok"] is False
    assert selftest.check_probe("u", lambda u: None)["ok"] is False


def test_probe_check_turns_an_exception_into_a_failed_check():
    def boom(u):
        raise RuntimeError("Sign in to confirm you're not a bot")
    out = selftest.check_probe("u", boom)
    assert out["ok"] is False and "not a bot" in out["detail"]


def test_run_writes_a_report_and_returns_zero_when_everything_passes(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"x" * 10)
    monkeypatch.setattr(subprocess, "run", fake_run)
    report = tmp_path / "r.json"
    code = selftest.run(str(report), _probe=lambda u: {"title": "t", "id": "i"},
                        _ffmpeg_exe="C:/x/ffmpeg.exe", _installed=_all_present())
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert [c["name"] for c in data["checks"]] == ["components", "ffmpeg", "probe"]
    assert data["components"]["yt-dlp"] == "1.0"
    assert data["python"].count(".") == 2


def test_run_returns_one_and_still_writes_the_report_on_a_failure(tmp_path):
    report = tmp_path / "r.json"
    code = selftest.run(str(report), _probe=lambda u: {},
                        _ffmpeg_exe=None, _installed=_all_present())
    assert code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is False
    failed = {c["name"] for c in data["checks"] if not c["ok"]}
    assert failed == {"ffmpeg", "probe"}


def test_run_never_escapes_an_unexpected_error(tmp_path):
    """No report would read as a hang from the release script's side, so a
    crash inside the checks becomes a failed check in the file instead."""
    class Bad(dict):
        def get(self, *a):
            raise RuntimeError("kaboom")
    report = tmp_path / "r.json"
    code = selftest.run(str(report), _probe=lambda u: {},
                        _ffmpeg_exe=None, _installed=Bad())
    assert code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert any("kaboom" in c["detail"] for c in data["checks"])


def test_the_smoke_url_is_youtube_and_public():
    assert selftest.SMOKE_URL.startswith("https://www.youtube.com/watch?v=")

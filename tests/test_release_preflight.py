"""The nightly's automated currency checks and safety gates.

Every package the Update page lists was already pip-upgraded on each nightly;
this pins the rest of the picture: the Python runtime is compared against
its series' newest release, a runtime change forces a full payload without
the maintainer remembering to ask, the built exe must pass its own self-test
before anything uploads, and --preflight shows all of it read-only.

``scripts/`` is maintainer-local (gitignored), so this file skips wholesale
when the script isn't present.
"""
import importlib.util
import json
import os
import subprocess
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "release.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_SCRIPT),
    reason="scripts/release.py is maintainer-local (gitignored)")


@pytest.fixture(scope="module")
def rel():
    spec = importlib.util.spec_from_file_location("cb_release_preflight", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_release_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Python currency ──────────────────────────────────────────────────────────
_EOL_ROWS = json.dumps([
    {"cycle": "3.15", "latest": "3.15.1"},
    {"cycle": "3.14", "latest": "3.14.7"},
    {"cycle": "3.13", "latest": "3.13.15"},
]).encode()


def test_python_check_flags_an_older_patch_of_the_running_series(rel):
    info = rel.check_python_currency(_fetch=lambda u, timeout: _EOL_ROWS,
                                     running="3.14.5")
    assert info["status"] == "outdated"
    assert info["latest"] == "3.14.7"
    assert info["newest_series"] == "3.15"


def test_python_check_is_current_on_the_newest_patch(rel):
    info = rel.check_python_currency(_fetch=lambda u, timeout: _EOL_ROWS,
                                     running="3.14.7")
    assert info["status"] == "current"


def test_python_check_never_calls_a_newer_series_outdated(rel):
    """3.15 exists, but building on 3.13's newest patch is 'current' —
    moving series is a decision, not a nightly's job."""
    info = rel.check_python_currency(_fetch=lambda u, timeout: _EOL_ROWS,
                                     running="3.13.15")
    assert info["status"] == "current"
    assert info["newest_series"] == "3.15"


def test_python_check_is_unknown_when_upstream_is_unreachable(rel):
    def boom(u, timeout):
        raise OSError("offline")
    info = rel.check_python_currency(_fetch=boom, running="3.14.5")
    assert info["status"] == "unknown"
    assert "offline" in info["detail"]


def test_python_check_is_unknown_for_an_unlisted_series(rel):
    info = rel.check_python_currency(_fetch=lambda u, timeout: _EOL_ROWS,
                                     running="3.9.2")
    assert info["status"] == "unknown"


# ── Runtime moved ⇒ full payload ─────────────────────────────────────────────
def test_runtime_moved_compares_against_the_baseline_state(rel):
    moved, base, run = rel.runtime_moved({"python": "3.14.5"}, running="3.14.7")
    assert (moved, base, run) == (True, "3.14.5", "3.14.7")
    assert rel.runtime_moved({"python": "3.14.7"}, running="3.14.7")[0] is False


def test_runtime_moved_falls_back_to_the_published_manifest(rel):
    """A baseline written before the field existed still gets an answer,
    from the components block the last nightly published."""
    manifest = {"components": {"python": "3.14.5"}}
    assert rel.runtime_moved({"base_build": 83}, manifest, running="3.14.7")[0]
    assert not rel.runtime_moved({"base_build": 83}, manifest, running="3.14.5")[0]


def test_runtime_moved_is_false_when_nothing_is_known(rel):
    assert rel.runtime_moved(None, None, running="3.14.7") == (False, None, "3.14.7")
    assert rel.runtime_moved({}, {}, running="3.14.7")[0] is False


def test_save_state_records_the_python_runtime(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(90, {"a": "1"}, deps={"yt-dlp": "1"}, python_version="3.14.7")
    state = rel.load_state()
    assert state["python"] == "3.14.7"
    assert rel.runtime_moved(state, running="3.14.7")[0] is False
    assert rel.runtime_moved(state, running="3.15.0")[0] is True


# ── Dependency preview ───────────────────────────────────────────────────────
def test_preview_reads_pip_dry_run_report(rel):
    freeze = "yt_dlp==2026.8.19\ncertifi==2026.7.22\nPillow==12.3.0\n"
    report = json.dumps({"install": [
        {"metadata": {"name": "yt-dlp", "version": "2026.9.10"}},
    ]})
    out = rel.preview_dependency_upgrades(
        wanted=("yt-dlp", "certifi", "Pillow"),
        _run=lambda cmd: types.SimpleNamespace(stdout=report),
        _freeze=lambda: freeze)
    assert out["yt-dlp"] == ("2026.8.19", "2026.9.10")
    assert out["certifi"] == ("2026.7.22", "2026.7.22")
    assert out["Pillow"] == ("12.3.0", "12.3.0")


def test_preview_passes_dry_run_and_never_installs(rel):
    seen = []

    def run(cmd):
        seen.append(cmd)
        return types.SimpleNamespace(stdout="{}")
    rel.preview_dependency_upgrades(wanted=("yt-dlp",), _run=run,
                                    _freeze=lambda: "yt_dlp==1\n")
    assert "--dry-run" in seen[0] and "--upgrade" in seen[0]


def test_preview_is_none_when_pip_cannot_answer(rel):
    def boom(cmd):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert rel.preview_dependency_upgrades(wanted=("yt-dlp",), _run=boom,
                                           _freeze=lambda: "") is None


# ── Build smoke test ─────────────────────────────────────────────────────────
def _fake_exe_run(report):
    """A subprocess.run stand-in that behaves like the built exe: writes the
    report the release script asked for, returns the exit code it implies."""
    def run(cmd, **kw):
        path = cmd[cmd.index("--self-test") + 1]
        if report is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
        return types.SimpleNamespace(returncode=0 if (report or {}).get("ok") else 1)
    return run


def test_smoke_test_passes_and_returns_the_report(rel, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    report = {"ok": True, "checks": [
        {"name": "components", "ok": True, "detail": "11 packages present"},
        {"name": "ffmpeg", "ok": True, "detail": "encoded"},
        {"name": "probe", "ok": True, "detail": "title: Me at the zoo"}]}
    out = rel.smoke_test_build(str(tmp_path / "dist"), _run=_fake_exe_run(report))
    assert out["ok"] is True
    text = capsys.readouterr().out
    assert "Me at the zoo" in text and "Smoke test passed" in text


def test_smoke_test_runs_the_built_exe_with_the_self_test_flag(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _fake_exe_run({"ok": True, "checks": [{"name": "x", "ok": True}]})(cmd, **kw)
    rel.smoke_test_build(str(tmp_path / "dist"), _run=run)
    exe = seen[0][0]
    assert exe.startswith(str(tmp_path / "dist"))
    assert os.path.basename(exe).startswith(rel.APP_NAME)
    assert seen[0][1] == "--self-test"


def test_smoke_test_failure_names_the_failed_checks_and_exits(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    report = {"ok": False, "checks": [
        {"name": "components", "ok": True, "detail": ""},
        {"name": "probe", "ok": False, "detail": "HTTP Error 403"}]}
    with pytest.raises(SystemExit) as info:
        rel.smoke_test_build(str(tmp_path / "dist"), _run=_fake_exe_run(report))
    assert "probe" in str(info.value) and "components" not in str(info.value)
    assert "--no-smoke-test" in str(info.value)


def test_smoke_test_with_no_report_is_a_crash_not_a_pass(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as info:
        rel.smoke_test_build(str(tmp_path / "dist"), _run=_fake_exe_run(None))
    assert "no report" in str(info.value)


def test_smoke_test_timeout_exits(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    with pytest.raises(SystemExit) as info:
        rel.smoke_test_build(str(tmp_path / "dist"), timeout=5, _run=hang)
    assert "timed out" in str(info.value)


def test_smoke_test_stale_report_is_removed_first(rel, tmp_path, monkeypatch):
    """A report left by the previous build must never pass this one."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    stale = tmp_path / rel.SMOKE_REPORT
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"ok": True, "checks": [{"name": "x", "ok": True}]}))

    def crash(cmd, **kw):
        return types.SimpleNamespace(returncode=3)
    with pytest.raises(SystemExit) as info:
        rel.smoke_test_build(str(tmp_path / "dist"), _run=crash)
    assert "no report" in str(info.value)


# ── FFmpeg staging shared by both publish paths ──────────────────────────────
def test_stage_ffmpeg_update_is_none_when_the_channel_already_has_it(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "derive_ffmpeg_version", lambda exe: "9.0.1-essentials+abc")
    manifest = {"ffmpeg": {"version": "9.0.1-essentials+abc"}}
    assert rel.stage_ffmpeg_update(manifest, "f.exe", "p.exe") is None


def test_stage_ffmpeg_update_zips_and_composes_the_block(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(rel, "derive_ffmpeg_version", lambda exe: "9.0.1-essentials+abc")
    ff = tmp_path / "ffmpeg.exe"
    fp = tmp_path / "ffprobe.exe"
    ff.write_bytes(b"F" * 100)
    fp.write_bytes(b"P" * 100)
    zip_path, block = rel.stage_ffmpeg_update({"ffmpeg": {"version": "8.1"}},
                                              str(ff), str(fp))
    assert os.path.isfile(zip_path)
    assert os.path.basename(zip_path) == "ffmpeg-9.0.1-essentials_abc.zip"
    assert block["version"] == "9.0.1-essentials+abc"
    assert block["url"].endswith("/ffmpeg-9.0.1-essentials_abc.zip")
    assert len(block["sha256"]) == 64


# ── Flags exist and are wired ────────────────────────────────────────────────
def test_new_flags_are_documented(rel, capsys):
    with pytest.raises(SystemExit):
        rel.main(["--help"])
    text = capsys.readouterr().out
    for flag in ("--preflight", "--python-check", "--with-ffmpeg",
                 "--no-smoke-test"):
        assert flag in text, flag


def test_preflight_is_read_only_and_reports_every_line(rel, monkeypatch, capsys):
    monkeypatch.setattr(rel, "check_python_currency", lambda: {
        "running": "3.14.5", "series": "3.14", "latest": "3.14.7",
        "newest_series": "3.15", "status": "outdated", "detail": ""})
    monkeypatch.setattr(rel, "check_ffmpeg_currency", lambda src_dir=None: {
        "exe": "x", "local": "9.0.1", "latest": "9.1", "status": "outdated",
        "detail": ""})
    monkeypatch.setattr(rel, "preview_dependency_upgrades", lambda: {
        "yt-dlp": ("2026.8.19", "2026.9.10"), "certifi": ("1", "1")})
    monkeypatch.setattr(rel, "load_state", lambda: {"base_build": 83, "python": "3.14.5"})
    monkeypatch.setattr(rel, "read_remote_manifest", lambda: {})
    monkeypatch.setattr(rel, "platform", types.SimpleNamespace(
        python_version=lambda: "3.14.5"))
    facts = rel.preflight(with_ffmpeg=True)
    text = capsys.readouterr().out
    assert "3.14.5" in text and "3.14.7" in text and "NEWER AVAILABLE" in text
    assert "will download, test and publish" in text
    assert "yt-dlp" in text and "will upgrade" in text
    assert "delta vs build 83" in text
    assert facts["runtime_moved"] is False
    assert "Smoke test" in text


# ── Baseline-age warning ─────────────────────────────────────────────────────
def _quiet_preflight(rel, monkeypatch, state, remote):
    """Stub every currency probe so preflight is offline, leaving only the
    baseline-age decision under test."""
    monkeypatch.setattr(rel, "check_python_currency", lambda: {
        "running": "3.14.5", "series": "3.14", "latest": "3.14.5",
        "newest_series": "3.14", "status": "current", "detail": ""})
    monkeypatch.setattr(rel, "check_ffmpeg_currency", lambda src_dir=None: {
        "exe": "x", "local": "9.0.1", "latest": "9.0.1", "status": "current",
        "detail": ""})
    monkeypatch.setattr(rel, "preview_dependency_upgrades", lambda: {})
    monkeypatch.setattr(rel, "load_state", lambda: state)
    monkeypatch.setattr(rel, "read_remote_manifest", lambda: remote)
    monkeypatch.setattr(rel, "platform", types.SimpleNamespace(
        python_version=lambda: "3.14.5"))


def test_preflight_warns_when_the_baseline_is_far_behind(rel, monkeypatch, capsys):
    _quiet_preflight(rel, monkeypatch,
                     {"base_build": 60, "python": "3.14.5"}, {"build": 90})
    facts = rel.preflight()
    text = capsys.readouterr().out
    assert facts["baseline_warning"] is True
    assert "builds old" in text and "--full" in text


def test_preflight_is_quiet_when_the_baseline_is_recent(rel, monkeypatch, capsys):
    _quiet_preflight(rel, monkeypatch,
                     {"base_build": 88, "python": "3.14.5"}, {"build": 90})
    facts = rel.preflight()
    text = capsys.readouterr().out
    assert facts["baseline_warning"] is False
    assert "Baseline" not in text


def test_preflight_baseline_warning_is_silent_on_a_forced_full(rel, monkeypatch, capsys):
    _quiet_preflight(rel, monkeypatch,
                     {"base_build": 60, "python": "3.14.5"}, {"build": 90})
    facts = rel.preflight(force_full=True)
    assert facts["baseline_warning"] is False

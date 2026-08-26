"""Release-script logic: bundled-dependency resolution and reporting.

``scripts/`` is deliberately kept out of the public repo (it carries the
maintainer's publishing setup), so these tests skip wholesale rather than
erroring when the script isn't present — a contributor's checkout still runs
the rest of the suite green.
"""
import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "release.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_SCRIPT),
    reason="scripts/release.py is maintainer-local (gitignored)")


def _load_release():
    spec = importlib.util.spec_from_file_location("cb_release", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_release"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rel():
    return _load_release()


# ── parse_installed_versions ──────────────────────────────────────────────────

FREEZE = """\
certifi==2026.7.22
Pillow==12.3.0
mutagen==1.48.1
pystray==0.19.5
send2trash==1.8.3
yt-dlp==2026.7.4
some-other-pkg==1.0.0
"""


def test_parse_installed_versions_picks_wanted_only(rel):
    got = rel.parse_installed_versions(FREEZE, ["yt-dlp", "certifi"])
    assert got == {"yt-dlp": "2026.7.4", "certifi": "2026.7.22"}


def test_parse_installed_versions_is_name_normalised(rel):
    """pip prints 'Pillow'/'send2trash'; a lookup for 'pillow'/'Send2Trash'
    must still match, and the wanted spelling is what comes back."""
    got = rel.parse_installed_versions(FREEZE, ["pillow", "Send2Trash"])
    assert got == {"pillow": "12.3.0", "Send2Trash": "1.8.3"}


def test_parse_installed_versions_underscore_dash_equivalence(rel):
    got = rel.parse_installed_versions("yt_dlp==2026.7.4\n", ["yt-dlp"])
    assert got == {"yt-dlp": "2026.7.4"}


def test_parse_installed_versions_missing_package_reported_as_none(rel):
    got = rel.parse_installed_versions(FREEZE, ["nonexistent"])
    assert got == {"nonexistent": None}


def test_parse_installed_versions_ignores_junk_lines(rel):
    text = "# comment\n\n-e git+https://x#egg=thing\ncertifi==2026.7.22\n"
    assert rel.parse_installed_versions(text, ["certifi"]) == {
        "certifi": "2026.7.22"}


# ── format_dep_report ─────────────────────────────────────────────────────────

def test_format_dep_report_flags_upgrades_and_steady_state(rel):
    before = {"yt-dlp": "2026.6.9", "certifi": "2026.7.22"}
    after = {"yt-dlp": "2026.7.4", "certifi": "2026.7.22"}
    lines = rel.format_dep_report(before, after)
    joined = "\n".join(lines)
    assert "yt-dlp" in joined and "2026.6.9" in joined and "2026.7.4" in joined
    assert "certifi" in joined and "2026.7.22" in joined
    # the upgraded line is marked, the unchanged one is not
    ytdlp_line = next(l for l in lines if "yt-dlp" in l)
    certifi_line = next(l for l in lines if "certifi" in l)
    assert "->" in ytdlp_line
    assert "->" not in certifi_line


def test_format_dep_report_marks_missing_package(rel):
    lines = rel.format_dep_report({"pystray": None}, {"pystray": None})
    assert any("NOT INSTALLED" in l for l in lines)


def test_format_dep_report_handles_newly_installed(rel):
    lines = rel.format_dep_report({"mutagen": None}, {"mutagen": "1.48.1"})
    assert any("1.48.1" in l for l in lines)


# ── bundled dependency list ───────────────────────────────────────────────────

def test_bundled_deps_covers_the_runtime_requirements(rel):
    """Every non-comment entry in requirements.txt must be tracked, or a build
    can silently ship a dependency nobody is watching."""
    req_path = os.path.join(_ROOT, "requirements.txt")
    with open(req_path, encoding="utf-8") as f:
        wanted = {
            rel.normalize_pkg_name(
                line.split("=")[0].split(">")[0].split("<")[0].strip())
            for line in f
            if line.strip() and not line.strip().startswith("#")
        }
    tracked = {rel.normalize_pkg_name(p) for p in rel.BUNDLED_DEPS}
    assert wanted <= tracked, f"untracked runtime deps: {wanted - tracked}"


def test_normalize_pkg_name_drops_extras(rel):
    """Extras are a requirement specifier, not a name — pip freeze reports the
    bare package, so uvicorn[standard] must match a plain uvicorn pin."""
    assert rel.normalize_pkg_name("uvicorn[standard]") == "uvicorn"
    assert rel.parse_installed_versions(
        "uvicorn==0.44.0\n", ["uvicorn[standard]"]) == {"uvicorn[standard]": "0.44.0"}


def test_bundled_deps_includes_certifi(rel):
    """certifi is a transitive dep (never in requirements.txt) but its CA
    bundle is frozen into the exe, so a stale one ships to every user."""
    tracked = {rel.normalize_pkg_name(p) for p in rel.BUNDLED_DEPS}
    assert "certifi" in tracked


# ── state file records the resolved versions ──────────────────────────────────

def test_save_state_records_deps(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(51, {"a.exe": "hash"}, deps={"yt-dlp": "2026.7.4"})
    data = json.load(open(tmp_path / rel.STATE_FILE, encoding="utf-8"))
    assert data["base_build"] == 51
    assert data["files"] == {"a.exe": "hash"}
    assert data["deps"] == {"yt-dlp": "2026.7.4"}


def test_save_state_without_deps_stays_backward_compatible(rel, tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(51, {"a.exe": "hash"})
    data = json.load(open(tmp_path / rel.STATE_FILE, encoding="utf-8"))
    assert data["base_build"] == 51
    assert "deps" not in data or data["deps"] == {}


def test_load_state_reads_back_a_saved_state(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(7, {"x": "y"}, deps={"certifi": "2026.7.22"})
    assert rel.load_state()["deps"] == {"certifi": "2026.7.22"}


# ── record_build_deps: per-build history that must not disturb the baseline ───

def test_record_build_deps_preserves_the_delta_baseline(rel, tmp_path,
                                                        monkeypatch):
    """A delta nightly records its dependency set without rewriting the
    baseline — clobbering `files`/`base_build` would break delta composition
    for users who skipped builds."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(40, {"app.exe": "aaa"})
    rel.record_build_deps(51, {"yt-dlp": "2026.7.4"})
    state = rel.load_state()
    assert state["base_build"] == 40
    assert state["files"] == {"app.exe": "aaa"}
    assert state["builds"]["51"] == {"yt-dlp": "2026.7.4"}


def test_record_build_deps_accumulates_across_builds(rel, tmp_path,
                                                     monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(40, {})
    rel.record_build_deps(50, {"certifi": "2026.1.4"})
    rel.record_build_deps(51, {"certifi": "2026.7.22"})
    builds = rel.load_state()["builds"]
    assert builds["50"] == {"certifi": "2026.1.4"}
    assert builds["51"] == {"certifi": "2026.7.22"}


def test_record_build_deps_is_bounded(rel, tmp_path, monkeypatch):
    """History is capped so the state file can't grow without limit; the most
    recent builds are the ones kept."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(1, {})
    for b in range(1, rel.DEP_HISTORY_LIMIT + 6):
        rel.record_build_deps(b, {"yt-dlp": f"v{b}"})
    builds = rel.load_state()["builds"]
    assert len(builds) == rel.DEP_HISTORY_LIMIT
    newest = rel.DEP_HISTORY_LIMIT + 5
    assert builds[str(newest)] == {"yt-dlp": f"v{newest}"}
    assert "1" not in builds


def test_record_build_deps_sorts_numerically_not_lexically(rel, tmp_path,
                                                           monkeypatch):
    """Build numbers are strings in JSON; pruning must not decide that build
    '9' is newer than build '100'."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(1, {})
    monkeypatch.setattr(rel, "DEP_HISTORY_LIMIT", 2)
    for b in (9, 100, 101):
        rel.record_build_deps(b, {"yt-dlp": f"v{b}"})
    builds = rel.load_state()["builds"]
    assert set(builds) == {"100", "101"}


def test_record_build_deps_no_state_file_is_a_safe_noop(rel, tmp_path,
                                                        monkeypatch):
    """Never create a state file from scratch here: a state with no `files`
    would make choose_payload treat every file as changed."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.record_build_deps(51, {"yt-dlp": "2026.7.4"})
    assert not os.path.exists(os.path.join(str(tmp_path), rel.STATE_FILE))

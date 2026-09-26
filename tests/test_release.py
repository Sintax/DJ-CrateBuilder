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
import types

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


def test_record_build_deps_preserves_the_full_download_pointers(rel, tmp_path,
                                                                monkeypatch):
    """A delta nightly re-reads full_url/full_sha256 from the state file to
    fill its manifest, and record_build_deps rewrites that file every night —
    so it must keep those pointers intact or auto-repair silently breaks."""
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(40, {"app.exe": "aaa"}, full_url="https://x/full-40.zip",
                   full_sha256="e" * 64)
    rel.record_build_deps(51, {"yt-dlp": "2026.7.4"})
    state = rel.load_state()
    assert state["full_url"] == "https://x/full-40.zip"
    assert state["full_sha256"] == "e" * 64


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


# ── delta-baseline guard: full-zip naming, auto-repair pointers, retention ────

def test_payload_zip_name_marks_full_with_an_infix(rel):
    assert rel.payload_zip_name("2.1", 100, True) == \
        "DJ-CrateBuilder-full-2.1.100.zip"
    assert rel.payload_zip_name("2.1", 100, False) == \
        "DJ-CrateBuilder-2.1.100.zip"


def test_save_state_records_the_full_download(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(90, {"a": "1"}, full_url="https://x/full-90.zip",
                   full_sha256="d" * 64)
    data = json.load(open(tmp_path / rel.STATE_FILE, encoding="utf-8"))
    assert data["full_url"] == "https://x/full-90.zip"
    assert data["full_sha256"] == "d" * 64


def test_save_state_omits_full_fields_when_absent(rel, tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    rel.save_state(90, {"a": "1"})
    data = json.load(open(tmp_path / rel.STATE_FILE, encoding="utf-8"))
    assert "full_url" not in data and "full_sha256" not in data


def test_full_manifest_fields_on_a_full_points_at_the_new_zip(rel):
    out = rel.full_manifest_fields(True, "https://x/full-100.zip", "a" * 64,
                                   None, {})
    assert out == {"full_url": "https://x/full-100.zip", "full_sha256": "a" * 64}


def test_full_manifest_fields_echoes_from_state_on_a_delta(rel):
    state = {"full_url": "https://x/full-90.zip", "full_sha256": "b" * 64}
    out = rel.full_manifest_fields(False, "ignored", "ignored", state,
                                   {"full_url": "stale", "full_sha256": "stale"})
    assert out == {"full_url": "https://x/full-90.zip", "full_sha256": "b" * 64}


def test_full_manifest_fields_carries_forward_from_the_live_manifest(rel):
    """A state written before this feature has no full_* fields; the last
    published manifest still carries them."""
    manifest = {"full_url": "https://x/full-90.zip", "full_sha256": "c" * 64}
    out = rel.full_manifest_fields(False, "i", "i", {"base_build": 90}, manifest)
    assert out == {"full_url": "https://x/full-90.zip", "full_sha256": "c" * 64}


def test_full_manifest_fields_omitted_when_no_full_is_known(rel):
    assert rel.full_manifest_fields(False, "i", "i", {"base_build": 90}, {}) == {}
    assert rel.full_manifest_fields(False, "i", "i", None, None) == {}


def _fake_gh(assets, deleted):
    def run(cmd, **kw):
        if "view" in cmd:
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join(assets) + "\n", stderr="")
        if "delete-asset" in cmd:
            deleted.append(cmd[cmd.index("delete-asset") + 2])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_prune_keeps_the_current_full_on_a_delta(rel, monkeypatch):
    """A delta publish prunes old deltas but spares the retained full (which
    every later delta's auto-repair points at) and the FFmpeg channel."""
    deleted = []
    assets = ["DJ-CrateBuilder-full-2.1.90.zip", "DJ-CrateBuilder-2.1.95.zip",
              "DJ-CrateBuilder-2.1.96.zip", "ffmpeg-9.0.1.zip"]
    monkeypatch.setattr(rel.subprocess, "run", _fake_gh(assets, deleted))
    rel.prune_old_zip_assets("R/E", "nightly",
                             keep="DJ-CrateBuilder-2.1.96.zip",
                             prefix="DJ-CrateBuilder-", exclude_substr="-full-")
    assert deleted == ["DJ-CrateBuilder-2.1.95.zip"]


def test_prune_removes_old_full_and_deltas_on_a_full(rel, monkeypatch):
    deleted = []
    assets = ["DJ-CrateBuilder-full-2.1.90.zip", "DJ-CrateBuilder-2.1.95.zip",
              "DJ-CrateBuilder-full-2.1.100.zip", "ffmpeg-9.0.1.zip"]
    monkeypatch.setattr(rel.subprocess, "run", _fake_gh(assets, deleted))
    rel.prune_old_zip_assets("R/E", "nightly",
                             keep="DJ-CrateBuilder-full-2.1.100.zip",
                             prefix="DJ-CrateBuilder-", exclude_substr=None)
    assert set(deleted) == {"DJ-CrateBuilder-full-2.1.90.zip",
                            "DJ-CrateBuilder-2.1.95.zip"}
    assert "ffmpeg-9.0.1.zip" not in deleted


def test_prune_spares_what_the_live_manifest_still_points_at(rel, monkeypatch):
    """The raw CDN serves the previous update.json for up to 5 minutes after a
    publish; the payload it names must still download until the next publish."""
    deleted = []
    assets = ["DJ-CrateBuilder-full-2.1.95.zip", "DJ-CrateBuilder-2.1.98.zip",
              "DJ-CrateBuilder-2.1.99.zip", "DJ-CrateBuilder-2.1.100.zip"]
    monkeypatch.setattr(rel.subprocess, "run", _fake_gh(assets, deleted))
    rel.prune_old_zip_assets("R/E", "nightly",
                             keep="DJ-CrateBuilder-2.1.100.zip",
                             prefix="DJ-CrateBuilder-", exclude_substr="-full-",
                             spare={"DJ-CrateBuilder-2.1.99.zip"})
    assert deleted == ["DJ-CrateBuilder-2.1.98.zip"]


def test_prune_on_a_full_spares_the_previous_delta_and_full(rel, monkeypatch):
    deleted = []
    assets = ["DJ-CrateBuilder-full-2.1.95.zip", "DJ-CrateBuilder-2.1.99.zip",
              "DJ-CrateBuilder-full-2.1.100.zip"]
    monkeypatch.setattr(rel.subprocess, "run", _fake_gh(assets, deleted))
    rel.prune_old_zip_assets("R/E", "nightly",
                             keep="DJ-CrateBuilder-full-2.1.100.zip",
                             prefix="DJ-CrateBuilder-", exclude_substr=None,
                             spare={"DJ-CrateBuilder-2.1.99.zip",
                                    "DJ-CrateBuilder-full-2.1.95.zip"})
    assert deleted == []


def test_manifest_asset_names_lists_every_download_it_names(rel):
    base = "https://github.com/R/E/releases/download/nightly/"
    manifest = {"url": base + "DJ-CrateBuilder-2.1.99.zip",
                "full_url": base + "DJ-CrateBuilder-full-2.1.95.zip",
                "ffmpeg": {"url": base + "ffmpeg-9.0.2.zip"}}
    assert rel.manifest_asset_names(manifest) == {
        "DJ-CrateBuilder-2.1.99.zip", "DJ-CrateBuilder-full-2.1.95.zip",
        "ffmpeg-9.0.2.zip"}


def test_manifest_asset_names_tolerates_an_empty_or_partial_manifest(rel):
    assert rel.manifest_asset_names({}) == set()
    assert rel.manifest_asset_names(None) == set()
    assert rel.manifest_asset_names({"url": "", "ffmpeg": None}) == set()

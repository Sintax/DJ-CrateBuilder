"""cratebuilder.components: the Update page's components table — what this
install has next to what the live build carries, and the release script's
side of the same contract (the manifest's "components" block)."""
import pytest

from cratebuilder import components as comp
from cratebuilder import updater_core as ucore


def test_names_are_pep503_normalised_and_extras_dropped():
    assert comp.normalize_name("Pillow") == "pillow"
    assert comp.normalize_name("yt_dlp") == "yt-dlp"
    assert comp.normalize_name("uvicorn[standard]") == "uvicorn"
    assert comp.normalize_name("Yt.Dlp__EJS") == "yt-dlp-ejs"
    assert comp.normalize_name(None) == ""


def test_every_component_key_is_already_normalised():
    """The manifest's keys are matched against these, so a key that isn't in
    its own normal form could never match."""
    for key, label, dist in comp.COMPONENTS:
        assert comp.normalize_name(key) == key, key
        assert label
        if dist is not None:
            assert comp.normalize_name(dist) == key, (key, dist)


def test_installed_versions_reads_python_ffmpeg_and_each_package():
    seen = []

    def pkg(dist):
        seen.append(dist)
        return {"yt-dlp": "2026.8.19", "Pillow": "12.3.0"}.get(dist)

    out = comp.installed_versions(ffmpeg_dir="C:/app",
                                  _package_version=pkg,
                                  _ffmpeg=lambda d: f"ff@{d}")
    assert out["python"].count(".") == 2
    assert out["ffmpeg"] == "ff@C:/app"
    assert out["yt-dlp"] == "2026.8.19"
    assert out["pillow"] == "12.3.0"
    assert out["mutagen"] is None                 # pkg() said not installed
    assert sorted(seen) == sorted(d for _k, _l, d in comp.COMPONENTS if d)


def test_a_package_that_is_not_installed_is_none():
    assert comp.package_version("definitely-not-a-real-distribution-xyz") is None


def test_ffmpeg_version_prefers_the_marker_then_the_binary(tmp_path):
    assert comp.ffmpeg_version(None) is None
    assert comp.ffmpeg_version(str(tmp_path), _probe=lambda d: None) is None
    assert comp.ffmpeg_version(str(tmp_path), _probe=lambda d: "9.0.1") == "9.0.1"
    ucore.write_ffmpeg_version(str(tmp_path), "9.0.1-essentials+72a489ec")
    assert comp.ffmpeg_version(str(tmp_path), _probe=lambda d: "wrong") == \
        "9.0.1-essentials+72a489ec"


def test_offered_versions_normalises_keys_and_blanks():
    assert comp.offered_versions(None) is None
    assert comp.offered_versions({"build": 82}) is None      # pre-block manifest
    assert comp.offered_versions({"components": {}}) is None
    assert comp.offered_versions({"components": "nope"}) is None
    out = comp.offered_versions({"components": {
        "Python": "3.14.5", "yt_dlp": " 2026.9.1 ", "Pillow": "", "FFmpeg": None}})
    assert out == {"python": "3.14.5", "yt-dlp": "2026.9.1",
                   "pillow": None, "ffmpeg": None}


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_compare_marks_each_row_with_one_of_the_four_states():
    installed = {"python": "3.14.5", "ffmpeg": "9.0.1+aa", "yt-dlp": "2026.8.19",
                 "pillow": "12.3.0", "mutagen": None}
    offered = {"python": "3.14.5", "yt-dlp": "2026.9.1", "pillow": "12.3.0",
               "mutagen": "1.47.0"}
    rows = comp.compare(installed, offered)
    assert [r["key"] for r in rows] == [k for k, _l, _d in comp.COMPONENTS]
    assert _row(rows, "python")["state"] == comp.SAME
    assert _row(rows, "yt-dlp") == {
        "key": "yt-dlp", "label": "yt-dlp", "installed": "2026.8.19",
        "offered": "2026.9.1", "state": comp.NEWER}
    assert _row(rows, "ffmpeg")["state"] == comp.UNKNOWN   # offered didn't say
    assert _row(rows, "mutagen")["state"] == comp.MISSING  # not installed here
    assert _row(rows, "certifi")["state"] == comp.MISSING


def test_compare_with_no_offer_is_unknown_not_newer():
    """Build 82's manifest predates the block: nothing may be flagged as an
    update on the strength of a missing answer."""
    rows = comp.compare({"python": "3.14.5", "yt-dlp": "2026.8.19"}, None)
    assert _row(rows, "python")["state"] == comp.UNKNOWN
    assert _row(rows, "yt-dlp")["state"] == comp.UNKNOWN
    assert all(r["offered"] is None for r in rows)


def test_compare_ignores_surrounding_whitespace():
    rows = comp.compare({"python": "3.14.5 "}, {"python": " 3.14.5"})
    assert _row(rows, "python")["state"] == comp.SAME


def test_freeze_versions_is_the_release_scripts_side_of_the_contract():
    block = comp.freeze_versions(
        {"yt-dlp": "2026.9.1", "Pillow": "12.3.0", "uvicorn[standard]": "0.40.0",
         "mutagen": None},
        python_version="3.14.5", ffmpeg_version_text=" 9.0.1+aa ")
    assert block == {"python": "3.14.5", "ffmpeg": "9.0.1+aa",
                     "yt-dlp": "2026.9.1", "pillow": "12.3.0", "uvicorn": "0.40.0"}
    # Round trip: what the script writes is what the app reads back.
    assert comp.offered_versions({"components": block}) == block


def test_freeze_versions_without_ffmpeg_or_deps_still_names_python():
    block = comp.freeze_versions({}, python_version="3.14.5")
    assert block == {"python": "3.14.5"}
    assert "python" in comp.freeze_versions(None)

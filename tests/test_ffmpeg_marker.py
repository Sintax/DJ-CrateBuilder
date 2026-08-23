"""The ffmpeg.version marker is a claim; the binary beside it is the evidence.

Build 57 shipped a marker for an FFmpeg its own payload deliberately excluded,
so every install that took it recorded 9.0.1 while still holding 8.1 — and
because the update decision compared marker to manifest and nothing else, that
state was permanent. The app could never fetch the FFmpeg it was being offered.

Two things changed. The release script no longer lets the marker travel in a
payload (pinned in test_release_ffmpeg.py), and the decision below now prefers
what the binary reports over what the marker claims, so an install already
stranded heals itself on the next check.
"""
import os

import pytest

from cratebuilder import updater_core as ucore


OFFERED = "9.0.1-essentials_build-www.gyan.dev+72a489ec"
OFFERED_BUILD = "9.0.1-essentials_build-www.gyan.dev"
OLDER_BUILD = "8.1-essentials_build-www.gyan.dev"
OLDER_MARKER = "8.1-essentials_build-www.gyan.dev+1a65d5b0"

MANIFEST = {"ffmpeg": {"version": OFFERED,
                       "url": "https://example.invalid/ffmpeg.zip",
                       "sha256": "5" * 64}}


def _action(marker, reported=None, manifest=MANIFEST):
    return ucore.ffmpeg_update_action(manifest, marker, reported_build=reported)


# ══════════════════════════════════════════════════════════════════════════════
# The build-57 incident
# ══════════════════════════════════════════════════════════════════════════════
def test_a_marker_that_lies_about_its_binary_forces_an_update():
    """THE regression. Marker says 9.0.1, ffmpeg.exe says 8.1 — exactly what
    every install that took build 57 looks like. Without the binary's evidence
    this answers 'none' and the install is stuck for good."""
    assert _action(OFFERED, reported=OLDER_BUILD) == "update"


def test_the_same_state_without_evidence_is_still_read_as_current():
    """Pinning the old behaviour as the fallback: when the binary can't be
    probed the marker is all there is, and it is trusted exactly as before."""
    assert _action(OFFERED, reported=None) == "none"


def test_an_install_that_really_is_current_is_left_alone():
    """The evidence must not provoke a pointless 73 MB re-download on every
    check for an install that is genuinely fine."""
    assert _action(OFFERED, reported=OFFERED_BUILD) == "none"


# ══════════════════════════════════════════════════════════════════════════════
# adopt — trusting the shipped binary, but only on evidence
# ══════════════════════════════════════════════════════════════════════════════
def test_no_marker_and_a_matching_binary_adopts_without_downloading():
    """The reason adopt exists: the feature's debut must not force a large
    download onto an install that already holds the offered build."""
    assert _action(None, reported=OFFERED_BUILD) == "adopt"


def test_no_marker_and_a_stale_binary_updates_instead_of_adopting():
    """Adopting here would write 9.0.1 over an 8.1 install and recreate the
    exact stranding this file exists to prevent — deleting the marker must not
    be a way back into the bug."""
    assert _action(None, reported=OLDER_BUILD) == "update"


def test_no_marker_and_no_evidence_still_adopts():
    assert _action(None, reported=None) == "adopt"


# ══════════════════════════════════════════════════════════════════════════════
# Ordinary updates and malformed input
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reported", [OLDER_BUILD, None])
def test_a_genuinely_older_marker_updates_either_way(reported):
    assert _action(OLDER_MARKER, reported=reported) == "update"


@pytest.mark.parametrize("manifest", [
    None, {}, "not a dict",
    {"ffmpeg": None},
    {"ffmpeg": {"version": "", "url": "u", "sha256": "5" * 64}},
    {"ffmpeg": {"version": "v", "url": "", "sha256": "5" * 64}},
    {"ffmpeg": {"version": "v", "url": "u", "sha256": "nothex"}},
])
def test_a_bad_or_absent_block_never_triggers_anything(manifest):
    """Evidence must not talk a malformed manifest into an update."""
    assert _action(OFFERED, reported=OLDER_BUILD, manifest=manifest) == "none"


# ══════════════════════════════════════════════════════════════════════════════
# ffmpeg_build_token
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("version,expected", [
    (OFFERED, OFFERED_BUILD),
    (OLDER_MARKER, OLDER_BUILD),
    # The hash-only fallback has no '+' and is returned whole; it can never
    # equal a real report, which the caller reads as "can't verify".
    ("ffmpeg-0123456789abcdef", "ffmpeg-0123456789abcdef"),
    ("  padded+deadbeef  ", "padded"),
    (None, ""),
    ("", ""),
])
def test_the_build_token_is_the_part_before_the_last_plus(version, expected):
    assert ucore.ffmpeg_build_token(version) == expected


def test_a_build_token_containing_a_plus_keeps_all_but_the_hash():
    """rsplit, not split: only the trailing '+<sha8>' the release script adds
    is stripped, whatever the upstream token happens to contain."""
    assert ucore.ffmpeg_build_token("7.1+odd-build+abcd1234") == "7.1+odd-build"


# ══════════════════════════════════════════════════════════════════════════════
# probe_ffmpeg_build
# ══════════════════════════════════════════════════════════════════════════════
def _fake_ffmpeg(tmp_path):
    exe = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_bytes(b"not really a binary")
    return exe


def test_the_probe_reads_the_build_off_the_version_banner(tmp_path):
    _fake_ffmpeg(tmp_path)
    banner = (f"ffmpeg version {OLDER_BUILD} Copyright (c) 2000-2026\n"
              "built with gcc 15.2.0\n")
    assert ucore.probe_ffmpeg_build(str(tmp_path),
                                    _runner=lambda exe: banner) == OLDER_BUILD


def test_the_probe_answers_none_when_there_is_no_binary(tmp_path):
    """A source or Linux run has no bundled ffmpeg.exe — a normal answer, and
    the caller must fall back to the marker rather than see an exception."""
    assert ucore.probe_ffmpeg_build(str(tmp_path)) is None


@pytest.mark.parametrize("output", ["", "garbage that is not a banner", None])
def test_the_probe_answers_none_on_unparseable_output(tmp_path, output):
    _fake_ffmpeg(tmp_path)
    assert ucore.probe_ffmpeg_build(str(tmp_path),
                                    _runner=lambda exe: output) is None


def test_the_probe_never_raises_when_the_binary_explodes(tmp_path):
    """A corrupt or non-executable file must not take the update check down
    with it — this runs on the UI thread."""
    _fake_ffmpeg(tmp_path)

    def _boom(exe):
        raise OSError("not executable")

    assert ucore.probe_ffmpeg_build(str(tmp_path), _runner=_boom) is None


def test_the_real_probe_runs_without_a_console_window(tmp_path):
    """The default runner spawns a subprocess; on Windows it must carry
    CREATE_NO_WINDOW or a console flashes over the GUI on every session."""
    if os.name == "nt":
        assert ucore._NO_WINDOW == getattr(__import__("subprocess"),
                                           "CREATE_NO_WINDOW")
    _fake_ffmpeg(tmp_path)
    # The stub file is not a real executable, so this exercises the failure
    # path of the *real* runner end to end.
    assert ucore.probe_ffmpeg_build(str(tmp_path)) is None


# ══════════════════════════════════════════════════════════════════════════════
# App wiring — the probe is cached, and dropped when the binary changes
# ══════════════════════════════════════════════════════════════════════════════
def test_the_app_probes_once_per_session(app, monkeypatch, tmp_path):
    """The update check runs on the UI thread, so the subprocess behind the
    probe must not be spawned on every check."""
    calls = []
    monkeypatch.setattr(ucore, "probe_ffmpeg_build",
                        lambda d: calls.append(d) or OFFERED_BUILD)

    first = app._installed_ffmpeg_build(str(tmp_path))
    second = app._installed_ffmpeg_build(str(tmp_path))

    assert (first, second) == (OFFERED_BUILD, OFFERED_BUILD)
    assert len(calls) == 1


def test_a_cached_none_is_not_re_probed(app, monkeypatch, tmp_path):
    """None is a real answer (no bundled binary), not a cache miss — probing
    again every check would spawn a subprocess for nothing."""
    calls = []
    monkeypatch.setattr(ucore, "probe_ffmpeg_build",
                        lambda d: calls.append(d) or None)

    assert app._installed_ffmpeg_build(str(tmp_path)) is None
    assert app._installed_ffmpeg_build(str(tmp_path)) is None
    assert len(calls) == 1


def test_the_cache_is_dropped_after_a_swap(app, monkeypatch, tmp_path):
    """The swap replaces the binary, so the session's answer is stale. The
    sentinel the app resets to must be the one that re-probes."""
    calls = []
    monkeypatch.setattr(ucore, "probe_ffmpeg_build",
                        lambda d: calls.append(d) or OLDER_BUILD)
    app._installed_ffmpeg_build(str(tmp_path))

    app._ffmpeg_build_cache = False          # what the swap worker sets
    app._installed_ffmpeg_build(str(tmp_path))

    assert len(calls) == 2

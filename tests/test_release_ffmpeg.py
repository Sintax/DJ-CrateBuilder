"""FFmpeg currency: is the binary we would publish the newest one upstream?

The publish path derives its version from the binaries on this machine and
compares that to the MANIFEST, never to upstream. So without a separate check
a stale FFmpeg publishes as a clean "nothing to do" and nobody finds out —
which is exactly what happened while the channel sat on 8.1 for five months.

`--ffmpeg-check` answers the question and `--ffmpeg-pull` acts on it. Neither
touches GitHub, update.json or APP_BUILD, and both are pinned here as such.

``scripts/`` is maintainer-local (gitignored), so this file skips wholesale
when the script isn't present — a contributor's checkout still runs green.
"""
import importlib.util
import os
import shutil
import sys
import zipfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "release.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_SCRIPT),
    reason="scripts/release.py is maintainer-local (gitignored)")


@pytest.fixture(scope="module")
def rel():
    spec = importlib.util.spec_from_file_location("cb_release_ffmpeg", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_release_ffmpeg"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeRun:
    """subprocess.run stand-in returning one canned `ffmpeg -version` line."""

    def __init__(self, first_line):
        self.stdout = first_line + "\nbuilt with gcc 15.2.0\n"


def _arm_currency(rel, monkeypatch, local, latest, net_error=False):
    """Point the currency check at a fixed local version and upstream answer.

    Both the version lookup and the zip download go through `_http_get`, so
    this splits them: the version URL is answered here, and anything else is
    handed to the returned dict's 'download' hook. That hook defaults to
    failing the test, so a pull that fetches when it shouldn't is caught
    rather than quietly allowed — hand it to `_serve` when a fetch is wanted.
    """
    monkeypatch.setattr(rel, "locate_ffmpeg_binaries",
                        lambda src: (r"C:\ffmpeg\bin\ffmpeg.exe",
                                     r"C:\ffmpeg\bin\ffprobe.exe"))
    monkeypatch.setattr(rel, "local_ffmpeg_release", lambda exe: local)

    hooks = {"download": lambda *a: pytest.fail(
        "downloaded the release zip when it should not have")}

    def _get(url, dest=None, timeout=60):
        if url == rel.GYAN_VERSION_URL:
            if net_error:
                raise OSError("no route to host")
            return latest.encode()
        return hooks["download"](url, dest, timeout)

    monkeypatch.setattr(rel, "_http_get", _get)
    return hooks


def _stub_zip(path, ffmpeg=b"NEW-ffmpeg", ffprobe=b"NEW-ffprobe"):
    """An upstream-shaped archive: binaries nested under <build>/bin/."""
    with zipfile.ZipFile(path, "w") as zf:
        root = "ffmpeg-9.0.1-essentials_build"
        if ffmpeg is not None:
            zf.writestr(f"{root}/bin/ffmpeg.exe", ffmpeg)
        if ffprobe is not None:
            zf.writestr(f"{root}/bin/ffprobe.exe", ffprobe)
        zf.writestr(f"{root}/README.txt", b"ignore me")


def _serve(hooks, zip_src):
    """Let the pull download *zip_src* as the release archive."""
    hooks["download"] = lambda url, dest, timeout: (
        shutil.copy(zip_src, dest) or dest)


# ══════════════════════════════════════════════════════════════════════════════
# local_ffmpeg_release — reading a release number off the binary
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("first_line,expected", [
    ("ffmpeg version 8.1-essentials_build-www.gyan.dev Copyright (c) 2000", "8.1"),
    ("ffmpeg version 9.0.1-essentials_build-www.gyan.dev Copyright (c)", "9.0.1"),
    ("ffmpeg version 7 Copyright (c) 2000-2024", "7"),
    # A git/nightly build carries no release number to compare against.
    ("ffmpeg version N-119345-g1a2b3c4 Copyright (c) 2000-2026", None),
    ("something that is not ffmpeg at all", None),
])
def test_the_release_number_is_read_off_the_build_token(
        rel, monkeypatch, first_line, expected):
    monkeypatch.setattr(rel.subprocess, "run",
                        lambda *a, **k: _FakeRun(first_line))
    assert rel.local_ffmpeg_release("ffmpeg.exe") == expected


def test_an_unrunnable_binary_answers_none_rather_than_raising(
        rel, monkeypatch):
    """The caller turns None into an 'unknown' verdict and asks the user —
    a missing ffmpeg must not abort a publish before it starts."""
    def _boom(*a, **k):
        raise OSError("not executable")

    monkeypatch.setattr(rel.subprocess, "run", _boom)
    assert rel.local_ffmpeg_release("nope.exe") is None


def test_versions_order_numerically_not_lexically(rel):
    assert rel._version_tuple("9.0.1") > rel._version_tuple("8.1")
    assert rel._version_tuple("8.10") > rel._version_tuple("8.9")
    assert rel._version_tuple("") == ()


# ══════════════════════════════════════════════════════════════════════════════
# check_ffmpeg_currency — the verdict the skill branches on
# ══════════════════════════════════════════════════════════════════════════════
def test_upstream_being_newer_reads_as_outdated(rel, monkeypatch):
    _arm_currency(rel, monkeypatch, "8.1", "9.0.1")
    info = rel.check_ffmpeg_currency()
    assert info["status"] == "outdated"
    assert (info["local"], info["latest"]) == ("8.1", "9.0.1")


def test_an_exact_match_reads_as_current(rel, monkeypatch):
    _arm_currency(rel, monkeypatch, "9.0.1", "9.0.1")
    assert rel.check_ffmpeg_currency()["status"] == "current"


def test_a_local_build_ahead_of_upstream_is_current_not_outdated(
        rel, monkeypatch):
    """gyan.dev can lag a binary pulled from elsewhere. Newer-than-latest must
    not read as 'needs updating' and provoke a downgrade."""
    _arm_currency(rel, monkeypatch, "9.1", "9.0.1")
    assert rel.check_ffmpeg_currency()["status"] == "current"


def test_an_unreachable_upstream_is_unknown_not_an_exception(rel, monkeypatch):
    """This runs at the head of a publish the maintainer already decided to
    make; a flaky network must not be able to abort it."""
    _arm_currency(rel, monkeypatch, "8.1", "9.0.1", net_error=True)
    info = rel.check_ffmpeg_currency()
    assert info["status"] == "unknown"
    assert "gyan.dev" in info["detail"]


def test_a_build_with_no_release_number_is_unknown(rel, monkeypatch):
    _arm_currency(rel, monkeypatch, None, "9.0.1")
    assert rel.check_ffmpeg_currency()["status"] == "unknown"


def test_the_check_never_reaches_git_or_github(rel, monkeypatch):
    """It is a read-only local question — any git call would be a bug."""
    _arm_currency(rel, monkeypatch, "8.1", "9.0.1")
    monkeypatch.setattr(rel, "_git", lambda *a, **k: pytest.fail("git ran"))
    rel.check_ffmpeg_currency()


# ══════════════════════════════════════════════════════════════════════════════
# smoke_test_ffmpeg — the gate a downloaded binary has to pass
# ══════════════════════════════════════════════════════════════════════════════
def test_the_smoke_test_rejects_something_that_is_not_ffmpeg(rel, tmp_path):
    """Handed the Python interpreter, it must fail rather than pass by
    accident — this is the only thing standing between a broken upstream
    build and every user's MP3 conversion."""
    assert rel.smoke_test_ffmpeg(sys.executable, str(tmp_path)) is False


def test_the_smoke_test_rejects_a_missing_binary(rel, tmp_path):
    assert rel.smoke_test_ffmpeg(str(tmp_path / "nope.exe"),
                                 str(tmp_path)) is False


# ══════════════════════════════════════════════════════════════════════════════
# pull_ffmpeg — fetching and installing, safely
# ══════════════════════════════════════════════════════════════════════════════
def test_pulling_when_already_current_downloads_nothing(
        rel, monkeypatch, capsys):
    """Re-running must not re-fetch 106 MB to install what is already there.
    The armed download hook fails the test if it is called at all."""
    _arm_currency(rel, monkeypatch, "9.0.1", "9.0.1")
    assert rel.pull_ffmpeg() == 0
    assert "nothing to pull" in capsys.readouterr().out


def test_an_unknown_version_refuses_to_pull_without_force(rel, monkeypatch):
    """Can't-tell is not permission to overwrite the maintainer's binaries."""
    _arm_currency(rel, monkeypatch, "8.1", "9.0.1", net_error=True)
    with pytest.raises(SystemExit):
        rel.pull_ffmpeg()


def test_a_pull_installs_the_pair_and_backs_up_the_old_one(
        rel, tmp_path, monkeypatch):
    """End to end against a stand-in archive: the new pair lands, the old pair
    survives as *.exe.bak, and the staging directory is swept up."""
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "ffmpeg.exe").write_bytes(b"OLD-ffmpeg")
    (dest / "ffprobe.exe").write_bytes(b"OLD-ffprobe")
    zip_src = tmp_path / "upstream.zip"
    _stub_zip(zip_src)

    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    hooks = _arm_currency(rel, monkeypatch, "8.1", "9.0.1")
    _serve(hooks, zip_src)
    monkeypatch.setattr(rel, "smoke_test_ffmpeg", lambda exe, wd: True)

    assert rel.pull_ffmpeg(str(dest)) == 0

    assert (dest / "ffmpeg.exe").read_bytes() == b"NEW-ffmpeg"
    assert (dest / "ffprobe.exe").read_bytes() == b"NEW-ffprobe"
    assert (dest / "ffmpeg.exe.bak").read_bytes() == b"OLD-ffmpeg"
    assert (dest / "ffprobe.exe.bak").read_bytes() == b"OLD-ffprobe"
    assert not os.path.exists(os.path.join(str(tmp_path), ".ffmpeg-pull"))


def test_a_failed_smoke_test_leaves_the_old_binaries_untouched(
        rel, tmp_path, monkeypatch):
    """THE safety property. A broken upstream build must not reach the install
    directory, let alone the nightly channel."""
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "ffmpeg.exe").write_bytes(b"OLD-ffmpeg")
    (dest / "ffprobe.exe").write_bytes(b"OLD-ffprobe")
    zip_src = tmp_path / "upstream.zip"
    _stub_zip(zip_src, ffmpeg=b"BROKEN", ffprobe=b"BROKEN")

    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    hooks = _arm_currency(rel, monkeypatch, "8.1", "9.0.1")
    _serve(hooks, zip_src)
    monkeypatch.setattr(rel, "smoke_test_ffmpeg", lambda exe, wd: False)

    with pytest.raises(SystemExit):
        rel.pull_ffmpeg(str(dest))

    assert (dest / "ffmpeg.exe").read_bytes() == b"OLD-ffmpeg"
    assert (dest / "ffprobe.exe").read_bytes() == b"OLD-ffprobe"
    assert not (dest / "ffmpeg.exe.bak").exists()


def test_an_archive_missing_a_binary_is_refused_before_anything_moves(
        rel, tmp_path, monkeypatch):
    """Upstream could restructure its zip. Half an install is worse than none,
    so the layout is checked before the live folder is touched."""
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "ffmpeg.exe").write_bytes(b"OLD-ffmpeg")
    zip_src = tmp_path / "upstream.zip"
    _stub_zip(zip_src, ffprobe=None)

    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    hooks = _arm_currency(rel, monkeypatch, "8.1", "9.0.1")
    _serve(hooks, zip_src)

    with pytest.raises(SystemExit):
        rel.pull_ffmpeg(str(dest))
    assert (dest / "ffmpeg.exe").read_bytes() == b"OLD-ffmpeg"


# ══════════════════════════════════════════════════════════════════════════════
# CLI wiring
# ══════════════════════════════════════════════════════════════════════════════
def test_the_check_flag_exits_without_publishing(rel, monkeypatch):
    called = []
    monkeypatch.setattr(rel, "print_ffmpeg_currency",
                        lambda src: called.append(src))
    monkeypatch.setattr(rel, "publish_ffmpeg",
                        lambda *a, **k: pytest.fail("published"))
    monkeypatch.setattr(rel.os, "chdir", lambda p: None)

    assert rel.main(["--ffmpeg-check"]) == 0
    assert called == [None]


def test_the_pull_flag_exits_without_publishing_and_carries_force(
        rel, monkeypatch):
    called = []
    monkeypatch.setattr(
        rel, "pull_ffmpeg",
        lambda src, force=False: called.append((src, force)) or 0)
    monkeypatch.setattr(rel, "publish_ffmpeg",
                        lambda *a, **k: pytest.fail("published"))
    monkeypatch.setattr(rel.os, "chdir", lambda p: None)

    assert rel.main(["--ffmpeg-pull", "--force"]) == 0
    assert called == [(None, True)]


def test_a_plain_ffmpeg_publish_makes_no_upstream_call(rel, monkeypatch):
    """--ffmpeg on its own keeps working exactly as before. Running the
    currency check is the skill's job, not an implicit network call buried in
    the publish path."""
    monkeypatch.setattr(rel, "check_ffmpeg_currency",
                        lambda *a, **k: pytest.fail("checked upstream"))
    monkeypatch.setattr(rel, "publish_ffmpeg", lambda dry, src: 0)
    monkeypatch.setattr(rel.os, "chdir", lambda p: None)

    assert rel.main(["--ffmpeg", "--dry-run"]) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Payload exclusions — what must never travel in an app update
# ══════════════════════════════════════════════════════════════════════════════
# ffmpeg.version describes the BINARIES, and those are excluded from every
# payload. Shipping the marker alone told installs they held an FFmpeg they did
# not have, and since the app decided by comparing marker to manifest, it then
# never fetched the real one. Build 57 did exactly that.

FFMPEG_FILES = ("ffmpeg.exe", "ffprobe.exe", "ffmpeg.version")


def _dist(tmp_path, *names):
    """A dist/ tree containing *names*, each with distinct content."""
    root = tmp_path / "dist"
    root.mkdir()
    for i, name in enumerate(names):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (i + 1))
    return str(root)


def test_the_marker_is_excluded_alongside_the_binaries(rel):
    assert set(rel.FFMPEG_PAYLOAD_EXCLUDES) == set(FFMPEG_FILES)


def test_a_full_payload_carries_none_of_the_ffmpeg_files(rel, tmp_path,
                                                         monkeypatch):
    dist = _dist(tmp_path, "app.exe", *FFMPEG_FILES)
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))

    rels, is_full, _base, _hashes = rel.choose_payload(dist, 58, True)

    assert is_full
    assert rels == ["app.exe"]


def test_a_delta_payload_carries_none_of_them_either(rel, tmp_path,
                                                     monkeypatch):
    """THE build-57 regression. The delta branch had no exclusion at all — it
    rested on "FFmpeg never changes", which stopped being true the moment the
    build machine got a new FFmpeg. All three files differ from the baseline
    here, and not one of them may ship."""
    dist = _dist(tmp_path, "app.exe", *FFMPEG_FILES)
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(rel, "load_state",
                        lambda: {"files": {}, "base_build": 50})

    rels, is_full, base_build, _hashes = rel.choose_payload(dist, 58, False)

    assert not is_full and base_build == 50
    assert rels == ["app.exe"]


def test_an_unchanged_file_still_stays_out_of_a_delta(rel, tmp_path,
                                                      monkeypatch):
    """The exclusion must not accidentally disable the delta itself."""
    dist = _dist(tmp_path, "app.exe", "steady.dll", *FFMPEG_FILES)
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))
    hashes = rel.hash_tree(dist)
    monkeypatch.setattr(rel, "load_state",
                        lambda: {"files": {"steady.dll": hashes["steady.dll"]},
                                 "base_build": 50})

    rels, _is_full, _base, _hashes = rel.choose_payload(dist, 58, False)

    assert rels == ["app.exe"]


def test_the_baseline_still_records_every_file(rel, tmp_path, monkeypatch):
    """Exclusion is about what SHIPS, not what is tracked — the returned hash
    map becomes the next baseline and must stay complete, or the excluded
    files would read as changed on every future delta."""
    dist = _dist(tmp_path, "app.exe", *FFMPEG_FILES)
    monkeypatch.setattr(rel, "REPO_ROOT", str(tmp_path))

    _rels, _is_full, _base, hashes = rel.choose_payload(dist, 58, True)

    assert set(hashes) == {"app.exe", *FFMPEG_FILES}

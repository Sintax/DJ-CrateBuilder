"""What a build is made of: the bundled components, installed vs offered."""

import platform
import re
import sys
from importlib import metadata

from cratebuilder import updater_core as ucore

# The components the Update page lists, in display order. `dist` is the
# package name pip knows (None for the two that aren't packages); `key` is
# the spelling the release manifest's "components" block uses — the PEP 503
# form of the name, so the release script and the app never disagree on
# "Pillow" vs "pillow" or "yt_dlp" vs "yt-dlp".
COMPONENTS = (
    ("python", "Python", None),
    ("ffmpeg", "FFmpeg", None),
    ("yt-dlp", "yt-dlp", "yt-dlp"),
    ("yt-dlp-ejs", "yt-dlp JS helper (yt-dlp-ejs)", "yt-dlp-ejs"),
    ("certifi", "Certificate bundle (certifi)", "certifi"),
    ("pywebview", "Window engine (pywebview)", "pywebview"),
    ("bottle", "Bundle server (bottle)", "bottle"),
    ("fastapi", "Remote API (FastAPI)", "fastapi"),
    ("uvicorn", "Remote server (uvicorn)", "uvicorn"),
    ("pillow", "Artwork (Pillow)", "Pillow"),
    ("mutagen", "ID3 tags (mutagen)", "mutagen"),
    ("pystray", "Tray icon (pystray)", "pystray"),
    ("send2trash", "Recycle bin (send2trash)", "send2trash"),
)

# Row states, in the order the page explains them.
NEWER = "newer"          # the offered build carries a different version
SAME = "same"            # installed and offered agree
UNKNOWN = "unknown"      # the manifest doesn't say (a build before the block)
MISSING = "missing"      # not installed here at all


def normalize_name(name):
    """PEP 503: case-insensitive, runs of `-`, `_` and `.` fold to one `-`,
    and an extras suffix ("uvicorn[standard]") is not part of the name."""
    bare = str(name or "").split("[", 1)[0]
    return re.sub(r"[-_.]+", "-", bare).lower().strip()


def package_version(dist):
    """The installed version of *dist*, or None when it isn't installed."""
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def ffmpeg_version(install_dir, _probe=None):
    """The bundled FFmpeg's version as the updater tracks it: the on-disk
    marker first (it holds the release channel's full version string), the
    binary's own report when there is no marker, None when there is neither
    — a source run finds FFmpeg on PATH and the page simply says so."""
    if not install_dir:
        return None
    marker = ucore.read_ffmpeg_version(install_dir)
    if marker:
        return marker
    return (_probe or ucore.probe_ffmpeg_build)(install_dir)


def installed_versions(ffmpeg_dir=None, _package_version=None, _ffmpeg=None):
    """{key: version-or-None} for every component in COMPONENTS, read from
    the running process. Injection points are for tests only."""
    pkg = _package_version or package_version
    ffm = _ffmpeg or ffmpeg_version
    out = {}
    for key, _label, dist in COMPONENTS:
        if key == "python":
            out[key] = platform.python_version()
        elif key == "ffmpeg":
            out[key] = ffm(ffmpeg_dir)
        else:
            out[key] = pkg(dist)
    return out


def offered_versions(manifest):
    """The manifest's "components" block with its keys normalised, or None
    when the manifest predates the block (build 82 and earlier)."""
    if not isinstance(manifest, dict):
        return None
    block = manifest.get("components")
    if not isinstance(block, dict) or not block:
        return None
    out = {}
    for name, version in block.items():
        text = str(version or "").strip()
        out[normalize_name(name)] = text or None
    return out


def compare(installed, offered):
    """One row per component: what this install has, what the offered build
    carries, and which of the four states that is. *offered* None means
    the build didn't say, so every row is UNKNOWN rather than NEWER."""
    rows = []
    installed = installed or {}
    for key, label, _dist in COMPONENTS:
        have = installed.get(key)
        will = offered.get(key) if offered else None
        if have is None:
            state = MISSING
        elif offered is None or will is None:
            state = UNKNOWN
        elif have.strip() == will.strip():
            state = SAME
        else:
            state = NEWER
        rows.append({"key": key, "label": label, "installed": have,
                     "offered": will, "state": state})
    return rows


def freeze_versions(deps, python_version=None, ffmpeg_version_text=None):
    """The release script's side of the contract: the "components" block
    for a manifest, from its {package: version} map of what it froze plus
    the build machine's Python and the FFmpeg the channel offers. Keys are
    normalised here so the script never has to know the spelling rule."""
    out = {"python": python_version or platform.python_version()}
    if ffmpeg_version_text:
        out["ffmpeg"] = str(ffmpeg_version_text).strip()
    for name, version in (deps or {}).items():
        if version:
            out[normalize_name(name)] = str(version).strip()
    return out

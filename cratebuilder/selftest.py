"""The built app's self-test: can this bundle load, encode and probe?"""

import json
import os
import platform
import subprocess
import sys
import tempfile

from cratebuilder import components

# A public video that has been on YouTube since 2005 and is not going away:
# the first one ever uploaded. Only its metadata is fetched, never the media.
SMOKE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _find_ffmpeg():
    """The ffmpeg this build would use: beside the frozen exe, else PATH."""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if getattr(sys, "frozen", False):
        for cand in (os.path.dirname(sys.executable),
                     getattr(sys, "_MEIPASS", None)):
            if cand and os.path.isfile(os.path.join(cand, name)):
                return os.path.join(cand, name)
    import shutil
    return shutil.which("ffmpeg")


def check_components(installed):
    """Every pip-installed component the Update page lists must be present
    and readable; a missing one means the freeze dropped its metadata."""
    missing = [key for key, _label, dist in components.COMPONENTS
               if dist and not installed.get(key)]
    return {"name": "components", "ok": not missing,
            "detail": ("missing: " + ", ".join(missing)) if missing
            else f"{sum(1 for _k, _l, d in components.COMPONENTS if d)} packages present"}


def check_ffmpeg(exe, workdir):
    """One second of silence through libmp3lame — the encoder every real
    download depends on. Proves the bundled binary runs and can write MP3."""
    if not exe:
        return {"name": "ffmpeg", "ok": False, "detail": "ffmpeg not found"}
    out = os.path.join(workdir, "selftest.mp3")
    try:
        subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
             "-codec:a", "libmp3lame", "-y", out],
            capture_output=True, timeout=120, check=True,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"name": "ffmpeg", "ok": False, "detail": f"encode failed: {exc}"}
    ok = os.path.isfile(out) and os.path.getsize(out) > 0
    return {"name": "ffmpeg", "ok": ok,
            "detail": os.path.basename(exe) + (" encoded MP3" if ok
                                               else " wrote no output")}


def check_probe(url, probe):
    """The bundled yt-dlp must be able to read a public video's title. This
    is the check that catches a broken yt-dlp release before users do."""
    try:
        info = probe(url) or {}
    except Exception as exc:                       # noqa: BLE001 — any failure is the answer
        return {"name": "probe", "ok": False,
                "detail": f"{type(exc).__name__}: {exc}"}
    title = info.get("title")
    ok = bool(title) and bool(info.get("id"))
    return {"name": "probe", "ok": ok,
            "detail": f"title: {title}" if ok else "no title/id in the answer"}


def _default_probe(url):
    from cratebuilder.ydl import YdlSession
    return YdlSession(cookies=None).probe_metadata(url)


_DEFAULT = object()


def run(report_path, url=SMOKE_URL, _probe=None, _ffmpeg_exe=_DEFAULT,
        _installed=None):
    """Run every check, write the JSON report, return the exit code.

    Never prints: the frozen app is a windowed exe with no stdout, so the
    report file is the only channel back to the release script. Any
    unexpected error is written into the report as a failed check rather
    than escaping — a crash with no report would read as "hung"."""
    report = {"ok": False, "python": platform.python_version(),
              "frozen": bool(getattr(sys, "frozen", False)),
              "components": {}, "checks": []}
    try:
        exe = _ffmpeg_exe if _ffmpeg_exe is not _DEFAULT else _find_ffmpeg()
        installed = _installed if _installed is not None else \
            components.installed_versions(
                ffmpeg_dir=os.path.dirname(exe) if exe else None)
        report["components"] = installed
        with tempfile.TemporaryDirectory(prefix="cb-selftest-") as workdir:
            report["checks"] = [
                check_components(installed),
                check_ffmpeg(exe, workdir),
                check_probe(url, _probe or _default_probe),
            ]
    except Exception as exc:                       # noqa: BLE001
        report["checks"].append({"name": "selftest", "ok": False,
                                 "detail": f"{type(exc).__name__}: {exc}"})
    report["ok"] = bool(report["checks"]) and all(c["ok"] for c in report["checks"])
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return 0 if report["ok"] else 1

"""Self-update core: manifest checks, integrity, download, and the file swap.

No tkinter imports — every function here is safe to unit-test in isolation.
The GUI (the About tab) and the standalone ``updater.py`` swap script both call
into this module so the moving parts live in one tested place.

The "nightly build" channel works like this:
  * The app ships a fixed integer ``APP_BUILD`` (e.g. 7) alongside the pinned
    display version ("1.3").
  * GitHub hosts a small ``update.json`` manifest on a dedicated ``nightly``
    branch. It names the newest build number and a download URL + SHA-256.
  * The app fetches the manifest, and if its build is higher, downloads the
    zipped PyInstaller folder, verifies the hash, and hands off to the
    separate updater process to swap the files in and relaunch.

Nothing here imports tkinter or touches the GUI, so the logic stays testable.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

# Windows: keep a probed subprocess from flashing a console over the GUI.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Required manifest keys and the shape we expect.
_REQUIRED_KEYS = ("build", "url", "sha256")


def is_frozen():
    """True when running as the packaged (PyInstaller) app, not from source."""
    return bool(getattr(sys, "frozen", False))


def is_linux():
    """True when running on a Linux platform."""
    return sys.platform.startswith("linux")


def linux_install_kind(module_path=None, deb_root="/opt/dj-cratebuilder"):
    """Return "deb" when the app runs from the Debian package, else "source".

    Detection is purely by location: the app installed via the ``.deb`` lives
    under ``deb_root`` (``/opt/dj-cratebuilder``), whereas a git checkout or any
    other layout does not. ``module_path`` defaults to this module's own file so
    the running install is classified by where its code sits on disk; both
    arguments are injectable so the logic stays platform-independent under test.
    """
    probe = os.path.realpath(module_path or __file__)
    root = os.path.realpath(deb_root)
    try:
        return "deb" if os.path.commonpath([probe, root]) == root else "source"
    except ValueError:
        # commonpath raises when the paths live on different drives/roots.
        return "source"


def pkexec_available():
    """True when the PolicyKit ``pkexec`` helper is on PATH (Linux privilege)."""
    return shutil.which("pkexec") is not None


def build_deb_install_cmd(deb_path):
    """Return the privileged command to install a ``.deb`` via apt + pkexec.

    Kept in one place so the exact argument list is unit-testable and the GUI
    never hand-rolls it. ``pkexec`` raises a graphical PolicyKit prompt for the
    user's password before apt runs.
    """
    return ["pkexec", "apt-get", "install", "-y", deb_path]


def can_self_update():
    """True when the running app can perform an in-app self-update.

    That means either the Windows packaged (frozen) build, or a Linux install
    that came from the ``.deb`` package. A plain source/git checkout can't
    self-update (there's nothing to swap), so this returns False there.
    """
    return is_frozen() or (is_linux() and linux_install_kind() == "deb")


def install_dir():
    """Directory the running executable lives in (the install folder when frozen)."""
    return os.path.dirname(os.path.abspath(sys.executable))


def default_workspace():
    """Per-user scratch dir for downloads/staging, outside the install folder."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "DJ-CrateBuilder", "update")


def purge_dir(path):
    """Best-effort recursive delete; never raises (used to clean leftovers)."""
    shutil.rmtree(path, ignore_errors=True)


def is_update_available(manifest, current_build):
    """Return True only when ``manifest`` is valid and names a newer build.

    Defensive by design: any malformed manifest, missing field, or
    non-integer build number returns False (treat as "no update") rather
    than raising, so a bad nightly push can never crash the running app.
    """
    if not isinstance(manifest, dict):
        return False
    try:
        return int(manifest.get("build", 0)) > int(current_build)
    except (TypeError, ValueError):
        return False


def validate_manifest(manifest):
    """Return (ok, reason). Checks the manifest has the fields we rely on.

    ``reason`` is a short human-readable string when ok is False, else "".
    """
    if not isinstance(manifest, dict):
        return False, "manifest is not a JSON object"
    for key in _REQUIRED_KEYS:
        if key not in manifest:
            return False, f"missing required field: {key}"
    try:
        int(manifest["build"])
    except (TypeError, ValueError):
        return False, "build is not an integer"
    if not str(manifest.get("url", "")).strip():
        return False, "url is empty"
    sha = str(manifest.get("sha256", "")).strip()
    if len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        return False, "sha256 is not a 64-character hex digest"
    return True, ""


FFMPEG_VERSION_FILE = "ffmpeg.version"


def validate_ffmpeg_block(block):
    """Return (ok, reason) for the optional manifest ``ffmpeg`` sub-block.

    Mirrors ``validate_manifest``: any non-dict, empty version/url, or malformed
    sha256 is reported (ok=False) rather than raised, so a bad block is treated
    as "no ffmpeg update" and can never crash the running app.
    """
    if not isinstance(block, dict):
        return False, "ffmpeg block is not a JSON object"
    if not str(block.get("version", "")).strip():
        return False, "ffmpeg version is empty"
    if not str(block.get("url", "")).strip():
        return False, "ffmpeg url is empty"
    sha = str(block.get("sha256", "")).strip()
    if len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        return False, "ffmpeg sha256 is not a 64-character hex digest"
    return True, ""


def read_ffmpeg_version(install_dir):
    """Return the recorded on-disk ffmpeg version string, or None if absent/blank."""
    path = os.path.join(install_dir, FFMPEG_VERSION_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_ffmpeg_version(install_dir, version):
    """Record the installed ffmpeg version (write-then-rename for atomicity)."""
    path = os.path.join(install_dir, FFMPEG_VERSION_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(version).strip())
    os.replace(tmp, path)
    return path


def ffmpeg_build_token(version):
    """The build part of a '<build>+<sha8>' version marker.

    ``derive_ffmpeg_version`` in the release script appends '+<sha8>' to what
    ``ffmpeg -version`` reports, so stripping the last '+' segment yields
    something directly comparable to a live binary's own answer. A marker in
    the hash-only fallback form ('ffmpeg-<sha16>') has no '+' and comes back
    unchanged, which simply never matches a real report — the caller treats an
    unmatchable pair as 'can't verify', not as proof of a mismatch."""
    return str(version or "").strip().rsplit("+", 1)[0]


def probe_ffmpeg_build(install_dir, _runner=None):
    """What the bundled ffmpeg.exe actually reports, or None if it can't say.

    The marker file is a claim; this is the evidence. Runs the binary next to
    the app with ``-version`` and returns its build token. Never raises and
    never opens a console window: a missing, broken or unrunnable FFmpeg is a
    normal answer here (None), and the caller then falls back to trusting the
    marker exactly as before."""
    exe = os.path.join(install_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.isfile(exe):
        return None
    runner = _runner or _run_ffmpeg_version
    try:
        lines = (runner(exe) or "").splitlines()
    except Exception:
        return None
    match = re.match(r"ffmpeg version (\S+)", lines[0] if lines else "")
    return match.group(1) if match else None


def _run_ffmpeg_version(exe):
    """`<exe> -version` stdout, or '' — the one place a subprocess is spawned."""
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True,
                              timeout=15, creationflags=_NO_WINDOW)
        return proc.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def ffmpeg_update_action(manifest, installed_version, reported_build=None):
    """Classify the ffmpeg state as 'none', 'adopt', or 'update'.

    'none'   → no valid ffmpeg block, or the installed marker already matches
               the offer *and* nothing contradicts it.
    'adopt'  → valid block but no local marker yet: record the offered version
               WITHOUT downloading, so the feature's debut doesn't force a
               large download on an install that already has that build.
    'update' → the marker differs from the offer, or the binary on disk
               disagrees with what the marker claims about it.

    Decision is installed-vs-offered only — never build-number based — so it is
    immune to skipped builds and to --full baseline resets.

    *reported_build* is what the installed ffmpeg.exe actually reports (see
    ``probe_ffmpeg_build``); pass None when it can't be determined and the
    marker is trusted on its own, as it was before. When it IS known it
    outranks the marker, because the marker is only a note about the binary
    and can be wrong — build 57 shipped a marker claiming an FFmpeg its own
    payload deliberately excluded, stranding every install that took it. The
    same evidence makes 'adopt' honest: adopting is only safe when the binary
    really is the offered build.
    """
    if not isinstance(manifest, dict):
        return "none"
    block = manifest.get("ffmpeg")
    ok, _reason = validate_ffmpeg_block(block)
    if not ok:
        return "none"
    offered = str(block["version"]).strip()
    reported = str(reported_build or "").strip()

    if installed_version is None:
        # No marker. Trust the shipped binary as current only while nothing
        # says otherwise; a binary that reports a different build IS stale.
        if reported and reported != ffmpeg_build_token(offered):
            return "update"
        return "adopt"
    if str(installed_version).strip() != offered:
        return "update"
    if reported and reported != ffmpeg_build_token(installed_version):
        return "update"          # the marker is lying about the binary beside it
    return "none"


def fetch_manifest(url, timeout=4.0, _opener=None):
    """GET the manifest JSON and return it as a dict, or None on any failure.

    A cache-busting query param is appended so raw.githubusercontent's ~5 min
    CDN cache doesn't hide a fresh push. Network errors, timeouts, and bad
    JSON all return None — the caller treats that as "no update right now".

    ``_opener`` is injectable for tests; defaults to urllib's urlopen.
    """
    opener = _opener or urllib.request.urlopen
    busted = _cache_bust(url)
    try:
        req = urllib.request.Request(
            busted, headers={"User-Agent": "DJ-CrateBuilder-Updater"})
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _cache_bust(url):
    """Append a coarse time-based query param to defeat CDN caching."""
    # Imported lazily so the module stays import-cheap and Date-free at top.
    import time
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(time.time())}"


def sha256_file(path, chunk=65536):
    """Return the lowercase hex SHA-256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_sha256(path, expected):
    """True if the file's SHA-256 matches ``expected`` (case-insensitive)."""
    if not expected:
        return False
    return sha256_file(path) == str(expected).strip().lower()


def download(url, dest_path, progress_cb=None, timeout=30.0, _opener=None):
    """Stream ``url`` to ``dest_path``, calling progress_cb(done, total).

    total is the Content-Length when known, else None. Writes to a ``.part``
    file first and renames on success so a half-finished download is never
    mistaken for a complete one.
    """
    opener = _opener or urllib.request.urlopen
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    part = dest_path + ".part"
    req = urllib.request.Request(
        url, headers={"User-Agent": "DJ-CrateBuilder-Updater"})
    with opener(req, timeout=timeout) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        with open(part, "wb") as f:
            for block in iter(lambda: resp.read(65536), b""):
                f.write(block)
                done += len(block)
                if progress_cb:
                    progress_cb(done, total)
    os.replace(part, dest_path)
    return dest_path


def extract_zip(zip_path, dest_dir):
    """Extract a zip into ``dest_dir`` (created if needed). Returns dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir


def _iter_files(root):
    """Yield (abs_path, rel_path) for every file under root."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ap = os.path.join(dirpath, name)
            yield ap, os.path.relpath(ap, root)


def apply_update(staged_dir, app_dir, backup_dir, _copyfn=shutil.copy2):
    """Replace ``app_dir`` files with ``staged_dir`` files, with rollback.

    Done per-file rather than as a whole-folder swap because the running
    ``updater.exe`` lives inside ``app_dir``: Windows lets you *rename/move* a
    running executable (so we can shove the old one into ``backup_dir``) but not
    *delete* it, and it won't let a whole directory containing it be moved.

    For each file in the staged tree:
      1. If a same-named file already exists in app_dir, move it to backup_dir
         (preserving the relative path).
      2. Copy the staged file into place.

    Files present in app_dir but absent from the staged tree are left alone
    (an update is additive/replacement, not a destructive sync).

    On any error mid-swap, every change made so far is rolled back: copied
    files removed and backed-up originals restored. Returns True on success,
    raises the original exception after rolling back on failure.

    ``_copyfn`` is injectable for tests (to simulate a mid-swap failure).
    """
    os.makedirs(backup_dir, exist_ok=True)
    moved = []    # (backup_abs, original_abs) pairs we relocated
    copied = []   # target_abs files we wrote (that had no prior original)
    try:
        for staged_abs, rel in _iter_files(staged_dir):
            target = os.path.join(app_dir, rel)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            if os.path.exists(target):
                backup = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(backup) or ".", exist_ok=True)
                shutil.move(target, backup)
                moved.append((backup, target))
            else:
                copied.append(target)
            _copyfn(staged_abs, target)
        return True
    except Exception:
        # Roll back: undo fresh copies, then restore moved-aside originals.
        for target in copied:
            try:
                if os.path.exists(target):
                    os.remove(target)
            except OSError:
                pass
        for backup, original in moved:
            try:
                if os.path.exists(original):
                    os.remove(original)
                shutil.move(backup, original)
            except OSError:
                pass
        raise


def launch_updater_command(pid, staged, app_dir, relaunch, backup, log):
    """The argv to hand off to the separate updater process.

    Mirrors the command construction inside DJ-CrateBuilder_v2.0.py's
    `_launch_updater_and_quit`: prefers `updater.exe` beside the running app,
    falling back to driving `updater.py` with the current interpreter for a
    dev/source run. Pure — builds the list, spawns nothing; the caller Popens
    it. The monolith keeps its own inline copy of this logic (not refactored
    to call here), so a change here does not silently change the desktop app.
    """
    updater_exe = os.path.join(app_dir, "updater.exe")
    if os.path.exists(updater_exe):
        cmd = [updater_exe]
    else:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd = [sys.executable, os.path.join(repo_root, "updater.py")]
    cmd += ["--pid", str(pid), "--src", staged, "--dst", app_dir,
            "--relaunch", relaunch, "--backup", backup, "--log", log]
    return cmd


def install_ffmpeg_from_zip(zip_path, expected_sha, install_dir,
                            staged_dir, backup_dir, version):
    """Verify, extract, and swap a downloaded ffmpeg zip into ``install_dir``.

    The zip carries ``ffmpeg.exe``/``ffprobe.exe`` at its root. We reuse the
    tested ``apply_update`` overlay (per-file move-aside + copy, with rollback)
    so a swap that fails mid-way — e.g. a binary locked by a running yt-dlp
    subprocess raising a Windows sharing violation — rolls back cleanly. The
    version marker is only advanced after a fully successful swap.

    The caller downloads the zip first (existing ``download``) and only calls
    this while the app is idle. Raises on checksum/extract/swap failure; returns
    True on success.
    """
    if not verify_sha256(zip_path, expected_sha):
        raise ValueError("ffmpeg checksum mismatch — download may be corrupt")
    purge_dir(staged_dir)
    extract_zip(zip_path, staged_dir)
    apply_update(staged_dir, install_dir, backup_dir)
    write_ffmpeg_version(install_dir, version)
    return True

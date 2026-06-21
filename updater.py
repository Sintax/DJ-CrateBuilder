"""Standalone updater — the separate process that swaps the app's files.

A running Windows program can't overwrite its own .exe, so the main app stages
the verified new build, launches THIS script (shipped as ``updater.exe`` next to
the app), and exits. We then:

    1. wait for the main app's process to fully exit (so its files unlock),
    2. swap the staged files into the install folder (backing up originals),
    3. relaunch the app on the new build,
    4. best-effort clean up the workspace.

All file-swap logic lives in ``cratebuilder.updater_core.apply_update`` (which
is unit-tested); this file only handles the process choreography. The whole
thing is intentionally tiny and dependency-free (stdlib + ctypes on Windows).

Usage:
    updater.exe --pid <main_pid> --src <staged_dir> --dst <app_dir>
                --relaunch <app_exe> [--backup <dir>] [--log <file>]
"""
import argparse
import os
import subprocess
import sys
import time

# Works both frozen (PyInstaller bundles the package) and from source.
try:
    from cratebuilder.updater_core import apply_update
except ImportError:  # running as a loose script: add repo root to path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cratebuilder.updater_core import apply_update


def _log(msg, path=None):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def wait_for_pid_exit(pid, timeout=30.0, log=None):
    """Block until process ``pid`` exits or ``timeout`` elapses.

    Returns True if it exited, False on timeout. Uses the Win32 API directly so
    we never accidentally signal/terminate the process (os.kill on Windows can
    kill rather than probe). Off-Windows, falls back to a poll loop.
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True   # process already gone (or no access) — proceed
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return result != WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    # Non-Windows fallback: poll until os.kill(pid, 0) reports it's gone.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return True
        time.sleep(0.2)
    return False


def _relaunch(exe, log=None):
    """Start the app detached so this updater can exit cleanly."""
    try:
        flags = 0
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survives our exit.
            flags = 0x00000008 | 0x00000200
        subprocess.Popen([exe], close_fds=True, creationflags=flags,
                          cwd=os.path.dirname(exe) or None)
        _log(f"relaunched {exe}", log)
        return True
    except OSError as e:
        _log(f"relaunch FAILED: {e}", log)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="DJ-CrateBuilder updater")
    ap.add_argument("--pid", type=int, required=True,
                    help="PID of the main app to wait on")
    ap.add_argument("--src", required=True, help="staged (new) files directory")
    ap.add_argument("--dst", required=True, help="install directory to update")
    ap.add_argument("--relaunch", required=True, help="app exe to start after")
    ap.add_argument("--backup", default=None, help="where to back up originals")
    ap.add_argument("--log", default=None, help="optional log file path")
    args = ap.parse_args(argv)

    log = args.log
    backup = args.backup or os.path.join(args.src + "_backup")

    _log(f"updater start: pid={args.pid} src={args.src} dst={args.dst}", log)

    if not wait_for_pid_exit(args.pid, timeout=30.0, log=log):
        _log("WARNING: main app did not exit within 30s; applying anyway", log)
    # Small settle so the OS fully releases file handles after exit.
    time.sleep(0.5)

    try:
        apply_update(args.src, args.dst, backup)
        _log("file swap OK", log)
    except Exception as e:   # noqa: BLE001 - log and bail; rollback already ran
        _log(f"update FAILED, rolled back: {e}", log)
        # Still relaunch the (restored) app so the user isn't left with nothing.
        _relaunch(args.relaunch, log)
        return 1

    _relaunch(args.relaunch, log)

    # Best-effort cleanup. Leftovers (e.g. the old updater.exe we can't delete
    # while running) are purged by the app on its next launch.
    for path in (args.src, backup):
        try:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    _log("updater done", log)
    return 0


if __name__ == "__main__":
    sys.exit(main())

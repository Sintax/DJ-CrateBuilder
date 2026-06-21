"""Integration tests for the standalone updater.py process choreography.

Drives updater.main() against temp directories with --pid 0 (an already-exited
PID) and a stubbed relaunch, so no real process is waited on or spawned.
"""
import os

import pytest

import updater


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


def test_wait_for_pid_exit_zero_is_immediate():
    assert updater.wait_for_pid_exit(0) is True


def test_main_applies_update_and_cleans_up(tmp_path, monkeypatch):
    app = tmp_path / "app"; staged = tmp_path / "staged"; bak = tmp_path / "bak"
    _write(str(app / "DJ-CrateBuilder.exe"), "OLD")
    _write(str(app / "keep.dat"), "keep")
    _write(str(staged / "DJ-CrateBuilder.exe"), "NEW")
    _write(str(staged / "sub" / "lib.dll"), "NEWLIB")

    relaunched = {}
    monkeypatch.setattr(updater, "_relaunch",
                        lambda exe, log=None: relaunched.setdefault("exe", exe))

    rc = updater.main([
        "--pid", "0",
        "--src", str(staged),
        "--dst", str(app),
        "--relaunch", str(app / "DJ-CrateBuilder.exe"),
        "--backup", str(bak),
        "--log", str(tmp_path / "u.log"),
    ])

    assert rc == 0
    assert (app / "DJ-CrateBuilder.exe").read_text() == "NEW"
    assert (app / "sub" / "lib.dll").read_text() == "NEWLIB"
    assert (app / "keep.dat").read_text() == "keep"      # untouched
    assert relaunched["exe"] == str(app / "DJ-CrateBuilder.exe")
    # Workspace cleaned up after a successful swap.
    assert not staged.exists()


def test_main_rolls_back_and_still_relaunches_on_failure(tmp_path, monkeypatch):
    app = tmp_path / "app"; staged = tmp_path / "staged"; bak = tmp_path / "bak"
    _write(str(app / "a.txt"), "OLD-a")
    _write(str(app / "b.txt"), "OLD-b")
    _write(str(staged / "a.txt"), "NEW-a")
    _write(str(staged / "b.txt"), "NEW-b")

    # Force the file swap to fail mid-way.
    def boom(*a, **k):
        raise OSError("simulated swap failure")
    monkeypatch.setattr(updater, "apply_update", boom)

    relaunched = {}
    monkeypatch.setattr(updater, "_relaunch",
                        lambda exe, log=None: relaunched.setdefault("exe", exe))

    rc = updater.main([
        "--pid", "0", "--src", str(staged), "--dst", str(app),
        "--relaunch", str(app / "a.txt"), "--backup", str(bak),
    ])

    assert rc == 1
    # Even on failure the user gets the app relaunched (restored state).
    assert relaunched["exe"] == str(app / "a.txt")

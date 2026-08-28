"""Shared fixtures. Loads the app's pure-logic functions.

Phase 0/1: `cb` is the single-file app module loaded via importlib.
After extraction, individual tests import from `cratebuilder.*` directly;
this loader remains as a fallback for code still living in the main file.

App-level tests use `cb_mod` (fresh monolith module per test file) and
`make_app` / `app` (isolated, quiet MP3DownloaderApp instances with all
runtime artefacts redirected into the test's tmp_path).
"""
import gc
import importlib.util
import logging
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

import pytest

from cratebuilder import service as cb_service
from cratebuilder import startup as cb_startup

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_ROOT, "DJ-CrateBuilder_v1.3.py")


def _load_main():
    spec = importlib.util.spec_from_file_location("cb_main", _MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def cb():
    return _load_main()


# Fixtures whose use means the test builds (or maps) a real Tk window.
_GUI_FIXTURES = {"app", "make_app", "shared_app", "show"}


def pytest_collection_modifyitems(items):
    """Auto-mark every Tk-window test as `gui`.

    Any test requesting one of the window-building fixtures gets the marker,
    so the fast lane (`pytest -m "not gui"`) stays correct without per-file
    marker lists — new GUI tests are covered the moment they use `app`.
    """
    for item in items:
        if _GUI_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.gui)


@pytest.fixture(scope="session")
def _service_sandbox(tmp_path_factory):
    """One throwaway HOME + app dir for the whole non-GUI lane.

    Session-scoped and OUTSIDE any test's own tmp_path on purpose: nothing is
    supposed to write here at all — it is the floor a forgetful test lands on,
    not a workspace — and a per-test directory inside tmp_path would show up in
    the several tests that assert on their tmp dir's exact contents.
    """
    root = tmp_path_factory.mktemp("service_sandbox")
    home = root / "home"
    runtime = root / "runtime"
    home.mkdir()
    runtime.mkdir()
    return str(home), str(runtime)


@pytest.fixture(autouse=True)
def _isolate_service_paths(request, monkeypatch, _service_sandbox):
    """Make service-layer isolation STRUCTURAL, not a convention.

    Every service/DB fixture in the suite passes explicit tmp paths today, but
    nothing stopped the next test from writing `CrateBuilderService()` bare —
    which would read the developer's real ~/.dj_cratebuilder_config.json, probe
    the real cratebuilder.db beside the checkout, and (through `remote_state`)
    write the real cratebuilder_remote.json. Two seams close all of it: HOME /
    USERPROFILE, which is what `util.config_path` and `util.default_base_dir`
    expand, and `service.app_dir`, which is where the database, the two logs,
    the link store and the token store live.

    GUI tests are skipped: `make_app` / `shared_app` already redirect the same
    things for the monolith, and this fixture is function-scoped while
    `shared_app` is not — undoing its environment between tests in a file would
    be a regression, not a guard.
    """
    if _GUI_FIXTURES & set(request.fixturenames):
        return
    home, runtime = _service_sandbox
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setattr(cb_service, "app_dir", lambda: runtime)


@pytest.fixture(scope="module")
def cb_mod():
    """Fresh monolith module for the requesting test file.

    Module-scoped so every test file gets its own execution of the main
    script (replacing the per-test inline loaders), while tests within one
    file share it. Registered as sys.modules['cb_main'] like `cb`.
    """
    return _load_main()


# TclError messages that mean Tk itself is unusable on this machine (headless
# session, or the transient same-process Tcl re-init race) rather than a bug
# in the code under test. Only these may turn into a skip.
_TK_UNAVAILABLE = (
    "no display name",
    "couldn't connect to display",
    "application-specific initialization failed",
    "tcl_findlibrary",
    "usable tk.tcl",
    "init.tcl",
)


def _cancel_pending_afters(app):
    """Cancel every pending Tk 'after' callback on *app*'s interpreter.

    Torn-down apps otherwise leave stray timers that fire into the NEXT
    Tk root created in the same process and break its init.
    """
    try:
        ids = app.tk.splitlist(app.tk.call("after", "info"))
    except Exception:
        return
    for after_id in ids:
        try:
            app.after_cancel(after_id)
        except Exception:
            pass


def _isolate_runtime(mp, mod, tmp_path):
    """Point every runtime artefact of the monolith *mod* into *tmp_path*.

    HOME/USERPROFILE point at tmp_path, the monolith's runtime_data_dir
    binding returns tmp_path/'runtime' (so cratebuilder.db, activity.log,
    debug.log land there), DEFAULT_BASE points at tmp_path/'Music', and
    cratebuilder.startup.startup_is_enabled returns False so app init never
    reads the real Windows registry.
    """
    mp.setenv("HOME", str(tmp_path))
    mp.setenv("USERPROFILE", str(tmp_path))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    mp.setattr(mod, "runtime_data_dir",
               lambda script_path=None: str(runtime_dir))
    mp.setattr(mod, "DEFAULT_BASE", str(tmp_path / "Music"))
    mp.setattr(cb_startup, "startup_is_enabled", lambda: False)


def _build_app(mod, quiet=True, **kwargs):
    """One MP3DownloaderApp, with the Tk-init retry dance.

    Tk init is retried up to three times (a root created right after an
    earlier one was torn down can fail Tcl init spuriously), destroying any
    half-built root between attempts. If it still fails, the test is
    SKIPPED only when the error looks like Tk being unavailable on the
    machine; any other TclError propagates as a failure, so a genuine
    widget-construction regression can never hide behind a skip.
    """
    last_err = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.25)
            gc.collect()
        root_before = tk._default_root
        try:
            return mod.MP3DownloaderApp(quiet=quiet, **kwargs)
        except tk.TclError as e:
            last_err = e
            debris = tk._default_root
            if debris is not None and debris is not root_before:
                _cancel_pending_afters(debris)
                try:
                    debris.destroy()
                except Exception:
                    pass
    msg = str(last_err).lower()
    if any(sig in msg for sig in _TK_UNAVAILABLE):
        pytest.skip(f"Tk unavailable after 3 attempts: {last_err}")
    raise last_err


def _destroy_app(application):
    """Cancel pending 'after' callbacks, then destroy(), swallowing
    teardown-only errors."""
    _cancel_pending_afters(application)
    try:
        application.destroy()
    except Exception:
        pass


def _close_crate_log_handlers():
    """Close the file handlers the app attached to the process-global
    CrateBuilder loggers, so tmp_path log files can be deleted."""
    for name in ("CrateBuilder", "CrateBuilder.debug"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)


@pytest.fixture
def make_app(cb_mod, tmp_path, monkeypatch):
    """Factory: make_app(quiet=True, **kwargs) -> isolated MP3DownloaderApp.

    Isolation applied before any app is built (and active for the whole
    test): see _isolate_runtime.

    To pre-seed a config, write tmp_path/'.dj_cratebuilder_config.json'
    BEFORE calling make_app() — load_config() resolves '~' at call time.

    quiet=True (the default) suppresses ambient startup side effects; pass
    quiet=False for a production-faithful init. Extra kwargs pass through
    to MP3DownloaderApp. Tk-init retry/skip semantics: see _build_app.
    """
    _isolate_runtime(monkeypatch, cb_mod, tmp_path)

    created = []

    def _make(quiet=True, **kwargs):
        application = _build_app(cb_mod, quiet=quiet, **kwargs)
        created.append(application)
        return application

    yield _make

    for application in created:
        _destroy_app(application)
    _close_crate_log_handlers()


@pytest.fixture
def app(make_app):
    """Convenience: one isolated quiet app, built with make_app() defaults."""
    return make_app()


@pytest.fixture(scope="module")
def shared_app(cb_mod, tmp_path_factory):
    """One isolated quiet app shared by every test in the requesting file.

    STRICTLY for read-only tests: anything that mutates app state (Tk
    variables it doesn't restore, the app's DB, files under its crate root)
    belongs on `app` / `make_app` instead, or the mutation leaks into the
    file's other tests. Same isolation and Tk-retry semantics as make_app,
    against a module-lifetime tmp dir.
    """
    mp = pytest.MonkeyPatch()
    tmp_path = tmp_path_factory.mktemp("shared_app")
    _isolate_runtime(mp, cb_mod, tmp_path)
    application = _build_app(cb_mod)
    yield application
    _destroy_app(application)
    _close_crate_log_handlers()
    mp.undo()


@pytest.fixture
def show():
    """show(widget) -> widget, mapped so synthesised pointer events reach it.

    Tk discards <Button-1> / <MouseWheel> aimed at an unmapped window, so a
    test that generates one must first raise every notebook tab between the
    widget and the toplevel, then let the toplevel map.
    """
    def _show(widget):
        chain, node = [], widget
        while node.master is not None:
            chain.append(node)
            node = node.master
        for child in chain:
            if isinstance(child.master, ttk.Notebook):
                child.master.select(child)
        node.deiconify()
        node.update()
        return widget
    return _show


import shutil
import subprocess

_FFMPEG = shutil.which("ffmpeg")

# Marks a test that needs a real audio container. A hand-rolled byte literal
# cannot produce a valid MP4 or Ogg file, so these tests generate one.
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None, reason="FFmpeg not on PATH")


def make_silent(path, codec, seconds=1):
    """Generate a real, valid silent audio file for tagging tests."""
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds),
         "-c:a", codec, str(path)],
        check=True)
    return str(path)

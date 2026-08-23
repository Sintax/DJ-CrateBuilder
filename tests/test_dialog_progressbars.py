"""Every progress bar in a popup window wears the app's red, not clam's default.

The Main tab's two bars were styled from the start; the five that live in
dialogs (Folders Cleanup, Fetch Missing Artwork, Repair Track Tags, and the
two updater windows) were left on the stock clam style, which renders a pale
off-white bar against a dark dialog.

They share Dialog.Horizontal.TProgressbar rather than the Main tab's
Accent.* — same red, but without that style's thickness=5, which is tuned for
the slim Progress card and squashes a dialog's taller bar.
"""
import threading
import tkinter as tk
from tkinter import ttk

import pytest

from cratebuilder import updater_core as ucore


DIALOG_STYLE = "Dialog.Horizontal.TProgressbar"

MANIFEST = {
    "version": "1.3", "build": 99,
    "url": "https://example.invalid/build-99.zip", "sha256": "5" * 64,
}


def _bars_under(widget):
    """Every ttk.Progressbar in this widget's subtree."""
    found = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Progressbar):
            found.append(child)
        found.extend(_bars_under(child))
    return found


def _newest_dialog(app):
    """The most recently created Toplevel under the app window."""
    tops = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops, "expected the call to open a dialog"
    return tops[-1]


def _assert_red_dialog_bar(dlg):
    bars = _bars_under(dlg)
    assert len(bars) == 1, f"expected one progress bar, got {len(bars)}"
    assert bars[0].cget("style") == DIALOG_STYLE
    return bars[0]


@pytest.fixture
def no_threads(cb_mod, monkeypatch):
    """Build the dialog, but never let its background worker start."""
    class _Inert:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(cb_mod.threading, "Thread", _Inert)


# ══════════════════════════════════════════════════════════════════════════════
# The style itself
# ══════════════════════════════════════════════════════════════════════════════
def test_the_dialog_style_paints_the_bar_youtube_red(app, cb_mod):
    style = ttk.Style(app)
    assert style.lookup(DIALOG_STYLE, "background") == cb_mod.YT_RED
    assert style.lookup(DIALOG_STYLE, "lightcolor") == cb_mod.YT_RED
    assert style.lookup(DIALOG_STYLE, "darkcolor") == cb_mod.YT_RED


def test_the_dialog_style_keeps_the_theme_thickness(app):
    """The Main tab's 5px is sized for the Progress card. Inheriting it would
    flatten every dialog bar — so this style must leave thickness unset."""
    style = ttk.Style(app)
    assert style.lookup("Accent.Horizontal.TProgressbar", "thickness") == 5
    # Unset, exactly as the stock horizontal style leaves it.
    assert style.lookup(DIALOG_STYLE, "thickness") == ""
    assert style.lookup("Horizontal.TProgressbar", "thickness") == ""


def test_the_main_tab_bars_are_left_on_their_own_styles(app):
    """This change is dialogs-only — the inline pair keeps red-over-maroon."""
    assert app._vid_progress.cget("style") == "Accent.Horizontal.TProgressbar"
    assert app._overall_progress.cget("style") == "Maroon.Horizontal.TProgressbar"


# ══════════════════════════════════════════════════════════════════════════════
# The five dialogs
# ══════════════════════════════════════════════════════════════════════════════
def test_folders_cleanup_scan_bar(app, cb_mod):
    """The indeterminate one, and the only bar parented to the Database
    Viewer rather than the main window."""
    viewer = tk.Toplevel(app)
    viewer._parent = app
    viewer._db = app._db
    try:
        session = cb_mod._FoldersCleanupSession(viewer, [])
        session._show_progress({"display_name": "Some Channel"})
        dlg, bar = session._progress
        assert bar.cget("style") == DIALOG_STYLE
        assert str(bar.cget("mode")) == "indeterminate"
        session._hide_progress()
    finally:
        viewer.destroy()


def test_fetch_missing_artwork_bar(app, cb_mod):
    session = cb_mod._ArtworkBackfillSession(app, [], mode="missing")
    session._show_progress()
    try:
        assert session._progress[1].cget("style") == DIALOG_STYLE
    finally:
        session._hide_progress()


def test_repair_track_tags_bar(app):
    # The repair run owns this event; the dialog's Cancel button binds to it.
    app._tag_repair_cancel = threading.Event()
    dlg, bar, _sub = app._tag_repair_progress(10)
    try:
        assert bar.cget("style") == DIALOG_STYLE
    finally:
        dlg.destroy()


def test_windows_updater_bar(app, monkeypatch, tmp_path, no_threads):
    """The bar users watch during an actual update."""
    monkeypatch.setattr(ucore, "default_workspace", lambda: str(tmp_path / "ws"))
    try:
        app._run_update(MANIFEST, 99)
        _assert_red_dialog_bar(_newest_dialog(app))
    finally:
        _newest_dialog(app).destroy()
        app._update_in_progress = False


def test_linux_updater_bar(app, monkeypatch, tmp_path, no_threads):
    monkeypatch.setattr(ucore, "default_workspace", lambda: str(tmp_path / "ws"))
    monkeypatch.setattr(ucore, "pkexec_available", lambda: True)
    try:
        app._run_update_linux(MANIFEST, 99)
        _assert_red_dialog_bar(_newest_dialog(app))
    finally:
        _newest_dialog(app).destroy()
        app._update_in_progress = False

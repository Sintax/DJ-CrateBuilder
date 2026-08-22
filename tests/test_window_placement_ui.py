"""The window's remembered placement as the app actually uses it, and the
Settings-tab layout changes that shipped alongside it.
"""
import sys

import pytest

from cratebuilder import util


def _on_a_screen(geometry, screens):
    width, height, x, y = util.parse_window_geometry(geometry)
    return any(util._overlap((x, y, width, height), s) > 0 for s in screens)


# ── reading the real monitor layout ──────────────────────────────────────────
def test_screen_work_areas_describes_whole_monitors_or_nothing(cb_mod):
    """Every rectangle has to be usable as one — a zero or negative extent
    would make the overlap test answer 'nowhere' and send every window back to
    the primary display."""
    rects = cb_mod.screen_work_areas()
    if sys.platform != "win32":
        assert rects == []
        return
    for x, y, width, height in rects:
        assert width > 0 and height > 0
        assert isinstance(x, int) and isinstance(y, int)


def test_screen_rects_always_offers_somewhere_to_put_a_window(app):
    """Tk's own screen is the floor: on Linux, and on Windows if the
    enumeration fails, there is still exactly one rectangle to place into."""
    rects = app._screen_rects()
    assert rects
    for _x, _y, width, height in rects:
        assert width > 0 and height > 0


# ── restoring ────────────────────────────────────────────────────────────────
def test_a_remembered_size_is_reapplied(app):
    sx, sy, _sw, _sh = app._screen_rects()[0]
    app._settings.set("window_geometry", f"900x800+{sx + 60}+{sy + 40}")
    app._restore_window_placement()
    app.update_idletasks()
    width, height, _x, _y = util.parse_window_geometry(app.geometry())
    assert (width, height) == (900, 800)


def test_a_geometry_from_a_monitor_that_is_gone_lands_back_on_screen(app):
    app._settings.set("window_geometry", "900x800+99000+99000")
    app._restore_window_placement()
    app.update_idletasks()
    assert _on_a_screen(app.geometry(), app._screen_rects())


def test_a_corrupt_remembered_geometry_never_stops_the_window_opening(app):
    """A config file is user-editable and survives upgrades. A value that
    cannot be parsed has to be ignored, not raised — the alternative is an app
    that will not start until someone deletes the file by hand."""
    for bad in ("not a geometry", "0x0+0+0", "850x950", None):
        app._settings.set("window_geometry", bad if bad is not None else "")
        app._restore_window_placement()      # must not raise
    assert _on_a_screen(app.geometry(), app._screen_rects())


# ── saving ───────────────────────────────────────────────────────────────────
def test_saving_records_a_geometry_that_can_be_read_back(app):
    app.update_idletasks()
    app._save_window_placement()
    saved = app._settings.get("window_geometry")
    assert util.parse_window_geometry(saved), saved
    assert app._settings.get("window_maximized") is False


def test_a_child_widget_resizing_is_not_the_window_moving(app):
    """<Configure> fires for the queue widget, every tab and every row. Only
    the toplevel's own describes where the window is."""
    class _Event:
        widget = app._qtxt

    app._geometry_after_id = None
    app._on_root_configure(_Event())
    assert app._geometry_after_id is None


def test_moving_the_window_schedules_exactly_one_save(app):
    """A drag emits a <Configure> per pixel and every save rewrites the whole
    config file, so the pending write is replaced rather than stacked up."""
    class _Event:
        widget = app

    app._geometry_after_id = None
    app._on_root_configure(_Event())
    first = app._geometry_after_id
    assert first is not None
    app._on_root_configure(_Event())
    assert app._geometry_after_id not in (None, first)
    app.after_cancel(app._geometry_after_id)
    app._geometry_after_id = None


def test_the_debounce_is_cancelled_on_quit(app):
    assert "_geometry_after_id" in app._RECURRING_TIMERS


@pytest.mark.parametrize("state", ["iconic", "withdrawn"])
def test_a_hidden_window_does_not_overwrite_the_remembered_geometry(app, state):
    """A minimized or tray-hidden window reports a geometry that describes
    nothing. Saved, it would be restored to next time."""
    app._settings.set("window_geometry", "900x800+50+60")
    app.state = lambda: state
    app._save_window_placement()
    assert app._settings.get("window_geometry") == "900x800+50+60"


def test_maximizing_is_remembered_without_losing_the_restored_size(app):
    """'zoomed' reports the size of the screen it fills. Written back, the
    window could never be unmaximized to the size the user actually chose."""
    app._settings.set("window_geometry", "900x800+50+60")
    app.state = lambda: "zoomed"
    app._save_window_placement()
    assert app._settings.get("window_maximized") is True
    assert app._settings.get("window_geometry") == "900x800+50+60"


# ── Settings tab layout ──────────────────────────────────────────────────────
def _settings_rows(widget):
    """The row frames of the Settings tab's main column, in packed order."""
    return widget.master.master.pack_slaves()


def test_the_cookie_checkbox_is_named_for_what_it_does(shared_app):
    assert str(shared_app._use_cookies_cb.cget("text")) \
        == "Browser Cookie Authentication"


def test_nothing_still_tells_the_user_to_tick_the_old_label(cb_mod):
    """Six per-browser how-to blocks and an FAQ answer name this checkbox in
    prose. Renaming the widget without them sends the user looking in Settings
    for a control that is no longer called that."""
    import pathlib
    source = pathlib.Path(cb_mod.__file__).read_text(encoding="utf-8")
    assert "Use browser cookies" not in source


def test_no_divider_separates_throttling_from_the_cookie_options(shared_app,
                                                                 cb_mod):
    """Cookies are part of Download Behavior; the rule between them read as
    the end of the section rather than a break inside it."""
    rows = _settings_rows(shared_app._sleep_mode_combo)
    start = rows.index(shared_app._sleep_mode_combo.master)
    end = rows.index(shared_app._use_cookies_cb.master)
    assert start < end
    for row in rows[start + 1:end]:
        is_rule = (row.winfo_class() == "Frame"
                   and str(row.cget("bg")) == cb_mod.BORDER)
        assert not is_rule, row


def test_the_log_size_limit_sits_below_the_debug_log_buttons(shared_app):
    rows = _settings_rows(shared_app._log_limit_combo)
    assert rows.index(shared_app._log_limit_combo.master) \
        > rows.index(shared_app._debug_path_lbl.master)


def test_download_behavior_is_no_longer_labelled_experimental(shared_app):
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    for widget in walk(shared_app):
        try:
            text = str(widget.cget("text"))
        except Exception:
            continue
        assert "Experimental" not in text, widget


def test_the_grey_action_buttons_are_darker_than_the_panel_headings(shared_app,
                                                                    cb_mod):
    for button in (shared_app._open_yt_btn, shared_app._howto_btn):
        assert str(button.cget("bg")) == cb_mod.BTN_GREY
        assert str(button.cget("activebackground")) == cb_mod.BTN_GREY_ACT
    # Darker than the mid-grey they replaced, and still lighter than the
    # panel behind them so they read as buttons.
    assert cb_mod.BORDER < cb_mod.BTN_GREY < "#7f7f7f"

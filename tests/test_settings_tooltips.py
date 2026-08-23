"""The Settings maintenance buttons explain themselves on hover.

Each of the four orange tools used to sit beside a separate '?' icon that
owned the explanation, so the button itself was silent and the row carried a
widget whose only job was to be hovered. The tooltip now hangs off the button,
which is the thing the user is already pointing at.

The '?' icon is still right for a checkbox or a dropdown — hovering those
would fight the control — so the duplicate-warning checkbox keeps its own, and
this file pins that distinction rather than banning the icon outright.
"""
import tkinter as tk

import pytest


ORANGE_BUTTONS = ("_rebuild_db_btn", "_dedupe_db_btn",
                  "_fetch_art_btn", "_repair_tags_btn")


def _help_icons(row):
    """Every '?'-in-a-box help icon packed directly into *row*."""
    return [w for w in row.winfo_children()
            if isinstance(w, tk.Label) and str(w.cget("text")) == "?"]


@pytest.mark.parametrize("attr", ORANGE_BUTTONS)
def test_each_orange_tool_is_its_own_hover_target(shared_app, attr):
    """A Tooltip binds <Enter>; without one the button says nothing at all."""
    btn = getattr(shared_app, attr)
    assert str(btn.bind("<Enter>")).strip(), attr


@pytest.mark.parametrize("attr", ORANGE_BUTTONS)
def test_each_orange_tool_still_hides_its_tip_again(shared_app, attr):
    """<Leave> is what unschedules and tears the tip down — a tooltip bound on
    enter alone would strand a popup over the window."""
    btn = getattr(shared_app, attr)
    assert str(btn.bind("<Leave>")).strip(), attr


def test_the_file_tools_row_has_no_help_icons_left(shared_app):
    """Fetch Missing Artwork and Repair Track Tags are the whole row, so it
    should now hold nothing but the two buttons."""
    row = shared_app._fetch_art_btn.master
    assert shared_app._repair_tags_btn.master is row
    assert _help_icons(row) == []


def test_the_database_row_keeps_only_the_checkboxs_help_icon(shared_app):
    """Rebuild and Remove Duplicates gave theirs up; the checkbox keeps its
    own, because hovering a checkbox to read about it invites a stray click."""
    row = shared_app._rebuild_db_btn.master
    assert shared_app._dedupe_db_btn.master is row
    assert len(_help_icons(row)) == 1
    assert shared_app._dupe_check_cb.master is row


def test_repair_track_tags_kept_the_longer_of_its_two_descriptions(
        cb_mod, make_app, monkeypatch):
    """It carried both a button tooltip and a '?' tip. The '?' text described
    only the genre half, which the tag-repair rework outgrew — the surviving
    text has to be the one that mentions titles."""
    tips = {}
    real = cb_mod.Tooltip

    def _record(widget, text, **kw):
        tips[str(widget)] = text
        return real(widget, text, **kw)

    monkeypatch.setattr(cb_mod, "Tooltip", _record)
    app = make_app()
    text = tips[str(app._repair_tags_btn)]

    assert "Title" in text and "source URL" in text
    assert "Realigns the tags on every track" in text

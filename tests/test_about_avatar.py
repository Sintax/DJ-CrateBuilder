"""The About tab's author avatar and the Download New button's tooltip."""
import os
import tkinter as tk

import pytest


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def test_the_avatar_file_ships_sized_for_the_two_text_lines(cb_mod):
    path = cb_mod.about_avatar_path()
    assert path and os.path.isfile(path)
    from PIL import Image
    assert Image.open(path).size == (44, 44)


def test_the_avatar_renders_beside_the_credit(shared_app, cb_mod):
    img_labels = [w for w in _walk(shared_app)
                  if isinstance(w, tk.Label) and w.cget("image")]
    avatar = [w for w in img_labels
              if str(w.cget("image")) == str(shared_app._about_avatar_img)]
    assert len(avatar) == 1
    # Same row as the name/email column, so they sit side by side.
    name = next(w for w in _walk(shared_app)
                if isinstance(w, tk.Label)
                and w.cget("text") == cb_mod.ABOUT_CREATED_BY)
    assert avatar[0].master is name.master.master


def test_a_missing_avatar_is_no_picture_not_a_crash(make_app, cb_mod,
                                                    monkeypatch):
    monkeypatch.setattr(cb_mod, "about_avatar_path", lambda: None)
    app = make_app()
    assert not hasattr(app, "_about_avatar_img")


def test_download_new_tooltip_covers_the_join_behaviour(make_app, cb_mod,
                                                        monkeypatch):
    tips = []
    real = cb_mod.Tooltip
    monkeypatch.setattr(cb_mod, "Tooltip",
                        lambda w, text, **kw: tips.append((w, text)) or
                        real(w, text, **kw))
    app = make_app()
    ch = {"id": 1, "url": "https://youtube.com/@chan", "display_name": "Chan",
          "platform": "YouTube", "genre": "DnB", "status": "idle",
          "pending_new_count": 3, "pending_entries_json": "[]",
          "channel_id": "UCx", "last_error": ""}
    card = tk.Frame(app)
    app._watchlist_fill_card(card, ch)
    dl_tips = [t for w, t in tips
               if isinstance(w, tk.Button)
               and "Download New" in str(w.cget("text"))]
    assert len(dl_tips) == 1
    tip = dl_tips[0]
    assert "new tracks" in tip                      # the general description
    assert "Batch Queue" in tip                     # the additive behaviour
    assert "never queued twice" in tip              # the dedup promise

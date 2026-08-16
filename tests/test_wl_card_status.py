"""Watch List card: a failed scan must not disguise a good link as broken."""
import gc
import time
import tkinter as tk

import pytest


@pytest.fixture(scope="module")
def app(cb):
    # One Tk root for the whole module. An app torn down by an earlier test
    # file can leave stray `after` callbacks that make the next root's Tcl
    # init fail spuriously, so collect the debris and retry before giving up
    # — a silent skip here would hide the regression these tests guard.
    last = None
    for _ in range(3):
        gc.collect()
        try:
            a = cb.MP3DownloaderApp()
            break
        except Exception as e:      # pragma: no cover - environment dependent
            last = e
            time.sleep(0.25)
    else:
        pytest.skip(f"no display: {last}")
    a.update()
    yield a
    a.destroy()


def _card_labels(app, ch):
    """Every text= string in a freshly rendered card, flattened."""
    holder = tk.Frame(app)
    app._watchlist_build_channel_card(holder, ch)
    out = []

    def walk(w):
        for c in w.winfo_children():
            try:
                out.append(str(c.cget("text")))
            except Exception:
                pass
            walk(c)

    walk(holder)
    return out


BASE = dict(id=9001, display_name="Test Chan", platform="YouTube",
            genre="DnB", url="https://www.youtube.com/channel/UCx/videos",
            pending_new_count=3, last_error="getaddrinfo failed",
            last_scanned_timestamp=1000)


def test_card_offers_fix_link_only_when_the_link_is_actually_unknown(app):
    def fix_link(status):
        return any("Fix Link" in t
                   for t in _card_labels(app, {**BASE, "status": status}))

    # An offline / errored scan says nothing about the stored URL — the card
    # keeps its normal actions. This is the regression: one scan run with the
    # network down used to paint every card with Fix Link.
    assert fix_link("offline") is False
    assert fix_link("error") is False
    assert fix_link("found") is False
    # A channel we know is unidentified still asks to be fixed.
    assert fix_link("needs_resolve") is True


def test_offline_status_reads_as_offline_not_as_an_error(app):
    labels = _card_labels(app, {**BASE, "status": "offline"})
    assert any("offline" in t for t in labels)
    assert not any("needs channel ID" in t for t in labels)

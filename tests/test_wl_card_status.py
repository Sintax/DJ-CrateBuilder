"""Watch List card: a failed scan must not disguise a good link as broken."""
import tkinter as tk


def _card_labels(app, ch):
    """Every text= string in a freshly rendered card, flattened."""
    app.update()
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

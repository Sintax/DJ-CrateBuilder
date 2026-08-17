"""Watch List card: premieres the scan held back are explained, not hidden.

A card reading "0 new" for a channel whose page clearly has fresh uploads on
it looks broken. The scheduled note is what tells the user the count is right."""
import tkinter as tk


def _card_text(app, ch):
    """Every text= string in a freshly rendered card, joined."""
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
    return "  ".join(out)


CH = dict(id=9101, display_name="DnB Portal", platform="YouTube",
          genre="DnB", url="https://www.youtube.com/channel/UCx/videos",
          pending_new_count=0, status="idle", total_downloaded=12,
          last_scanned_timestamp=1000)


def test_a_card_with_no_held_back_tracks_says_nothing_about_scheduling(app):
    assert "scheduled" not in _card_text(app, CH)


def test_held_back_premieres_are_reported_on_the_card(app):
    app._wl_upcoming_counts[CH["id"]] = 2
    assert "2 scheduled" in _card_text(app, CH)


def test_a_zero_count_is_treated_as_nothing_to_say(app):
    """A scan that held nothing back must clear the note, not print '0'."""
    app._wl_upcoming_counts[CH["id"]] = 0
    assert "scheduled" not in _card_text(app, CH)


def test_the_note_is_scoped_to_its_own_channel(app):
    """Counts are keyed per channel id — one channel's premieres must not
    show up on another's card."""
    app._wl_upcoming_counts[CH["id"]] = 3
    other = {**CH, "id": 9102, "display_name": "Other Chan"}
    assert "scheduled" not in _card_text(app, other)
    assert "3 scheduled" in _card_text(app, CH)


def test_held_back_tracks_do_not_inflate_the_new_badge(app):
    """The whole point: pending stays 0, so the download button stays dim."""
    app._wl_upcoming_counts[CH["id"]] = 2
    text = _card_text(app, CH)
    assert "2 scheduled" in text
    assert "2 new" not in text


def test_the_counter_starts_empty_on_a_fresh_app(app):
    """It is a scan result, deliberately not persisted — a fresh session
    shows no note until the channel is scanned again."""
    assert isinstance(app._wl_upcoming_counts, dict)
    assert app._wl_upcoming_counts == {}

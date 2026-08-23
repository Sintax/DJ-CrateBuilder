"""Auto-adding a Main-tab channel download to the Watch List.

The card is raised the moment the channel's listing resolves — before the
first track downloads — because a full channel can run for an hour and until
then the Watch List simply did not know the download existed. That timing is
the whole point of these tests: the row and its card must exist while entries
are still being fetched, not only once the run ends.

The cost of moving it earlier is that the row's total_downloaded is inserted
from a count taken before this run, so the finish must recount that one row.
Both halves are pinned below.

No network and no real download — the yt-dlp stand-ins from test_crate_paths
drive _process_one_url end to end, and every path lives under tmp_path via the
`app` / `make_app` fixtures.
"""
import tkinter as tk

import yt_dlp

from tests.test_crate_paths import _FailingYdl, _StubSession, _WritingYdl


CHANNEL_URL = "https://yt/c"


def _two_track_listing():
    first = {"id": "vone", "title": "Cascade",
             "url": "https://yt/watch?v=vone"}
    second = {"id": "vtwo", "title": "Undertow",
              "url": "https://yt/watch?v=vtwo"}
    return first, second


def _arm(app, monkeypatch, ydl_factory, *, info=None):
    """Point the app at a two-entry channel served by *ydl_factory*."""
    first, second = _two_track_listing()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ydl_factory)
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        info if info is not None
        else {"_type": "playlist", "title": "Chan",
              "channel_id": "UCchan", "entries": [first, second]}))
    app._skip_existing.set(False)
    app._wl_download_active = False
    app._auto_add_to_watchlist.set(True)
    app._grand_dl = app._grand_sk = app._grand_er = 0
    return first, second


def _writer(attempts, titles):
    return lambda opts: _WritingYdl(attempts, titles, opts)


def _titles():
    first, second = _two_track_listing()
    return {first["url"]: "Cascade", second["url"]: "Undertow"}


def _run(app, cb_mod, **kw):
    return app._process_one_url(
        CHANNEL_URL, "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"], **kw)


def _names(app):
    return [c["display_name"] for c in app._db.get_all_watchlist_channels()]


def _card_labels(app):
    """Every label string rendered across the Watch List's cards."""
    out = []

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Label):
                try:
                    out.append(str(child["text"]))
                except tk.TclError:
                    pass
            walk(child)

    walk(app._wl_cards_frame)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Timing — the card exists while the channel is still downloading
# ══════════════════════════════════════════════════════════════════════════════
def test_the_channel_is_tracked_before_its_first_track_downloads(
        cb_mod, app, monkeypatch):
    """THE bug this fixes. The row had to wait for the last entry of the
    channel to finish, so a long download left the Watch List wrong for its
    whole run — and the user only ever saw the channel after a restart."""
    seen = []
    first, second = _two_track_listing()
    titles = _titles()

    class _Watching(_WritingYdl):
        def extract_info(self, url, download=False):
            seen.append(sorted(_names(app)))
            return super().extract_info(url, download=download)

    _arm(app, monkeypatch, lambda opts: _Watching([], titles, opts))
    _run(app, cb_mod)

    # Both tracks saw the channel already listed — not just the second.
    assert seen == [["Chan"], ["Chan"]]


def test_the_card_is_on_screen_while_the_download_is_still_running(
        cb_mod, app, monkeypatch):
    """The row is only half the fix: the rebuild is marshalled to the main
    thread with after(0), so the card itself has to be there too."""
    painted = []
    titles = _titles()

    class _Watching(_WritingYdl):
        def extract_info(self, url, download=False):
            app.update()          # drain the after(0) the auto-add scheduled
            painted.append([t for t in _card_labels(app) if "Chan" in t])
            return super().extract_info(url, download=download)

    _arm(app, monkeypatch, lambda opts: _Watching([], titles, opts))
    _run(app, cb_mod)

    assert painted and all(labels for labels in painted), painted


def test_a_run_cancelled_part_way_still_leaves_the_channel_tracked(
        cb_mod, app, monkeypatch):
    """The deliberate behaviour change. Auto-add used to require at least one
    completed track, so cancelling early tracked nothing. Now the user pointed
    the app at the channel, so it is kept — and they can remove the card."""
    titles = _titles()

    class _Cancelling(_WritingYdl):
        def extract_info(self, url, download=False):
            app._cancel_flag.set()
            return super().extract_info(url, download=download)

    _arm(app, monkeypatch, lambda opts: _Cancelling([], titles, opts))
    try:
        _run(app, cb_mod)
    finally:
        app._cancel_flag.clear()

    assert _names(app) == ["Chan"]


def test_a_channel_whose_every_track_fails_is_still_tracked(
        cb_mod, app, monkeypatch):
    """Same change, the other way in: nothing downloaded, but the listing
    resolved, so the channel is real and worth watching."""
    attempts = []
    _arm(app, monkeypatch, lambda opts: _FailingYdl(attempts, opts))

    downloaded, _skipped, errors = _run(app, cb_mod)

    assert (downloaded, errors) == (0, 2)
    assert _names(app) == ["Chan"]


# ══════════════════════════════════════════════════════════════════════════════
# What is (and is not) auto-added
# ══════════════════════════════════════════════════════════════════════════════
def test_a_single_track_url_never_creates_a_card(cb_mod, app, monkeypatch):
    """Only collections are channels. One pasted video is not a subscription."""
    one = {"id": "vsolo", "title": "Solo", "url": "https://yt/watch?v=vsolo"}
    _arm(app, monkeypatch,
         lambda opts: _WritingYdl([], {one["url"]: "Solo"}, opts),
         info=dict(one))

    _run(app, cb_mod)

    assert _names(app) == []


def test_a_watch_list_download_new_run_adds_nothing(cb_mod, app, monkeypatch):
    """Those runs pass channel_name_override and are tracked by definition —
    re-adding would build a second card for a channel that already has one."""
    _arm(app, monkeypatch, _writer([], _titles()))

    _run(app, cb_mod, channel_name_override="Chan")

    assert _names(app) == []


def test_the_setting_still_switches_the_whole_thing_off(
        cb_mod, app, monkeypatch):
    _arm(app, monkeypatch, _writer([], _titles()))
    app._auto_add_to_watchlist.set(False)

    _run(app, cb_mod)

    assert _names(app) == []


def test_a_nameless_collection_is_not_inserted(cb_mod, app, monkeypatch):
    """A blank card is unusable and cannot be scanned — the old auto-add bug
    this guard was written for. derive_collection_name has to come up empty
    for it to fire, which needs every name field absent."""
    first, second = _two_track_listing()
    _arm(app, monkeypatch, _writer([], _titles()),
         info={"_type": "playlist", "entries": [first, second]})

    _run(app, cb_mod)

    assert _names(app) == []


def test_the_same_channel_downloaded_twice_keeps_one_card(
        cb_mod, app, monkeypatch):
    _arm(app, monkeypatch, _writer([], _titles()))
    _run(app, cb_mod)
    _arm(app, monkeypatch, _writer([], _titles()))
    _run(app, cb_mod)

    assert _names(app) == ["Chan"]


def test_an_existing_row_is_backfilled_and_its_card_repainted(
        cb_mod, app, monkeypatch):
    """A row imported without its canonical id gets one, and the card is
    redrawn — a full list rebuild would blank every other card."""
    wl_id = app._db.add_watchlist_channel(
        url=CHANNEL_URL, display_name="Chan", platform="YouTube",
        genre="DnB")
    repainted = []
    monkeypatch.setattr(app, "_watchlist_update_card", repainted.append)
    _arm(app, monkeypatch, _writer([], _titles()))

    _run(app, cb_mod)
    app.update()

    row = app._db.get_watchlist_channel(wl_id)
    assert row["channel_id"] == "UCchan"
    assert wl_id in repainted
    assert len(app._db.get_all_watchlist_channels()) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Totals — the cost of raising the card early, paid back at the finish
# ══════════════════════════════════════════════════════════════════════════════
def test_the_total_is_recounted_once_the_download_finishes(
        cb_mod, app, monkeypatch):
    """The row is inserted with the count from BEFORE this run — zero for a
    brand-new channel. Without the recount the card would read 'Total
    downloaded: 0' for a channel it just filled."""
    _arm(app, monkeypatch, _writer([], _titles()))

    _run(app, cb_mod)

    row = app._db.get_all_watchlist_channels()[0]
    assert row["total_downloaded"] == 2


def test_a_second_download_adds_to_the_total(cb_mod, app, monkeypatch):
    """The recount reads the downloads table, so it is a true count and not an
    increment that could drift."""
    _arm(app, monkeypatch, _writer([], _titles()))
    _run(app, cb_mod)
    _arm(app, monkeypatch, _writer([], _titles()))
    app._skip_existing.set(False)
    _run(app, cb_mod)

    row = app._db.get_all_watchlist_channels()[0]
    assert row["total_downloaded"] == app._db.get_channel_download_count(
        CHANNEL_URL)


def test_nothing_downloaded_means_no_recount_and_no_repaint(
        cb_mod, app, monkeypatch):
    """A run that achieved nothing has nothing to recount; repainting anyway
    would be a pointless card teardown mid-batch."""
    repainted = []
    monkeypatch.setattr(app, "_watchlist_update_card", repainted.append)
    _arm(app, monkeypatch, lambda opts: _FailingYdl([], opts))

    _run(app, cb_mod)
    app.update()

    assert repainted == []


def test_the_finished_card_is_repainted_not_the_whole_list(
        cb_mod, app, monkeypatch):
    """Only the channel that just downloaded changed, and a full refresh
    visibly blanks every other card."""
    repainted = []
    monkeypatch.setattr(app, "_watchlist_update_card", repainted.append)
    _arm(app, monkeypatch, _writer([], _titles()))

    _run(app, cb_mod)
    app.update()

    wl_id = app._db.get_all_watchlist_channels()[0]["id"]
    assert repainted == [wl_id]


# ══════════════════════════════════════════════════════════════════════════════
# db.refresh_watchlist_total
# ══════════════════════════════════════════════════════════════════════════════
def test_refresh_total_counts_only_the_rows_channel(app, tmp_path):
    db = app._db
    mine = db.add_watchlist_channel(url="https://yt/a", display_name="A",
                                    platform="YouTube", genre="DnB")
    db.add_watchlist_channel(url="https://yt/b", display_name="B",
                             platform="YouTube", genre="DnB")
    for i in range(3):
        db.add_download(video_id=f"a{i}", title=f"A{i}", channel_name="A",
                        channel_url="https://yt/a", platform="YouTube",
                        genre="DnB", file_path=str(tmp_path / f"a{i}.mp3"),
                        upload_date="", bitrate="")
    db.add_download(video_id="b0", title="B0", channel_name="B",
                    channel_url="https://yt/b", platform="YouTube",
                    genre="DnB", file_path=str(tmp_path / "b0.mp3"),
                    upload_date="", bitrate="")

    assert db.refresh_watchlist_total(mine) == 3
    rows = {c["display_name"]: c["total_downloaded"]
            for c in db.get_all_watchlist_channels()}
    # B was inserted before its download and is deliberately left alone.
    assert rows == {"A": 3, "B": 0}


def test_refresh_total_on_a_row_that_is_gone_answers_none(app):
    """The card can be removed while its download is still running."""
    assert app._db.refresh_watchlist_total(9999) is None

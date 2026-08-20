"""The Batch Queue's per-row Skip button, from the widget down to the worker.

Skip is a promise about timing, not just a flag: a URL the batch has not
reached yet is passed over without a single network call, and the URL that is
downloading right now lets its in-flight track finish before the rest of it is
abandoned — the same promise Cancel makes. The mark rides on the queue item's
own dict, which is the one channel that already crosses to the worker thread.

The safety-critical case has its own test: a skipped URL must never be counted
clean, because a clean Watch List item retires that channel's pending list and
those tracks would be forgotten for good.

No network and no real download — the yt-dlp stand-ins from test_crate_paths
drive _process_one_url end to end, and every path lives under tmp_path via the
`app` / `make_app` fixtures.
"""
import itertools

import tkinter as tk

import yt_dlp

from tests.test_crate_paths import _FailingYdl, _StubSession, _WritingYdl


class _HookedYdl(_WritingYdl):
    """_WritingYdl that runs *hook* the instant a track's download starts.

    The test's stand-in for the user pressing Skip with a track in flight:
    whatever the hook does, this track still finishes and reports its file.
    """

    def __init__(self, attempts, titles, opts, hook):
        super().__init__(attempts, titles, opts)
        self._hook = hook

    def extract_info(self, url, download=False):
        self._hook(url)
        return super().extract_info(url, download=download)


class _StubDB:
    """Records only the calls the batch cleanup makes. Never touches sqlite —
    the real cratebuilder.db is live user data."""

    def __init__(self, cids):
        self.channels = [{"id": c, "url": f"https://ch/{c}",
                          "display_name": f"ch{c}", "platform": "YouTube",
                          "genre": "(none)", "pending_new_count": 2}
                         for c in cids]
        self.cleared = []
        self.statuses = []

    def get_watchlist_channel(self, cid):
        return next((c for c in self.channels if c["id"] == cid), None)

    def get_all_watchlist_channels(self):
        return list(self.channels)

    def get_total_pending_count(self):
        return sum(c["pending_new_count"] for c in self.channels)

    def clear_pending_for_channel(self, cid):
        self.cleared.append(cid)
        ch = self.get_watchlist_channel(cid)
        if ch:
            ch["pending_new_count"] = 0

    def update_watchlist_status(self, cid, status):
        self.statuses.append((cid, status))

    def set_watchlist_download_started(self, cids, ts):
        pass


def _two_track_listing():
    first = {"id": "vone", "title": "Cascade",
             "url": "https://yt/watch?v=vone"}
    second = {"id": "vtwo", "title": "Undertow",
              "url": "https://yt/watch?v=vtwo"}
    return first, second


def _arm_two_track_url(app, monkeypatch, ydl_factory):
    """Point the app at a two-entry channel served by *ydl_factory*."""
    first, second = _two_track_listing()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ydl_factory)
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        {"_type": "playlist", "title": "Chan", "entries": [first, second]}))
    app._skip_existing.set(False)
    app._wl_download_active = False
    app._grand_dl = app._grand_sk = app._grand_er = 0
    return first, second


def _skip_buttons(app):
    """Every row's Skip button, in row order."""
    found = []
    for row in app._batch_frame.winfo_children():
        for widget in row.winfo_children():
            if (isinstance(widget, tk.Button)
                    and str(widget["text"]) in ("Skip", "Skipped")):
                found.append(widget)
    return found


def _queue(app, *urls):
    app._batch_urls = [{"url": u, "genre": "DnB", "platform": "YouTube"}
                       for u in urls]
    app._batch_rebuild_rows()
    return app._batch_urls


# ══════════════════════════════════════════════════════════════════════════════
# Timing — what Skip promises about the track that is already downloading
# ══════════════════════════════════════════════════════════════════════════════
def test_skip_stops_the_url_after_the_in_flight_track_finishes(
        cb_mod, app, monkeypatch):
    """The whole point of the feature.

    Skip lands while track 1 is downloading. Track 1 must still complete —
    file written, counted, recorded — and track 2 must never be attempted.
    """
    attempts = []
    mark = {"skip": False}
    first, second = _two_track_listing()
    titles = {first["url"]: "Cascade", second["url"]: "Undertow"}

    def _press_skip_mid_track(url):
        mark["skip"] = True

    _arm_two_track_url(
        app, monkeypatch,
        lambda opts: _HookedYdl(attempts, titles, opts, _press_skip_mid_track))

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan",
        skip_requested=lambda: mark["skip"])

    assert attempts == [first["url"]]
    assert (downloaded, skipped, errors) == (1, 0, 0)
    assert app._url_skipped is True
    rows = {r["video_id"] for r in app._db.get_all_downloads()}
    assert "vone" in rows and "vtwo" not in rows


def test_skip_before_any_entry_downloads_nothing(cb_mod, app, monkeypatch):
    """Pressed while the metadata fetch was still running: the listing is in
    hand but not one entry of it is attempted."""
    attempts = []
    _arm_two_track_url(app, monkeypatch,
                       lambda opts: _FailingYdl(attempts, opts))

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan",
        skip_requested=lambda: True)

    assert attempts == []
    assert (downloaded, skipped, errors) == (0, 0, 0)
    assert app._url_skipped is True
    assert app._grand_dl == app._grand_sk == app._grand_er == 0


def test_a_skipped_url_is_never_the_fatal_sentinel(cb_mod, app, monkeypatch):
    """(None, None, None) breaks the WHOLE batch — it is how _batch_worker
    hears "fatal error". A skip must return honest counts instead, or skipping
    one URL would silently abandon every URL queued behind it."""
    attempts = []
    mark = {"skip": False}
    first, second = _two_track_listing()
    titles = {first["url"]: "Cascade", second["url"]: "Undertow"}
    _arm_two_track_url(
        app, monkeypatch,
        lambda opts: _HookedYdl(attempts, titles, opts,
                                lambda u: mark.__setitem__("skip", True)))

    result = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan",
        skip_requested=lambda: mark["skip"])

    assert None not in result
    assert result == (1, 0, 0)


def test_skip_works_while_paused(cb_mod, app, monkeypatch):
    """Paused, the worker sits in a 0.2s poll loop that only ever watched the
    cancel flag. Without a Skip check in there the button looks dead until the
    user resumes."""
    attempts = []
    _arm_two_track_url(app, monkeypatch,
                       lambda opts: _FailingYdl(attempts, opts))

    # False for the post-fetch checkpoint and the entry-loop head, True from
    # inside the pause wait onward.
    polls = itertools.count(1)
    app._pause_flag.set()
    try:
        downloaded, skipped, errors = app._process_one_url(
            "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
            channel_name_override="Chan",
            skip_requested=lambda: next(polls) >= 3)
    finally:
        app._pause_flag.clear()

    assert attempts == []
    assert (downloaded, skipped, errors) == (0, 0, 0)
    assert app._url_skipped is True


def test_url_skipped_resets_per_url(cb_mod, app, monkeypatch):
    """A stale flag would poison the NEXT URL's clean/not-clean verdict."""
    attempts = []
    _arm_two_track_url(app, monkeypatch,
                       lambda opts: _FailingYdl(attempts, opts))

    app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan", skip_requested=lambda: True)
    assert app._url_skipped is True

    app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan")
    assert app._url_skipped is False


# ══════════════════════════════════════════════════════════════════════════════
# The batch worker — a skipped URL is never a clean URL
# ══════════════════════════════════════════════════════════════════════════════
def _run_worker(make_app, monkeypatch, run_batch, process):
    """Drive _batch_worker over *run_batch* as a Watch List batch, with
    _process_one_url replaced by *process*. Returns the stub DB."""
    app = make_app()
    cids = list(range(1, len(run_batch) + 1))
    db = _StubDB(cids)
    app._db = db
    app._active_watchlist_batch = {"channel_ids": cids}
    app._wl_download_active = True
    app._last_url_error = "stub failure"
    monkeypatch.setattr(app, "_process_one_url", process)
    monkeypatch.setattr(app, "_watchlist_update_cards", lambda *a, **k: None)
    monkeypatch.setattr(app, "_refresh_genre_list", lambda *a, **k: None)
    app._batch_worker(run_batch)
    app.update()
    return db


def _wl_items(count):
    return [{"url": f"https://ch/{c}", "genre": "(none)",
             "platform": "YouTube", "channel_name": f"ch{c}",
             "title": f"ch{c}"} for c in range(1, count + 1)]


def test_a_url_skipped_before_it_ran_keeps_its_pending_list(
        make_app, monkeypatch):
    """SAFETY: retiring a skipped channel's pending list forgets those tracks
    permanently — the next scan re-finds them and the count silently resets.
    Item 2 is marked before the worker reaches it, so it is never processed at
    all; its pending list has to survive untouched."""
    items = _wl_items(3)
    items[1]["skip"] = True
    seen = []

    def _process(url, *a, **kw):
        seen.append(url)
        return (1, 0, 0)

    db = _run_worker(make_app, monkeypatch, items, _process)

    assert seen == ["https://ch/1", "https://ch/3"]
    assert db.cleared == [1, 3]
    assert db.get_watchlist_channel(2)["pending_new_count"] == 2
    assert (2, "idle") in db.statuses


def test_a_url_skipped_mid_run_keeps_its_pending_list(make_app, monkeypatch):
    """Same guarantee for the harder case: the URL DID run and DID download a
    track before Skip landed, so it reports zero errors. Zero errors is not
    enough to call it clean — the rest of its listing was never attempted."""
    items = _wl_items(3)
    apps = []

    def _process(url, *a, **kw):
        app = apps[0]
        app._url_skipped = (url == "https://ch/2")
        return (1, 0, 0)

    app = make_app()
    apps.append(app)
    cids = [1, 2, 3]
    db = _StubDB(cids)
    app._db = db
    app._active_watchlist_batch = {"channel_ids": cids}
    app._wl_download_active = True
    app._last_url_error = "stub failure"
    monkeypatch.setattr(app, "_process_one_url", _process)
    monkeypatch.setattr(app, "_watchlist_update_cards", lambda *a, **k: None)
    monkeypatch.setattr(app, "_refresh_genre_list", lambda *a, **k: None)
    app._batch_worker(items)
    app.update()

    assert db.cleared == [1, 3]
    assert db.get_watchlist_channel(2)["pending_new_count"] == 2


def test_the_worker_hands_the_url_a_predicate_over_its_own_item(
        make_app, monkeypatch):
    """The predicate must read the item dict live, so a mark set after the URL
    started is seen. Keying by row index would desync on the first ✕ or ▲▼."""
    items = _wl_items(1)
    captured = {}

    def _process(url, *a, **kw):
        captured["ask"] = kw["skip_requested"]
        return (1, 0, 0)

    _run_worker(make_app, monkeypatch, items, _process)

    assert captured["ask"]() is False
    items[0]["skip"] = True
    assert captured["ask"]() is True


# ══════════════════════════════════════════════════════════════════════════════
# The queue rows — marking, redrawing, and when the button is live
# ══════════════════════════════════════════════════════════════════════════════
def test_the_button_marks_the_item_its_row_renders(app):
    items = _queue(app, "https://yt/a", "https://yt/b", "https://yt/c")
    app._batch_run_active = True
    app._batch_rebuild_rows()

    _skip_buttons(app)[1].invoke()

    assert items[1].get("skip") is True
    assert "skip" not in items[0] and "skip" not in items[2]
    assert str(_skip_buttons(app)[1]["text"]) == "Skipped"
    assert str(_skip_buttons(app)[1]["state"]) == "disabled"


def test_a_mark_survives_a_remove_that_shifts_every_index(app):
    """The mark is on the dict, not on a row number. Removing row 1 renumbers
    everything below it; an index-keyed mark would land on the wrong URL."""
    items = _queue(app, "https://yt/a", "https://yt/b", "https://yt/c")
    marked = items[2]
    app._batch_run_active = True
    app._batch_rebuild_rows()
    _skip_buttons(app)[2].invoke()

    app._batch_remove(0)

    assert app._batch_urls[1] is marked
    assert marked.get("skip") is True
    texts = [str(b["text"]) for b in _skip_buttons(app)]
    assert texts == ["Skip", "Skipped"]


def test_skip_is_one_way(app):
    items = _queue(app, "https://yt/a")
    app._batch_run_active = True
    app._batch_rebuild_rows()
    _skip_buttons(app)[0].invoke()

    # Disabled, so a second press cannot reach the command at all.
    _skip_buttons(app)[0].invoke()
    app._batch_skip(items[0])

    assert items[0].get("skip") is True


def test_batch_highlight_cannot_repaint_the_button(cb_mod, app):
    """_batch_highlight blanket-recolours every tk.Label in every row while the
    batch runs. A Label here would be greyed out mid-run — a Button is not."""
    _queue(app, "https://yt/a", "https://yt/b")
    app._batch_run_active = True
    app._batch_rebuild_rows()

    app._batch_highlight(0)

    row = app._batch_frame.winfo_children()[1]
    labels = [w for w in row.winfo_children() if isinstance(w, tk.Label)]
    assert labels and all(str(w["fg"]) == cb_mod.TEXT_DIM for w in labels)
    assert str(_skip_buttons(app)[1]["fg"]) == cb_mod.YT_DARK


def test_buttons_are_disabled_when_idle_and_re_disabled_by_finish(
        app, monkeypatch):
    _queue(app, "https://yt/a", "https://yt/b")
    assert [str(b["state"]) for b in _skip_buttons(app)] == \
        ["disabled", "disabled"]

    monkeypatch.setattr(app, "_run_bg", lambda *a, **k: None)
    app._start()
    assert [str(b["state"]) for b in _skip_buttons(app)] == ["normal", "normal"]

    app._finish()
    assert [str(b["state"]) for b in _skip_buttons(app)] == \
        ["disabled", "disabled"]


def test_an_idle_press_cannot_mark_anything(app):
    items = _queue(app, "https://yt/a")
    _skip_buttons(app)[0].invoke()
    app._batch_skip(items[0])
    assert "skip" not in items[0]


def test_skip_marks_do_not_survive_into_the_next_run(app, monkeypatch):
    """_batch_urls is never cleared after a run, so a mark left behind would
    silently suppress that URL on every future batch."""
    items = _queue(app, "https://yt/a", "https://yt/b")
    items[1]["skip"] = True

    captured = []
    monkeypatch.setattr(app, "_run_bg",
                        lambda fn, *a, **k: captured.append(a[0]))
    app._start()

    assert all("skip" not in it for it in items)
    assert captured[0] == items
    assert [str(b["text"]) for b in _skip_buttons(app)] == ["Skip", "Skip"]


def test_the_run_snapshot_shares_the_dicts_the_rows_mark(app, monkeypatch):
    """The mark only reaches the worker because _start's list() copy holds the
    very same dict objects the rows close over."""
    items = _queue(app, "https://yt/a")
    captured = []
    monkeypatch.setattr(app, "_run_bg",
                        lambda fn, *a, **k: captured.append(a[0]))
    app._start()

    _skip_buttons(app)[0].invoke()

    assert captured[0][0] is items[0]
    assert captured[0][0].get("skip") is True


# ══════════════════════════════════════════════════════════════════════════════
# Skip on the Watch List mirror — active row only, multi-channel runs only
# ══════════════════════════════════════════════════════════════════════════════
def _wl_mirror(app, n=3, active=1):
    """Arm the Batch Queue panel's Watch List mirror with n channels."""
    items = [{"url": f"https://ch/{i}", "genre": "(none)",
              "platform": "YouTube", "channel_name": f"Chan {i}"}
             for i in range(n)]
    app._wl_download_active = True
    app._wl_batch_channels = [it["channel_name"] for it in items]
    app._wl_batch_genres = [it["genre"] for it in items]
    app._wl_batch_items = items
    app._wl_batch_active_idx = active
    app._batch_rebuild_rows()
    return items


def _mirror_buttons(app):
    """(row_index, button) for every Button in the mirror's rows."""
    out = []
    for i, row in enumerate(app._batch_frame.winfo_children()):
        for w in row.winfo_children():
            if isinstance(w, tk.Button):
                out.append((i, w))
    return out


def test_wl_mirror_shows_skip_on_the_active_row_only(app):
    _wl_mirror(app, n=3, active=1)
    buttons = _mirror_buttons(app)
    assert [i for i, _b in buttons] == [1]
    btn = buttons[0][1]
    assert btn.cget("text") == "Skip"
    assert str(btn.cget("fg")).lower() == "#ffffff"
    assert "bold" in str(btn.cget("font"))
    assert str(btn.cget("bg")).lower() == "#cc2222"   # sits on the red row


def test_wl_mirror_single_channel_run_has_no_skip(app):
    _wl_mirror(app, n=1, active=0)
    assert _mirror_buttons(app) == []


def test_wl_mirror_skip_marks_the_active_item_and_disables(app):
    items = _wl_mirror(app, n=3, active=1)
    _i, btn = _mirror_buttons(app)[0]
    btn.invoke()
    assert items[1].get("skip") is True
    assert [it.get("skip") for it in items] == [None, True, None]
    # The rebuild replaced the button with a disabled "Skipping…" one.
    _i, btn2 = _mirror_buttons(app)[0]
    assert btn2.cget("text") == "Skipping…"
    assert str(btn2.cget("state")) == "disabled"


def test_wl_mirror_a_passed_over_channel_renders_skipped(app):
    items = _wl_mirror(app, n=3, active=1)
    items[1]["skip"] = True
    app._wl_batch_active_idx = 2          # the worker moved on
    app._batch_rebuild_rows()
    rows = app._batch_frame.winfo_children()
    texts1 = [w.cget("text") for w in rows[1].winfo_children()
              if isinstance(w, tk.Label)]
    texts0 = [w.cget("text") for w in rows[0].winfo_children()
              if isinstance(w, tk.Label)]
    assert "⊘" in texts1                  # skipped, not ✓
    assert "✓" in texts0                  # a genuinely finished channel


def test_wl_batch_skip_is_dead_outside_a_wl_run(app):
    item = {"url": "https://ch/0", "channel_name": "Chan 0"}
    app._wl_download_active = False
    app._wl_batch_skip(item)
    assert "skip" not in item


# ══════════════════════════════════════════════════════════════════════════════
# Joining a running Watch List batch from a card's Download New
# ══════════════════════════════════════════════════════════════════════════════
import json


def _arm_running_wl(app, n=1):
    """A live single-or-multi channel WL run, with a stub DB behind it."""
    items = _wl_items(n)
    cids = list(range(1, n + 1))
    db = _StubDB(cids + [99])
    extra = db.get_watchlist_channel(99)
    extra["pending_entries_json"] = json.dumps(
        [{"id": "vnew", "title": "New Track", "url": "https://yt/w?v=vnew"}])
    app._db = db
    app._downloading = True
    app._wl_download_active = True
    app._active_watchlist_batch = {"channel_ids": list(cids)}
    app._wl_batch_channels = [it["channel_name"] for it in items]
    app._wl_batch_genres = [it["genre"] for it in items]
    app._wl_batch_items = items
    app._wl_batch_active_idx = 0
    app._batch_rebuild_rows()
    return items, db


def test_a_card_press_mid_run_joins_the_queue_and_reveals_skip(
        app, monkeypatch):
    """The user's scenario end to end: one channel downloading (no Skip —
    single-channel runs have none), press another card's Download New, and
    the panel now lists both with Skip on the one being worked."""
    monkeypatch.setattr(app, "_watchlist_update_card", lambda *a: None)
    items, db = _arm_running_wl(app, n=1)
    assert _mirror_buttons(app) == []            # single channel: no Skip yet

    assert app._watchlist_append_to_running(99) is True

    assert len(items) == 2                       # the worker's own list grew
    assert items[1]["channel_name"] == "ch99"
    assert app._active_watchlist_batch["channel_ids"] == [1, 99]
    assert app._wl_batch_channels == ["ch1", "ch99"]
    buttons = _mirror_buttons(app)
    assert [i for i, _b in buttons] == [0]       # Skip on the active row now
    assert buttons[0][1].cget("text") == "Skip"


def test_joining_declines_when_no_wl_batch_is_running(app):
    """A manual Main-tab batch (or Force Download) is not a Watch List run —
    the caller keeps today's wait-your-turn popup."""
    app._downloading = True
    app._wl_download_active = False
    assert app._watchlist_append_to_running(99) is False


def test_joining_twice_does_not_double_queue(app, monkeypatch):
    monkeypatch.setattr(app, "_watchlist_update_card", lambda *a: None)
    items, db = _arm_running_wl(app, n=1)
    app._watchlist_append_to_running(99)
    assert app._watchlist_append_to_running(99) is True
    assert len(items) == 2
    assert app._active_watchlist_batch["channel_ids"] == [1, 99]


def test_joining_with_nothing_pending_queues_nothing(app, monkeypatch):
    infos = []
    monkeypatch.setattr("cb_main.messagebox.showinfo",
                        lambda *a, **k: infos.append(a))
    items, db = _arm_running_wl(app, n=1)
    db.get_watchlist_channel(99)["pending_new_count"] = 0
    assert app._watchlist_append_to_running(99) is True   # handled, not queued
    assert len(items) == 1
    assert infos, "the user deserves to hear why nothing happened"


def test_the_worker_downloads_a_channel_appended_mid_run(make_app,
                                                         monkeypatch):
    """The load-bearing mechanics: run_batch and _wl_batch_items are the SAME
    list, so an append lands inside the worker's own loop, runs, and — having
    run clean — retires the appended channel's pending list too."""
    app = make_app()
    items = _wl_items(1)
    db = _StubDB([1, 99])
    db.get_watchlist_channel(99)["pending_entries_json"] = json.dumps(
        [{"id": "vnew", "title": "New Track", "url": "https://yt/w?v=vnew"}])
    app._db = db
    app._downloading = True
    app._wl_download_active = True
    app._active_watchlist_batch = {"channel_ids": [1]}
    app._wl_batch_channels = [items[0]["channel_name"]]
    app._wl_batch_genres = [items[0]["genre"]]
    app._wl_batch_items = items                  # identity with run_batch
    app._wl_batch_active_idx = 0
    app._last_url_error = None
    seen = []

    def _process(url, *a, **kw):
        if len(seen) == 0:                       # mid-first-channel: card press
            app._watchlist_append_to_running(99)
        seen.append(url)
        return (1, 0, 0)

    monkeypatch.setattr(app, "_process_one_url", _process)
    monkeypatch.setattr(app, "_watchlist_update_card", lambda *a: None)
    monkeypatch.setattr(app, "_watchlist_update_cards", lambda *a, **k: None)
    monkeypatch.setattr(app, "_refresh_genre_list", lambda *a, **k: None)
    app._batch_worker(items)
    app.update()

    assert len(seen) == 2                        # the appended channel ran
    assert db.cleared == [1, 99]                 # and ran clean

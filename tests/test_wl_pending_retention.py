"""A Watch List batch must not retire a channel's pending list unless that
channel's tracks actually downloaded.

The old cleanup cleared pending_new_count for every channel in the batch from
a `finally`, regardless of outcome. When downloads failed the count reset to
zero, the next scan re-found the very same tracks, and the user saw "Download
All New (N)" produce nothing over and over.
"""
import importlib.util
import os
import sys

import pytest


def _load():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    return m


class _StubDB:
    """Records only the calls the batch cleanup makes. Never touches sqlite —
    the real cratebuilder.db is live user data."""

    def __init__(self, cids):
        self.channels = [{"id": c, "url": f"https://ch/{c}",
                          "display_name": f"ch{c}", "platform": "YouTube",
                          "genre": "(none)", "pending_new_count": 2}
                         for c in cids]
        self.cleared = []
        self.cutoffs = []
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

    def update_watchlist_cutoff(self, url, cutoff):
        self.cutoffs.append(url)

    def update_watchlist_status(self, cid, status):
        self.statuses.append((cid, status))


def _run_batch(monkeypatch, tmp_path, outcomes):
    """Drive _batch_worker over one item per channel, with _process_one_url
    stubbed to the given (downloaded, skipped, errored) tuples. Returns the
    stub DB so the caller can assert on what the cleanup did."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    mod = _load()
    try:
        app = mod.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")

    cids = list(range(1, len(outcomes) + 1))
    db = _StubDB(cids)
    app._db = db
    app._active_watchlist_batch = {"channel_ids": cids}
    app._wl_download_active = True

    calls = iter(outcomes)
    # The real _process_one_url sets _last_url_error on every call; the
    # progress log reads it back when an item reports errors.
    app._last_url_error = "stub failure"
    monkeypatch.setattr(app, "_process_one_url",
                        lambda *a, **k: next(calls))
    # The cleanup schedules card redraws through Tk; the stub DB has no card
    # widgets, so keep the batch worker's UI callbacks out of the way.
    monkeypatch.setattr(app, "_watchlist_update_cards", lambda *a, **k: None)
    monkeypatch.setattr(app, "_refresh_genre_list", lambda *a, **k: None)

    run_batch = [{"url": f"https://ch/{c}", "genre": "(none)",
                  "platform": "YouTube", "channel_name": f"ch{c}",
                  "title": f"ch{c}"} for c in cids]
    app._batch_worker(run_batch)
    app.update()
    app.destroy()
    return db


def test_failed_channel_keeps_its_pending_list(tmp_path, monkeypatch):
    db = _run_batch(monkeypatch, tmp_path, [(0, 0, 2)])
    assert db.cleared == []
    assert db.cutoffs == []
    assert db.get_total_pending_count() == 2
    assert (1, "idle") in db.statuses


def test_clean_channel_still_retires_its_pending_list(tmp_path, monkeypatch):
    db = _run_batch(monkeypatch, tmp_path, [(2, 0, 0)])
    assert db.cleared == [1]
    assert db.cutoffs == ["https://ch/1"]
    assert db.get_total_pending_count() == 0


def test_a_failure_does_not_drag_down_its_neighbours(tmp_path, monkeypatch):
    """Per-channel accounting: channel 2 failing must not hold back 1 and 3,
    and channel 2's own pending list must survive for the retry."""
    db = _run_batch(monkeypatch, tmp_path,
                    [(1, 0, 0), (0, 0, 1), (1, 0, 0)])
    assert db.cleared == [1, 3]
    assert db.get_watchlist_channel(2)["pending_new_count"] == 2


def test_skipped_only_channel_counts_as_clean(tmp_path, monkeypatch):
    """Everything already on disk is a success, not a failure — the pending
    list is genuinely done and must not linger forever."""
    db = _run_batch(monkeypatch, tmp_path, [(0, 5, 0)])
    assert db.cleared == [1]

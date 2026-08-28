"""A watchlist row saying 'scanning' with no live thread behind it is a ghost.

'scanning' only means anything while a scan thread owns the row, and no thread
survives a restart — a session that dies mid-scan (a crash, an update swap)
freezes its rows there, and the cards then boot with cancel buttons nothing can
ever clear. reset_stale_watchlist_scans is the cure: startup calls it before
the first render, and the global Cancel button calls it to sweep ghosts.
"""
from cratebuilder.db import DownloadsDatabase


def _db_with(tmp_path, statuses):
    db = DownloadsDatabase(str(tmp_path / "t.db"))
    ids = []
    for i, status in enumerate(statuses):
        wid = db.add_watchlist_channel(
            url=f"https://www.youtube.com/channel/UC{i}/videos",
            display_name=f"Ch{i}", platform="YouTube", genre="(none)")
        db.update_watchlist_status(wid, status)
        ids.append(wid)
    return db, ids


def test_scanning_rows_reset_to_idle_and_are_counted(tmp_path):
    db, ids = _db_with(tmp_path, ["scanning", "scanning", "idle"])
    assert db.reset_stale_watchlist_scans() == 2
    assert [db.get_watchlist_channel(w)["status"] for w in ids] == \
        ["idle", "idle", "idle"]


def test_downloading_rows_are_swept_too(tmp_path):
    """The web frontend writes 'downloading' where the tkinter app only wrote
    'scanning', and both open the same database — so a row left mid-download by
    a killed frontend has to be cleared by whichever one starts next. Task 9's
    downloading card greys out every control except a Cancel, so a stuck row
    there is a permanently dead card."""
    db, ids = _db_with(tmp_path, ["downloading", "scanning", "found"])
    assert db.reset_stale_watchlist_scans() == 2
    assert [db.get_watchlist_channel(w)["status"] for w in ids] == \
        ["idle", "idle", "found"]


def test_every_other_status_survives_untouched(tmp_path):
    """The sweep must not eat real state — a 'found' card's new-track badge,
    a dead link's needs_resolve, an offline marker."""
    statuses = ["found", "needs_resolve", "offline", "error", "idle"]
    db, ids = _db_with(tmp_path, statuses)
    assert db.reset_stale_watchlist_scans() == 0
    assert [db.get_watchlist_channel(w)["status"] for w in ids] == statuses


def test_an_empty_watchlist_is_a_safe_no_op(tmp_path):
    db = DownloadsDatabase(str(tmp_path / "t.db"))
    assert db.reset_stale_watchlist_scans() == 0

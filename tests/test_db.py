def _new_db(cb, tmp_path):
    # DownloadsDatabase takes a db path (db_path, debug_logger=None).
    return cb.DownloadsDatabase(str(tmp_path / "test.db"))


def test_schema_init_idempotent(cb, tmp_path):
    db = _new_db(cb, tmp_path)
    # Re-initialising must not raise (idempotent CREATE/ALTER).
    db2 = cb.DownloadsDatabase(str(tmp_path / "test.db"))
    assert db2 is not None


def test_watchlist_insert_and_dedup(cb, tmp_path):
    db = _new_db(cb, tmp_path)
    row = dict(url="https://www.youtube.com/channel/UC1/videos",
               channel_id="UC1", display_name="One", platform="YouTube",
               genre="DnB", scan_cutoff_date="2026-01-01")
    # Real insert method is `add_watchlist_channel` (keyword-only args).
    first = db.add_watchlist_channel(**row)   # confirmed method name in source
    second = db.add_watchlist_channel(**row)  # duplicate url
    chans = db.get_all_watchlist_channels()
    urls = [c["url"] for c in chans]
    assert urls.count(row["url"]) == 1  # UNIQUE(url) prevented a duplicate

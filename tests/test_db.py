from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path):
    # DownloadsDatabase takes a db path (db_path, debug_logger=None).
    return DownloadsDatabase(str(tmp_path / "test.db"))


def test_schema_init_idempotent(tmp_path):
    db = _new_db(tmp_path)
    # Re-initialising the same DB file must not raise (idempotent CREATE/ALTER)
    # and the schema must be usable afterward.
    db2 = DownloadsDatabase(str(tmp_path / "test.db"))
    assert db2.get_all_watchlist_channels() == []


def test_watchlist_insert_and_dedup(tmp_path):
    db = _new_db(tmp_path)
    row = dict(url="https://www.youtube.com/channel/UC1/videos",
               channel_id="UC1", display_name="One", platform="YouTube",
               genre="DnB", scan_cutoff_date="2026-01-01")
    # Real insert method is `add_watchlist_channel` (keyword-only args).
    first = db.add_watchlist_channel(**row)   # confirmed method name in source
    second = db.add_watchlist_channel(**row)  # duplicate url
    # Insert returns a row id; the duplicate is swallowed (IntegrityError) -> None.
    assert first is not None
    assert second is None
    chans = db.get_all_watchlist_channels()
    urls = [c["url"] for c in chans]
    assert urls.count(row["url"]) == 1  # UNIQUE(url) prevented a duplicate


def test_update_fields_returns_true_on_success(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCaaa/videos",
        display_name="A", platform="YouTube", genre="(none)",
        scan_cutoff_date="20260101")
    assert db.update_watchlist_channel_fields(
        wid, channel_id="UCaaa", status="idle") is True


def test_update_fields_returns_false_on_unique_collision(tmp_path):
    db = _new_db(tmp_path)
    db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCdup/videos",
        display_name="Existing", platform="YouTube", genre="(none)",
        scan_cutoff_date="20260101")
    other = db.add_watchlist_channel(
        url="https://www.youtube.com/@Some Name", display_name="Dup",
        platform="YouTube", genre="(none)", scan_cutoff_date="20260101")
    ok = db.update_watchlist_channel_fields(
        other, url="https://www.youtube.com/channel/UCdup/videos",
        channel_id="UCdup", status="idle")
    assert ok is False
    row = db.get_watchlist_channel(other)
    assert row["channel_id"] in (None, "")
    assert " " in row["url"]

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


def test_set_watchlist_download_started(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCdl/videos",
        display_name="DL", platform="YouTube", genre="(none)",
        scan_cutoff_date="20260101")
    # Brand-new channel has never downloaded.
    assert db.get_watchlist_channel(wid).get("last_download_started") is None
    db.set_watchlist_download_started([wid], 4242)
    assert db.get_watchlist_channel(wid)["last_download_started"] == 4242
    # Empty/iterable-of-nothing is a safe no-op.
    db.set_watchlist_download_started([], 9999)
    assert db.get_watchlist_channel(wid)["last_download_started"] == 4242


def test_get_all_downloads_empty(tmp_path):
    db = _new_db(tmp_path)
    assert db.get_all_downloads() == []


def test_get_all_downloads_newest_first(tmp_path):
    db = _new_db(tmp_path)

    def _add(title, ts):
        db.add_download(video_id=title, title=title, channel_name="Chan",
                        channel_url="https://yt/c", platform="YouTube",
                        genre="House", file_path=f"/x/{title}.mp3",
                        upload_date="20260101", bitrate="320")
        # add_download stamps download_timestamp=now(); override for ordering.
        with db._conn() as conn:
            conn.execute(
                "UPDATE downloads SET download_timestamp = ? WHERE video_id = ?",
                (ts, title))

    _add("older", 1000)
    _add("newer", 2000)

    rows = db.get_all_downloads()
    assert [r["title"] for r in rows] == ["newer", "older"]
    # Rows come back as plain dicts carrying the expected columns.
    assert rows[0]["channel_name"] == "Chan"
    assert rows[0]["genre"] == "House"


def test_backfill_downloads_roundtrip(tmp_path):
    # Rebuild-from-files relies on backfill_downloads to bulk-insert rows
    # discovered on disk, carrying an explicit timestamp for ordering.
    db = _new_db(tmp_path)
    rows = [
        dict(video_id=None, title="Track Two", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/Track Two.mp3", upload_date="20260102",
             ts=2000, bitrate=""),
        dict(video_id=None, title="Track One", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/Track One.mp3", upload_date="20260101",
             ts=1000, bitrate=""),
    ]
    n = db.backfill_downloads(rows)
    assert n == 2
    got = db.get_all_downloads()
    # get_all_downloads orders by download_timestamp DESC -> ts 2000 first.
    assert [r["title"] for r in got] == ["Track Two", "Track One"]
    assert got[0]["genre"] == "DnB"
    assert got[0]["file_path"] == "/x/Track Two.mp3"
    # Empty list is a no-op that inserts nothing.
    assert db.backfill_downloads([]) == 0


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

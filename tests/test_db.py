import sqlite3

from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path):
    # DownloadsDatabase takes a db path (db_path, debug_logger=None).
    return DownloadsDatabase(str(tmp_path / "test.db"))


def _columns(db, table="downloads"):
    with db._conn() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


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
               genre="DnB")
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
        display_name="A", platform="YouTube", genre="(none)")
    assert db.update_watchlist_channel_fields(
        wid, channel_id="UCaaa", status="idle") is True


def test_set_watchlist_download_started(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCdl/videos",
        display_name="DL", platform="YouTube", genre="(none)")
    # Brand-new channel has never downloaded.
    assert db.get_watchlist_channel(wid).get("last_download_started") is None
    db.set_watchlist_download_started([wid], 4242)
    assert db.get_watchlist_channel(wid)["last_download_started"] == 4242
    # Empty/iterable-of-nothing is a safe no-op.
    db.set_watchlist_download_started([], 9999)
    assert db.get_watchlist_channel(wid)["last_download_started"] == 4242


def test_status_only_update_preserves_scan_results(tmp_path):
    # A transient scan failure (offline) writes ONLY the status, so the pending
    # tracks and last-scanned stamp a previous good scan found must survive.
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCoff/videos",
        display_name="Off", platform="YouTube", genre="(none)")
    db.update_watchlist_scan_result(
        wid, timestamp=1000, pending_count=3,
        pending_entries=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
        status="found")
    db.update_watchlist_status(wid, "offline", "getaddrinfo failed")
    row = db.get_watchlist_channel(wid)
    assert row["status"] == "offline"
    assert row["last_error"] == "getaddrinfo failed"
    assert row["pending_new_count"] == 3
    assert row["last_scanned_timestamp"] == 1000
    assert row["url"] == "https://www.youtube.com/channel/UCoff/videos"


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


def test_backfill_missing_download_timestamps(tmp_path):
    # Tracks imported before the DB feature carry download_timestamp <= 0; the
    # viewer fills these from the file's creation time and persists them here.
    db = _new_db(tmp_path)
    db.backfill_downloads([
        dict(video_id=None, title="No Date", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/No Date.mp3", upload_date="",
             ts=0, bitrate=""),
        dict(video_id=None, title="Has Date", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/Has Date.mp3", upload_date="",
             ts=5000, bitrate=""),
    ])
    rows = {r["title"]: r for r in db.get_all_downloads()}
    no_date_id = rows["No Date"]["id"]
    has_date_id = rows["Has Date"]["id"]

    n = db.backfill_missing_download_timestamps(
        [(1234, no_date_id), (9999, has_date_id)])
    assert n == 2  # returns the number of update tuples it was handed

    rows = {r["title"]: r for r in db.get_all_downloads()}
    # The zero-timestamp row was filled in...
    assert rows["No Date"]["download_timestamp"] == 1234
    # ...but the row that already had a timestamp is guarded and left untouched.
    assert rows["Has Date"]["download_timestamp"] == 5000

    # Empty input is a safe no-op.
    assert db.backfill_missing_download_timestamps([]) == 0


def test_update_fields_returns_false_on_unique_collision(tmp_path):
    db = _new_db(tmp_path)
    db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCdup/videos",
        display_name="Existing", platform="YouTube", genre="(none)")
    other = db.add_watchlist_channel(
        url="https://www.youtube.com/@Some Name", display_name="Dup",
        platform="YouTube", genre="(none)")
    ok = db.update_watchlist_channel_fields(
        other, url="https://www.youtube.com/channel/UCdup/videos",
        channel_id="UCdup", status="idle")
    assert ok is False
    row = db.get_watchlist_channel(other)
    assert row["channel_id"] in (None, "")
    assert " " in row["url"]


def test_delete_downloads_by_paths_removes_only_matches(tmp_path):
    db = _new_db(tmp_path)
    db.add_download(video_id="v1", title="A", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/a.mp3", upload_date="20240101", bitrate="192")
    db.add_download(video_id="v2", title="B", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/b.mp3", upload_date="20240102", bitrate="192")
    db.add_download(video_id="v3", title="D", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/keep.mp3", upload_date="20240103", bitrate="192")

    removed = db.delete_downloads_by_paths(["/f/a.mp3", "/f/b.mp3"])
    assert removed == 2

    paths = {d["file_path"] for d in db.get_all_downloads()}
    assert paths == {"/f/keep.mp3"}


def test_delete_downloads_by_paths_empty_is_noop(tmp_path):
    db = _new_db(tmp_path)
    db.add_download(video_id="v1", title="A", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/a.mp3", upload_date="20240101", bitrate="192")
    assert db.delete_downloads_by_paths([]) == 0
    assert len(db.get_all_downloads()) == 1


def test_get_watchlist_channel_by_channel_id(tmp_path):
    db = _new_db(tmp_path)
    db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCmatch/videos",
        channel_id="UCmatch", display_name="Match", platform="YouTube",
        genre="(none)")
    # A row whose channel_id is NULL must never be returned by an id lookup.
    db.add_watchlist_channel(
        url="https://www.youtube.com/@nullid", display_name="NullId",
        platform="YouTube", genre="(none)")

    row = db.get_watchlist_channel_by_channel_id("UCmatch")
    assert row is not None
    assert row["display_name"] == "Match"

    # Missing / blank ids never match (and must not return the NULL-id row).
    assert db.get_watchlist_channel_by_channel_id("UCnope") is None
    assert db.get_watchlist_channel_by_channel_id("") is None
    assert db.get_watchlist_channel_by_channel_id(None) is None


def test_delete_blank_watchlist_channels(tmp_path):
    db = _new_db(tmp_path)
    db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCnamed/videos",
        display_name="Named", platform="YouTube", genre="(none)")
    # Blank-name rows: empty string and whitespace-only. These are the broken
    # cards we want gone. (The schema is display_name TEXT NOT NULL, so a true
    # NULL can't occur; '' and '   ' are the only blank forms to handle.)
    blank_empty = db.add_watchlist_channel(
        url="https://www.youtube.com/@blank1", display_name="",
        platform="YouTube", genre="(none)")
    blank_space = db.add_watchlist_channel(
        url="https://www.youtube.com/@blank2", display_name="   ",
        platform="YouTube", genre="(none)")
    assert blank_empty is not None and blank_space is not None

    removed = db.delete_blank_watchlist_channels()
    assert removed == 2

    remaining = db.get_all_watchlist_channels()
    assert [c["display_name"] for c in remaining] == ["Named"]

    # Idempotent: nothing left to delete on a second pass.
    assert db.delete_blank_watchlist_channels() == 0


# ── schema v4: cover art columns ───────────────────────────────────────────

# The v3 downloads table, verbatim, as it exists in databases already on disk.
_V3_DOWNLOADS_SCHEMA = """
    CREATE TABLE downloads (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id            TEXT,
        title               TEXT NOT NULL,
        channel_name        TEXT,
        channel_url         TEXT,
        channel_id          TEXT,
        platform            TEXT NOT NULL,
        genre               TEXT,
        file_path           TEXT,
        upload_date         TEXT,
        download_timestamp  INTEGER NOT NULL,
        bitrate             TEXT
    );
"""

_ARTWORK_COLUMNS = {"artwork_path", "artwork_embedded", "thumbnail_url"}


def test_fresh_db_has_artwork_columns(tmp_path):
    db = _new_db(tmp_path)
    assert _ARTWORK_COLUMNS <= _columns(db)


def test_schema_version_is_6(tmp_path):
    db = _new_db(tmp_path)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'").fetchone()
    assert row["value"] == "6"
    assert DownloadsDatabase.SCHEMA_VERSION == 6


def test_fresh_watchlist_has_no_scan_cutoff_column(tmp_path):
    db = _new_db(tmp_path)
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)")}
    assert "scan_cutoff_date" not in cols


def test_v3_database_migrates_to_v4_without_data_loss(tmp_path):
    """A live v3 database on a user's disk must open cleanly at v4, keep every
    existing row exactly as it was, and gain the three cover-art columns."""
    path = str(tmp_path / "legacy.db")

    # Hand-build a v3 database: old downloads schema, no artwork columns.
    conn = sqlite3.connect(path)
    conn.executescript(_V3_DOWNLOADS_SCHEMA)
    conn.execute("""
        INSERT INTO downloads
          (video_id, title, channel_name, channel_url, channel_id, platform,
           genre, file_path, upload_date, download_timestamp, bitrate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("abc123", "Legacy Track", "Old Chan", "https://yt/old", "UColl",
          "YouTube", "DnB", "/music/Legacy Track.mp3", "20250314",
          1700000000, "320"))
    conn.commit()
    conn.close()

    conn = sqlite3.connect(path)
    pre_migration_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(downloads)")}
    conn.close()
    assert _ARTWORK_COLUMNS.isdisjoint(pre_migration_cols)

    # Opening it must migrate in place, not raise.
    db = DownloadsDatabase(path)

    # (a) the columns now exist...
    assert _ARTWORK_COLUMNS <= _columns(db)

    # (b) ...and the pre-existing row survived intact.
    rows = db.get_all_downloads()
    assert len(rows) == 1
    row = rows[0]
    assert row["video_id"] == "abc123"
    assert row["title"] == "Legacy Track"
    assert row["channel_name"] == "Old Chan"
    assert row["channel_url"] == "https://yt/old"
    assert row["channel_id"] == "UColl"
    assert row["platform"] == "YouTube"
    assert row["genre"] == "DnB"
    assert row["file_path"] == "/music/Legacy Track.mp3"
    assert row["upload_date"] == "20250314"
    assert row["download_timestamp"] == 1700000000
    assert row["bitrate"] == "320"

    # (c) the new columns default sanely on the old row. ALTER TABLE backfills
    # existing rows with the column default, so artwork_embedded is 0 — not
    # NULL — while the two TEXT columns have no default and land NULL.
    assert row["artwork_path"] is None
    assert row["artwork_embedded"] == 0
    assert row["thumbnail_url"] is None

    # The migration is idempotent: reopening must not raise or duplicate.
    db2 = DownloadsDatabase(path)
    assert len(db2.get_all_downloads()) == 1


# ── schema v5: unavailable_tracks memory ───────────────────────────────────

# The v4 downloads/watchlist tables, verbatim, as they exist on disk before
# unavailable_tracks was introduced.
_V4_SCHEMA = """
    CREATE TABLE schema_info (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    INSERT INTO schema_info (key, value) VALUES ('version', '4');
    CREATE TABLE downloads (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id            TEXT,
        title               TEXT NOT NULL,
        channel_name        TEXT,
        channel_url         TEXT,
        channel_id          TEXT,
        platform            TEXT NOT NULL,
        genre               TEXT,
        file_path           TEXT,
        upload_date         TEXT,
        download_timestamp  INTEGER NOT NULL,
        bitrate             TEXT,
        artwork_path        TEXT,
        artwork_embedded    INTEGER DEFAULT 0,
        thumbnail_url       TEXT
    );
    CREATE TABLE watchlist (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        url                      TEXT NOT NULL UNIQUE,
        channel_id               TEXT,
        display_name             TEXT NOT NULL,
        platform                 TEXT NOT NULL,
        genre                    TEXT,
        scan_cutoff_date         TEXT NOT NULL,
        date_added               INTEGER NOT NULL,
        last_scanned_timestamp   INTEGER,
        last_download_started    INTEGER,
        pending_new_count        INTEGER DEFAULT 0,
        pending_entries_json     TEXT    DEFAULT '[]',
        total_downloaded         INTEGER DEFAULT 0,
        auto_added               INTEGER DEFAULT 0,
        status                   TEXT    DEFAULT 'idle',
        last_error               TEXT
    );
"""


def test_v5_database_drops_scan_cutoff_without_data_loss(tmp_path):
    """v6 removes watchlist.scan_cutoff_date. A live v5 database has to lose
    exactly that column and nothing else — the surrounding row must survive,
    because this runs against the user's real library on first launch."""
    path = str(tmp_path / "legacy_v5.db")
    conn = sqlite3.connect(path)
    conn.executescript(_V4_SCHEMA)          # v5 shares the v4 watchlist shape
    conn.executescript("""
        CREATE TABLE unavailable_tracks (
            platform TEXT NOT NULL, video_id TEXT NOT NULL,
            channel_url TEXT, title TEXT, reason TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            first_failed INTEGER NOT NULL, last_failed INTEGER NOT NULL,
            PRIMARY KEY (platform, video_id));
    """)
    conn.execute("""
        INSERT INTO watchlist
          (url, channel_id, display_name, platform, genre, scan_cutoff_date,
           date_added, last_scanned_timestamp, pending_new_count,
           pending_entries_json, total_downloaded, auto_added, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("https://www.youtube.com/channel/UCv5/videos", "UCv5", "V5 Chan",
          "YouTube", "DnB", "20250101", 1700000000, 1700000500, 3,
          '[{"id": "x"}]', 7, 1, "idle"))
    conn.commit()
    conn.close()

    db = DownloadsDatabase(path)

    with db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(watchlist)")}
        version = c.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()["value"]
    assert "scan_cutoff_date" not in cols
    assert version == "6"

    rows = db.get_all_watchlist_channels()
    assert len(rows) == 1
    wl = rows[0]
    assert wl["url"] == "https://www.youtube.com/channel/UCv5/videos"
    assert wl["channel_id"] == "UCv5"
    assert wl["display_name"] == "V5 Chan"
    assert wl["platform"] == "YouTube"
    assert wl["genre"] == "DnB"
    assert wl["date_added"] == 1700000000
    assert wl["last_scanned_timestamp"] == 1700000500
    # The pending queue is the one thing a botched migration would quietly
    # destroy, taking the user's unfinished downloads with it.
    assert wl["pending_new_count"] == 3
    assert wl["pending_entries_json"] == '[{"id": "x"}]'
    assert wl["total_downloaded"] == 7
    assert wl["auto_added"] == 1
    assert wl["status"] == "idle"


def test_a_failing_column_drop_does_not_block_startup(tmp_path):
    """The drop is cosmetic — nothing reads the column. When it cannot happen
    (SQLite older than 3.35 has no DROP COLUMN; here, an index pins the
    column) the app must still open the database rather than refusing to
    start on a user's machine right after an auto-update."""
    path = str(tmp_path / "olds.db")
    conn = sqlite3.connect(path)
    conn.executescript(_V4_SCHEMA)
    # SQLite refuses to drop an indexed column — a real, reproducible failure
    # of the same shape as the missing-DROP-COLUMN one.
    conn.execute("CREATE INDEX idx_cutoff ON watchlist(scan_cutoff_date)")
    conn.execute("""
        INSERT INTO watchlist
          (url, display_name, platform, genre, scan_cutoff_date, date_added)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("https://yt/x", "X", "YouTube", "DnB", "20250101", 1700000000))
    conn.commit()
    conn.close()

    db = DownloadsDatabase(path)          # must not raise

    with db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(watchlist)")}
    assert "scan_cutoff_date" in cols     # the drop really did fail

    rows = db.get_all_watchlist_channels()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "X"
    # The column survives, inert — and inserts that never mention it still work.
    assert db.add_watchlist_channel(
        url="https://yt/y", display_name="Y",
        platform="YouTube", genre="DnB") is not None


def test_migration_is_idempotent_on_an_already_v6_database(tmp_path):
    """Re-opening a migrated database must not raise on the DROP COLUMN that
    has nothing left to drop."""
    path = str(tmp_path / "twice.db")
    db = DownloadsDatabase(path)
    db.add_watchlist_channel(url="https://yt/a", display_name="A",
                             platform="YouTube", genre="DnB")
    reopened = DownloadsDatabase(path)
    assert len(reopened.get_all_watchlist_channels()) == 1


def test_v4_database_migrates_to_v5_without_data_loss(tmp_path):
    """A live v4 database on a user's disk must open cleanly at v5, keep every
    existing row exactly as it was, and gain the unavailable_tracks table."""
    path = str(tmp_path / "legacy_v4.db")

    # Hand-build a v4 database: old downloads/watchlist shape, no
    # unavailable_tracks table.
    conn = sqlite3.connect(path)
    conn.executescript(_V4_SCHEMA)
    conn.execute("""
        INSERT INTO downloads
          (video_id, title, channel_name, channel_url, channel_id, platform,
           genre, file_path, upload_date, download_timestamp, bitrate,
           artwork_path, artwork_embedded, thumbnail_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("abc123", "V4 Track", "V4 Chan", "https://yt/v4", "UCv4",
          "YouTube", "DnB", "/music/V4 Track.mp3", "20250314",
          1700000000, "320", "/music/.artwork/abc123.jpg", 1,
          "https://i.ytimg.com/vi/abc123/hq.jpg"))
    conn.execute("""
        INSERT INTO watchlist
          (url, channel_id, display_name, platform, genre, scan_cutoff_date,
           date_added, total_downloaded, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("https://www.youtube.com/channel/UCv4/videos", "UCv4", "V4 Chan",
          "YouTube", "DnB", "20250101", 1700000000, 1, "idle"))
    conn.commit()
    conn.close()

    conn = sqlite3.connect(path)
    pre_migration_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert "unavailable_tracks" not in pre_migration_tables

    # Opening it must migrate in place, not raise.
    db = DownloadsDatabase(path)

    # (a) the unavailable_tracks table and its index now exist...
    with db._conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "unavailable_tracks" in tables
    assert "idx_unavail_channel_url" in indexes

    # (b) schema_info reports the current version...
    with db._conn() as conn:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'").fetchone()
    assert row["value"] == "6"

    # (c) ...and the pre-existing rows survived byte-for-byte.
    dl_rows = db.get_all_downloads()
    assert len(dl_rows) == 1
    dl = dl_rows[0]
    assert dl["video_id"] == "abc123"
    assert dl["title"] == "V4 Track"
    assert dl["channel_name"] == "V4 Chan"
    assert dl["channel_url"] == "https://yt/v4"
    assert dl["channel_id"] == "UCv4"
    assert dl["platform"] == "YouTube"
    assert dl["genre"] == "DnB"
    assert dl["file_path"] == "/music/V4 Track.mp3"
    assert dl["upload_date"] == "20250314"
    assert dl["download_timestamp"] == 1700000000
    assert dl["bitrate"] == "320"
    assert dl["artwork_path"] == "/music/.artwork/abc123.jpg"
    assert dl["artwork_embedded"] == 1
    assert dl["thumbnail_url"] == "https://i.ytimg.com/vi/abc123/hq.jpg"

    wl_rows = db.get_all_watchlist_channels()
    assert len(wl_rows) == 1
    wl = wl_rows[0]
    assert wl["url"] == "https://www.youtube.com/channel/UCv4/videos"
    assert wl["channel_id"] == "UCv4"
    assert wl["display_name"] == "V4 Chan"
    assert wl["platform"] == "YouTube"
    assert wl["genre"] == "DnB"
    # scan_cutoff_date went away in v6 — the row survives without it.
    assert "scan_cutoff_date" not in wl.keys()
    assert wl["date_added"] == 1700000000
    assert wl["total_downloaded"] == 1
    assert wl["status"] == "idle"

    # (d) a record_unavailable upsert works against the migrated DB.
    assert db.record_unavailable(
        platform="YouTube", video_id="abc123",
        channel_url="https://www.youtube.com/channel/UCv4",
        title="V4 Track", reason="DRM-protected") is True
    assert db.count_unavailable_for_channel(
        "https://www.youtube.com/channel/UCv4") == 1

    # The migration is idempotent: reopening must not raise or duplicate.
    db2 = DownloadsDatabase(path)
    assert len(db2.get_all_downloads()) == 1
    assert len(db2.get_all_watchlist_channels()) == 1


def test_add_download_without_artwork_args_still_works(tmp_path):
    # Every pre-cover-art call site omits the new kwargs and must keep working.
    db = _new_db(tmp_path)
    db.add_download(video_id="v1", title="No Art", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/noart.mp3", upload_date="20240101",
                    bitrate="320")
    row = db.get_all_downloads()[0]
    assert row["title"] == "No Art"
    assert row["artwork_path"] is None
    assert row["artwork_embedded"] == 0
    assert row["thumbnail_url"] is None


def test_add_download_with_artwork_args_roundtrip(tmp_path):
    db = _new_db(tmp_path)
    db.add_download(video_id="v2", title="Arted", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/arted.mp3", upload_date="20240101",
                    bitrate="320",
                    artwork_path="/f/.artwork/v2.jpg",
                    artwork_embedded=True,
                    thumbnail_url="https://i.ytimg.com/vi/v2/hq.jpg")
    row = db.get_all_downloads()[0]
    assert row["artwork_path"] == "/f/.artwork/v2.jpg"
    # A True/False from the caller is coerced to the 1/0 the column stores.
    assert row["artwork_embedded"] == 1
    assert row["thumbnail_url"] == "https://i.ytimg.com/vi/v2/hq.jpg"


def test_backfill_downloads_without_artwork_keys(tmp_path):
    # Regression: named-style binding raises on a missing key, and every
    # existing caller builds row dicts with no artwork keys at all.
    db = _new_db(tmp_path)
    n = db.backfill_downloads([
        dict(video_id=None, title="Bare", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/Bare.mp3", upload_date="20260101",
             ts=1000, bitrate=""),
    ])
    assert n == 1
    row = db.get_all_downloads()[0]
    assert row["title"] == "Bare"
    assert row["artwork_path"] is None
    assert row["artwork_embedded"] == 0
    assert row["thumbnail_url"] is None


def test_backfill_downloads_carries_artwork_when_present(tmp_path):
    db = _new_db(tmp_path)
    n = db.backfill_downloads([
        dict(video_id="v9", title="Arted", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path="/x/Arted.mp3", upload_date="20260101",
             ts=1000, bitrate="", artwork_path="/x/.artwork/v9.jpg",
             artwork_embedded=1, thumbnail_url="https://img/v9.jpg"),
    ])
    assert n == 1
    row = db.get_all_downloads()[0]
    assert row["artwork_path"] == "/x/.artwork/v9.jpg"
    assert row["artwork_embedded"] == 1
    assert row["thumbnail_url"] == "https://img/v9.jpg"


def test_set_download_artwork_updates_matching_row(tmp_path):
    db = _new_db(tmp_path)
    db.add_download(video_id="v1", title="Target", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/target.mp3", upload_date="20240101",
                    bitrate="320")
    db.add_download(video_id="v2", title="Other", channel_name="C",
                    channel_url="http://c", platform="YouTube", genre="g",
                    file_path="/f/other.mp3", upload_date="20240102",
                    bitrate="320")

    n = db.set_download_artwork("/f/target.mp3", "/f/.artwork/v1.jpg", True,
                                thumbnail_url="https://img/v1.jpg")
    assert n == 1

    rows = {r["title"]: r for r in db.get_all_downloads()}
    assert rows["Target"]["artwork_path"] == "/f/.artwork/v1.jpg"
    assert rows["Target"]["artwork_embedded"] == 1
    assert rows["Target"]["thumbnail_url"] == "https://img/v1.jpg"
    # The sibling row is untouched.
    assert rows["Other"]["artwork_path"] is None
    assert rows["Other"]["artwork_embedded"] == 0

    # No match and blank input are safe no-ops, not raises.
    assert db.set_download_artwork("/f/nope.mp3", "/a.jpg", 1) == 0
    assert db.set_download_artwork("", "/a.jpg", 1) == 0


# ── artwork backfill query layer ───────────────────────────────────────────

def _add_backfill_row(db, title, ts, *, file_path=None, artwork_path=None,
                      artwork_embedded=0, thumbnail_url=None):
    """Insert one downloads row with an explicit timestamp and artwork state.
    backfill_downloads is the only insert path that lets us pin the timestamp,
    which the ordering assertions need."""
    db.backfill_downloads([
        dict(video_id=title, title=title, channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB",
             file_path=f"/x/{title}.mp3" if file_path is None else file_path,
             upload_date="20260101", ts=ts, bitrate="320",
             artwork_path=artwork_path, artwork_embedded=artwork_embedded,
             thumbnail_url=thumbnail_url),
    ])


def test_set_download_video_id_updates_matching_row(tmp_path):
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Lost Id", 1000, file_path="/x/Lost Id.mp3")
    with db._conn() as conn:
        conn.execute("UPDATE downloads SET video_id = NULL "
                     "WHERE title = 'Lost Id'")

    assert db.set_download_video_id("/x/Lost Id.mp3", "vidXYZ") == 1
    with db._conn() as conn:
        row = conn.execute("SELECT video_id FROM downloads "
                           "WHERE title = 'Lost Id'").fetchone()
    assert row["video_id"] == "vidXYZ"


def test_set_download_video_id_rejects_falsy_inputs(tmp_path):
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Row", 1000, file_path="/x/Row.mp3")
    assert db.set_download_video_id(None, "vid") == 0
    assert db.set_download_video_id("/x/Row.mp3", None) == 0
    assert db.set_download_video_id("/x/NoSuch.mp3", "vid") == 0


def test_get_downloads_missing_artwork_excludes_embedded(tmp_path):
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Needs Art", 1000)
    _add_backfill_row(db, "Has Art", 2000,
                      artwork_path="/x/.artwork/a.jpg", artwork_embedded=1,
                      thumbnail_url="https://img/a.jpg")

    titles = [r["title"] for r in db.get_downloads_missing_artwork()]
    assert titles == ["Needs Art"]


def test_get_downloads_missing_artwork_includes_null_embedded(tmp_path):
    # A row migrated from v3 could carry NULL rather than 0 if the flag was ever
    # explicitly nulled; NULL means "not embedded" and must appear in the list.
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Nulled", 1000)
    with db._conn() as conn:
        conn.execute(
            "UPDATE downloads SET artwork_embedded = NULL WHERE title = ?",
            ("Nulled",))

    titles = [r["title"] for r in db.get_downloads_missing_artwork()]
    assert titles == ["Nulled"]


def test_get_downloads_missing_artwork_excludes_pathless_rows(tmp_path):
    # A row with no file on disk can never be backfilled — nothing to tag.
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Real", 1000)
    _add_backfill_row(db, "Empty Path", 2000, file_path="")
    _add_backfill_row(db, "Null Path", 3000)
    with db._conn() as conn:
        conn.execute("UPDATE downloads SET file_path = NULL WHERE title = ?",
                     ("Null Path",))

    titles = [r["title"] for r in db.get_downloads_missing_artwork()]
    assert titles == ["Real"]


def test_get_downloads_missing_artwork_orders_oldest_first(tmp_path):
    # A long backfill should walk the user's history from the beginning.
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Middle", 2000)
    _add_backfill_row(db, "Newest", 3000)
    _add_backfill_row(db, "Oldest", 1000)

    titles = [r["title"] for r in db.get_downloads_missing_artwork()]
    assert titles == ["Oldest", "Middle", "Newest"]


def test_count_downloads_missing_artwork_agrees_with_rows(tmp_path):
    db = _new_db(tmp_path)
    assert db.count_downloads_missing_artwork() == 0

    _add_backfill_row(db, "One", 1000)
    _add_backfill_row(db, "Two", 2000)
    _add_backfill_row(db, "Done", 3000,
                      artwork_path="/x/.artwork/d.jpg", artwork_embedded=1)
    _add_backfill_row(db, "Pathless", 4000, file_path="")

    assert db.count_downloads_missing_artwork() == 2
    assert (db.count_downloads_missing_artwork()
            == len(db.get_downloads_missing_artwork()))


def test_get_artwork_by_path_returns_only_arted_rows(tmp_path):
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Bare", 1000)  # no artwork data at all -> excluded
    _add_backfill_row(db, "Embedded", 2000,
                      artwork_path="/x/.artwork/e.jpg", artwork_embedded=1,
                      thumbnail_url="https://img/e.jpg")
    # Art fetched but never embedded: still artwork bookkeeping worth keeping.
    _add_backfill_row(db, "Fetched", 3000,
                      artwork_path="/x/.artwork/f.jpg", artwork_embedded=0)
    # Only a thumbnail URL recorded (SoundCloud art URLs can't be rebuilt).
    _add_backfill_row(db, "ThumbOnly", 4000,
                      thumbnail_url="https://img/t.jpg")

    snap = db.get_artwork_by_path()
    assert set(snap) == {"/x/Embedded.mp3", "/x/Fetched.mp3",
                         "/x/ThumbOnly.mp3"}
    # video_id is the 4th element; _add_backfill_row stamps it as the title.
    assert snap["/x/Embedded.mp3"] == ("/x/.artwork/e.jpg", 1,
                                       "https://img/e.jpg", "Embedded")
    assert snap["/x/Fetched.mp3"] == ("/x/.artwork/f.jpg", 0, None, "Fetched")
    assert snap["/x/ThumbOnly.mp3"] == (None, 0, "https://img/t.jpg",
                                        "ThumbOnly")


def test_get_artwork_by_path_skips_pathless_rows(tmp_path):
    db = _new_db(tmp_path)
    _add_backfill_row(db, "NoPath", 1000, file_path="",
                      artwork_path="/x/.artwork/n.jpg", artwork_embedded=1)
    assert db.get_artwork_by_path() == {}


def test_get_artwork_by_path_empty_db(tmp_path):
    assert _new_db(tmp_path).get_artwork_by_path() == {}


def test_rebuild_from_files_preserves_artwork(tmp_path):
    """Regression: "Rebuild Database from Files" clears the downloads table and
    re-derives it from disk. The rebuilt rows carry no artwork bookkeeping, so
    without a snapshot every track's cover art is orphaned. This is the exact
    mechanism the monolith uses — snapshot, clear, re-attach by file_path."""
    db = _new_db(tmp_path)
    _add_backfill_row(db, "Arted", 1000,
                      artwork_path="/x/.artwork/arted.jpg", artwork_embedded=1,
                      thumbnail_url="https://img/arted.jpg")
    _add_backfill_row(db, "ThumbOnly", 2000,
                      thumbnail_url="https://img/thumb.jpg")
    _add_backfill_row(db, "Bare", 3000)

    # 1. Snapshot BEFORE the destructive clear.
    snap = db.get_artwork_by_path()
    assert len(snap) == 2

    # 2. The rebuild wipes everything.
    db.clear_all_downloads()
    assert db.get_all_downloads() == []

    # 3. Rows are re-derived from disk — filenames only, no artwork columns —
    #    and the snapshot is re-attached by file_path as they are rebuilt.
    rediscovered = [
        dict(video_id=None, title=t, channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path=f"/x/{t}.mp3", upload_date="20260101",
             ts=ts, bitrate="320")
        for t, ts in (("Arted", 1000), ("ThumbOnly", 2000), ("Bare", 3000))
    ]
    for r in rediscovered:
        art = snap.get(r["file_path"])
        if art:
            (r["artwork_path"], r["artwork_embedded"],
             r["thumbnail_url"], r["video_id"]) = art
    assert db.backfill_downloads(rediscovered) == 3

    # 4. The artwork survived the rebuild intact.
    rows = {r["title"]: r for r in db.get_all_downloads()}
    assert rows["Arted"]["artwork_path"] == "/x/.artwork/arted.jpg"
    assert rows["Arted"]["artwork_embedded"] == 1
    assert rows["Arted"]["thumbnail_url"] == "https://img/arted.jpg"
    assert rows["ThumbOnly"]["artwork_path"] is None
    assert rows["ThumbOnly"]["artwork_embedded"] == 0
    assert rows["ThumbOnly"]["thumbnail_url"] == "https://img/thumb.jpg"
    # The row that never had art still has none — nothing was invented.
    assert rows["Bare"]["artwork_path"] is None
    assert rows["Bare"]["artwork_embedded"] == 0
    assert rows["Bare"]["thumbnail_url"] is None

    # A second snapshot round-trips identically: the rebuild is now idempotent.
    assert db.get_artwork_by_path() == snap

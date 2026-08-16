"""Duplicate downloads rows: prevention (upsert) and cleanup (dedupe).

The bug these cover: the downloads table had no uniqueness on file_path and
every write path was INSERT-only. A "Rebuild Database from Files" re-derives
rows from disk and loses the video_id of any file whose id can't be recovered;
the next Watch List scan then sees those tracks as unknown ids that happen to
be on disk, and backfills a SECOND row for the same file. One rebuild followed
by one scan doubled a 27k-track library.
"""
import sqlite3

from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path, name="test.db"):
    return DownloadsDatabase(str(tmp_path / name))


def _rows(db, path=None):
    with db._conn() as conn:
        if path is None:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM downloads ORDER BY id")]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM downloads WHERE file_path = ? ORDER BY id", (path,))]


def _add(db, **kw):
    base = dict(video_id="v1", title="T", channel_name="C",
                channel_url="u", platform="YouTube", genre="DnB",
                file_path=r"C:\m\a.mp3", upload_date="20250101",
                bitrate="320 kbps")
    base.update(kw)
    db.add_download(**base)


def _backfill(db, **kw):
    base = dict(video_id="v1", title="T", channel_name="C", channel_url="u",
                platform="YouTube", genre="DnB", file_path=r"C:\m\a.mp3",
                upload_date="", ts=1000, bitrate="")
    base.update(kw)
    return db.backfill_downloads([base])


# ── schema ────────────────────────────────────────────────────────────────

def test_schema_version_is_7(tmp_path):
    db = _new_db(tmp_path)
    with db._conn() as conn:
        v = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'").fetchone()
    assert v["value"] == "7"


def test_fresh_db_gets_unique_path_index(tmp_path):
    db = _new_db(tmp_path)
    assert db.has_unique_path_index is True
    with db._conn() as conn:
        names = {r[1] for r in conn.execute("PRAGMA index_list(downloads)")}
    assert "idx_dl_file_path_unique" in names


def test_pathless_rows_do_not_collide(tmp_path):
    # The index is partial, so rows with no file_path (nothing on disk to key
    # on) stay insertable and never conflict with each other.
    db = _new_db(tmp_path)
    _add(db, video_id="v1", file_path="")
    _add(db, video_id="v2", file_path="")
    assert len(_rows(db)) == 2


# ── prevention: add_download upserts ──────────────────────────────────────

def test_redownload_updates_instead_of_inserting(tmp_path):
    db = _new_db(tmp_path)
    _add(db, bitrate="192 kbps")
    _add(db, bitrate="320 kbps")           # Force Download of the same track
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["bitrate"] == "320 kbps"   # fresh download's facts win


def test_redownload_keeps_known_values_the_new_row_lacks(tmp_path):
    db = _new_db(tmp_path)
    _add(db, artwork_path=r"C:\art\a.jpg", artwork_embedded=1,
         thumbnail_url="http://t/1.jpg")
    _add(db, artwork_path=None, thumbnail_url=None, bitrate="")
    row = _rows(db)[0]
    assert row["artwork_path"] == r"C:\art\a.jpg"
    assert row["thumbnail_url"] == "http://t/1.jpg"
    assert row["bitrate"] == "320 kbps"    # empty new value never blanks it


def test_two_videos_landing_on_one_file_keep_one_row(tmp_path):
    # A recurring upload title ("Neurofunk Drum and Bass 2025") sanitises to
    # the same filename, so the newest download overwrites the file on disk.
    # One file, one row.
    db = _new_db(tmp_path)
    _add(db, video_id="vA")
    _add(db, video_id="vB")
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["video_id"] == "vB"


# ── prevention: backfill_downloads upserts and never clobbers ─────────────

def test_scan_backfill_of_known_path_adds_no_row(tmp_path):
    db = _new_db(tmp_path)
    _add(db, video_id=None, bitrate="320 kbps")
    assert _backfill(db, video_id="v1") == 1
    assert len(_rows(db)) == 1


def test_scan_backfill_fills_missing_video_id(tmp_path):
    # This is the repair the scan was always trying to make: an existing row
    # with no id gets the id, rather than a second row carrying it.
    db = _new_db(tmp_path)
    _add(db, video_id=None)
    _backfill(db, video_id="v1")
    assert _rows(db)[0]["video_id"] == "v1"


def test_scan_backfill_does_not_downgrade_a_real_download(tmp_path):
    db = _new_db(tmp_path)
    _add(db, bitrate="320 kbps", upload_date="20250101",
         artwork_path=r"C:\art\a.jpg", artwork_embedded=1)
    _backfill(db, bitrate="", upload_date="", artwork_embedded=0)
    row = _rows(db)[0]
    assert row["bitrate"] == "320 kbps"
    assert row["upload_date"] == "20250101"
    assert row["artwork_embedded"] == 1
    assert row["artwork_path"] == r"C:\art\a.jpg"


def test_backfill_takes_current_channel_and_genre(tmp_path):
    # Where the track *lives* is what a scan/rebuild actually knows, so those
    # columns follow the backfill; what it *is* stays with the download.
    db = _new_db(tmp_path)
    _add(db, genre="DnB", channel_name="Old Name")
    _backfill(db, genre="House", channel_name="New Name")
    row = _rows(db)[0]
    assert row["genre"] == "House"
    assert row["channel_name"] == "New Name"


def test_rebuild_then_scan_does_not_double_the_library(tmp_path):
    """The exact production sequence that created 27,677 redundant rows."""
    db = _new_db(tmp_path)
    paths = [rf"C:\m\t{i}.mp3" for i in range(5)]
    for i, p in enumerate(paths):
        _add(db, video_id=f"v{i}", file_path=p, bitrate="320 kbps")

    # Rebuild from files: video_id unrecoverable, upload_date from mtime.
    db.clear_all_downloads()
    db.backfill_downloads([
        dict(video_id=None, title=f"t{i}", channel_name="C", channel_url="u",
             platform="YouTube", genre="DnB", file_path=p,
             upload_date="20260714", ts=1000, bitrate="",
             artwork_path=rf"C:\art\{i}.jpg", artwork_embedded=1)
        for i, p in enumerate(paths)])
    assert len(_rows(db)) == 5

    # Watch List scan: ids look unknown, files are on disk -> legacy backfill.
    db.backfill_downloads([
        dict(video_id=f"v{i}", title=f"t{i}", channel_name="C",
             channel_url="u", platform="YouTube", genre="DnB", file_path=p,
             upload_date="", ts=2000, bitrate="")
        for i, p in enumerate(paths)])

    rows = _rows(db)
    assert len(rows) == 5                       # not 10
    assert [r["video_id"] for r in rows] == [f"v{i}" for i in range(5)]
    assert all(r["artwork_embedded"] == 1 for r in rows)


# ── legacy databases that already hold duplicates ─────────────────────────

def _legacy_db_with_dupes(tmp_path):
    """A DB opened at v7 whose duplicates block the unique index — i.e. the
    user's live database before any cleanup."""
    path = str(tmp_path / "legacy.db")
    db = DownloadsDatabase(path)
    with db._conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_dl_file_path_unique")
        for vid, up, br, emb, ts in (
                (None, "20260714", "", 1, 1000),      # rebuild row
                ("v1", "", "", 0, 2000)):             # scan backfill row
            conn.execute(
                "INSERT INTO downloads (video_id, title, channel_name,"
                " channel_url, platform, genre, file_path, upload_date,"
                " download_timestamp, bitrate, artwork_path,"
                " artwork_embedded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, "T", "C", "u", "YouTube", "DnB", r"C:\m\a.mp3", up,
                 ts, br, r"C:\art\a.jpg" if emb else None, emb))
    return DownloadsDatabase(path)      # re-open: migration re-runs


def test_duplicates_block_the_index_without_breaking_startup(tmp_path):
    db = _legacy_db_with_dupes(tmp_path)
    assert db.has_unique_path_index is False
    # And the app still works: inserts fall back to plain INSERT.
    _add(db, file_path=r"C:\m\b.mp3")
    assert len(_rows(db, r"C:\m\b.mp3")) == 1


def test_count_duplicate_downloads(tmp_path):
    db = _legacy_db_with_dupes(tmp_path)
    assert db.count_duplicate_downloads() == (1, 1)   # 1 group, 1 extra row
    assert _new_db(tmp_path, "clean.db").count_duplicate_downloads() == (0, 0)


# ── cleanup: the deliberate de-dup action ─────────────────────────────────

def test_dedupe_keeps_one_row_and_merges_the_richest_values(tmp_path):
    db = _legacy_db_with_dupes(tmp_path)
    result = db.dedupe_downloads_by_path()
    assert result["groups"] == 1
    assert result["removed"] == 1
    rows = _rows(db)
    assert len(rows) == 1
    # Nothing either row knew is lost: the id came from the scan row, the
    # upload date and cover art from the rebuild row.
    assert rows[0]["video_id"] == "v1"
    assert rows[0]["upload_date"] == "20260714"
    assert rows[0]["artwork_embedded"] == 1
    assert rows[0]["artwork_path"] == r"C:\art\a.jpg"


def test_dedupe_prefers_the_row_with_a_real_bitrate(tmp_path):
    db = _new_db(tmp_path)
    with db._conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_dl_file_path_unique")
        for vid, br in (("vA", "320 kbps"), ("vB", "")):
            conn.execute(
                "INSERT INTO downloads (video_id, title, platform, genre,"
                " file_path, upload_date, download_timestamp, bitrate)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (vid, "T", "YouTube", "DnB", r"C:\m\a.mp3", "20250101", 5, br))
    db = DownloadsDatabase(db.db_path)
    db.dedupe_downloads_by_path()
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["video_id"] == "vA"          # the real download survived


def test_dedupe_enables_the_index_so_it_cannot_happen_again(tmp_path):
    db = _legacy_db_with_dupes(tmp_path)
    result = db.dedupe_downloads_by_path()
    assert result["indexed"] is True
    assert db.has_unique_path_index is True
    _backfill(db, video_id="v9")                 # a later scan
    assert len(_rows(db)) == 1


def test_dedupe_is_a_no_op_on_a_clean_database(tmp_path):
    db = _new_db(tmp_path)
    _add(db)
    result = db.dedupe_downloads_by_path()
    assert result == {"groups": 0, "removed": 0, "indexed": True}
    assert len(_rows(db)) == 1


def test_dedupe_leaves_pathless_rows_alone(tmp_path):
    db = _new_db(tmp_path)
    _add(db, video_id="v1", file_path="")
    _add(db, video_id="v2", file_path="")
    assert db.dedupe_downloads_by_path()["removed"] == 0
    assert len(_rows(db)) == 2


def test_dedupe_survivor_keeps_artwork_lookup_working(tmp_path):
    # get_artwork_by_path() keys by file_path; a duplicate group must collapse
    # to a row that still carries the art, or a later rebuild orphans it.
    db = _legacy_db_with_dupes(tmp_path)
    db.dedupe_downloads_by_path()
    snap = db.get_artwork_by_path()
    assert snap[r"C:\m\a.mp3"][0] == r"C:\art\a.jpg"
    assert snap[r"C:\m\a.mp3"][3] == "v1"


# ── path rewrites still work once file_path is unique ────────────────────

def test_channel_move_onto_an_already_recorded_path(tmp_path):
    # A genre move can land a track on a path the database already has a row
    # for. Uniqueness must not turn that into a silently abandoned move: the
    # moved row wins, the stale row at the destination goes.
    db = _new_db(tmp_path)
    _add(db, video_id="v1", genre="Old", bitrate="320 kbps",
         file_path=r"C:\m\Old\Chan\a.mp3")
    _add(db, video_id="v2", genre="New", file_path=r"C:\m\New\Chan\a.mp3")
    _add(db, video_id="v3", genre="Old", file_path=r"C:\m\Old\Chan\b.mp3")

    moved = db.move_channel_downloads(
        wl_id=None, old_dir=r"C:\m\Old\Chan", new_dir=r"C:\m\New\Chan",
        new_genre="New")

    assert moved == 2
    rows = _rows(db)
    assert [r["file_path"] for r in rows] == [r"C:\m\New\Chan\a.mp3",
                                              r"C:\m\New\Chan\b.mp3"]
    assert rows[0]["video_id"] == "v1"        # the moved row, not the stale one
    assert rows[0]["bitrate"] == "320 kbps"
    assert all(r["genre"] == "New" for r in rows)


def test_update_download_path_onto_an_existing_path(tmp_path):
    # The artwork backfill repoints a row when a remux changes the container.
    # If a row for the remuxed file already exists, the repointed row wins.
    db = _new_db(tmp_path)
    _add(db, video_id="v1", bitrate="320 kbps", file_path=r"C:\m\a.webm")
    _add(db, video_id="v2", file_path=r"C:\m\a.opus")
    assert db.update_download_path(r"C:\m\a.webm", r"C:\m\a.opus") == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["video_id"] == "v1"


def test_one_batch_holding_the_same_path_twice(tmp_path):
    # Several videos sharing a normalised title map to one file on disk, so a
    # single scan can backfill the same path more than once.
    db = _new_db(tmp_path)
    assert db.backfill_downloads([
        dict(video_id="vA", title="Mix 2025", channel_name="C",
             channel_url="u", platform="YouTube", genre="DnB",
             file_path=r"C:\m\mix.mp3", upload_date="", ts=1, bitrate=""),
        dict(video_id="vB", title="Mix 2025", channel_name="C",
             channel_url="u", platform="YouTube", genre="DnB",
             file_path=r"C:\m\mix.mp3", upload_date="", ts=1, bitrate=""),
    ]) == 2
    assert len(_rows(db)) == 1


def test_dedupe_reports_failure_rather_than_raising(tmp_path):
    db = _legacy_db_with_dupes(tmp_path)
    db.db_path = str(tmp_path / "nope" / "missing.db")   # unopenable
    result = db.dedupe_downloads_by_path()
    assert result == {"groups": 0, "removed": 0, "indexed": False}
    assert db.has_unique_path_index is False   # no false claim of protection

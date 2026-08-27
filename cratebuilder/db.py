"""SQLite persistence: downloads history + watchlist."""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

# How long a geo-blocked track stays suppressed before the app gives it
# another chance (the user's location or VPN may have changed since).
GEO_RECHECK_SECONDS = 7 * 24 * 3600


def is_suppressed(reason, attempts, last_failed, now):
    """True if a remembered permanent failure should hide a track from 'new'.

    DRM is deterministic proof on the first failure. Removed and Geo-blocked
    need two, so a transient outage or 404 blip can't bury a live track.
    Geo-blocked additionally expires after GEO_RECHECK_SECONDS. An unrecognised
    reason is never suppressed, so a future reason string added to
    classify_permanent_failure degrades to today's behaviour rather than
    hiding tracks silently."""
    if reason == "DRM-protected":
        return attempts >= 1
    if reason == "Removed":
        return attempts >= 2
    if reason == "Geo-blocked":
        return attempts >= 2 and (now - last_failed) < GEO_RECHECK_SECONDS
    return False


# The partial UNIQUE index that makes one file on disk mean one downloads row,
# and the matching ON CONFLICT target. Partial because file_path may be empty
# (a download whose real path was never resolved): those rows key on nothing,
# so they are left out of the index and can never collide with each other.
_PATH_INDEX_NAME  = "idx_dl_file_path_unique"
_PATH_INDEX_WHERE = "WHERE file_path IS NOT NULL AND file_path != ''"
_PATH_CONFLICT    = f"ON CONFLICT(file_path) {_PATH_INDEX_WHERE}"

# Columns carried over when a duplicate group is collapsed: every column that
# holds knowledge about the track, so the surviving row can be filled from the
# rows removed around it. id and file_path are excluded — one identifies the
# survivor, the other is what the group is keyed on.
_MERGE_COLUMNS = ("video_id", "title", "channel_name", "channel_url",
                  "channel_id", "platform", "genre", "upload_date",
                  "bitrate", "artwork_path", "artwork_embedded",
                  "thumbnail_url", "download_timestamp")


def _row_richness(row):
    """Score a duplicate row so the most informative one survives a de-dup.

    A real download is the richest thing a row can be: only add_download
    records a bitrate. After that, evidence that anything was ever resolved
    for this track — a real upload date, embedded cover art, a sidecar, a
    video id — each counts once."""
    return (
        1 if (row["bitrate"] or "").strip() else 0,
        1 if (row["upload_date"] or "").strip() else 0,
        1 if row["artwork_embedded"] else 0,
        1 if row["artwork_path"] else 0,
        1 if (row["video_id"] or "").strip() else 0,
        row["download_timestamp"] or 0,
        -(row["id"] or 0),
    )


class DownloadsDatabase:
    SCHEMA_VERSION = 7

    # True when the v6 migration could not drop watchlist.scan_cutoff_date and
    # the legacy NOT NULL column is still there. Nothing reads it, but an
    # INSERT that omits it would violate the constraint, so add_watchlist_
    # channel has to keep feeding it a placeholder. See _init_schema.
    _legacy_cutoff_column = False

    # True once downloads.file_path carries the partial UNIQUE index, which is
    # what lets the write paths upsert instead of insert. False on a database
    # that still holds duplicate paths — the index cannot be built over them,
    # so those databases keep today's plain-INSERT behaviour until the user
    # runs the de-dup. See _try_unique_path_index.
    _path_unique_index = False

    # ── Web viewer: paged/grouped read helpers ──────────────────────────────
    # Additive, read-only surface behind the web Database viewer. These mirror
    # DatabaseViewerWindow's GROUP_PRESETS / _ART_FILTERS in the monolith (a
    # deliberate copy, not an import — the monolith depends on cratebuilder,
    # never the other way around) so a preset or filter label means the same
    # thing in both UIs.
    GROUP_PRESETS = {
        "Platform › Genre › Channel": ["platform", "genre", "channel_name"],
        "Genre › Channel":            ["genre", "channel_name"],
        "Channel":                    ["channel_name"],
        "Platform › Channel":         ["platform", "channel_name"],
    }

    ARTWORK_FILTERS = (
        "All tracks",
        "Has artwork",
        "Missing artwork",
        "Embedded only",
        "Sidecar missing on disk",
    )

    # ORDER BY can't be parameterized in SQLite, so order_by is checked
    # against this set before it ever reaches a query string. "bitrate" sorts
    # by a registered SQL function (_bitrate_sort_key), not the raw column —
    # see query_downloads.
    _DL_SORT_COLUMNS = {
        "id", "title", "channel_name", "genre", "platform",
        "upload_date", "download_timestamp", "bitrate",
    }

    # Bucket expressions shared by filtering and grouping, so a filter value
    # of "(unknown)"/"(none)" means the same blank-value bucket whichever
    # method applies it. Also the group_key whitelist: a key not in here
    # can't select a column in a WHERE/GROUP BY clause.
    _DL_BUCKET_SQL = {
        "platform": "COALESCE(NULLIF(TRIM(platform), ''), '(unknown)')",
        "genre": "CASE WHEN TRIM(COALESCE(genre, '')) = '' "
                 "OR TRIM(genre) = '(none)' THEN '(none)' ELSE TRIM(genre) END",
        "channel_name":
            "COALESCE(NULLIF(TRIM(channel_name), ''), '(unknown)')",
    }

    # "Has artwork" per the monolith's _art_state is embedded OR a recorded
    # sidecar path — on-disk truth doesn't change this (a broken sidecar path
    # still counts as "has artwork", just not as the working kind).
    _ART_HAS_ARTWORK_SQL = (
        "(artwork_embedded = 1 OR "
        "(artwork_path IS NOT NULL AND TRIM(artwork_path) != ''))"
    )

    # order_by whitelist for query_artwork_rows, same reasoning as
    # _DL_SORT_COLUMNS: raw columns only, checked before reaching a query
    # string. artwork_embedded is a plain 0/1 int column, so it sorts
    # correctly without a bitrate-style custom key function.
    _ART_SORT_COLUMNS = {
        "title", "channel_name", "platform", "artwork_embedded",
        "artwork_path", "thumbnail_url",
    }

    # The monolith's UNRESOLVED_URL_PREFIX (DJ-CrateBuilder_v1.3.py) — a
    # duplicate literal, not an import, since the monolith depends on
    # cratebuilder and not the other way around. query_watchlist_rows blanks
    # a url carrying this sentinel, matching _wl_display_url.
    _UNRESOLVED_URL_PREFIX = "unresolved://"

    def __init__(self, db_path, debug_logger=None):
        self.db_path = db_path
        self._lock   = threading.Lock()
        self._dbg    = debug_logger
        self._init_schema()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except Exception:
                pass
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def _log(self, level, msg):
        if self._dbg:
            try:
                getattr(self._dbg, level)(f"DB | {msg}")
            except Exception:
                pass

    def _init_schema(self):
        try:
            with self._conn() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS schema_info (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS downloads (
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
                    CREATE INDEX IF NOT EXISTS idx_dl_video_id
                        ON downloads(video_id);
                    CREATE INDEX IF NOT EXISTS idx_dl_channel_url
                        ON downloads(channel_url);
                    CREATE INDEX IF NOT EXISTS idx_dl_channel_name
                        ON downloads(channel_name);
                    CREATE INDEX IF NOT EXISTS idx_dl_platform
                        ON downloads(platform);
                    CREATE TABLE IF NOT EXISTS watchlist (
                        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                        url                      TEXT NOT NULL UNIQUE,
                        channel_id               TEXT,
                        display_name             TEXT NOT NULL,
                        platform                 TEXT NOT NULL,
                        genre                    TEXT,
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
                    CREATE TABLE IF NOT EXISTS unavailable_tracks (
                        platform     TEXT NOT NULL,
                        video_id     TEXT NOT NULL,
                        channel_url  TEXT,
                        title        TEXT,
                        reason       TEXT NOT NULL,
                        attempts     INTEGER NOT NULL DEFAULT 1,
                        first_failed INTEGER NOT NULL,
                        last_failed  INTEGER NOT NULL,
                        PRIMARY KEY (platform, video_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_unavail_channel_url
                        ON unavailable_tracks(channel_url);
                """)
                # ── Migrations for pre-existing databases ──────────────────
                # Older DBs (schema v1) lack the channel_id columns. Add them
                # idempotently — "duplicate column" errors are expected and
                # harmless on already-migrated DBs.
                for table in ("downloads", "watchlist"):
                    try:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN channel_id TEXT")
                        self._log("info",
                                  f"migration: added channel_id to {table}")
                    except sqlite3.OperationalError:
                        pass  # column already exists

                # schema v3: per-channel "last download started" timestamp.
                try:
                    conn.execute(
                        "ALTER TABLE watchlist "
                        "ADD COLUMN last_download_started INTEGER")
                    self._log("info",
                              "migration: added last_download_started to watchlist")
                except sqlite3.OperationalError:
                    pass  # column already exists

                # schema v4: cover art — sidecar JPEG path, whether the APIC
                # frame actually landed on the file, and the source thumbnail
                # URL (kept because SoundCloud art URLs can't be rebuilt from
                # the track id, so a later backfill has no other way home).
                for col, decl in (("artwork_path", "TEXT"),
                                  ("artwork_embedded", "INTEGER DEFAULT 0"),
                                  ("thumbnail_url", "TEXT")):
                    try:
                        conn.execute(
                            f"ALTER TABLE downloads ADD COLUMN {col} {decl}")
                        self._log("info",
                                  f"migration: added {col} to downloads")
                    except sqlite3.OperationalError:
                        pass  # column already exists

                # schema v6: drop watchlist.scan_cutoff_date. Nothing ever
                # read it to make a decision — a scan enumerates the channel
                # and decides "new" by DB membership plus on-disk presence, so
                # the date only travelled from the UI back to the UI. Guarded
                # by a column check because DROP COLUMN raises on a table that
                # has already been migrated.
                try:
                    cols = {r[1] for r in conn.execute(
                        "PRAGMA table_info(watchlist)")}
                    if "scan_cutoff_date" in cols:
                        conn.execute("ALTER TABLE watchlist "
                                     "DROP COLUMN scan_cutoff_date")
                        self._log("info", "migration: dropped "
                                          "scan_cutoff_date from watchlist")
                except Exception as e:
                    # SQLite older than 3.35 has no DROP COLUMN, and an index
                    # pinning the column blocks it too. Letting this escape
                    # would take schema init down and stop the app starting at
                    # all, which is far worse than a dead column — so note it
                    # and carry on. The column is NOT NULL with no default, so
                    # inserts have to keep filling it; see _legacy_cutoff_column.
                    self._log("warning",
                              f"migration: could not drop scan_cutoff_date: {e}")
                try:
                    self._legacy_cutoff_column = "scan_cutoff_date" in {
                        r[1] for r in conn.execute(
                            "PRAGMA table_info(watchlist)")}
                except Exception:
                    self._legacy_cutoff_column = False

                conn.execute(
                    "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
                    ("version", str(self.SCHEMA_VERSION))
                )
            # schema v7: one file on disk = one downloads row. Runs outside the
            # block above because it is allowed to fail on an existing library
            # (see _try_unique_path_index) and must not take schema init with
            # it — a failed index leaves the app exactly as it behaved at v6.
            self._try_unique_path_index()
            self._log("info", f"schema initialized at {self.db_path}")
        except Exception as e:
            self._log("error", f"schema init failed: {e}")
            raise

    @property
    def has_unique_path_index(self):
        """True when downloads.file_path is uniquely indexed, i.e. this
        database can no longer grow a second row for a file it already knows.
        False means duplicates are still present and blocking the index."""
        return bool(self._path_unique_index)

    def _try_unique_path_index(self):
        """Build the partial UNIQUE index on downloads.file_path if the data
        allows it, and record the outcome in _path_unique_index.

        A library that already contains duplicate paths cannot take the index,
        and rebuilding it would mean deleting the user's rows behind their back
        during a routine app start — so the failure is expected, logged, and
        survivable: the write paths fall back to plain INSERT, exactly as they
        behaved before v7. The user removes the duplicates deliberately via
        dedupe_downloads_by_path, which retries this. Never raises."""
        try:
            with self._conn() as conn:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {_PATH_INDEX_NAME} "
                    f"ON downloads(file_path) {_PATH_INDEX_WHERE}")
            self._path_unique_index = True
        except sqlite3.IntegrityError:
            self._path_unique_index = False
            groups, extra = self.count_duplicate_downloads()
            self._log("warning",
                      f"migration: downloads.file_path is not unique — "
                      f"{extra} duplicate row(s) across {groups} file(s). "
                      f"Use 'Remove Duplicate Rows' to clean them up; "
                      f"until then duplicate protection is off.")
        except Exception as e:
            self._path_unique_index = False
            self._log("warning",
                      f"migration: could not create {_PATH_INDEX_NAME}: {e}")
        return self._path_unique_index

    def add_download(self, *, video_id, title, channel_name, channel_url,
                     platform, genre, file_path, upload_date, bitrate,
                     channel_id=None, artwork_path=None, artwork_embedded=0,
                     thumbnail_url=None):
        """Record a completed download.

        Keyed on file_path: re-downloading a track (a Force Download, or the
        same recurring upload title overwriting the file) updates the row for
        that file instead of adding another one. The fresh download describes
        what is on disk now, so its values win — except where it has nothing
        to say, which never blanks out what the row already knew."""
        upsert = f"""
            {_PATH_CONFLICT} DO UPDATE SET
                video_id     = COALESCE(excluded.video_id, video_id),
                title        = COALESCE(NULLIF(excluded.title, ''), title),
                channel_name = COALESCE(NULLIF(excluded.channel_name, ''),
                                        channel_name),
                channel_url  = COALESCE(NULLIF(excluded.channel_url, ''),
                                        channel_url),
                channel_id   = COALESCE(excluded.channel_id, channel_id),
                platform     = COALESCE(NULLIF(excluded.platform, ''),
                                        platform),
                genre        = COALESCE(NULLIF(excluded.genre, ''), genre),
                upload_date  = COALESCE(NULLIF(excluded.upload_date, ''),
                                        upload_date),
                download_timestamp = excluded.download_timestamp,
                bitrate      = COALESCE(NULLIF(excluded.bitrate, ''), bitrate),
                artwork_path = COALESCE(excluded.artwork_path, artwork_path),
                artwork_embedded = excluded.artwork_embedded,
                thumbnail_url = COALESCE(excluded.thumbnail_url, thumbnail_url)
        """ if self._path_unique_index else ""
        try:
            with self._conn() as conn:
                conn.execute(f"""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate,
                       artwork_path, artwork_embedded, thumbnail_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    {upsert}
                """, (video_id or None, title or "", channel_name or "",
                      channel_url or "", channel_id or None, platform,
                      genre or "", file_path or "", upload_date or "",
                      int(time.time()), bitrate or "",
                      artwork_path or None, int(bool(artwork_embedded)),
                      thumbnail_url or None))
        except Exception as e:
            self._log("error", f"add_download failed for {title!r}: {e}")

    def backfill_downloads(self, rows):
        """Bulk-insert tracks discovered already-on-disk during a scan, so
        future dedup is exact and instant. `rows` is a list of dicts with the
        download columns plus a 'ts' timestamp. Returns the count inserted.

        Rows are normalised before binding: callers predating the cover-art
        columns omit them entirely, and sqlite3's named-style binding raises on
        a missing key rather than substituting NULL.

        Keyed on file_path, so a track this database already knows is repaired
        rather than duplicated. That is the whole point on the scan path: a
        Watch List scan backfills tracks it found on disk whose video_id it
        could not match, and before v7 every one of those inserted a second row
        for a file already recorded — one rebuild (which loses unrecoverable
        video_ids) followed by one scan doubled the table. What a scan or a
        rebuild genuinely knows is where a track lives, so the channel, genre
        and title it supplies win; what a track *is* — its bitrate, upload
        date, cover art, video id, and the moment it was downloaded — is only
        filled in where the existing row has nothing, never overwritten."""
        if not rows:
            return 0
        try:
            bound = [{
                "video_id":         r.get("video_id"),
                "title":            r.get("title") or "",
                "channel_name":     r.get("channel_name") or "",
                "channel_url":      r.get("channel_url") or "",
                "channel_id":       r.get("channel_id"),
                "platform":         r.get("platform"),
                "genre":            r.get("genre") or "",
                "file_path":        r.get("file_path") or "",
                "upload_date":      r.get("upload_date") or "",
                "ts":               r.get("ts") or 0,
                "bitrate":          r.get("bitrate") or "",
                "artwork_path":     r.get("artwork_path"),
                "artwork_embedded": int(bool(r.get("artwork_embedded"))),
                "thumbnail_url":    r.get("thumbnail_url"),
            } for r in rows]
            upsert = f"""
                {_PATH_CONFLICT} DO UPDATE SET
                    title        = COALESCE(NULLIF(excluded.title, ''), title),
                    channel_name = COALESCE(NULLIF(excluded.channel_name, ''),
                                            channel_name),
                    channel_url  = COALESCE(NULLIF(excluded.channel_url, ''),
                                            channel_url),
                    channel_id   = COALESCE(excluded.channel_id, channel_id),
                    platform     = COALESCE(NULLIF(excluded.platform, ''),
                                            platform),
                    genre        = COALESCE(NULLIF(excluded.genre, ''), genre),
                    video_id     = COALESCE(video_id, excluded.video_id),
                    upload_date  = COALESCE(NULLIF(upload_date, ''),
                                            excluded.upload_date),
                    bitrate      = COALESCE(NULLIF(bitrate, ''),
                                            excluded.bitrate),
                    artwork_path = COALESCE(artwork_path,
                                            excluded.artwork_path),
                    artwork_embedded = MAX(COALESCE(artwork_embedded, 0),
                                           COALESCE(excluded.artwork_embedded,
                                                    0)),
                    thumbnail_url = COALESCE(thumbnail_url,
                                             excluded.thumbnail_url),
                    download_timestamp = CASE
                        WHEN download_timestamp IS NULL
                             OR download_timestamp <= 0
                        THEN excluded.download_timestamp
                        ELSE download_timestamp END
            """ if self._path_unique_index else ""
            with self._conn() as conn:
                conn.executemany(f"""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate,
                       artwork_path, artwork_embedded, thumbnail_url)
                    VALUES (:video_id, :title, :channel_name, :channel_url,
                            :channel_id, :platform, :genre, :file_path,
                            :upload_date, :ts, :bitrate,
                            :artwork_path, :artwork_embedded, :thumbnail_url)
                    {upsert}
                """, bound)
            return len(bound)
        except Exception as e:
            self._log("error", f"backfill_downloads failed: {e}")
            return 0

    def update_download_path(self, old_path, new_path):
        """Repoint the download row(s) at *old_path* to *new_path*.
        Returns the number of rows updated, 0 on failure or no match. Used by
        the artwork backfill when embedding art forces a container change
        (a WebM remuxed to Opus so mutagen can write the cover), so the row
        does not keep pointing at the file that no longer exists.

        OR REPLACE because *new_path* may already have a row of its own — one
        file can only be one row (see the v7 index), and the row being moved is
        the one that describes the file that is actually there now."""
        if not old_path or not new_path:
            return 0
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE OR REPLACE downloads SET file_path = ? "
                    "WHERE file_path = ?",
                    (new_path, old_path))
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"update_download_path failed for "
                               f"{old_path!r}: {e}")
            return 0

    def set_download_video_id(self, file_path, video_id):
        """Record a recovered video id against the download row(s) for
        *file_path*. Returns the number of rows updated, 0 on failure or no
        match. Used by the artwork backfill when it re-derives an id (lost to
        an earlier rebuild of a tagless file) from the channel's upload
        listing, so later runs and rebuilds keep the match."""
        if not file_path or not video_id:
            return 0
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE downloads SET video_id = ? WHERE file_path = ?",
                    (video_id, file_path))
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"set_download_video_id failed for "
                               f"{file_path!r}: {e}")
            return 0

    def set_download_artwork(self, file_path, artwork_path, artwork_embedded,
                             thumbnail_url=None):
        """Record cover art against the download row(s) for *file_path*.
        Returns the number of rows updated, 0 on failure or no match. Used by
        the artwork backfill, which finds art for tracks downloaded before the
        cover-art feature existed."""
        if not file_path:
            return 0
        try:
            with self._conn() as conn:
                cur = conn.execute("""
                    UPDATE downloads
                    SET artwork_path = ?, artwork_embedded = ?,
                        thumbnail_url = ?
                    WHERE file_path = ?
                """, (artwork_path or None, int(bool(artwork_embedded)),
                      thumbnail_url or None, file_path))
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"set_download_artwork failed for "
                               f"{file_path!r}: {e}")
            return 0

    def get_downloads_missing_artwork(self):
        """Return every downloads row that still has no embedded cover art, as
        a list of dicts, oldest first.

        This is the worklist for the artwork backfill. "Missing" means
        artwork_embedded is 0 or NULL — the art either was never fetched or was
        fetched but never made it onto the file, and in both cases the track
        still needs work. Rows with no file_path are skipped: there is nothing
        on disk to tag, so they can never be backfilled.

        Ordering is download_timestamp ASC (id ASC as a tiebreak for rows that
        share a timestamp) so a long backfill walks the user's history from the
        beginning and makes visible, sensible progress rather than jumping
        around. Returns [] on failure."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT * FROM downloads
                    WHERE (artwork_embedded IS NULL OR artwork_embedded = 0)
                      AND file_path IS NOT NULL AND file_path != ''
                    ORDER BY download_timestamp ASC, id ASC
                """).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"get_downloads_missing_artwork failed: {e}")
            return []

    def count_downloads_missing_artwork(self):
        """Count the rows get_downloads_missing_artwork would return, without
        materialising any of them. Lets the UI show "N tracks need artwork"
        cheaply on a large history. Returns 0 on failure."""
        try:
            with self._conn() as conn:
                row = conn.execute("""
                    SELECT COUNT(*) AS n FROM downloads
                    WHERE (artwork_embedded IS NULL OR artwork_embedded = 0)
                      AND file_path IS NOT NULL AND file_path != ''
                """).fetchone()
                return int(row["n"]) if row else 0
        except Exception as e:
            self._log("error", f"count_downloads_missing_artwork failed: {e}")
            return 0

    def get_artwork_by_path(self):
        """Snapshot every row's cover-art bookkeeping, keyed by file_path:
        {file_path: (artwork_path, artwork_embedded, thumbnail_url, video_id)}.

        Only rows carrying some artwork data are included (an artwork_path, an
        embedded flag, or a thumbnail_url); rows with no file_path are skipped
        because the key would be meaningless.

        video_id is the 4th element, appended after the original 3-tuple so
        existing positional access to (artwork_path, artwork_embedded,
        thumbnail_url) is unaffected. It lets a rebuild recover a SoundCloud
        track's sidecar (keyed on that id, not the filename stem) even though
        SoundCloud URLs carry no id `recover_video_id` can read back off the
        file itself.

        WHY THIS EXISTS: "Rebuild Database from Files" wipes the downloads table
        with clear_all_downloads() and re-derives it from disk via
        backfill_downloads(). The rebuilt rows are reconstructed from filenames,
        which carry no artwork bookkeeping — so a naive rebuild silently orphans
        the cover art of every track the user ever downloaded. The rebuild takes
        this snapshot *before* the clear and re-attaches the values to the
        rebuilt rows by file_path, so the art survives. Returns {} on failure."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT file_path, artwork_path, artwork_embedded,
                           thumbnail_url, video_id
                    FROM downloads
                    WHERE file_path IS NOT NULL AND file_path != ''
                      AND (artwork_path IS NOT NULL
                           OR artwork_embedded = 1
                           OR thumbnail_url IS NOT NULL)
                """).fetchall()
                return {r["file_path"]: (r["artwork_path"],
                                         r["artwork_embedded"],
                                         r["thumbnail_url"],
                                         r["video_id"])
                        for r in rows}
        except Exception as e:
            self._log("error", f"get_artwork_by_path failed: {e}")
            return {}

    def get_track_facts_by_path(self):
        """Snapshot {file_path: (title, video_id, platform)} for every row
        that names a file.

        The bulk read behind the tag repair. The database is the only place a
        track's real title survives when the file itself was never tagged —
        the file name is the title after yt-dlp's sanitiser has been at it, so
        it is a fallback rather than an equal. Read once per sweep: asking per
        file would open a connection per track through the same lock the UI
        thread takes to redraw.

        Rows with no file_path are skipped, since the key would be
        meaningless. Returns {} on failure, which costs the sweep only its
        preferred title source, not the sweep."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT file_path, title, video_id, platform
                    FROM downloads
                    WHERE file_path IS NOT NULL AND file_path != ''
                """).fetchall()
            return {r["file_path"]: (r["title"] or "", r["video_id"] or "",
                                     r["platform"] or "")
                    for r in rows}
        except Exception as e:
            self._log("error", f"get_track_facts_by_path failed: {e}")
            return {}

    def backfill_missing_download_timestamps(self, updates):
        """Persist download timestamps for rows that never had one (e.g. tracks
        imported before the database existed). `updates` is a list of
        (timestamp, row_id) tuples. Returns the count written."""
        if not updates:
            return 0
        try:
            with self._conn() as conn:
                conn.executemany(
                    "UPDATE downloads SET download_timestamp = ? "
                    "WHERE id = ? AND "
                    "(download_timestamp IS NULL OR download_timestamp <= 0)",
                    updates)
            return len(updates)
        except Exception as e:
            self._log("error",
                      f"backfill_missing_download_timestamps failed: {e}")
            return 0

    def is_video_downloaded(self, video_id):
        if not video_id:
            return False
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM downloads WHERE video_id = ? LIMIT 1",
                    (video_id,)).fetchone()
                return row is not None
        except Exception as e:
            self._log("error", f"is_video_downloaded failed: {e}")
            return False

    def get_downloaded_video_ids(self):
        """Return the set of every video_id the downloads table holds.

        The bulk form of is_video_downloaded: fetched once per scan and handed
        to crate.ChannelCrate as its is_downloaded oracle (set.__contains__),
        so classifying a channel costs one query rather than one per entry —
        a connection each, all through the same lock the UI thread needs.

        Not platform-scoped, exactly like the per-id check it stands in for.
        Returns an empty set on failure, where every track then reads as new
        — the same way is_video_downloaded fails."""
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT video_id FROM downloads").fetchall()
            return {r["video_id"] for r in rows if r["video_id"]}
        except Exception as e:
            self._log("error", f"get_downloaded_video_ids failed: {e}")
            return set()

    def record_unavailable(self, *, platform, video_id, channel_url, title,
                           reason, now=None):
        """Remember that a track failed for a permanent reason.

        Upsert keyed on (platform, video_id): a first failure inserts with
        attempts=1, a repeat bumps attempts and refreshes last_failed and
        reason. Returns True when a row was written. Tracks with no id or no
        reason cannot be keyed or judged, so they are ignored (False)."""
        if not video_id or not reason:
            return False
        ts = int(now if now is not None else time.time())
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO unavailable_tracks
                      (platform, video_id, channel_url, title, reason,
                       attempts, first_failed, last_failed)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(platform, video_id) DO UPDATE SET
                        attempts    = attempts + 1,
                        last_failed = excluded.last_failed,
                        reason      = excluded.reason,
                        title       = COALESCE(NULLIF(excluded.title, ''),
                                               title),
                        channel_url = COALESCE(NULLIF(excluded.channel_url, ''),
                                               channel_url)
                """, (platform or "", video_id, channel_url or "",
                      title or "", reason, ts, ts))
            return True
        except Exception as e:
            self._log("error", f"record_unavailable failed for "
                               f"{video_id!r}: {e}")
            return False

    def get_suppressed_reasons(self, platform, now=None):
        """Return {video_id: reason} for every track on *platform* currently
        hidden by the unavailable-track memory.

        Fetched once per scan and handed to crate.ChannelCrate as its
        suppressed_reason oracle (dict.get), so a scan costs one query rather
        than one per entry. Returns {} on failure."""
        ts = int(now if now is not None else time.time())
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT video_id, reason, attempts, last_failed
                    FROM unavailable_tracks WHERE platform = ?
                """, (platform or "",)).fetchall()
            return {r["video_id"]: r["reason"] for r in rows
                    if is_suppressed(r["reason"], r["attempts"],
                                     r["last_failed"], ts)}
        except Exception as e:
            self._log("error", f"get_suppressed_reasons failed: {e}")
            return {}

    def count_unavailable_for_channel(self, channel_url):
        """How many permanent failures are remembered for *channel_url*.

        Counts every remembered row, not just the currently-suppressed ones,
        because this is the number behind the Watch List "Forget unavailable
        tracks" button and forgetting clears the lot. Returns 0 on failure or
        an empty url."""
        if not channel_url:
            return 0
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM unavailable_tracks "
                    "WHERE channel_url = ?", (channel_url,)).fetchone()
                return int(row["n"]) if row else 0
        except Exception as e:
            self._log("error", f"count_unavailable_for_channel failed: {e}")
            return 0

    def forget_unavailable_for_channel(self, channel_url):
        """Drop the unavailable-track memory for one channel and return the
        number of rows removed. Scoped strictly to *channel_url* — no files
        and no download history are touched. Returns 0 on failure or an empty
        url."""
        if not channel_url:
            return 0
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM unavailable_tracks WHERE channel_url = ?",
                    (channel_url,))
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"forget_unavailable_for_channel failed: {e}")
            return 0

    def get_most_recent_upload_date(self, channel_url):
        try:
            with self._conn() as conn:
                row = conn.execute("""
                    SELECT MAX(upload_date) AS max_date FROM downloads
                    WHERE channel_url = ? AND upload_date IS NOT NULL
                      AND upload_date != ''
                """, (channel_url,)).fetchone()
                return row["max_date"] if row else None
        except Exception as e:
            self._log("error", f"get_most_recent_upload_date failed: {e}")
            return None

    def get_channel_download_count(self, channel_url):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM downloads WHERE channel_url = ?",
                    (channel_url,)).fetchone()
                return row["n"] if row else 0
        except Exception as e:
            self._log("error", f"get_channel_download_count failed: {e}")
            return 0

    def get_all_downloads(self):
        """Return every downloads row as a list of dicts, newest first.
        Used by the Database window to present the full download history."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM downloads ORDER BY download_timestamp DESC"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"get_all_downloads failed: {e}")
            return []

    def get_download_count(self):
        try:
            with self._conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) AS n FROM downloads").fetchone()["n"]
        except Exception as e:
            self._log("error", f"get_download_count failed: {e}")
            return 0

    def clear_all_downloads(self):
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM downloads")
        except Exception as e:
            self._log("error", f"clear_all_downloads failed: {e}")

    def count_duplicate_downloads(self):
        """(files, redundant_rows) — how many files hold more than one row, and
        how many rows a de-dup would remove. (0, 0) on failure or when clean.

        This is the number behind the "Remove Duplicate Rows" button, so the
        user sees the damage before agreeing to anything."""
        try:
            with self._conn() as conn:
                row = conn.execute(f"""
                    SELECT COUNT(*) AS files,
                           COALESCE(SUM(n), 0) - COUNT(*) AS extra
                    FROM (SELECT COUNT(*) AS n FROM downloads
                          {_PATH_INDEX_WHERE}
                          GROUP BY file_path HAVING n > 1)
                """).fetchone()
                return (int(row["files"] or 0), int(row["extra"] or 0))
        except Exception as e:
            self._log("error", f"count_duplicate_downloads failed: {e}")
            return (0, 0)

    def dedupe_downloads_by_path(self):
        """Collapse every group of rows sharing a file_path down to one row,
        then take the UNIQUE index that stops the duplicates coming back.

        DELIBERATE ONLY. This deletes rows, so nothing calls it on its own —
        it runs when the user asks for it and never during startup, a scan, a
        download or a rebuild.

        Nothing a group knew is thrown away. The richest row survives (see
        _row_richness) and any column it left empty is filled from the other
        rows in the group, richest first — so the id a scan recovered and the
        cover art a rebuild resolved end up on the same surviving row. Rows
        with no file_path are not touched: they key on nothing, so no two of
        them can be shown to be the same track.

        Returns {"groups": files collapsed, "removed": rows deleted,
        "indexed": whether file_path is now uniquely indexed}. Never raises."""
        try:
            with self._conn() as conn:
                dupes = conn.execute(f"""
                    SELECT * FROM downloads
                    {_PATH_INDEX_WHERE} AND file_path IN (
                        SELECT file_path FROM downloads
                        {_PATH_INDEX_WHERE}
                        GROUP BY file_path HAVING COUNT(*) > 1)
                    ORDER BY file_path
                """).fetchall()

                groups = {}
                for r in dupes:
                    groups.setdefault(r["file_path"], []).append(r)

                doomed, patches = [], []
                for rows in groups.values():
                    rows.sort(key=_row_richness, reverse=True)
                    keeper, rest = rows[0], rows[1:]
                    merged = {}
                    for col in _MERGE_COLUMNS:
                        if keeper[col] not in (None, "", 0):
                            continue
                        for other in rest:
                            if other[col] not in (None, "", 0):
                                merged[col] = other[col]
                                break
                    if merged:
                        sets = ", ".join(f"{c} = ?" for c in merged)
                        patches.append((f"UPDATE downloads SET {sets} "
                                        f"WHERE id = ?",
                                        [*merged.values(), keeper["id"]]))
                    doomed.extend(r["id"] for r in rest)

                for sql, vals in patches:
                    conn.execute(sql, vals)
                for i in range(0, len(doomed), 500):
                    chunk = doomed[i:i + 500]
                    conn.execute(
                        f"DELETE FROM downloads WHERE id IN "
                        f"({','.join('?' * len(chunk))})", chunk)

            removed = len(doomed)
            self._log("info", f"dedupe: collapsed {len(groups)} file(s), "
                              f"removed {removed} redundant row(s)")
        except Exception as e:
            self._log("error", f"dedupe_downloads_by_path failed: {e}")
            return {"groups": 0, "removed": 0,
                    "indexed": bool(self._path_unique_index)}
        return {"groups": len(groups), "removed": removed,
                "indexed": self._try_unique_path_index()}

    def delete_downloads_by_paths(self, paths):
        """Delete download rows whose file_path is in *paths*. Returns the
        number of rows removed. Best-effort: logs and returns 0 on error.
        Used by Folders Cleanup after a file is sent to the Recycle Bin.
        Safe for typical per-folder counts; SQLite caps bind variables at ~999,
        so a single call with 1000+ paths would error out and return 0."""
        paths = [p for p in (paths or []) if p]
        if not paths:
            return 0
        try:
            with self._conn() as conn:
                placeholders = ",".join("?" for _ in paths)
                cur = conn.execute(
                    f"DELETE FROM downloads WHERE file_path IN ({placeholders})",
                    paths)
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"delete_downloads_by_paths failed: {e}")
            return 0

    def add_watchlist_channel(self, *, url, display_name, platform, genre,
                               auto_added=False,
                               channel_id=None, status="idle"):
        cols = ["url", "channel_id", "display_name", "platform", "genre",
                "date_added", "auto_added", "total_downloaded", "status"]
        vals = [url, channel_id or None, display_name, platform, genre,
                int(time.time()), 1 if auto_added else 0, None, status]
        if self._legacy_cutoff_column:
            # The v6 drop didn't take on this database. The leftover column is
            # NOT NULL with no default, so omitting it would fail every insert.
            cols.append("scan_cutoff_date")
            vals.append("")
        try:
            vals[7] = self.get_channel_download_count(url)
            with self._conn() as conn:
                cur = conn.execute(
                    f"INSERT INTO watchlist ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})", vals)
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        except Exception as e:
            self._log("error", f"add_watchlist_channel failed: {e}")
            return None

    def update_watchlist_scan_result(self, channel_id, *, timestamp,
                                      pending_count, pending_entries, status,
                                      last_error=None):
        try:
            with self._conn() as conn:
                conn.execute("""
                    UPDATE watchlist
                    SET last_scanned_timestamp = ?, pending_new_count = ?,
                        pending_entries_json = ?, status = ?, last_error = ?
                    WHERE id = ?
                """, (timestamp, pending_count,
                      json.dumps(pending_entries or []), status,
                      last_error, channel_id))
        except Exception as e:
            self._log("error", f"update_watchlist_scan_result failed: {e}")

    def set_watchlist_download_started(self, channel_ids, timestamp):
        """Stamp the moment a (re)download started for one or more channels.
        `channel_ids` is an iterable of watchlist row ids."""
        ids = [int(c) for c in (channel_ids or [])]
        if not ids:
            return
        try:
            placeholders = ",".join("?" for _ in ids)
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE watchlist SET last_download_started = ? "
                    f"WHERE id IN ({placeholders})",
                    [timestamp, *ids])
        except Exception as e:
            self._log("error", f"set_watchlist_download_started failed: {e}")

    def update_watchlist_status(self, channel_id, status, last_error=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE watchlist SET status = ?, last_error = ? WHERE id = ?",
                    (status, last_error, channel_id))
        except Exception as e:
            self._log("error", f"update_watchlist_status failed: {e}")

    def reset_stale_watchlist_scans(self):
        """Reset every 'scanning' row to 'idle'; returns how many were reset.

        'scanning' is only meaningful while a live thread owns the row, and no
        thread survives a restart — a row still saying it after a crash or an
        update swap renders a ghost cancel button nothing can ever clear."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE watchlist SET status = 'idle' "
                    "WHERE status = 'scanning'")
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"reset_stale_watchlist_scans failed: {e}")
            return 0

    def update_watchlist_channel_fields(self, wl_id, **fields):
        """Update allowed watchlist columns for one row.

        Returns True on success, False on failure (including a UNIQUE(url)
        collision, which means the target url already belongs to another row).
        Never raises — callers branch on the bool instead of getting a silent
        no-op."""
        allowed = {"display_name", "genre",
                   "channel_id", "url", "status", "last_error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [wl_id]
            with self._conn() as conn:
                conn.execute(f"UPDATE watchlist SET {sets} WHERE id = ?", vals)
            return True
        except sqlite3.IntegrityError as e:
            self._log("info",
                      f"update_watchlist_channel_fields collision: {e}")
            return False
        except Exception as e:
            self._log("error", f"update_watchlist_channel_fields failed: {e}")
            return False

    def remove_watchlist_channel(self, channel_id):
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM watchlist WHERE id = ?", (channel_id,))
        except Exception as e:
            self._log("error", f"remove_watchlist_channel failed: {e}")

    def move_channel_downloads(self, *, wl_id, old_dir, new_dir, new_genre):
        """Rewrite downloads.file_path + artwork_path prefixes from old_dir to
        new_dir and set downloads.genre = new_genre, PLUS set watchlist.genre for
        wl_id — all in one transaction. Returns rows updated in downloads.

        Prefix match is anchored with a trailing separator so partial-name
        collisions can't leak. old_dir == new_dir is a valid genre-only patch
        (used by verify-on-open when the folder was already found in the right
        place but the DB rows still say the wrong genre). Never raises.

        OR REPLACE so one already-recorded destination path cannot abandon the
        whole move: the rows being moved describe the files that are now in
        new_dir, so where a stale row already sits on one of those paths, the
        moved row replaces it rather than colliding with the v7 unique index
        and rolling the entire transaction back."""
        if not old_dir or not new_dir:
            return 0
        sep = "\\" if "\\" in old_dir else "/"
        old_pref = old_dir.rstrip("\\/") + sep
        new_pref = new_dir.rstrip("\\/") + sep
        start_idx = len(old_pref) + 1  # SQLite SUBSTR is 1-indexed
        try:
            with self._conn() as conn:
                cur = conn.execute("""
                    UPDATE OR REPLACE downloads
                    SET file_path = ? || SUBSTR(file_path, ?),
                        artwork_path = CASE
                            WHEN artwork_path IS NOT NULL AND artwork_path LIKE ?
                            THEN ? || SUBSTR(artwork_path, ?)
                            ELSE artwork_path
                        END,
                        genre = ?
                    WHERE file_path LIKE ?
                """, (new_pref, start_idx,
                      old_pref + "%", new_pref, start_idx,
                      new_genre,
                      old_pref + "%"))
                rows = cur.rowcount or 0
                if wl_id is not None:
                    conn.execute(
                        "UPDATE watchlist SET genre = ? WHERE id = ?",
                        (new_genre, wl_id))
                return rows
        except Exception as e:
            self._log("error", f"move_channel_downloads failed: {e}")
            return 0

    def delete_blank_watchlist_channels(self):
        """Delete watchlist rows whose display_name is NULL, empty, or only
        whitespace — the broken "blank cards" left by older auto-add bugs.
        Returns the number of rows removed."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM watchlist "
                    "WHERE display_name IS NULL OR TRIM(display_name) = ''")
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"delete_blank_watchlist_channels failed: {e}")
            return 0

    def get_all_watchlist_channels(self):
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM watchlist ORDER BY date_added DESC"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"get_all_watchlist_channels failed: {e}")
            return []

    def get_watchlist_channel(self, channel_id):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM watchlist WHERE id = ?", (channel_id,)
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            self._log("error", f"get_watchlist_channel failed: {e}")
            return None

    def get_watchlist_channel_by_url(self, url):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM watchlist WHERE url = ?", (url,)
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            self._log("error", f"get_watchlist_channel_by_url failed: {e}")
            return None

    def get_watchlist_channel_by_channel_id(self, channel_id):
        """Return the watchlist row matching a YouTube UC channel_id, or None.
        NULL/empty channel_id rows (auto-added by URL only) are never matched,
        so a blank lookup can't collide with them."""
        cid = (channel_id or "").strip()
        if not cid:
            return None
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM watchlist "
                    "WHERE channel_id = ? AND channel_id IS NOT NULL "
                    "AND channel_id != ''",
                    (cid,)
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            self._log("error",
                      f"get_watchlist_channel_by_channel_id failed: {e}")
            return None

    def get_total_pending_count(self):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(pending_new_count), 0) AS total FROM watchlist"
                ).fetchone()
                return int(row["total"] or 0)
        except Exception as e:
            self._log("error", f"get_total_pending_count failed: {e}")
            return 0

    def clear_pending_for_channel(self, channel_id):
        try:
            with self._conn() as conn:
                conn.execute("""
                    UPDATE watchlist SET pending_new_count = 0,
                        pending_entries_json = '[]' WHERE id = ?
                """, (channel_id,))
        except Exception as e:
            self._log("error", f"clear_pending_for_channel failed: {e}")

    def refresh_watchlist_total(self, wl_id):
        """Recount one watchlist row's downloaded total from its own URL.

        The whole-table twin below is a maintenance sweep; this is what a
        finished download calls, so a card raised before its first track
        landed stops reporting the count it was inserted with. Returns the
        new total, or None when the row is gone."""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT url FROM watchlist WHERE id = ?", (wl_id,)
                ).fetchone()
                if row is None:
                    return None
                cnt = conn.execute(
                    "SELECT COUNT(*) AS n FROM downloads WHERE channel_url = ?",
                    (row["url"],)).fetchone()["n"]
                conn.execute(
                    "UPDATE watchlist SET total_downloaded = ? WHERE id = ?",
                    (cnt, wl_id))
                return cnt
        except Exception as e:
            self._log("error", f"refresh_watchlist_total failed: {e}")
            return None

    def refresh_watchlist_totals(self):
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT id, url FROM watchlist").fetchall()
                for r in rows:
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS n FROM downloads WHERE channel_url = ?",
                        (r["url"],)).fetchone()["n"]
                    conn.execute(
                        "UPDATE watchlist SET total_downloaded = ? WHERE id = ?",
                        (cnt, r["id"]))
        except Exception as e:
            self._log("error", f"refresh_watchlist_totals failed: {e}")

    # ── Web viewer: paged/grouped downloads ─────────────────────────────────
    @staticmethod
    def _escape_like(text):
        return (text.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_"))

    def _downloads_filter_sql(self, filters):
        """Build a ' WHERE ...' clause + bound params for *filters*, shared by
        count_downloads/query_downloads/group_downloads.

        filters (all optional): platform, genre, search (title/channel_name
        LIKE, literal % and _ escaped), group_key/group_value — group_key is
        one of _DL_BUCKET_SQL's keys ("platform"/"genre"/"channel_name") and
        group_value the bucketed string a group_downloads() row's "key"
        carries (e.g. "(unknown)"), so a drill-down round-trips exactly.
        Every value is bound as a parameter; group_key only ever selects a
        SQL fragment out of the hard-coded _DL_BUCKET_SQL map, so caller
        input never reaches the query string directly. An unrecognised
        group_key raises ValueError rather than being silently ignored.

        A group_key of "platform"/"genre" may legitimately coincide with
        that same dimension's own dedicated filter key — Task 7's UI can
        plausibly hold a platform dropdown and a drill-down state that both
        say "YouTube" at once, and that's an ordinary interaction, not an
        error. Only when the two values actually disagree would the two
        equality clauses on the same bucketed column silently AND to zero
        rows with no indication why; that combination raises instead.
        Agreement is exact string equality (no case-folding, no whitespace
        trimming) — the same comparison the generated SQL itself performs
        on these bound parameters (no COLLATE NOCASE is used on this
        equality, unlike the ORDER BY clauses elsewhere in this file, and
        the parameter side is never trimmed the way _DL_BUCKET_SQL trims
        the column side), so this check can't be fooled into calling two
        values "the same" when the query would in fact treat them as
        different."""
        filters = filters or {}
        clauses, params = [], []

        platform = filters.get("platform")
        if platform:
            clauses.append(f"{self._DL_BUCKET_SQL['platform']} = ?")
            params.append(platform)

        genre = filters.get("genre")
        if genre:
            clauses.append(f"{self._DL_BUCKET_SQL['genre']} = ?")
            params.append(genre)

        search = (filters.get("search") or "").strip()
        if search:
            needle = f"%{self._escape_like(search)}%"
            clauses.append("(title LIKE ? ESCAPE '\\' "
                           "OR channel_name LIKE ? ESCAPE '\\')")
            params.extend([needle, needle])

        group_key = filters.get("group_key")
        group_value = filters.get("group_value")
        if group_key:
            if group_key not in self._DL_BUCKET_SQL:
                raise ValueError(f"unknown group_key: {group_key!r}")
            if group_key in ("platform", "genre") and group_value is not None:
                dedicated_value = filters.get(group_key)
                if dedicated_value and dedicated_value != group_value:
                    raise ValueError(
                        f"group_key={group_key!r} contradicts the "
                        f"dedicated {group_key!r} filter: "
                        f"{dedicated_value!r} != {group_value!r}")
            if group_value is not None:
                clauses.append(f"{self._DL_BUCKET_SQL[group_key]} = ?")
                params.append(group_value)

        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def count_downloads(self, filters=None):
        """Count downloads rows matching *filters* (see
        _downloads_filter_sql). Returns 0 on a DB failure; a bad group_key in
        *filters* raises ValueError rather than being swallowed."""
        where_sql, params = self._downloads_filter_sql(filters)
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM downloads{where_sql}",
                    params).fetchone()
                return int(row["n"]) if row else 0
        except Exception as e:
            self._log("error", f"count_downloads failed: {e}")
            return 0

    @staticmethod
    def _bitrate_sort_key(value):
        """Numeric sort key for one bitrate cell, registered as the SQL
        function cb_bitrate_key by query_downloads.

        The stored string is always TrackDownloader's own `quality` text
        (cratebuilder/download.py) — "{target} kbps MP3", or "{source} kbps
        src → {target} kbps MP3" when the source stream's own bitrate was
        also detected — never the "Xk → Yk" bitrate_text form (that one is
        Queue-display-only and never reaches the database). This returns
        the LAST integer in the string, i.e. the target: the MP3 actually
        written to disk, not the source stream that was transcoded from.
        Returns None for blank/non-numeric values (legacy/backfilled rows
        can carry ""), which SQLite's own NULL ordering then sorts first
        under ASC and last under DESC — deterministic, if it should ever
        need to be forced to one end regardless of direction that is a
        follow-up, not a silent surprise today.

        Matches "<digits> kbps MP3" specifically rather than just taking
        the last digit run in the string — the literal suffix "MP3" itself
        contains a digit ('3'), so a naive last-digit-run extraction reads
        every value, including a plain "320 kbps MP3", as bitrate 3. Falls
        back to the last digit run only for a value that doesn't match this
        shape at all (e.g. a hypothetical bare-numeric legacy value)."""
        value = value or ""
        m = re.search(r"(\d+)\s*kbps\s*mp3", value, re.IGNORECASE)
        if m:
            return int(m.group(1))
        digits = re.findall(r"\d+", value)
        return int(digits[-1]) if digits else None

    def query_downloads(self, filters=None, *, order_by="download_timestamp",
                        descending=True, limit=100, offset=0):
        """One page of downloads rows matching *filters*, newest-first by
        default, as plain dicts carrying every downloads column.

        order_by is checked against _DL_SORT_COLUMNS before use — an
        unrecognised column raises ValueError rather than ever reaching the
        query string, since SQLite can't bind a column/direction the way it
        binds a value. Rows are always tie-broken on id in the sort
        direction, so two rows sharing a sort value can't be dropped or
        duplicated across a limit/offset page boundary.

        order_by="bitrate" sorts numerically (_bitrate_sort_key), not as
        text — the column holds strings like "320 kbps MP3" and "70 kbps
        src → 192 kbps MP3", and a lexical sort puts "70..." after
        "320..." alphabetically, which is backwards."""
        if order_by not in self._DL_SORT_COLUMNS:
            raise ValueError(f"unknown order_by column: {order_by!r}")
        where_sql, params = self._downloads_filter_sql(filters)
        direction = "DESC" if descending else "ASC"
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        order_expr = ("cb_bitrate_key(bitrate)" if order_by == "bitrate"
                      else order_by)
        try:
            with self._conn() as conn:
                if order_by == "bitrate":
                    conn.create_function(
                        "cb_bitrate_key", 1, self._bitrate_sort_key)
                rows = conn.execute(
                    f"SELECT * FROM downloads{where_sql} "
                    f"ORDER BY {order_expr} COLLATE NOCASE {direction}, "
                    f"id {direction} LIMIT ? OFFSET ?",
                    [*params, limit, offset]).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"query_downloads failed: {e}")
            return []

    def group_downloads(self, preset, filters=None):
        """[{key, label, count}] breaking down downloads by the next
        ungrouped level of *preset*'s hierarchy (GROUP_PRESETS), under
        *filters*.

        "Next ungrouped" is the first hierarchy column not already pinned by
        filters["platform"]/["genre"]/(filters["group_key"] together with a
        non-None filters["group_value"] — a group_key with no value pins
        nothing, matching _downloads_filter_sql's own "no value, no filter"
        rule). One call breaks down exactly one level; a UI drills deeper by
        calling this again with that level's chosen bucket pinned via
        group_key/group_value (the same pair count_downloads/query_downloads
        take) — i.e. expanding an N-level tree in the browser is N round
        trips, one per node expansion, not one call returning the whole
        nested tree. Returns [] once every hierarchy level is pinned, i.e.
        there is nothing left to break down. key and label are both the
        bucketed string (e.g. "(unknown)", "(none)") a caller feeds back as
        group_value to drill further. An unrecognised preset raises
        ValueError (and _downloads_filter_sql raises ValueError first for an
        unrecognised group_key, or a group_key that duplicates the
        dedicated platform/genre filter)."""
        if preset not in self.GROUP_PRESETS:
            raise ValueError(f"unknown group preset: {preset!r}")
        hierarchy = self.GROUP_PRESETS[preset]
        filters = filters or {}
        pinned = set()
        if filters.get("platform"):
            pinned.add("platform")
        if filters.get("genre"):
            pinned.add("genre")
        if filters.get("group_key") and filters.get("group_value") is not None:
            pinned.add(filters["group_key"])
        next_key = next((k for k in hierarchy if k not in pinned), None)
        if next_key is None:
            return []
        where_sql, params = self._downloads_filter_sql(filters)
        bucket_sql = self._DL_BUCKET_SQL[next_key]
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT {bucket_sql} AS bucket, COUNT(*) AS n "
                    f"FROM downloads{where_sql} "
                    f"GROUP BY bucket ORDER BY bucket COLLATE NOCASE ASC",
                    params).fetchall()
                return [{"key": r["bucket"], "label": r["bucket"],
                         "count": r["n"]} for r in rows]
        except Exception as e:
            self._log("error", f"group_downloads failed: {e}")
            return []

    def query_watchlist_rows(self):
        """Every watchlist row as a plain dict, plus a live per-channel
        download_count from a single LEFT JOIN — cheaper than a
        get_channel_download_count call per row for a table this shape.
        total_downloaded (the stored, possibly-stale column written by
        refresh_watchlist_total(s)) is left in the row alongside it, since
        both are legitimate answers to "how many" and callers can pick.
        url is blanked to "" when it carries the "unresolved://" sentinel,
        matching the monolith's _wl_display_url — an unresolved channel has
        no real link to show, so the raw internal placeholder shouldn't
        reach a caller that might render it as one. Ordered by display_name.
        Returns [] on failure."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT w.*, COUNT(d.id) AS download_count
                    FROM watchlist w
                    LEFT JOIN downloads d ON d.channel_url = w.url
                    GROUP BY w.id
                    ORDER BY w.display_name COLLATE NOCASE ASC
                """).fetchall()
        except Exception as e:
            self._log("error", f"query_watchlist_rows failed: {e}")
            return []
        result = [dict(r) for r in rows]
        for row in result:
            if (row.get("url") or "").startswith(self._UNRESOLVED_URL_PREFIX):
                row["url"] = ""
        return result

    def _artwork_where_sql(self, filter_name):
        """WHERE clause for one of ARTWORK_FILTERS other than "Sidecar
        missing on disk" (that one needs a live filesystem check, not a
        column predicate — see _artwork_broken_candidates). Validates
        filter_name itself rather than trusting the caller to have checked
        already, the same defence-in-depth every other whitelist in this
        file gets."""
        if filter_name not in self.ARTWORK_FILTERS:
            raise ValueError(f"unknown artwork filter: {filter_name!r}")
        if filter_name == "All tracks":
            return ""
        if filter_name == "Has artwork":
            return f" WHERE {self._ART_HAS_ARTWORK_SQL}"
        if filter_name == "Missing artwork":
            return f" WHERE NOT {self._ART_HAS_ARTWORK_SQL}"
        if filter_name == "Embedded only":
            return " WHERE artwork_embedded = 1"
        raise ValueError(  # "Sidecar missing on disk"
            f"{filter_name!r} needs a filesystem check, not a WHERE clause "
            "— see _artwork_broken_candidates")

    def _artwork_broken_candidates(self):
        """downloads rows with a recorded artwork_path — every row "Sidecar
        missing on disk" must stat() to decide, returned as plain dicts.

        SQL-only: this is the *only* part of that filter's work that runs
        under the pooled connection/lock. The filesystem check itself always
        happens after this returns and the lock has been released (see
        query_artwork_rows/count_artwork_rows) — the app-wide lock is shared
        with every other db.py caller, including an in-progress download's
        add_download write, so os.path.isfile() run while holding it would
        stall the whole app for as long as the scan takes, not just this
        query. Returns [] on a DB failure (logged)."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT * FROM downloads
                    WHERE artwork_path IS NOT NULL AND TRIM(artwork_path) != ''
                    ORDER BY title COLLATE NOCASE ASC, id ASC
                """).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"_artwork_broken_candidates failed: {e}")
            return []

    @staticmethod
    def _broken_page(candidates, offset, limit):
        """Walk already-fetched, lock-released *candidates* (in whatever
        order the caller already sorted them), calling os.path.isfile()
        only until *limit* broken rows past *offset* have been found — not
        the whole candidate set regardless of page size. Paging the front of
        a large "Sidecar missing on disk" result therefore stats a handful
        of rows, not every candidate in the library; only an exact count
        (count_artwork_rows) has to look at all of them, since a count can't
        skip anything and still be right."""
        if limit <= 0:
            return []
        page, skipped = [], 0
        for row in candidates:
            if os.path.isfile(row["artwork_path"]):
                continue
            if skipped < offset:
                skipped += 1
                continue
            page.append(row)
            if len(page) >= limit:
                break
        return page

    @staticmethod
    def _art_row_matches_search(row, needle):
        hay = f"{row.get('title', '')} {row.get('channel_name', '')}".lower()
        return needle in hay

    @staticmethod
    def _art_sort_key(order_by):
        """A Python sort key function for one whitelisted order_by column,
        used only by the "Sidecar missing on disk" branch (its candidates
        are a Python list, not a SQL result) — every other filter sorts in
        SQL instead. artwork_embedded is a real 0/1 int already; everything
        else is compared as lowercased text, matching COLLATE NOCASE."""
        if order_by == "artwork_embedded":
            return lambda r: int(bool(r.get("artwork_embedded")))
        return lambda r: (r.get(order_by) or "").lower()

    def query_artwork_rows(self, filter_name, *, search=None, order_by="title",
                           descending=False, limit=100, offset=0):
        """One page of downloads rows under *filter_name* (ARTWORK_FILTERS),
        as plain dicts. An unrecognised filter_name or order_by raises
        ValueError; a DB failure logs and returns [].

        *search* is an optional LIKE over title/channel_name, same escaping
        as _downloads_filter_sql's search filter (literal %/_ escaped so
        they can't be mistaken for SQL wildcards).

        "Sidecar missing on disk" fetches its (SQL-narrowed) candidates
        under the pooled lock via _artwork_broken_candidates, then — lock
        already released — filters by search, sorts by order_by, and stats
        only as many candidates as it takes to fill this page
        (_broken_page), rather than scanning the whole candidate set on
        every page turn."""
        if filter_name not in self.ARTWORK_FILTERS:
            raise ValueError(f"unknown artwork filter: {filter_name!r}")
        if order_by not in self._ART_SORT_COLUMNS:
            raise ValueError(f"unknown order_by column: {order_by!r}")
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        search = (search or "").strip()
        if filter_name == "Sidecar missing on disk":
            candidates = self._artwork_broken_candidates()
            if search:
                needle = search.lower()
                candidates = [r for r in candidates
                             if self._art_row_matches_search(r, needle)]
            candidates.sort(key=self._art_sort_key(order_by), reverse=descending)
            return [dict(r) for r in
                    self._broken_page(candidates, offset, limit)]
        where_sql = self._artwork_where_sql(filter_name)
        params = []
        if search:
            needle = f"%{self._escape_like(search)}%"
            clause = "(title LIKE ? ESCAPE '\\' OR channel_name LIKE ? ESCAPE '\\')"
            where_sql = f"{where_sql} AND {clause}" if where_sql else f" WHERE {clause}"
            params.extend([needle, needle])
        direction = "DESC" if descending else "ASC"
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM downloads{where_sql} "
                    f"ORDER BY {order_by} COLLATE NOCASE {direction}, id {direction} "
                    f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            self._log("error", f"query_artwork_rows failed: {e}")
            return []

    def count_artwork_rows(self, filter_name, *, search=None):
        """Count of downloads rows under *filter_name* (ARTWORK_FILTERS),
        with the same optional *search* query_artwork_rows takes. An
        unrecognised filter_name raises ValueError; a DB failure logs and
        returns 0.

        "Sidecar missing on disk" is inherently a full scan of its
        candidates — an exact count can't skip any of them — but, like
        query_artwork_rows, the candidates are fetched under the pooled
        lock via _artwork_broken_candidates and every os.path.isfile() call
        runs after that lock has already been released."""
        if filter_name not in self.ARTWORK_FILTERS:
            raise ValueError(f"unknown artwork filter: {filter_name!r}")
        search = (search or "").strip()
        if filter_name == "Sidecar missing on disk":
            candidates = self._artwork_broken_candidates()
            if search:
                needle = search.lower()
                candidates = [r for r in candidates
                             if self._art_row_matches_search(r, needle)]
            return sum(1 for r in candidates
                      if not os.path.isfile(r["artwork_path"]))
        where_sql = self._artwork_where_sql(filter_name)
        params = []
        if search:
            needle = f"%{self._escape_like(search)}%"
            clause = "(title LIKE ? ESCAPE '\\' OR channel_name LIKE ? ESCAPE '\\')"
            where_sql = f"{where_sql} AND {clause}" if where_sql else f" WHERE {clause}"
            params.extend([needle, needle])
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM downloads{where_sql}",
                    params).fetchone()
                return int(row["n"]) if row else 0
        except Exception as e:
            self._log("error", f"count_artwork_rows failed: {e}")
            return 0

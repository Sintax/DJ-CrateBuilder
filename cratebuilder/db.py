"""SQLite persistence: downloads history + watchlist."""
import json
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

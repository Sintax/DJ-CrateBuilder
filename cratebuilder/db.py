"""SQLite persistence: downloads history + watchlist."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager


class DownloadsDatabase:
    SCHEMA_VERSION = 4

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

                conn.execute(
                    "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
                    ("version", str(self.SCHEMA_VERSION))
                )
            self._log("info", f"schema initialized at {self.db_path}")
        except Exception as e:
            self._log("error", f"schema init failed: {e}")
            raise

    def add_download(self, *, video_id, title, channel_name, channel_url,
                     platform, genre, file_path, upload_date, bitrate,
                     channel_id=None, artwork_path=None, artwork_embedded=0,
                     thumbnail_url=None):
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate,
                       artwork_path, artwork_embedded, thumbnail_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        a missing key rather than substituting NULL."""
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
            with self._conn() as conn:
                conn.executemany("""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate,
                       artwork_path, artwork_embedded, thumbnail_url)
                    VALUES (:video_id, :title, :channel_name, :channel_url,
                            :channel_id, :platform, :genre, :file_path,
                            :upload_date, :ts, :bitrate,
                            :artwork_path, :artwork_embedded, :thumbnail_url)
                """, bound)
            return len(bound)
        except Exception as e:
            self._log("error", f"backfill_downloads failed: {e}")
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
        {file_path: (artwork_path, artwork_embedded, thumbnail_url)}.

        Only rows carrying some artwork data are included (an artwork_path, an
        embedded flag, or a thumbnail_url); rows with no file_path are skipped
        because the key would be meaningless.

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
                           thumbnail_url
                    FROM downloads
                    WHERE file_path IS NOT NULL AND file_path != ''
                      AND (artwork_path IS NOT NULL
                           OR artwork_embedded = 1
                           OR thumbnail_url IS NOT NULL)
                """).fetchall()
                return {r["file_path"]: (r["artwork_path"],
                                         r["artwork_embedded"],
                                         r["thumbnail_url"])
                        for r in rows}
        except Exception as e:
            self._log("error", f"get_artwork_by_path failed: {e}")
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
                               scan_cutoff_date, auto_added=False,
                               channel_id=None, status="idle"):
        try:
            total = self.get_channel_download_count(url)
            with self._conn() as conn:
                cur = conn.execute("""
                    INSERT INTO watchlist
                      (url, channel_id, display_name, platform, genre,
                       scan_cutoff_date, date_added, auto_added,
                       total_downloaded, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (url, channel_id or None, display_name, platform, genre,
                      scan_cutoff_date, int(time.time()),
                      1 if auto_added else 0, total, status))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        except Exception as e:
            self._log("error", f"add_watchlist_channel failed: {e}")
            return None

    def update_watchlist_cutoff(self, url, new_cutoff_date):
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE watchlist SET scan_cutoff_date = ? WHERE url = ?",
                    (new_cutoff_date, url))
        except Exception as e:
            self._log("error", f"update_watchlist_cutoff failed: {e}")

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
        allowed = {"display_name", "genre", "scan_cutoff_date",
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

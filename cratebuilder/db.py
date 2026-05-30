"""SQLite persistence: downloads history + watchlist."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager


class DownloadsDatabase:
    SCHEMA_VERSION = 2

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
                        bitrate             TEXT
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
                     channel_id=None):
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (video_id or None, title or "", channel_name or "",
                      channel_url or "", channel_id or None, platform,
                      genre or "", file_path or "", upload_date or "",
                      int(time.time()), bitrate or ""))
        except Exception as e:
            self._log("error", f"add_download failed for {title!r}: {e}")

    def backfill_downloads(self, rows):
        """Bulk-insert tracks discovered already-on-disk during a scan, so
        future dedup is exact and instant. `rows` is a list of dicts with the
        download columns plus a 'ts' timestamp. Returns the count inserted."""
        if not rows:
            return 0
        try:
            with self._conn() as conn:
                conn.executemany("""
                    INSERT INTO downloads
                      (video_id, title, channel_name, channel_url, channel_id,
                       platform, genre, file_path, upload_date,
                       download_timestamp, bitrate)
                    VALUES (:video_id, :title, :channel_name, :channel_url,
                            :channel_id, :platform, :genre, :file_path,
                            :upload_date, :ts, :bitrate)
                """, rows)
            return len(rows)
        except Exception as e:
            self._log("error", f"backfill_downloads failed: {e}")
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

    def update_watchlist_status(self, channel_id, status, last_error=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE watchlist SET status = ?, last_error = ? WHERE id = ?",
                    (status, last_error, channel_id))
        except Exception as e:
            self._log("error", f"update_watchlist_status failed: {e}")

    def update_watchlist_channel_fields(self, wl_id, **fields):
        allowed = {"display_name", "genre", "scan_cutoff_date",
                   "channel_id", "url", "status", "last_error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [wl_id]
            with self._conn() as conn:
                conn.execute(f"UPDATE watchlist SET {sets} WHERE id = ?", vals)
        except Exception as e:
            self._log("error", f"update_watchlist_channel_fields failed: {e}")

    def remove_watchlist_channel(self, channel_id):
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM watchlist WHERE id = ?", (channel_id,))
        except Exception as e:
            self._log("error", f"remove_watchlist_channel failed: {e}")

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

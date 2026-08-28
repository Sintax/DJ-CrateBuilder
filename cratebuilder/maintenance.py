"""Database maintenance jobs: rebuild, de-dup, tag repair, artwork backfill."""

import os
import threading
import time
from datetime import datetime

from cratebuilder import artwork as cb_artwork
from cratebuilder import genrefix, rebuild, tagging
from cratebuilder.batchresolve import PLATFORM_SUBDIR, platform_dir
from cratebuilder.crate import CrateLayout
from cratebuilder.service import MAINTENANCE_JOB, CBError
from cratebuilder.sidecar import channel_url_from_id, read_channel_sidecar
from cratebuilder.ydl import YdlSession

# The four jobs, named as the contract methods that start them so a progress
# frame, a notification and the RPC that began the run all say the same word.
TASK_REBUILD = "db.rebuild"
TASK_DEDUPE = "db.dedupe"
TASK_REPAIR_TAGS = "db.repair_tags"
TASK_FETCH_ARTWORK = "db.fetch_artwork"

TASKS = (TASK_REBUILD, TASK_DEDUPE, TASK_REPAIR_TAGS, TASK_FETCH_ARTWORK)

# Every task's user-facing name, used for the modal title and the notification.
TASK_TITLES = {
    TASK_REBUILD: "Rebuild Database from Files",
    TASK_DEDUPE: "Remove Duplicates",
    TASK_REPAIR_TAGS: "Repair Track Tags",
    TASK_FETCH_ARTWORK: "Fetch Missing Artwork",
}

# The two platform roots a library scan walks, in the monolith's own order.
PLATFORMS = tuple(PLATFORM_SUBDIR)

# Pause between artwork network fetches, straight from
# _ArtworkBackfillSession._FETCH_PAUSE_SEC: a backfill can walk thousands of
# tracks and must not look like a scrape to either platform.
FETCH_PAUSE_SEC = 0.25

# How often the interruptible wrapper re-checks Skip/Cancel while a blocking
# network call runs on its own thread.
POLL_SECONDS = 0.1


def _plural(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


class MaintenanceOps:
    """The four database maintenance runs, headless.

    The Tk-free re-expression of the monolith's Rebuild / Remove Duplicates /
    Repair Track Tags / Fetch Missing Artwork actions: the same sequence and
    the same tallies, with each modal dialog replaced by an event and each
    refusal by a CBError raised from `preview` — before a job is ever started,
    so the caller hears it synchronously.

    One run at a time is the job registry's guarantee (category
    "maintenance"), not this class's; what lives here is the run itself.
    Cancel and Skip are Events set from whatever thread the RPC arrives on,
    and they keep the tooltips' promises: cancelling stops after the item in
    flight and keeps everything already written, and Skip abandons only the
    track being fetched.

    No database call is held open across filesystem or network work. Every
    helper on DownloadsDatabase opens and closes its own pooled connection, so
    the loops below call them one row at a time rather than wrapping a walk in
    a connection the tkinter window is also waiting on.
    """

    def __init__(self, settings, db_factory, emit, *, log_line=None,
                 counts=None, flush=None, session_factory=YdlSession,
                 ffmpeg_dir=None, sleep=time.sleep, now=datetime.now):
        self._settings = settings
        self._db_factory = db_factory
        self._emit = emit
        self._log_line = log_line or (lambda text: None)
        self._counts = counts
        self._flush = flush or (lambda: None)
        self._session_factory = session_factory
        self._ffmpeg_dir = ffmpeg_dir
        self._sleep = sleep
        self._now = now
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._skip = threading.Event()
        self._task = None

    # ── controls ──────────────────────────────────────────────────────────────

    @property
    def task(self):
        """The task name of the run in flight, or None."""
        with self._lock:
            return self._task

    def cancel(self):
        """Ask the running job to stop. Safe at any moment, including when
        nothing is running: cancelling is never destructive, so a click that
        races the job's own ending must not come back as an error."""
        self._cancel.set()
        return {"cancelled": True, "task": self.task}

    def skip(self):
        """Abandon the track in flight. Only Fetch Missing Artwork watches
        this; for every other job it is a no-op the next run clears."""
        self._skip.set()
        return {"skipped": True, "task": self.task}

    def cancelled(self):
        return self._cancel.is_set()

    @staticmethod
    def title_for(task):
        """The user-facing name of one job — what the user actually pressed,
        so a crash report can be headed with it rather than "maintenance"."""
        return TASK_TITLES.get(task, task)

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _db(self):
        return self._db_factory()

    def _base_dir(self):
        return self._settings.get("base_dir")

    def _platform_dirs(self):
        """The per-platform roots the library layout hangs off."""
        base = self._base_dir()
        return [platform_dir(base, p) for p in PLATFORMS]

    def _cover_art_mode(self):
        """The effective mode this run acts on: "off" when the cover-art
        checkbox is clear, otherwise the chosen formatting."""
        policy = self._settings.download_policy()
        return policy.cover_art_mode if policy.cover_art_enabled else "off"

    def _session(self):
        return self._session_factory(cookies=self._settings.cookie_config())

    def _begin(self, task):
        with self._lock:
            self._task = task
        self._cancel.clear()
        self._skip.clear()

    def _end(self):
        with self._lock:
            self._task = None

    def _overall(self, task, done, total, **extra):
        """One determinate progress frame. `total` is what the counting pass
        found; an item appearing or vanishing since then only moves the bar,
        it can never break the run."""
        total = max(int(total or 0), int(done or 0))
        self._emit("progress.overall", dict(
            {"job": MAINTENANCE_JOB, "task": task, "done": int(done),
             "total": total,
             "percent": int(done / total * 100) if total else 0},
            **extra))

    def _current(self, task, title, note=""):
        self._emit("progress.current", {
            "job": MAINTENANCE_JOB, "task": task, "title": title or "",
            "note": note, "percent": None,
        })

    def _notify(self, task, level, body, cancelled=False):
        """The completion announcement, in the contract's notification shape
        ({level, title, body, at}). Flushed first so the last progress frame
        can never arrive after the summary that supersedes it.

        *cancelled* is carried explicitly rather than left to be read back out
        of the prose: the frontend has to tell a cancelled run from a finished
        one, and inferring that from the wording means an edit to a sentence
        here silently relabels the dialog."""
        self._flush()
        self._emit("notification", {
            "level": level,
            "title": self.title_for(task),
            "body": body,
            "at": self._now().isoformat(timespec="seconds"),
            "task": task,
            "job": MAINTENANCE_JOB,
            "cancelled": bool(cancelled),
        })

    def _patch_counts(self):
        if self._counts is None:
            return
        try:
            self._emit("state.patch", {"counts": self._counts()})
        except Exception:
            pass

    # ── preflight ─────────────────────────────────────────────────────────────

    def preview(self, task):
        """What the confirm modal needs, or the refusal the monolith shows as
        an info box instead of opening one.

        Raising here rather than inside the job is the point: every one of
        these answers is known before any work starts, and a CBError raised on
        the calling thread reaches the user as the reason, where one raised on
        a job thread would be swallowed by `_start_job`'s worker."""
        if task == TASK_REBUILD:
            return self._preview_rebuild()
        if task == TASK_DEDUPE:
            return self._preview_dedupe()
        if task == TASK_REPAIR_TAGS:
            return self._preview_repair_tags()
        if task == TASK_FETCH_ARTWORK:
            return self._preview_fetch_artwork()
        raise CBError(f"Unknown maintenance job: {task!r}")

    def _preview_rebuild(self):
        db = self._db()
        return {"task": TASK_REBUILD, "rows": db.get_download_count(),
                "base_dir": self._base_dir()}

    def _preview_dedupe(self):
        files, extra = self._db().count_duplicate_downloads()
        if not extra:
            raise CBError("No duplicate rows found — every file in the "
                          "database is recorded exactly once.")
        return {"task": TASK_DEDUPE, "files": files, "extra": extra}

    def _preview_repair_tags(self):
        total = genrefix.count_library_tracks(self._platform_dirs())
        if not total:
            raise CBError(f"Found no audio files under {self._base_dir()}.")
        return {"task": TASK_REPAIR_TAGS, "total": total,
                "no_genre_dir": CrateLayout.NO_GENRE_DIR}

    def _preview_fetch_artwork(self):
        if self._cover_art_mode() == "off":
            raise CBError("Cover Art is switched off. Choose a Cover Art mode "
                          "in Settings first, then run this again.")
        if not cb_artwork.artwork_available():
            raise CBError("Cover art needs Pillow and mutagen, which are not "
                          "available in this install.")
        rows = self._db().get_downloads_missing_artwork()
        if not rows:
            raise CBError("Every track in your library already has cover art.")
        return {"task": TASK_FETCH_ARTWORK, "total": len(rows)}

    # ══════════════════════════════════════════════════════════════════════════
    # Rebuild — rebuild the downloads table from the files already on disk
    # ══════════════════════════════════════════════════════════════════════════

    def run_rebuild(self):
        """Scan base/<Platform>/<Genre>/<Channel>/*.<ext> and rebuild the
        downloads table from what is actually there.

        The monolith's `_rebuild_db_from_files` walk, verbatim in sequence: the
        artwork columns are snapshotted by path first (the clear would
        otherwise orphan every track's cover-art bookkeeping), the walk resolves
        each track's id and art from disk, and only then is the table cleared
        and refilled. An empty scan aborts without touching the table — an
        unmounted drive or a reconfigured download root reads as empty, and
        clearing on that result would wipe the user's whole history behind a
        success message.

        Cancelling is safe for the same reason: the clear has not happened yet
        while the walk is running, so a cancelled rebuild leaves the existing
        rows exactly as they were.

        So is a folder that cannot be read. An unreadable folder — an ACL, an
        antivirus lock, a network share that drops mid-walk — must NEVER be
        allowed to contribute zero rows and let the run continue, because the
        clear that follows would then delete that channel's whole history
        while the tracks are still on disk. Any OSError from the walk aborts
        the entire rebuild before the clear, which is what the monolith does by
        wrapping its whole `_work()` in one try (DJ-CrateBuilder_v1.3.py:14201).
        The scan is only trustworthy if all of it succeeded."""
        self._begin(TASK_REBUILD)
        try:
            db = self._db()
            art_snapshot = db.get_artwork_by_path()
            rows = []
            done = 0
            total = 0
            try:
                folders = self._channel_folders()
                total = len(folders)
                self._overall(TASK_REBUILD, 0, total)
                for platform, genre, channel_path in folders:
                    if self._cancel.is_set():
                        break
                    self._current(TASK_REBUILD,
                                  os.path.basename(channel_path),
                                  note=f"{platform} • {genre}")
                    rows.extend(self._scan_channel(platform, genre,
                                                   channel_path, art_snapshot))
                    done += 1
                    self._overall(TASK_REBUILD, done, total, found=len(rows))
            except OSError as exc:
                raise CBError(
                    f"Rebuild stopped — a folder in your library could not be "
                    f"read ({exc}). Nothing was changed: the database is only "
                    f"cleared once the whole library has been scanned, and a "
                    f"scan that could not finish would delete the history of "
                    f"every folder it missed.")
            if self._cancel.is_set():
                self._notify(
                    TASK_REBUILD, "warn",
                    f"Cancelled after {done:,} of {total:,} channel folders. "
                    f"The database was left unchanged.", cancelled=True)
                return {"cancelled": True, "indexed": 0}
            if not rows:
                self._notify(
                    TASK_REBUILD, "warn",
                    f"Found no audio files under {self._base_dir()}. The "
                    f"database was left unchanged.")
                return {"cancelled": False, "indexed": 0}
            db.clear_all_downloads()
            count = db.backfill_downloads(rows)
            db.refresh_watchlist_totals()
            self._notify(TASK_REBUILD, "info",
                         f"Indexed {_plural(count, 'track')} from the files "
                         f"on disk.")
            self._patch_counts()
            return {"cancelled": False, "indexed": count}
        finally:
            self._end()

    def _channel_folders(self):
        """Every (platform, genre, path) channel folder under the crate root.

        Walked ahead of the scan so the bar is determinate — three listdirs
        deep is cheap next to reading a tag out of every file, and it is the
        same traversal the scan then repeats one level lower.

        Every OSError here propagates, deliberately: a genre folder that
        cannot be listed means its channels never enter the scan at all, and
        the clear that follows would erase exactly those channels' history.
        A platform root that simply is not there is not an error — the crate
        may only ever have held YouTube downloads."""
        found = []
        base = self._base_dir()
        for platform in PLATFORMS:
            proot = platform_dir(base, platform)
            if not os.path.isdir(proot):
                continue
            for genre_dir in sorted(os.listdir(proot)):
                genre_path = os.path.join(proot, genre_dir)
                if not os.path.isdir(genre_path):
                    continue
                genre = CrateLayout.genre_value(genre_dir)
                for channel_dir in sorted(os.listdir(genre_path)):
                    channel_path = os.path.join(genre_path, channel_dir)
                    if os.path.isdir(channel_path):
                        found.append((platform, genre, channel_path))
        return found

    def _scan_channel(self, platform, genre, channel_path, art_snapshot):
        """One channel folder's tracks as downloads rows, read from disk.

        `os.listdir` is deliberately unguarded — a folder that cannot be
        listed must abort the rebuild, not quietly contribute nothing. The
        per-file `getmtime` failure below IS swallowed, matching the monolith
        (DJ-CrateBuilder_v1.3.py:14160): one unreadable file is a track this
        rebuild cannot describe, not evidence the scan is untrustworthy."""
        sc = read_channel_sidecar(channel_path) or {}
        channel_name = sc.get("display_name") or os.path.basename(channel_path)
        channel_id = sc.get("channel_id")
        channel_url = (sc.get("channel_url")
                       or channel_url_from_id(channel_id) or "")
        art_index = rebuild.index_artwork_dir(channel_path)

        rows = []
        for name in sorted(os.listdir(channel_path)):
            if not name.lower().endswith(rebuild.AUDIO_EXTS):
                continue
            full = os.path.join(channel_path, name)
            try:
                mtime = int(os.path.getmtime(full))
            except OSError:
                continue
            # Upload date / downloaded time aren't recorded in the file
            # itself, so fall back to the file's mtime.
            date_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d")
            # Recovering the id keeps the <video_id>.jpg artwork key stable,
            # which is what stops the backfill writing a second identical JPEG
            # under the filename stem.
            vid = rebuild.recover_video_id(full)
            if not vid:
                # SoundCloud ids aren't recoverable from the file — carry the
                # one the old row held, or the NEXT rebuild would lose the
                # id-keyed sidecar match this one just used.
                snap = art_snapshot.get(full)
                if snap and len(snap) > 3:
                    vid = snap[3]
            art_path, art_embedded, thumb_url = rebuild.resolve_artwork(
                full, vid, art_index, snapshot=art_snapshot)
            rows.append({
                "video_id":     vid,
                "title":        os.path.splitext(name)[0],
                "channel_name": channel_name,
                "channel_url":  channel_url,
                "channel_id":   channel_id,
                "platform":     platform,
                "genre":        genre,
                "file_path":    full,
                "upload_date":  date_str,
                "ts":           mtime,
                "bitrate":      "",
                "artwork_path":     art_path,
                "artwork_embedded": art_embedded,
                "thumbnail_url":    thumb_url,
            })
        return rows

    # ══════════════════════════════════════════════════════════════════════════
    # De-dup — collapse repeated rows that point at the same file
    # ══════════════════════════════════════════════════════════════════════════

    def run_dedupe(self):
        """Collapse duplicate downloads rows and take the unique index.

        One transaction inside DownloadsDatabase, so there is no per-item
        progress to report and nothing to cancel partway — the bar goes 0 to 1
        and Cancel is inert here rather than pretending otherwise."""
        self._begin(TASK_DEDUPE)
        try:
            db = self._db()
            self._overall(TASK_DEDUPE, 0, 1)
            self._current(TASK_DEDUPE, "Merging duplicate rows…")
            files, extra = db.count_duplicate_downloads()
            if not extra:
                self._overall(TASK_DEDUPE, 1, 1)
                self._notify(TASK_DEDUPE, "info",
                             "No duplicate rows found — every file in the "
                             "database is recorded exactly once.")
                return {"removed": 0, "groups": 0, "indexed": True}
            result = db.dedupe_downloads_by_path()
            db.refresh_watchlist_totals()
            self._overall(TASK_DEDUPE, 1, 1)
            tail = ("Duplicate protection is now on: the database will keep "
                    "one row per file from here on."
                    if result.get("indexed") else
                    "Some duplicates could not be removed, so the protection "
                    "stayed off. See the debug log.")
            self._notify(
                TASK_DEDUPE, "info" if result.get("indexed") else "warn",
                f"Removed {_plural(result.get('removed', 0), 'redundant row')} "
                f"across {_plural(result.get('groups', 0), 'file')}. {tail}")
            self._patch_counts()
            return result
        finally:
            self._end()

    # ══════════════════════════════════════════════════════════════════════════
    # Tag repair — realign every file's tags with the folder it is filed under
    # ══════════════════════════════════════════════════════════════════════════

    def run_repair_tags(self):
        """Walk every track in the library and realign its tags.

        The monolith's `_start_tag_repair` sweep with `facts` supplied: genre
        forced to the folder the track is filed under, and Title / Encoded-by /
        source URL filled in only where the file carries none.

        The whole downloads table is read ONCE, before the walk, and the
        connection is closed again before the first file is touched — the
        tkinter window shares this database's write lock, and a sweep of ten
        thousand files must never be holding it.

        Cancelling keeps every tag already written: each file is complete the
        moment it is saved, which is the promise the Cancel tooltip makes."""
        self._begin(TASK_REPAIR_TAGS)
        try:
            dirs = self._platform_dirs()
            total = genrefix.count_library_tracks(dirs)
            facts = genrefix.index_by_path(self._db().get_track_facts_by_path())
            fixed = filled = untouched = errors = done = 0
            self._overall(TASK_REPAIR_TAGS, 0, total)
            for path, genre in genrefix.iter_library_tracks(dirs):
                if self._cancel.is_set():
                    break
                self._current(TASK_REPAIR_TAGS, os.path.basename(path),
                              note=genre or CrateLayout.NO_GENRE_DIR)
                try:
                    title, video_id, platform = genrefix.lookup_facts(facts,
                                                                      path)
                    changed, was_filled = genrefix.repair_track(
                        path, genre,
                        title=title or genrefix.title_from_filename(path),
                        source_url=genrefix.source_url_for(platform, video_id))
                    if changed:
                        fixed += 1
                    if was_filled:
                        filled += 1
                    if not changed and not was_filled:
                        untouched += 1
                except Exception:
                    errors += 1
                done += 1
                self._overall(TASK_REPAIR_TAGS, done, total, genres=fixed,
                              filled=filled)
            cancelled = self._cancel.is_set()
            summary = (f"{_plural(fixed, 'genre tag')} corrected. "
                       f"{_plural(filled, 'track')} had missing tags filled "
                       f"in. {untouched:,} already correct.")
            if errors:
                summary += f" {_plural(errors, 'track')} could not be written."
            if cancelled:
                summary = (f"Cancelled after {done:,} of {total:,} — tags "
                           f"already written are kept. ") + summary
            self._notify(TASK_REPAIR_TAGS,
                         "warn" if (errors or cancelled) else "info", summary,
                         cancelled=cancelled)
            return {"fixed": fixed, "filled": filled, "untouched": untouched,
                    "errors": errors, "done": done, "total": total,
                    "cancelled": cancelled}
        finally:
            self._end()

    # ══════════════════════════════════════════════════════════════════════════
    # Artwork backfill — find cover art for tracks that predate the feature
    # ══════════════════════════════════════════════════════════════════════════

    def run_fetch_artwork(self):
        """Walk every downloads row with no cover art and try to find it.

        The `_ArtworkBackfillSession` ladder, unchanged: the row's recorded
        thumbnail URL, then a sidecar JPEG already in `.artwork/`, then the
        YouTube thumbnail rebuilt from the video id, then the source URL read
        back out of the track's own ID3 tag. Every failure is counted and
        moved past — a track we cannot find art for is not an error, and a bad
        row must never abort the run.

        Skip applies to the row in flight only and is cleared at the top of
        every row, so it can interrupt a fetch mid-download and move straight
        on."""
        self._begin(TASK_FETCH_ARTWORK)
        try:
            db = self._db()
            rows = db.get_downloads_missing_artwork()
            mode = self._cover_art_mode()
            run = _ArtworkRun(self, db, rows, mode)
            return run.walk()
        finally:
            self._end()


class _ArtworkRun:
    """One pass of the artwork backfill. Holds the run's tallies and caches."""

    def __init__(self, ops, db, rows, mode):
        self._ops = ops
        self._db = db
        self.rows = list(rows)
        self.mode = mode
        self.total = len(self.rows)
        self.embedded = 0        # art found and written onto the file
        self.repaired = 0        # file already had art; only the DB was stale
        self.recovered = 0       # lost video ids re-derived from listings
        self.not_found = 0       # no artwork available from any rung
        self.missing = 0         # the row's file is no longer on disk
        self.skipped = 0         # rows abandoned via the Skip button
        self.errors = 0
        # One channel-listing fetch per folder per run, keyed by folder path.
        # Failures cache as {} so a dead channel costs one lookup, not one
        # per track.
        self._listing_cache = {}

    # ── the walk ──────────────────────────────────────────────────────────────

    def walk(self):
        ops = self._ops
        done = 0
        ops._overall(TASK_FETCH_ARTWORK, 0, self.total)
        for row in self.rows:
            if ops._cancel.is_set():
                break
            ops._skip.clear()   # Skip only ever applies to one row
            title = row.get("title") or ""
            ops._current(TASK_FETCH_ARTWORK, title,
                         note=str(row.get("platform") or ""))
            try:
                self._process(row)
                if ops._skip.is_set():
                    self.skipped += 1
            except Exception:
                self.errors += 1
            done += 1
            ops._overall(TASK_FETCH_ARTWORK, done, self.total,
                         embedded=self.embedded, not_found=self.not_found,
                         skipped=self.skipped)
        cancelled = ops._cancel.is_set()
        summary = (f"{self.embedded:,} embedded, {self.repaired:,} already "
                   f"had art, {self.not_found:,} none found")
        if self.recovered:
            summary += f", {self.recovered:,} ids recovered"
        if self.skipped:
            summary += f", {self.skipped:,} skipped"
        if self.missing:
            summary += f", {self.missing:,} files gone"
        if self.errors:
            summary += f", {self.errors:,} failed"
        if cancelled:
            summary = (f"Cancelled after {done:,} of {self.total:,} — tracks "
                       f"already done are kept. ") + summary
        ops._notify(TASK_FETCH_ARTWORK,
                    "warn" if (cancelled or self.errors) else "info",
                    summary + ".", cancelled=cancelled)
        return {"embedded": self.embedded, "repaired": self.repaired,
                "recovered": self.recovered, "not_found": self.not_found,
                "missing": self.missing, "skipped": self.skipped,
                "errors": self.errors, "done": done, "total": self.total,
                "cancelled": cancelled}

    def _process(self, row):
        """Resolve and embed artwork for a single downloads row."""
        ops = self._ops
        path = row.get("file_path") or ""
        if not os.path.isfile(path):
            self.missing += 1
            return

        key = cb_artwork.artwork_key(row.get("video_id"), path)
        if not key:
            self.not_found += 1
            return

        art_dir = cb_artwork.thumbnail_dir(os.path.dirname(path))
        if not art_dir:
            self.errors += 1
            return

        # The file may already carry art from outside the app (or from a run
        # whose DB write was lost). Nothing to fetch — just correct the row.
        if cb_artwork.has_cover_any(path):
            self._db.set_download_artwork(
                path, cb_artwork.existing_sidecar(art_dir, key), 1,
                row.get("thumbnail_url"))
            self.repaired += 1
            return

        # A YouTube row with no video id has no URL rungs at all — the id was
        # lost when a rebuild recreated the row from a tagless file. The
        # channel's upload listing can give it back: the filename is the title
        # as yt-dlp sanitised it, so matching it against the listing recovers
        # the id, which unlocks both the id-keyed sidecar cache and the
        # reconstructable thumbnail URLs.
        if (not row.get("video_id")
                and str(row.get("platform") or "").strip().lower() == "youtube"):
            vid = self._recover_video_id(path)
            if vid:
                row["video_id"] = vid
                self._db.set_download_video_id(path, vid)
                key = cb_artwork.artwork_key(vid, path)
                self.recovered += 1

        jpg = cb_artwork.existing_sidecar(art_dir, key)
        thumb_url = row.get("thumbnail_url")

        if not jpg:
            raw = os.path.join(art_dir, f"{key}.raw")
            thumb_url, jpg = self._fetch_sidecar(row, path, art_dir, key, raw)

        if ops._skip.is_set():
            return   # the walk tallies the row as skipped

        if not jpg:
            self.not_found += 1
            return

        final_path, embedded = cb_artwork.embed_cover_any(
            path, jpg, ops._ffmpeg_dir)
        if final_path != path:
            # A WebM was remuxed to Opus so the art could be written at all —
            # the row must follow the file or it is left pointing at a path
            # that no longer exists.
            self._db.update_download_path(path, final_path)
            path = final_path
        self._db.set_download_artwork(path, jpg, embedded, thumb_url)
        if embedded:
            self.embedded += 1
        else:
            # Sidecar saved but embedding failed (unsupported container, a
            # WebM whose audio wasn't Opus, or a write error) — the JPEG is
            # kept on disk for a future retry.
            self.not_found += 1

    def _fetch_sidecar(self, row, path, art_dir, key, raw):
        """Walk the remaining rungs of the ladder. Returns (thumb_url,
        jpg_path), either of which may be None."""
        ops = self._ops
        # First rung, zero network: an image yt-dlp left beside the track
        # (`writethumbnail` saves on the audio's own stem). Legacy SoundCloud
        # downloads resolved the wrong audio path, so the loose .jpg was never
        # harvested — it is the one artwork asset those tracks still have.
        sibling = cb_artwork.raw_thumbnail(path)
        if sibling:
            jpg = cb_artwork.ingest_thumbnail(sibling, art_dir, key, self.mode)
            if jpg:
                return row.get("thumbnail_url"), jpg

        candidates = list(cb_artwork.thumbnail_url_candidates(
            row.get("platform"), row.get("video_id"),
            row.get("thumbnail_url")))

        # Last rung: the source URL the app stamped into the track's own ID3
        # tag. Resolving it costs a yt-dlp metadata call, so it is only tried
        # when nothing cheaper produced a candidate.
        if not candidates:
            resolved = self._interruptible(self._thumbnail_from_source_tag,
                                           path)
            if resolved:
                candidates = [resolved]

        for url in candidates:
            if ops._cancel.is_set() or ops._skip.is_set():
                return None, None
            got = self._interruptible(cb_artwork.download_thumbnail, url, raw)
            if ops._skip.is_set():
                return None, None
            ops._sleep(FETCH_PAUSE_SEC)
            if not got:
                continue
            jpg = cb_artwork.ingest_thumbnail(raw, art_dir, key, self.mode)
            if jpg:
                return url, jpg
        return None, None

    def _interruptible(self, fn, *args):
        """Run a blocking call on a side thread and wait for it — unless Skip
        or Cancel fires first, in which case control returns IMMEDIATELY with
        None and the abandoned call finishes in the background with its result
        discarded. This is what makes Skip instant even mid-download."""
        ops = self._ops
        box = {}

        def runner():
            try:
                box["r"] = fn(*args)
            except Exception:
                pass

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(POLL_SECONDS)
            if ops._skip.is_set() or ops._cancel.is_set():
                return None
        return box.get("r")

    def _recover_video_id(self, path):
        """Re-derive a lost YouTube video id by matching *path*'s filename
        against the channel's upload listing (fetched once per folder, cached
        for the run — including failures). Returns the id or None."""
        folder = os.path.dirname(path)
        if folder not in self._listing_cache:
            index = self._interruptible(self._fetch_channel_index, folder)
            if index is None:
                # Skipped/cancelled mid-fetch — don't poison the cache with an
                # empty index; the next row in this folder can retry.
                return None
            self._listing_cache[folder] = index
        return cb_artwork.lookup_video_id(self._listing_cache[folder], path)

    def _fetch_channel_index(self, folder):
        """Fetch *folder*'s channel upload listing via its cratebuilder.json
        sidecar and build the normalized-title -> video-id index. One flat
        yt-dlp call per channel. Returns {} on any failure."""
        meta = read_channel_sidecar(folder) or {}
        url = meta.get("channel_url")
        platform = str(meta.get("platform") or "YouTube").strip().lower()
        if not url or platform != "youtube" or self._ops._cancel.is_set():
            return {}
        try:
            # ignore_no_formats: a channel whose videos serve no formats must
            # still yield its listing — the index only wants ids and titles.
            entries = self._ops._session().list_channel(
                url, ignore_no_formats=True)
            return cb_artwork.build_title_index(entries)
        except Exception:
            return {}

    def _thumbnail_from_source_tag(self, path):
        """Read the source URL out of the track's ID3 tag and ask yt-dlp for
        its thumbnail. The fallback for legacy SoundCloud tracks."""
        source_url = tagging.read_source_url(path)
        if not source_url:
            return None
        try:
            return self._ops._session().probe_thumbnail(source_url)
        except Exception:
            return None

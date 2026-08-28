"""Watch List orchestration: channel scans, per-channel downloads, channel edits."""
import json
import os
import shutil
import threading
import time

from cratebuilder import genrefix
from cratebuilder import links as cb_links
from cratebuilder import util
from cratebuilder.batchresolve import TrackSpec, entry_url, platform_dir
from cratebuilder.batchrun import BatchRunner
from cratebuilder.crate import (ChannelCrate, CrateLayout, SkipMode,
                                classify_scan_entries)
# cratebuilder.service does NOT import this module at import time — it reaches
# WatchlistOps through a deferred import inside its own accessor — so importing
# it from here is a one-way dependency, not a cycle.
from cratebuilder.service import WATCHLIST_JOB, CBError, watchlist_card
from cratebuilder.sidecar import (canonical_channel_url, channel_id_from_url,
                                  channel_url_from_id, classify_scan_error,
                                  is_unresolved_channel, watch_fetch_url,
                                  write_channel_sidecar)
from cratebuilder.ydl import (YdlOffline, YdlPermanent, YdlSession,
                              YdlUnclassified, network_is_reachable)

# The scan log's level vocabulary — the plan's normative scan.line contract, and
# the same class names web/app.js's log renderer already produces, so a line can
# be handed to it untranslated. The design's four scan-log colours map onto them:
# an ordinary SCAN line is default, a HELD premiere reads as skipped, a finished
# run reads as downloaded, and a failure is an error.
LINE_DEFAULT = "default"
LINE_DONE = "downloaded"
LINE_HELD = "skipped"
LINE_ERROR = "error"
LINE_LEVELS = (LINE_DEFAULT, LINE_DONE, LINE_HELD, LINE_ERROR)

# How often a mid-track progress frame may redraw a channel card. yt-dlp reports
# many frames per second and each one would otherwise replace a whole card in
# every connected client; a title change or a settled track still redraws
# immediately (see WatchlistOps._card_progress).
CARD_PROGRESS_INTERVAL = 0.5

# Mirrors DJ-CrateBuilder_v1.3.py's MP3DownloaderApp._AUDIO_EXTS: what counts as
# "this channel folder holds real tracks" when deciding whether a genre change
# has to move files. A duplicate literal rather than an import for the same
# reason cratebuilder.db keeps its own copy of the unresolved-URL prefix — this
# package never imports the monolith.
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".opus", ".webm",
                        ".flac", ".wav", ".ogg", ".aac"})

# The scan verdict a typed read-only failure carries, matched by isinstance so a
# future subclass inherits its verdict instead of having its message re-read.
# Mirrors the monolith's WL_SCAN_VERDICT_BY_YDL_ERROR.
SCAN_VERDICT_BY_YDL_ERROR = {
    YdlPermanent:    "needs_resolve",
    YdlOffline:      "offline",
    YdlUnclassified: "error",
}


def scan_verdict_for(exc):
    """The scan verdict for a typed read-only failure, or None if *exc* isn't
    one — the caller then reads the message instead."""
    for error_type, verdict in SCAN_VERDICT_BY_YDL_ERROR.items():
        if isinstance(exc, error_type):
            return verdict
    return None


def pending_entries(row):
    """The pending-new entries a scan stored on one watchlist row.

    The column holds exactly what crate.classify_scan_entries produced — a list
    of {id, title, url, upload_date} dicts — because that list is handed to
    db.update_watchlist_scan_result unchanged, by this module and by the
    tkinter app alike. Anything that isn't a dict is dropped rather than
    trusted; a row written by a future version must not be able to crash a
    download."""
    try:
        data = json.loads(row.get("pending_entries_json") or "[]")
    except (TypeError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def folder_has_audio(path):
    """True iff *path* is a directory holding at least one audio file at its
    top level — the monolith's _folder_has_audio, which is what tells a real
    channel folder from an empty placeholder."""
    if not path or not os.path.isdir(path):
        return False
    try:
        for name in os.listdir(path):
            if name.startswith("."):
                continue        # .artwork and other dot-hidden metadata
            if (os.path.splitext(name)[1].lower() in AUDIO_EXTS
                    and os.path.isfile(os.path.join(path, name))):
                return True
    except OSError:
        return False
    return False


def count_audio_files(path):
    """How many top-level audio files *path* holds; 0 for a missing folder."""
    try:
        return sum(1 for name in os.listdir(path)
                   if os.path.splitext(name)[1].lower() in AUDIO_EXTS)
    except OSError:
        return 0


def track_specs(row, save_dir, entries, row_id=None):
    """One TrackSpec per already-resolved entry — the Watch List's way into
    BatchRunner.run_tracks, which never probes.

    *entries* are the raw flat entries a scan classified (or a listing just
    returned), so the limiter, the premiere check and ChannelCrate ownership all
    read the same fields they would on a Main-tab batch. The channel URL is the
    listing URL yt-dlp is actually fed, which is what the monolith's Watch List
    run records on every downloads row it writes."""
    platform = row.get("platform") or "YouTube"
    genre = row.get("genre") or CrateLayout.NO_GENRE_VALUE
    listing_url = watch_fetch_url(platform, row.get("url") or "")
    name = row.get("display_name") or ""
    return [
        TrackSpec(
            row_id=row_id if row_id is not None else row.get("id"),
            url=entry_url(entry, platform),
            title=entry.get("title") or "",
            save_dir=save_dir,
            genre=genre,
            platform=platform,
            entry=entry,
            channel_name=name,
            channel_url=listing_url,
            channel_id=(row.get("channel_id") or None),
            suppress_channel_url=listing_url,
        )
        for entry in entries if isinstance(entry, dict)
    ]


def _plural(count, word):
    return f"{count} {word}{'s' if count != 1 else ''}"


def _daemon(target):
    threading.Thread(target=target, daemon=True).start()


class WatchlistOps:
    """Every Watch List action, headless.

    The Tk-free re-expression of the monolith's `_watchlist_scan_channel`,
    `_watchlist_download_new`, `_watchlist_edit_channel` and
    `_persist_resolved_channel` paths: the same order of operations and the same
    database writes, with every dialog replaced by a raised CBError and every
    log line and card redraw replaced by an event.

    One instance per service. The scan/download entry points (`run_scan`,
    `run_download`) are job bodies — the service runs them on its single
    "watchlist" job thread — while everything else answers on the calling
    thread. Cancellation mirrors the monolith's pair of flags: one run-wide
    event and a set of per-channel requests."""

    def __init__(self, settings, db_factory, emit, *, links_path,
                 session_factory=YdlSession, runner_factory=BatchRunner,
                 ffmpeg_dir=None, log_line=None, counts=None, flush=None,
                 spawn=_daemon, network_probe=network_is_reachable,
                 now=time.monotonic, timestamp=None,
                 claim_tag_writes=None, release_tag_writes=None):
        self._settings = settings
        self._db_factory = db_factory
        self._emit = emit
        self._links_path = links_path
        self._session_factory = session_factory
        self._runner_factory = runner_factory
        self._ffmpeg_dir = ffmpeg_dir
        self._log_line = log_line or (lambda text: None)
        self._counts = counts
        self._flush = flush or (lambda: None)
        self._spawn = spawn
        # Asked before a genre move's retag thread starts writing tags, and
        # released when it stops. The service answers False while a
        # db.repair_tags sweep holds the maintenance slot — two mutagen saves
        # to one MP3 truncate it. Defaulted to "always allowed" so a test (or
        # any caller with no maintenance jobs at all) needs no wiring.
        self._claim_tag_writes = claim_tag_writes or (lambda: True)
        self._release_tag_writes = release_tag_writes or (lambda: None)
        self._network_probe = network_probe
        self._now = now
        self._timestamp = timestamp or (lambda: time.strftime("%H:%M:%S"))
        self._lock = threading.Lock()
        self._cancel_all = threading.Event()
        self._cancel_cids = set()
        self._queue = []
        self._mode = None
        self._runner = None
        self._active_cid = None
        self._database = None

    # ── Collaborators ─────────────────────────────────────────────────────────
    def _db(self):
        """The database, opened once and kept. Watch List work always writes, so
        unlike the service's read-only probes this may bring the file into
        existence."""
        if self._database is None:
            self._database = self._db_factory()
        return self._database

    def _session(self):
        """A read-only yt-dlp session carrying the cookie policy as it stands
        right now — built per operation, like every other session in the app."""
        return self._session_factory(cookies=self._settings.cookie_config())

    def _row(self, cid):
        row = self._db().get_watchlist_channel(cid)
        if row is None:
            raise CBError("That channel is no longer in the Watch List.")
        return row

    def _name(self, cid):
        """A channel's display name for a log line, falling back to its id.

        Only the outer catch-all guards need this: every classified failure is
        reported from inside a path that already holds the row. A bare row id is
        exactly the wrong thing to show a user asking which channel broke."""
        try:
            row = self._db().get_watchlist_channel(cid)
        except Exception:
            row = None
        if row is None:
            return f"channel {cid}"
        return row.get("display_name") or row.get("url") or f"channel {cid}"

    def _folder(self, row, create=False):
        """The channel's crate folder. Pure naming via CrateLayout; the
        directory is created here (never under the database lock) because a
        channel the user is watching has a home whether or not it has downloaded
        anything yet."""
        platform = row.get("platform") or "YouTube"
        folder = CrateLayout.channel_dir(
            platform_dir(self._settings.get("base_dir"), platform),
            row.get("genre"), row.get("display_name"))
        if create:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                pass
        return folder

    # ── Events ────────────────────────────────────────────────────────────────
    def _line(self, level, text):
        """One line into the pinned scan log, in the plan's normative shape:
        {ts, level, text}.

        *level* is the log renderer's own class vocabulary (see LINE_LEVELS) so
        Task 9 can hand these straight to Task 5's line renderer with no
        translation shim; which KIND of line it is stays in the text's leading
        keyword (SCAN / HELD / ERROR / DONE), exactly as the design's scan log
        shows it. The timestamp is stamped here, at the moment the line is
        produced, rather than on arrival — a client that reconnects mid-run must
        not re-date the backlog."""
        self._emit("scan.line", {"ts": self._timestamp(), "level": level,
                                 "text": text})

    def _card(self, cid, **extra):
        row = self._db().get_watchlist_channel(cid)
        if row is not None:
            self._emit("watchlist.card", watchlist_card(row, **extra))

    def _patch_counts(self):
        if self._counts is None:
            return
        try:
            self._emit("state.patch", {"counts": self._counts()})
        except Exception:
            pass

    # ── Cancellation ──────────────────────────────────────────────────────────
    def _begin(self, mode, queue=()):
        """Start a run: drop every stale cancel request, exactly as a fresh
        user-initiated scan clears the monolith's leftover flag.

        The queue is seeded in the SAME locked section that publishes the mode.
        Published separately, a join landing between the two would be given a
        position in a queue the run is about to overwrite — acknowledged, then
        silently dropped."""
        with self._lock:
            self._cancel_all.clear()
            self._cancel_cids.clear()
            self._queue = list(queue)
            self._mode = mode

    def _end(self):
        """Close the run to joins. Cleared BEFORE the terminal flush, which
        forwards coalesced events to every subscriber and is long enough for a
        join to land in and be lost."""
        with self._lock:
            self._mode = None

    def _cancelled(self, cid):
        if self._cancel_all.is_set():
            return True
        with self._lock:
            return cid in self._cancel_cids

    def cancel(self, cid):
        """Stop the scan or download running on one channel; a channel still
        waiting its turn is dropped when the run reaches it."""
        with self._lock:
            self._cancel_cids.add(cid)
            runner = self._runner if self._active_cid == cid else None
        if runner is not None:
            runner.cancel()
        return {"cancelled": True}

    def cancel_all(self):
        """Stop the whole run after the current track."""
        self._cancel_all.set()
        with self._lock:
            runner = self._runner
        if runner is not None:
            runner.cancel()
        return {"cancelled": True}

    # ── Scans ─────────────────────────────────────────────────────────────────
    def run_scan(self, cids):
        """Scan each channel in turn — the "watchlist" job's scan body.

        Sequential rather than the monolith's throttled fan-out: there is no Tk
        main loop to keep responsive here, and one channel at a time is what
        keeps the scan log readable and the yt-dlp request rate honest."""
        self._begin("scan")
        total_new = 0
        scanned = 0
        try:
            for cid in cids:
                if self._cancel_all.is_set():
                    self._line(LINE_DEFAULT, "SCAN Cancelled — the remaining "
                                             "channels were skipped")
                    break
                try:
                    count = self._scan_channel(cid)
                except Exception as exc:
                    # One channel blowing up is that channel's failure, never
                    # a run that ends without saying so.
                    self._line(LINE_ERROR,
                               f"ERROR {self._name(cid)} — {str(exc)[:120]}")
                    count = None
                if count is not None:
                    scanned += 1
                    total_new += count
        finally:
            self._end()
            self._flush()
            self._line(LINE_DONE, f"DONE Scan complete — {total_new} new across "
                                  f"{_plural(scanned, 'channel')}")
            self._patch_counts()
        return {"new": total_new, "channels": scanned}

    def _scan_channel(self, cid):
        """One channel's scan. Returns how many new tracks it found, or None
        when it never got that far (unresolved, cancelled or failed)."""
        db = self._db()
        row = db.get_watchlist_channel(cid)
        if row is None:
            return None
        name = row.get("display_name") or row.get("url") or "Channel"

        # Never hand yt-dlp a folder-name URL or the unresolved:// sentinel —
        # that is what produced the HTTP 404s the monolith guards against.
        if is_unresolved_channel(row):
            db.update_watchlist_status(cid, "needs_resolve")
            self._line(LINE_ERROR, f"ERROR {name} — channel id unresolved, "
                                   f"using folder name")
            self._card(cid)
            return None
        if self._cancelled(cid):
            return self._scan_cancelled(db, cid, name)

        db.update_watchlist_status(cid, "scanning")
        self._card(cid)
        self._line(LINE_DEFAULT, f"SCAN {name} — enumerating uploads…")

        platform = row.get("platform") or "YouTube"
        # The same encoded listing URL a Watch List download feeds yt-dlp, so
        # scan and download crawl the channel identically.
        url = watch_fetch_url(platform, row.get("url") or "")
        try:
            entries = [e for e in self._session().list_channel(url)
                       if isinstance(e, dict)]
        except Exception as exc:
            self._scan_failed(db, row, name, exc)
            return None
        if self._cancelled(cid):
            return self._scan_cancelled(db, cid, name)

        # Everything below this line is filesystem and pure work first, then one
        # database write — no listdir or network call ever happens while the
        # pooled connection's lock is held.
        folder = self._folder(row, create=True)
        folder_keys = ChannelCrate.index_folder(folder)
        now_ts = int(time.time())
        policy = self._settings.download_policy()
        limit_sec = (int(policy.limit_minutes or 0) * 60
                     if policy.limit_enabled else None)
        suppressed = db.get_suppressed_reasons(platform)
        downloaded = db.get_downloaded_video_ids()
        classified = classify_scan_entries(
            entries, is_downloaded=downloaded.__contains__,
            folder_keys=folder_keys, limit_sec=limit_sec, platform=platform,
            is_unavailable=suppressed.get, now=now_ts)

        new_entries = classified["new"]
        upcoming = classified.get("upcoming") or []
        unavailable = classified.get("unavailable") or []
        self._backfill(db, row, platform, classified["on_disk"], now_ts)
        self._stamp_sidecar(row, folder)

        count = len(new_entries)
        db.update_watchlist_scan_result(
            cid, timestamp=int(time.time()), pending_count=count,
            pending_entries=new_entries,
            status="found" if count else "idle")

        self._line(LINE_DEFAULT, f"SCAN {name} — {len(entries)} entries, "
                                 f"{count} new since last scan")
        if upcoming:
            self._line(LINE_HELD, f"HELD {name} — "
                                  f"{_plural(len(upcoming), 'premiere')} held back")
        if unavailable:
            self._line(LINE_DEFAULT, f"SCAN {name} — "
                                     f"{len(unavailable)} permanently unavailable, "
                                     f"skipped")
        self._card(cid)
        return count

    def _scan_cancelled(self, db, cid, name):
        db.update_watchlist_status(cid, "idle")
        self._line(LINE_DEFAULT, f"SCAN {name} — scan cancelled")
        self._card(cid)
        return None

    def _scan_failed(self, db, row, name, exc):
        """Decide whether the LINK is at fault or the NETWORK was, and write the
        row accordingly — the monolith's rule, unchanged.

        A transient failure must leave the row exactly as it was (same URL, same
        pending tracks, no Fix Link button) or one offline scan strands the
        whole watch list. A yt-dlp failure arrives already judged; anything else
        that broke mid-scan is read from its message, and a permanent-looking
        verdict reached with no route to the network is downgraded, because a
        captive portal answers 404 for everything."""
        cid = row.get("id")
        err = str(exc)[:120]
        verdict = scan_verdict_for(exc)
        if verdict is None:
            verdict = classify_scan_error(err)
            if verdict == "needs_resolve" and not self._network_probe():
                verdict = "offline"
        if verdict == "needs_resolve":
            db.update_watchlist_scan_result(
                cid, timestamp=int(time.time()), pending_count=0,
                pending_entries=[], status="needs_resolve", last_error=err)
            message = (f"ERROR {name} — channel link looks dead, use Fix Link "
                       f"({err})")
        else:
            db.update_watchlist_status(cid, verdict, err)
            message = (f"ERROR {name} — skipped, no network. Link kept; it'll "
                       f"scan next time you're online."
                       if verdict == "offline" else
                       f"ERROR {name} — {err}")
        self._line(LINE_ERROR, message)
        self._card(cid)

    def _backfill(self, db, row, platform, on_disk, now_ts):
        """Record already-on-disk (legacy) tracks so future scans dedup exactly
        by video_id. Entries with no id can't be keyed, so they are simply
        hidden — matching the monolith."""
        rows = [{
            "video_id":     item["id"],
            "title":        item["title"],
            "channel_name": row.get("display_name") or "",
            "channel_url":  row.get("url") or "",
            "channel_id":   row.get("channel_id"),
            "platform":     platform,
            "genre":        row.get("genre") or CrateLayout.NO_GENRE_VALUE,
            "file_path":    item["file_path"],
            "upload_date":  item["upload_date"],
            "ts":           now_ts,
            "bitrate":      "",
        } for item in on_disk if item.get("id")]
        if rows:
            db.backfill_downloads(rows)

    def _stamp_sidecar(self, row, folder):
        """Keep the channel folder's cratebuilder.json naming the identity the
        watchlist row holds. Best-effort and additive — write_channel_sidecar
        overlays onto whatever is already there and returns False rather than
        raising, so this can never break a scan."""
        if not folder or not os.path.isdir(folder):
            return False
        try:
            return write_channel_sidecar(
                folder,
                channel_id=row.get("channel_id"),
                channel_url=row.get("url") or None,
                display_name=row.get("display_name"),
                platform=row.get("platform") or "YouTube",
                genre=row.get("genre") or CrateLayout.NO_GENRE_VALUE)
        except OSError:
            return False

    # ── Downloads ─────────────────────────────────────────────────────────────
    def enqueue(self, cid, force=False):
        """Join a channel onto the download run that is already going.

        The queue is read live by run_download, so an appended channel simply
        runs after the ones already listed — the headless twin of the monolith's
        _watchlist_append_to_running. A channel already queued is never queued
        twice; its existing position comes back instead.

        *force* rides on the QUEUE ENTRY, not the run: a Download New pressed
        while a Force Download is in flight must fetch that channel's pending
        entries with the Watch List's own skip rule, not re-take its whole
        catalogue because of what some other channel asked for.

        Returns the 1-based position, or None when there is no download run to
        join (a scan is running, or the run finished between the caller's check
        and this call) — the caller then starts a run of its own and lets the
        job registry settle the race."""
        with self._lock:
            if self._mode != "download":
                return None
            for index, (queued, _force) in enumerate(self._queue):
                if queued == cid:
                    return index + 1
            self._queue.append((cid, bool(force)))
            return len(self._queue)

    def run_download(self, cids, force=False):
        """Download each channel in turn — the "watchlist" job's download body.

        *force* is the mode the channels named in *cids* were requested with; it
        re-processes a channel's whole catalogue with skipping off, where the
        default fetches only the pending entries the last scan stored and lets
        the Watch List's own skip rule decide what is already owned. It is
        carried per queue entry, so a channel that joins later keeps the mode it
        was asked for."""
        self._begin("download", [(cid, bool(force)) for cid in cids])
        index = 0
        downloaded = 0
        try:
            while True:
                with self._lock:
                    if self._cancel_all.is_set() or index >= len(self._queue):
                        break
                    cid, entry_force = self._queue[index]
                index += 1
                try:
                    downloaded += self._download_channel(cid, force=entry_force)
                except Exception as exc:
                    self._line(LINE_ERROR,
                               f"ERROR {self._name(cid)} — {str(exc)[:120]}")
        finally:
            self._end()
            self._flush()
            self._line(LINE_DONE, f"DONE Download complete — "
                                  f"{_plural(downloaded, 'track')} downloaded")
            self._patch_counts()
        return {"downloaded": downloaded}

    def _download_channel(self, cid, force=False):
        """One channel end to end. Returns how many tracks it downloaded."""
        db = self._db()
        row = db.get_watchlist_channel(cid)
        if row is None or self._cancelled(cid):
            return 0
        name = row.get("display_name") or row.get("url") or "Channel"
        if is_unresolved_channel(row):
            self._line(LINE_ERROR, f"ERROR {name} — link unresolved; fix the link "
                                   f"before downloading")
            return 0

        folder = self._folder(row, create=True)
        if force:
            url = watch_fetch_url(row.get("platform") or "YouTube",
                                  row.get("url") or "")
            try:
                entries = [e for e in self._session().list_channel(url)
                           if isinstance(e, dict)]
            except Exception as exc:
                self._line(LINE_ERROR, f"ERROR {name} — {str(exc)[:120]}")
                return 0
        else:
            entries = pending_entries(row)
        if not entries:
            self._line(LINE_DEFAULT, f"SCAN {name} — nothing pending")
            return 0

        specs = track_specs(row, folder, entries, row_id=cid)
        db.set_watchlist_download_started([cid], int(time.time()))
        db.update_watchlist_status(cid, "downloading")
        state = {"done": 0, "total": len(specs), "percent": 0,
                 "title": "", "title_percent": None, "at": None}
        self._card(cid, progress=dict(state))
        self._line(LINE_DEFAULT, f"SCAN {name} — downloading "
                                 f"{_plural(len(specs), 'track')}…")

        runner = self._runner_factory(
            self._settings, db, self._channel_emit(cid, state),
            session_factory=self._session_factory,
            ffmpeg_dir=self._ffmpeg_dir, log_line=self._log_line,
            # Stamps every progress frame this run emits, so the Main tab's bar
            # can tell them from its own concurrent batch.
            job=WATCHLIST_JOB)
        with self._lock:
            self._runner, self._active_cid = runner, cid
        try:
            tally = runner.run_tracks(
                specs,
                skip_mode=None if force else SkipMode.WATCH_LIST,
                ignore_skip_existing=force)
        except Exception:
            # "downloading" is only meaningful while this call owns the row —
            # a raise must never leave a card stuck showing it.
            db.update_watchlist_status(cid, "idle")
            raise
        finally:
            with self._lock:
                self._runner, self._active_cid = None, None

        # Pending is cleared only when the run actually got through the list
        # without a failure: anything left unfinished stays pending so the next
        # Download New retries it instead of losing it.
        if not force and not tally["stopped"] and not tally["errors"]:
            db.clear_pending_for_channel(cid)
        db.refresh_watchlist_total(cid)
        db.update_watchlist_status(cid, "idle")
        self._flush()
        self._card(cid)
        self._line(LINE_DEFAULT if not tally["errors"] else LINE_ERROR,
                   f"SCAN {name} — {tally['downloaded']} downloaded, "
                   f"{tally['skipped']} skipped, {tally['errors']} failed")
        return tally["downloaded"]

    def _channel_emit(self, cid, state):
        """A BatchRunner emit sink that forwards every event untouched and keeps
        the channel's card in step with it.

        Everything the web UI needs still arrives as a normal event — this only
        adds the per-channel card the design's downloading state renders."""
        def emit(type, payload):
            self._emit(type, payload)
            if type == "progress.overall":
                state["done"] = payload.get("done", state["done"])
                state["total"] = payload.get("total", state["total"])
                state["percent"] = payload.get("percent", 0)
                self._card_progress(cid, state, force=True)
            elif type == "progress.current":
                title = payload.get("title") or ""
                changed = title != state["title"]
                state["title"] = title
                state["title_percent"] = payload.get("percent")
                self._card_progress(cid, state, force=changed)
        return emit

    def _card_progress(self, cid, state, force=False):
        """Redraw the card, at most CARD_PROGRESS_INTERVAL apart unless a track
        settled or the current title changed."""
        now = self._now()
        if not force and state["at"] is not None \
                and now - state["at"] < CARD_PROGRESS_INTERVAL:
            return
        state["at"] = now
        self._card(cid, progress={k: v for k, v in state.items() if k != "at"})

    # ── Channel management ────────────────────────────────────────────────────
    def add(self, url, genre=None):
        """Track a new channel. The identity probe only names it — a channel
        yt-dlp cannot read right now is still added, under its URL, exactly as
        the tkinter Add dialog does when its auto-fetch fails."""
        url = (url or "").strip()
        if not url:
            raise CBError("Paste a channel URL first.")
        genre = genre or CrateLayout.NO_GENRE_VALUE
        platform = util.detect_platform(url)
        db = self._db()
        display, channel_id = "", channel_id_from_url(url) or ""
        try:
            identity = self._session().probe_identity(url)
        except Exception as exc:
            self._line(LINE_ERROR, f"ERROR {url} — couldn't read the channel "
                                   f"({util.condense_error(str(exc))})")
        else:
            display = identity.display_name or ""
            channel_id = identity.channel_id or channel_id
        existing = util.find_matching_watchlist_row(
            db.get_all_watchlist_channels(), url, channel_id=channel_id,
            platform=platform)
        if existing is not None:
            raise CBError(f"“{existing.get('display_name') or url}” is already "
                          f"in the Watch List.")
        new_id = db.add_watchlist_channel(
            url=url, display_name=display or url, platform=platform,
            genre=genre, auto_added=False, channel_id=channel_id or None)
        if new_id is None:
            raise CBError("That channel is already in the Watch List.")
        row = db.get_watchlist_channel(new_id) or {}
        self._mirror_link(row, url, channel_id)
        self._line(LINE_DONE, f"DONE Added {display or url}")
        self._card(new_id)
        self._patch_counts()
        return {"channel_id": new_id}

    def remove(self, cid):
        """Drop the watchlist row. Files and folders are untouched."""
        row = self._row(cid)
        self._db().remove_watchlist_channel(cid)
        self._line(LINE_DONE, f"DONE Removed {row.get('display_name') or cid}")
        self._patch_counts()
        return {"ok": True}

    def details(self, cid):
        """The three facts the Edit dialog needs that a card cannot carry.

        Read-only, answered on the calling thread when the dialog opens, and
        deliberately NOT folded into watchlist_card: a card is re-emitted
        several times a second for every channel in a download run, and each of
        these answers costs a COUNT query or a listdir over a channel folder
        holding thousands of files. The Edit dialog opens once.

        - folder:      where this channel's tracks live (pure CrateLayout
                       naming, never created here).
        - tracks:      how many audio files a genre change would move — the
                       count the design's move confirmation names.
        - unavailable: how many permanently-failed tracks Forget would drop.
        """
        row = self._row(cid)
        folder = self._folder(row)
        url = canonical_channel_url(row.get("url") or "")
        return {"folder": folder,
                "tracks": count_audio_files(folder),
                "unavailable": self._db().count_unavailable_for_channel(url)}

    def forget_unavailable(self, cid):
        """Forget this channel's permanently-unavailable tracks, so the next
        scan offers them again and the next download re-attempts them."""
        row = self._row(cid)
        url = canonical_channel_url(row.get("url") or "")
        removed = self._db().forget_unavailable_for_channel(url)
        self._line(LINE_DEFAULT, f"SCAN {row.get('display_name')} — forgot "
                                 f"{_plural(removed, 'unavailable track')}")
        return {"removed": removed}

    def edit(self, cid, url=None, genre=None):
        """Change a channel's genre and/or its link.

        The order is the monolith's _watchlist_edit_channel save path, which is
        load-bearing: the genre move happens FIRST and completely (folder move,
        then the downloads rewrite, then the out-of-database mirrors, then the
        genre tags inside the files), and the URL is only re-pointed once that
        has settled — so a failed link resolve can never leave a half-moved
        channel behind.

        A refusal is reported before it is raised: the caller gets the CBError,
        but every other client learns about it too, and the card is re-emitted
        from whatever the database now holds. Raising straight out would leave
        every other viewer showing the state the edit did not reach."""
        row = self._row(cid)
        name = row.get("display_name") or row.get("url") or "Channel"
        result = {"ok": True}
        picked = (genre or "").strip()
        try:
            if picked and picked != (row.get("genre")
                                     or CrateLayout.NO_GENRE_VALUE):
                result["genre"] = self._change_genre(row, picked)
                row = self._row(cid)
            if url is not None:
                new_url = (url or "").strip()
                if new_url and new_url != (row.get("url") or ""):
                    result["link"] = self._apply_url(row, new_url)
        except CBError as exc:
            self._line(LINE_ERROR, f"ERROR {name} — {exc}")
            self._card(cid)
            raise
        self._card(cid)
        return result

    def _change_genre(self, row, picked):
        """Move a channel between genres: folder, then database, then mirrors.

        A destination that already exists is refused rather than merged — the
        collision is the user's to resolve. If the database write fails while
        files have already moved, the move is rolled back so disk and database
        never drift apart.

        Success is judged by READING THE ROW BACK, not by the rowcount
        move_channel_downloads returns. That rowcount counts `downloads` rows,
        and a channel folder holding audio with no matching downloads rows is a
        perfectly ordinary state — legacy files, a rebuilt database, tracks the
        user dropped in by hand. The monolith treats that zero as a failure
        (DJ-CrateBuilder_v1.3.py:12573) and rolls the folder back while the
        genre it just committed stands, which is exactly the drift the rollback
        exists to prevent; this does not port that. The write sets
        watchlist.genre in the same transaction as the downloads rewrite, so the
        row's own genre is the honest answer to "did it commit?" — and
        move_channel_downloads never raises, so there is nothing else to read.
        """
        cid = row.get("id")
        db = self._db()
        platform = row.get("platform") or "YouTube"
        old_genre = row.get("genre") or CrateLayout.NO_GENRE_VALUE
        src = self._folder(row)
        dst = self._folder(dict(row, genre=picked))
        if folder_has_audio(src):
            if os.path.exists(dst):
                raise CBError(f"Can't move — a folder named "
                              f"“{os.path.basename(dst)}” already exists under "
                              f"“{picked}”. Resolve the collision manually, "
                              f"then try again.")
            tracks = count_audio_files(src)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            except OSError as exc:
                raise CBError(f"Unable to move the channel folder: {exc}")
            rows = db.move_channel_downloads(wl_id=cid, old_dir=src,
                                             new_dir=dst, new_genre=picked)
            if not self._genre_committed(cid, picked):
                self._rollback_move(cid, src, dst, picked)
            self._repoint(row, platform, old_genre, picked, folder=dst)
            retagging = self._retag(dst, picked,
                                    row.get("display_name") or "")
            return {"moved": tracks, "rows": rows, "retagging": retagging}

        # No tracks to move. An empty placeholder folder still travels — there
        # is nothing to lose — so the old genre isn't left holding a hollow
        # shell and its sidecar.
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            except OSError:
                pass
        db.update_watchlist_channel_fields(cid, genre=picked)
        self._repoint(row, platform, old_genre, picked,
                      folder=dst if os.path.isdir(dst) else None)
        return {"moved": 0, "rows": 0, "retagging": 0}

    def _genre_committed(self, cid, picked):
        """Did the move's transaction land? Read the row's own genre back."""
        row = self._db().get_watchlist_channel(cid)
        return row is not None and (row.get("genre") or "") == picked

    def _rollback_move(self, cid, src, dst, picked):
        """Put the files back where the (uncommitted) database still says they
        are, then refuse the edit. Always raises.

        Nothing to undo on the database side — the transaction did not commit,
        which is how we got here. If the files cannot be put back, the two would
        disagree with the folder at *dst* and the row at the old genre, so the
        row is dragged to *picked* instead: whichever way this goes, the genre
        column and the folder location agree when it returns."""
        try:
            shutil.move(dst, src)
        except OSError as exc:
            self._db().update_watchlist_channel_fields(cid, genre=picked)
            raise CBError(
                f"The database could not be updated and the folder could not "
                f"be put back ({exc}). The channel is filed under “{picked}” "
                f"on disk; its download history still names the old folder — "
                f"run Rebuild Database to reconcile it.")
        raise CBError("The folder move was rolled back because the database "
                      "could not be updated. Nothing was changed.")

    def _retag(self, folder, genre, name):
        """Realign the moved folder's genre tags on a worker thread.

        The database and the files agree the moment the move lands, but the tag
        inside each file still names the genre it left. Off the calling thread
        because a large channel would otherwise hold the RPC open for as long as
        it takes to rewrite every tag — the monolith defers it behind a progress
        dialog for the same reason. Returns how many tracks were queued.

        Refused, with a line in the scan log and no thread started, when a
        maintenance tag sweep already owns the right to write tags. Both paths
        end in `mutagen`'s ID3.save(), which rewrites the file in place and
        shifts the audio when the tag grows — two of those interleaved on one
        MP3 truncates it. The monolith refuses this exact pair the same way
        (`_watchlist_retag_genre` checks `_tag_repair_active`,
        DJ-CrateBuilder_v1.3.py:13578), and the move itself has already
        committed either way: only the tags are skipped."""
        tag_genre = "" if genre == CrateLayout.NO_GENRE_VALUE else genre
        total = genrefix.count_channel_tracks(folder)
        if not total:
            return 0
        if not self._claim_tag_writes():
            self._line(LINE_ERROR,
                       f"{name}: genre tags not updated — a tag repair is "
                       f"already running. Use Settings ▸ Repair Track Tags "
                       f"later.")
            return 0

        def work():
            fixed = 0
            try:
                for path, value in genrefix.iter_channel_tracks(folder,
                                                                tag_genre):
                    changed, _filled = genrefix.repair_track(path, value)
                    if changed:
                        fixed += 1
                self._line(LINE_DONE, f"DONE {name} — "
                                      f"{_plural(fixed, 'genre tag')} updated")
            finally:
                # Held for the whole sweep: releasing early would let a
                # db.repair_tags run start on top of the files still being
                # written.
                self._release_tag_writes()
        self._spawn(work)
        return total

    def _repoint(self, row, platform, old_genre, new_genre, folder=None):
        """Carry a channel's out-of-database identity across a genre change.

        The link store is keyed by Platform/Genre/DisplayName, so a genre move
        has to re-file the entry and drop the old key or the stale one lingers
        and Fix Link prefills from the wrong record. The folder's
        cratebuilder.json travels with the files but still names the old genre,
        so it is rewritten too. Both are best-effort mirrors — a failure here
        must never undo a move the database has already committed."""
        display = row.get("display_name") or ""
        url = row.get("url") or ""
        self._mirror_link(dict(row, genre=new_genre), url,
                          row.get("channel_id"))
        if (old_genre or CrateLayout.NO_GENRE_VALUE) != new_genre:
            try:
                cb_links.remove_link(self._links_path, platform, old_genre,
                                     display)
            except OSError:
                pass
        if folder:
            try:
                write_channel_sidecar(
                    folder, channel_id=row.get("channel_id"),
                    channel_url=url or None, display_name=display,
                    platform=platform, genre=new_genre)
            except OSError:
                pass

    def _mirror_link(self, row, url, channel_id=None):
        """Mirror a channel's URL into the durable JSON link store. Never
        raises — a mirror write must never break a resolve."""
        if not url:
            return
        try:
            cb_links.save_link(
                self._links_path,
                platform=row.get("platform") or "YouTube",
                genre=row.get("genre") or CrateLayout.NO_GENRE_VALUE,
                display_name=row.get("display_name") or "",
                url=url, channel_id=channel_id or None,
                updated=util.today_yyyymmdd())
        except OSError:
            pass

    # ── Link resolution ───────────────────────────────────────────────────────
    def _apply_url(self, row, new_url):
        """Point a watchlist row at a new channel/playlist URL.

        A /channel/UC… URL is canonicalised immediately; a handle or playlist
        URL is stored as typed and then probed for the underlying channel id, so
        the folder sidecar can still be stamped. The probe runs inline rather
        than in the background the monolith uses — this call answers an RPC, and
        a caller that never learns the outcome is worse than one that waits."""
        cid = row.get("id")
        db = self._db()
        direct = channel_id_from_url(new_url)
        if direct:
            return self._persist_resolved(row, direct)
        db.update_watchlist_channel_fields(cid, url=new_url, status="idle",
                                           last_error=None)
        self._mirror_link(row, new_url)
        try:
            identity = self._session().probe_identity(new_url)
        except Exception as exc:
            self._line(LINE_ERROR, f"ERROR {row.get('display_name')} — couldn't "
                                   f"resolve the channel id "
                                   f"({util.condense_error(str(exc))})")
            return {"resolved": False, "url": new_url}
        ucid = identity.channel_id or channel_id_from_url(identity.channel_url)
        if not ucid:
            return {"resolved": False, "url": new_url}
        # Keep the user's URL (it may be a playlist) but record the channel id.
        return self._persist_resolved(row, ucid, handle=identity.handle,
                                      url=new_url)

    def _persist_resolved(self, row, channel_id, handle="", url=None):
        """Commit a resolved identity: the row, then the link store, then the
        folder sidecar.

        A resolved URL already owned by ANOTHER watchlist row is a duplicate and
        is refused by name — the monolith offers to delete the redundant row
        here, which is a decision no headless caller may take on the user's
        behalf."""
        db = self._db()
        store_url = url or channel_url_from_id(channel_id)
        owner = db.get_watchlist_channel_by_url(store_url)
        if owner is not None and owner.get("id") != row.get("id"):
            raise CBError(
                f"“{row.get('display_name') or 'This entry'}” is the same "
                f"channel you already track as "
                f"“{owner.get('display_name') or 'another entry'}”.")
        fields = {"url": store_url, "status": "idle", "last_error": None}
        if channel_id:
            fields["channel_id"] = channel_id
        if not db.update_watchlist_channel_fields(row.get("id"), **fields):
            db.update_watchlist_status(row.get("id"), "error",
                                       last_error="Could not save resolved link")
            raise CBError("Couldn't save the link — it may duplicate another "
                          "entry.")
        resolved = dict(row, url=store_url, channel_id=channel_id or None)
        self._mirror_link(resolved, store_url, channel_id)
        folder = self._folder(resolved, create=True)
        try:
            write_channel_sidecar(
                folder, channel_id=channel_id, channel_url=store_url,
                handle=handle, display_name=row.get("display_name"),
                platform=row.get("platform") or "YouTube",
                genre=row.get("genre") or CrateLayout.NO_GENRE_VALUE)
        except OSError:
            pass
        return {"resolved": True, "url": store_url,
                "channel_id": channel_id or None}

    def resolve_candidates(self, cid, max_results=3):
        """Search for the channel this row is meant to be, by its folder name.

        Returns the contract's bare list of candidates, each carrying one added
        field: a duplicate_of marker when the candidate would collide with
        another entry, so the Fix Link dialog can disable it with the reason
        inline rather than failing on save."""
        row = self._row(cid)
        name = (row.get("display_name") or "").strip()
        if not name:
            raise CBError("This entry has no name to search for.")
        platform = row.get("platform") or "YouTube"
        session = self._session()
        try:
            if platform == "SoundCloud":
                hits = session.search_soundcloud_tracks(name)
                candidates = [
                    {"title": c["title"], "channel_id": "", "url": c["url"],
                     "handle": c["handle"], "followers": None,
                     "confidence": c["confidence"]}
                    for c in util.merge_soundcloud_candidates(
                        hits, [], max_results)]
            else:
                candidates = session.search_channels(name,
                                                     max_results=max_results)
        except Exception as exc:
            raise CBError(f"Couldn't search for “{name}”: "
                          f"{util.condense_error(str(exc))}")
        others = [r for r in self._db().get_all_watchlist_channels()
                  if r.get("id") != cid]
        for candidate in candidates:
            owner = util.find_matching_watchlist_row(
                others, candidate.get("url"),
                channel_id=candidate.get("channel_id"), platform=platform)
            candidate["duplicate_of"] = (
                {"id": owner.get("id"),
                 "name": owner.get("display_name") or ""}
                if owner is not None else None)
        return candidates

    def resolve_apply(self, cid, resolved_url=None, channel_id=None):
        """Commit one Fix Link choice — sidecar, link store and database row."""
        row = self._row(cid)
        url = (resolved_url or "").strip()
        ucid = (channel_id or "").strip() or channel_id_from_url(url) or ""
        if not url and ucid:
            url = channel_url_from_id(ucid)
        if not url:
            raise CBError("Pick a channel to link to.")
        result = self._persist_resolved(row, ucid, url=url)
        self._line(LINE_DONE, f"DONE Channel set: {row.get('display_name')}")
        self._card(cid)
        return result

"""Headless batch orchestration: queue rows in, download progress events out."""
import random
import threading
import time
from datetime import datetime

from cratebuilder import activitylog
from cratebuilder import artwork as cb_artwork
from cratebuilder import tagging, util
# TrackSpec and entry_url are re-exported: they are this module's input shape,
# and every later caller reaches them through cratebuilder.batchrun.
from cratebuilder.batchresolve import (ResolvedRow, RowResolver,  # noqa: F401
                                       TrackSpec, entry_url,
                                       fetch_failure_reason)
from cratebuilder.crate import (ChannelCrate, CrateLayout, SkipDecision,
                                SkipMode, is_unreleased_entry, skip_decision)
from cratebuilder.download import (REASON_WIDTH, SkipOrCancel, TrackDownloader,
                                   TrackPlan, download_with)
from cratebuilder.ydl import YdlSession


# Which job category a progress frame belongs to. One job per category runs at a
# time, but a Main-tab batch and a Watch List download can run TOGETHER — both
# drive a BatchRunner, and without this field the two progress streams are
# indistinguishable and each overwrites the other's bar.
DEFAULT_JOB = "batch"


# ── Pure helpers ──────────────────────────────────────────────────────────────
def resolve_sleep_range(policy):
    """The (min, max) throttle seconds a DownloadPolicy asks for, or None when
    throttling is off. Auto reads the preset table; Manual reads the pair, with
    max never below min."""
    if not policy.sleep_enabled:
        return None
    if policy.sleep_mode == "Auto":
        return util.THROTTLE_PRESETS.get(policy.sleep_preset, (1, 5))
    s_min, s_max = int(policy.sleep_min), int(policy.sleep_max)
    return (s_min, max(s_min, s_max))


def pick_session_ua(policy, rng=None):
    """One User-Agent for a whole batch session, or None when rotation is off."""
    if not policy.rotate_ua:
        return None
    return (rng or random).choice(util.USER_AGENT_POOL)


def eta_text(durations, remaining):
    """"~N min left" from a rolling average of per-track seconds, "" when
    nothing has finished yet or nothing is left."""
    if not durations or remaining <= 0:
        return ""
    seconds = (sum(durations) / len(durations)) * remaining
    if seconds < 90:
        return f"~{max(1, int(round(seconds)))} sec left"
    return f"~{max(1, int(round(seconds / 60)))} min left"


# ── Sink ──────────────────────────────────────────────────────────────────────
class _EventSink:
    """A TrackDownloader Sink that publishes progress.current frames.

    No throttling of its own: the service's Coalescer is what rate-limits these,
    so every frame yt-dlp reports is handed over."""

    def __init__(self, emit, title, job=DEFAULT_JOB):
        self._emit = emit
        self._job = job
        self.title = title
        self.percent = None
        self.speed_text = ""
        self.bitrate_text = ""

    def started(self, title):
        self.title = title
        self.percent = 0
        self._frame()

    def progress(self, percent=None, speed_text=""):
        self.percent = percent
        self.speed_text = speed_text or ""
        self._frame()

    def bitrate_detected(self, source_abr, target_kbps):
        self.bitrate_text = f"{int(source_abr)}k → {target_kbps}k"
        self._frame()

    def title_corrected(self, title):
        self.title = title
        self._frame()

    def finished(self):
        self.percent = 100
        self.speed_text = ""
        self._frame()

    def _frame(self):
        self._emit("progress.current", {
            "title": self.title, "percent": self.percent,
            "speed_text": self.speed_text, "bitrate_text": self.bitrate_text,
            "job": self._job,
        })


# ── BatchRunner ───────────────────────────────────────────────────────────────
class BatchRunner:
    """One batch of queue rows, downloaded headlessly.

    The Tk-free re-expression of the monolith's `_batch_worker` /
    `_process_one_url` pair: the same per-entry order (limiter, then ownership,
    then the download), the same counters, the same activity-log lines — with
    every UI effect replaced by an event.

    Two entry points, deliberately. `run(rows)` hands each queue row to a
    RowResolver and passes the tracks that come back to `run_tracks`, which is
    the whole per-track loop and nothing else. A caller that already holds
    resolved entries (the Watch List, which scanned them minutes ago) calls
    `run_tracks` directly and never probes.

    Controls are set from whatever thread the RPC arrives on while `run` is on
    the job thread: pause/cancel are Events and the per-row skip set is
    lock-guarded. They keep the tkinter tooltips' promises — pause holds
    *before* the next track, cancel stops after the current one and keeps what
    landed, and a skip interrupts the running row on the spot through the
    downloader's canceller."""

    POLL_SECONDS = 0.05
    ETA_WINDOW = 5

    def __init__(self, settings, db, emit, *, session_factory=YdlSession,
                 downloader_factory=TrackDownloader, ffmpeg_dir=None,
                 log_line=None, counts=None, flush=None, runner=None,
                 write_sidecar=None, now=time.monotonic, job=DEFAULT_JOB):
        self._job = job
        self._settings = settings
        self._db = db
        self._emit = emit
        self._session_factory = session_factory
        self._resolver = RowResolver(settings, db, session_factory,
                                     write_sidecar=write_sidecar)
        self._downloader_factory = downloader_factory
        self._ffmpeg_dir = ffmpeg_dir
        self._log_line = log_line or (lambda text: None)
        self._counts = counts
        self._flush = flush or (lambda: None)
        self._runner = runner or download_with
        self._now = now
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._skips = set()
        self._crates = {}
        self._counted = set()
        self._session_ua = None
        self._batch_open = False
        self._reset(0)

    # ── Controls ──────────────────────────────────────────────────────────────
    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def cancel(self):
        """Stop after the current track. Clearing the pause gate is what lets a
        cancel pressed while paused actually land."""
        self._cancel.set()
        self._pause.clear()

    def skip_row(self, row_id):
        with self._lock:
            self._skips.add(row_id)

    @property
    def paused(self):
        return self._pause.is_set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _skip_requested(self, row_id):
        if row_id is None:
            return False
        with self._lock:
            return row_id in self._skips

    def _wait_while_paused(self, row_id):
        """Hold before the next track while paused. False when the wait ended
        in a cancel or a skip rather than a resume."""
        while self._pause.is_set():
            if self._cancel.is_set() or self._skip_requested(row_id):
                return False
            time.sleep(self.POLL_SECONDS)
        return not (self._cancel.is_set() or self._skip_requested(row_id))

    # ── The outer loop ────────────────────────────────────────────────────────
    def run(self, rows):
        """Download every queue row in *rows*, top to bottom.

        *rows* may GROW while this runs — the service appends to the same list
        when a row is added mid-batch — so the length is read live rather than
        captured. It never shrinks: the service refuses remove/move/clear for
        the duration.

        Always ends in a terminal event. The loop runs inside one guard because
        a raise anywhere in it would otherwise leave the frontend showing a
        batch that is running and a Cancel button that answers "no download is
        running" — the tkinter worker wraps itself the same way."""
        self._reset(0)
        self._batch_open = True
        try:
            self._loop(rows)
        except Exception as exc:
            self._fatal(exc)
        finally:
            self._batch_open = False
        return self._finish()

    def _loop(self, rows):
        seen = 0
        self._log_line(activitylog.separator(
            f"DOWNLOAD STARTED  —  {len(rows)} "
            f"URL{'s' if len(rows) != 1 else ''}"))
        index = 0
        while not self._cancel.is_set():
            while seen < len(rows):
                self._admit(rows[seen])
                seen += 1
            if index == 0:
                self._overall()
            if index >= len(rows):
                break
            self._run_row(rows[index], index)
            index += 1

    def _fatal(self, exc):
        """The batch driver's last guard: an unexpected raise is one error and
        an activity-log line, never a batch that ends without saying so."""
        reason = util.condense_error(str(exc), REASON_WIDTH)
        self._errors += 1
        self._log_line(activitylog.error("Batch", "", f"{reason}: {exc}"))

    def _run_row(self, row, index):
        """One queue row: probe it, expand it, download it."""
        row_id = row.get("id")
        url = (row.get("url") or "").strip()
        title = row.get("title") or url
        if row.get("state") == "skipped" or self._skip_requested(row_id):
            # Only a row that was counted gives a count back: one already marked
            # skipped when the batch started never entered the total at all.
            self._withdraw(row)
            self._log_line(activitylog.skipped(title, "", "skipped by user"))
            self._overall()
            self._row(row, index, "skipped", "skipped")
            return

        self._row(row, index, "active", "fetching…")
        try:
            resolved = self._resolver.resolve(row)
        except Exception as exc:
            # A metadata failure is per-URL (bot check, age gate, removed,
            # region block) — this row fails and the batch carries on.
            reason = fetch_failure_reason(exc)
            self._errors += 1
            self._done += 1
            self._log_line(activitylog.error(title, url, reason))
            self._overall()
            self._row(row, index, "error", reason)
            return

        tracks = resolved.tracks
        if not tracks:
            self._withdraw(row)
            self._overall()
            self._row(row, index, "skipped", "nothing found")
            return

        self._admit(row)                    # a no-op unless the row grew in
        self._total += len(tracks) - 1      # after the batch started
        tally = self.run_tracks(tracks, row=row, index=index)
        if resolved.watchlist_id is not None and tally["downloaded"]:
            # The card was raised before a single track landed, so the total it
            # was inserted with predates this run.
            try:
                self._db.refresh_watchlist_total(resolved.watchlist_id)
            except Exception:
                pass
        self._row(row, index, *_row_verdict(tally, len(tracks),
                                            self._cancel.is_set()))

    # ── The per-track loop ────────────────────────────────────────────────────
    def run_tracks(self, tracks, *, skip_mode=None, ignore_skip_existing=False,
                   row=None, index=0):
        """Download already-resolved *tracks* — the reusable inner loop.

        *skip_mode* overrides the user's Skip-Existing setting with one of
        crate.SkipMode (the Watch List's own rule); *ignore_skip_existing*
        turns skipping off entirely, which is what a forced re-download is.
        *row* is the queue row these tracks came from, present only so the
        row's own progress detail can be updated as they settle.

        Called on its own — the Watch List's entry point — this IS the run, so
        it seeds the overall total with its own tracks and zeroes the counters
        first. Called from `run`, it is one row of a longer batch and leaves
        both alone.

        The policy is re-read per track, exactly as the monolith re-read its Tk
        variables, so a setting changed mid-batch reaches the very next track.
        """
        tracks = list(tracks)
        if not self._batch_open:
            self._reset(len(tracks))
        tally = {"downloaded": 0, "skipped": 0, "errors": 0, "deferred": 0,
                 "stopped": False, "state": None, "detail": ""}
        for done_in_row, spec in enumerate(tracks):
            if self._cancel.is_set() or self._skip_requested(spec.row_id):
                tally["stopped"] = True
                break
            if not self._wait_while_paused(spec.row_id):
                tally["stopped"] = True
                break

            policy = self._settings.download_policy()
            verdict = self._pre_flight(spec, policy, skip_mode,
                                       ignore_skip_existing)
            if verdict is None:
                started = self._now()
                outcome = self._download(spec, policy)
                if outcome.kind == "cancelled":
                    tally["stopped"] = True
                    break
                self._durations.append(self._now() - started)
                del self._durations[:-self.ETA_WINDOW]
                verdict = (outcome.kind, outcome.reason or "done", "")
                spec_title = outcome.title or spec.title
            else:
                spec_title = spec.title
            self._settle(spec, spec_title, verdict, tally)
            self._overall()
            if row is not None and len(tracks) > 1:
                self._row(row, index, "active",
                          f"{done_in_row + 1}/{len(tracks)}")
        return tally

    def _pre_flight(self, spec, policy, skip_mode, ignore_skip_existing):
        """The two checks that run before a track is fetched, in the monolith's
        order: the Time Limiter, then ownership. Returns None to download, or
        the (kind, reason, path) the track settles as instead."""
        entry = spec.entry or {}
        if policy.limit_enabled:
            duration = entry.get("duration")
            limit_sec = int(policy.limit_minutes or 0) * 60
            if duration and limit_sec and duration > limit_sec:
                return ("skipped",
                        activitylog.over_limit(duration, policy.limit_minutes), "")
        if is_unreleased_entry(entry):
            # A premiere or in-progress stream: not a failure and not a skip,
            # so the next run after it airs picks it up.
            return "deferred", "not out yet", ""
        if ignore_skip_existing:
            return None
        mode = skip_mode if skip_mode is not None else (
            policy.skip_mode if policy.skip_existing else None)
        if mode is None:
            return None
        ownership = self._crate(spec).owns(entry)
        decision = skip_decision(ownership, mode)
        if decision is SkipDecision.DOWNLOAD:
            return None
        # CONFIRM_REDOWNLOAD — "the database says we have it but the file is
        # gone" — has no user to ask headlessly, so it takes the tkinter
        # dialog's own unattended answer, which is Skip.
        in_db, on_disk = ownership.in_db, bool(ownership.path)
        reason = ("already on disk"    if mode == SkipMode.FOLDER_ONLY else
                  "in database"        if mode == SkipMode.DB_ONLY else
                  "in database + disk" if (in_db and on_disk) else
                  "already on disk"    if on_disk else
                  "in database")
        return "skipped", reason, ownership.path

    def _download(self, spec, policy):
        """Build the downloader and the plan for one track and run it. Both are
        built per TRACK, not per row, so a mid-batch setting change lands."""
        crate = self._crate(spec)
        downloader = self._downloader_factory(
            runner=self._runner,
            db=self._db,
            policy=policy,
            canceller=SkipOrCancel(
                self._cancel, lambda: self._skip_requested(spec.row_id)),
            cookies=self._settings.cookie_config(),
            ffmpeg_dir=self._ffmpeg_dir,
            probe_formats=lambda u: self._session().probe_formats(u),
            tag=self._tag,
            harvest_art=self._harvest_art,
            remember=crate.remember,
            log_download=self._log_download,
            log_error=self._log_error)
        plan = TrackPlan(
            url=spec.url,
            title=spec.title,
            save_dir=spec.save_dir,
            genre=spec.genre,
            platform=spec.platform,
            video_id=(spec.entry or {}).get("id") or "",
            upload_date=(spec.entry or {}).get("upload_date") or "",
            thumbnail_url=(spec.entry or {}).get("thumbnail") or "",
            channel_name=spec.channel_name,
            channel_url=spec.channel_url,
            channel_id=spec.channel_id or None,
            expected_path=CrateLayout.track_path(spec.save_dir, spec.title),
            session_ua=self._session_ua,
            sleep_range=resolve_sleep_range(policy),
            cover_art=(self._cover_art_mode() != "off"
                       and cb_artwork.artwork_available()),
            target_kbps=(str(policy.bitrate_quality).split() or ["192"])[0],
            suppress_channel_url=spec.suppress_channel_url)
        return downloader.run(plan, _EventSink(self._emit, spec.title,
                                               self._job))

    def _settle(self, spec, title, verdict, tally):
        """Book one finished track: counters, the activity-log line the
        downloader did not already write, and the row detail it leaves behind."""
        kind, reason, path = verdict
        self._done += 1
        if kind == "downloaded":
            tally["downloaded"] += 1
            self._downloaded += 1
            state, detail = "done", "done"
        elif kind == "skipped":
            tally["skipped"] += 1
            self._skipped += 1
            state, detail = "skipped", reason
            self._log_line(activitylog.skipped(title, path, reason))
            # Backfill tags on the file we already own, so the source URL is
            # recoverable even for tracks grabbed before tagging existed.
            if path:
                self._tag(path, title, spec.url, genre=spec.genre)
        elif kind == "deferred":
            tally["deferred"] += 1
            self._deferred += 1
            state, detail = "skipped", reason
        else:
            # "unavailable" is counted with the errors, exactly as the
            # monolith's grand Failed counter does.
            tally["errors"] += 1
            self._errors += 1
            state, detail = "error", reason
        tally["state"], tally["detail"] = state, detail

    # ── Bookkeeping ───────────────────────────────────────────────────────────
    def _reset(self, total):
        """Zero the per-run counters and seed the total.

        Every run starts from nothing, so a runner asked for a second run — or
        a second `run_tracks`, which is how the Watch List downloads one channel
        after another — never reports the previous run's tallies again."""
        self._total = total
        self._done = 0
        self._downloaded = self._skipped = self._errors = self._deferred = 0
        self._durations = []
        self._counted = set()
        self._session_ua = pick_session_ua(self._settings.download_policy())

    def _admit(self, row):
        """Count one queue row toward the batch total, once. A row already
        marked skipped is never counted — there is no work in it to report."""
        row_id = row.get("id")
        if row.get("state") == "skipped" or row_id in self._counted:
            return
        self._counted.add(row_id)
        self._total += 1

    def _withdraw(self, row):
        """Give back the count of a row that turned out to hold no work."""
        row_id = row.get("id")
        if row_id in self._counted:
            self._counted.discard(row_id)
            self._total -= 1

    # ── Events ────────────────────────────────────────────────────────────────
    def _row(self, row, index, state, detail):
        self._flush()
        self._emit("queue.row", {
            "id": row.get("id"), "index": index, "state": state,
            "title": row.get("title") or row.get("url") or "", "detail": detail,
        })

    def _overall(self):
        total = max(self._total, self._done)
        self._emit("progress.overall", {
            "done": self._done, "total": total,
            "downloaded": self._downloaded, "skipped": self._skipped,
            "errors": self._errors,
            "percent": int(self._done / total * 100) if total else 0,
            "eta_text": eta_text(self._durations, total - self._done),
            "job": self._job,
        })

    def _finish(self):
        cancelled = self._cancel.is_set()
        counts = {"downloaded": self._downloaded, "skipped": self._skipped,
                  "errors": self._errors}
        self._log_line(activitylog.separator(
            "CANCELLED BY USER" if cancelled else
            f"BATCH COMPLETE  —  {self._downloaded} downloaded, "
            f"{self._skipped} skipped, {self._errors} failed"))
        self._flush()
        self._emit("batch.finished", dict(counts, cancelled=cancelled))
        # The same summary as a notification, for a client that is not on the
        # Downloads screen — the design's "batch complete" bell entry. A run
        # that was stopped, or that lost tracks, is not routine, so it arrives
        # at the level that gets the attention treatment.
        self._emit("notification", {
            "level": "warn" if (cancelled or self._errors) else "info",
            "title": "Batch cancelled" if cancelled else "Batch complete",
            "body": f"{self._downloaded} downloaded, {self._skipped} skipped, "
                    f"{self._errors} failed",
            "at": datetime.now().isoformat(timespec="seconds"),
            "job": self._job,
        })
        if self._counts is not None:
            try:
                self._emit("state.patch", {"counts": self._counts()})
            except Exception:
                pass
        return dict(counts, cancelled=cancelled, deferred=self._deferred)

    # ── Collaborators ─────────────────────────────────────────────────────────
    def _session(self):
        """A read-only yt-dlp session carrying the cookie policy as it stands
        right now — built per operation, like every other session in the app."""
        return self._session_factory(cookies=self._settings.cookie_config())

    def _crate(self, spec):
        """The channel folder's crate, indexed once per folder per batch."""
        crate = self._crates.get(spec.save_dir)
        if crate is None:
            crate = ChannelCrate(spec.save_dir,
                                 is_downloaded=self._db.is_video_downloaded,
                                 platform=spec.platform)
            self._crates[spec.save_dir] = crate
        return crate

    def _tag(self, path, title, url, genre=None):
        return tagging.tag_track(path, title=title, source_url=url,
                                 genre=genre)

    def _harvest_art(self, audio_path, video_id, title, source_url=None,
                     genre=None):
        """The TrackDownloader's cover-art hook, carrying the user's current
        cover-art setting into artwork.harvest_cover_art."""
        return cb_artwork.harvest_cover_art(
            audio_path, video_id, mode=self._cover_art_mode(),
            ffmpeg_dir=self._ffmpeg_dir,
            retag=lambda p: self._tag(p, title, source_url, genre=genre))

    def _cover_art_mode(self):
        """The effective mode the download path acts on: "off" when the
        cover-art checkbox is clear, otherwise the chosen formatting."""
        policy = self._settings.download_policy()
        return policy.cover_art_mode if policy.cover_art_enabled else "off"

    def _log_download(self, title, path, url, platform, genre,
                      quality="192 kbps MP3"):
        self._log_line(activitylog.downloaded(title, path, url, platform, genre,
                                       quality))

    def _log_error(self, title, url, error):
        self._log_line(activitylog.error(title, url, error))


def _row_verdict(tally, track_count, cancelled=False):
    """The state and detail a finished queue row settles on. A single-track row
    simply wears its one track's verdict; a collection reports its tally."""
    if tally["state"] is None:
        # Nothing settled — the row was cancelled or skipped before its first
        # track finished.
        return "skipped", "cancelled" if cancelled else "skipped"
    if track_count == 1:
        return tally["state"], tally["detail"]
    if tally["stopped"]:
        return "skipped", f"stopped after {tally['downloaded']} downloaded"
    if tally["downloaded"]:
        return "done", f"{tally['downloaded']} downloaded"
    if tally["errors"]:
        return "error", f"{tally['errors']} failed"
    return "skipped", f"{tally['skipped'] + tally['deferred']} skipped"

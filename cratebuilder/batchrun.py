"""Headless batch orchestration: queue rows in, download progress events out."""
import os
import random
import threading
import time
from dataclasses import dataclass, field

from cratebuilder import artwork as cb_artwork
from cratebuilder import tagging, util
from cratebuilder.crate import (ChannelCrate, CrateLayout, SkipDecision,
                                SkipMode, is_unreleased_entry, skip_decision)
from cratebuilder.download import (SkipOrCancel, TrackDownloader, TrackPlan,
                                   download_with)
from cratebuilder.sidecar import channel_url_from_id
from cratebuilder.ydl import (YdlOffline, YdlPermanent, YdlSession,
                              YdlUnclassified)

# ── Platform facts ────────────────────────────────────────────────────────────
# The three per-platform values the download path reads out of the monolith's
# PLATFORMS table. Restated here rather than imported: PLATFORMS carries Tk
# colours and widget copy, and this package may not import the monolith.
PLATFORM_SUBDIR = {"YouTube": "YouTube", "SoundCloud": "SoundCloud"}
ITEM_WORD = {"YouTube": "video", "SoundCloud": "track"}

FETCH_FAILURE_KIND = {
    YdlPermanent: "permanent",
    YdlOffline: "offline",
    YdlUnclassified: "unknown",
}

# One User-Agent is chosen per batch session, and the Auto throttle presets are
# keyed by the exact label the Settings dropdown stores.
USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

THROTTLE_PRESETS = {
    "Light  (1–5 s)":       (1, 5),
    "Moderate  (3–8 s)":    (3, 8),
    "Aggressive  (5–15 s)": (5, 15),
}


@dataclass(frozen=True)
class TrackSpec:
    """One resolved track: everything the per-track loop needs without asking
    the network again.

    *entry* is the raw yt-dlp flat entry the limiter, the premiere check and
    ChannelCrate ownership all read, so a caller that already holds resolved
    entries (the Watch List) can feed run_tracks directly."""
    row_id: object
    url: str
    title: str
    save_dir: str
    genre: str
    platform: str
    entry: dict = field(default_factory=dict)
    channel_name: str = ""
    channel_url: str = ""
    channel_id: str | None = None
    suppress_channel_url: str = ""


# ── Pure helpers ──────────────────────────────────────────────────────────────
def entry_url(entry, platform):
    """The watch URL for one flat-playlist entry — the monolith's per-platform
    url_builder, headless."""
    url = entry.get("url") or entry.get("webpage_url") or ""
    if url:
        return url
    video_id = entry.get("id") or ""
    if platform == "SoundCloud":
        return video_id
    return f"https://www.youtube.com/watch?v={video_id}"


def resolve_sleep_range(policy):
    """The (min, max) throttle seconds a DownloadPolicy asks for, or None when
    throttling is off. Auto reads the preset table; Manual reads the pair, with
    max never below min."""
    if not policy.sleep_enabled:
        return None
    if policy.sleep_mode == "Auto":
        return THROTTLE_PRESETS.get(policy.sleep_preset, (1, 5))
    s_min, s_max = int(policy.sleep_min), int(policy.sleep_max)
    return (s_min, max(s_min, s_max))


def pick_session_ua(policy, rng=None):
    """One User-Agent for a whole batch session, or None when rotation is off."""
    if not policy.rotate_ua:
        return None
    return (rng or random).choice(USER_AGENT_POOL)


def duration_reason(duration_sec, limit_minutes):
    """The activity-log reason for a track the Time Limiter turned away."""
    total = int(duration_sec)
    return (f"exceeds limit ({total // 60}:{total % 60:02d} > "
            f"{limit_minutes}:00)")


def eta_text(durations, remaining):
    """"~N min left" from a rolling average of per-track seconds, "" when
    nothing has finished yet or nothing is left."""
    if not durations or remaining <= 0:
        return ""
    seconds = (sum(durations) / len(durations)) * remaining
    if seconds < 90:
        return f"~{max(1, int(round(seconds)))} sec left"
    return f"~{max(1, int(round(seconds / 60)))} min left"


def downloaded_line(title, path, url, platform, genre, quality):
    genre_str = genre if genre and genre != CrateLayout.NO_GENRE_VALUE else "—"
    return (f"DOWNLOADED  | Platform: {platform:<11}| "
            f"Genre: {genre_str:<18}| Title: {title} | File: {path} | "
            f"URL: {url} | Quality: {quality}")


def skipped_line(title, path, reason):
    return f"SKIPPED     | Reason: {reason:<20}| Title: {title} | File: {path}"


def error_line(title, url, error):
    return f"ERROR       | Title: {title} | URL: {url} | Error: {error}"


def separator_line(label=""):
    if not label:
        return "═" * 80
    pad = max(0, 74 - len(label))
    return f"{'═' * (pad // 2)}  {label}  {'═' * (pad - pad // 2)}"


def _raw_thumbnail(audio_path):
    """The image yt-dlp wrote beside *audio_path*, or None."""
    stem = os.path.splitext(audio_path or "")[0]
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        if stem and os.path.isfile(stem + ext):
            return stem + ext
    return None


# ── Sink ──────────────────────────────────────────────────────────────────────
class _EventSink:
    """A TrackDownloader Sink that publishes progress.current frames.

    No throttling of its own: the service's Coalescer is what rate-limits these,
    so every frame yt-dlp reports is handed over."""

    def __init__(self, emit, title):
        self._emit = emit
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
        })


# ── BatchRunner ───────────────────────────────────────────────────────────────
class BatchRunner:
    """One batch of queue rows, downloaded headlessly.

    The Tk-free re-expression of the monolith's `_batch_worker` /
    `_process_one_url` pair: the same per-entry order (limiter, then ownership,
    then the download), the same counters, the same activity-log lines — with
    every UI effect replaced by an event.

    Two entry points, deliberately. `run(rows)` probes each queue row, expands a
    playlist or channel into entries and hands the resolved tracks to
    `run_tracks`, which is the whole per-track loop and nothing else. A caller
    that already holds resolved entries (the Watch List, which scanned them
    minutes ago) calls `run_tracks` directly and never probes.

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
                 now=time.monotonic):
        self._settings = settings
        self._db = db
        self._emit = emit
        self._session_factory = session_factory
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
        self._durations = []
        self._session_ua = None
        self._total = 0
        self._done = 0
        self._downloaded = 0
        self._skipped = 0
        self._errors = 0
        self._deferred = 0

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
        the duration."""
        self._session_ua = pick_session_ua(self._settings.download_policy())
        seen = len(rows)
        self._total = sum(1 for r in rows if r.get("state") != "skipped")
        self._log_line(separator_line(
            f"DOWNLOAD STARTED  —  {seen} URL{'s' if seen != 1 else ''}"))
        self._overall()
        index = 0
        while not self._cancel.is_set():
            if len(rows) > seen:
                self._total += sum(1 for r in rows[seen:]
                                   if r.get("state") != "skipped")
                seen = len(rows)
            if index >= len(rows):
                break
            self._run_row(rows[index], index)
            index += 1
        return self._finish()

    def _run_row(self, row, index):
        """One queue row: probe it, expand it, download it."""
        row_id = row.get("id")
        url = (row.get("url") or "").strip()
        title = row.get("title") or url
        if row.get("state") == "skipped" or self._skip_requested(row_id):
            self._total -= 1
            self._log_line(skipped_line(title, "", "skipped by user"))
            self._row(row, index, "skipped", "skipped")
            return

        self._row(row, index, "active", "fetching…")
        try:
            specs = self._resolve(row, url)
        except Exception as exc:
            reason = util.describe_fetch_failure(
                _fetch_failure_kind(exc), str(exc))
            self._errors += 1
            self._done += 1
            self._log_line(error_line(title, url, reason))
            self._overall()
            self._row(row, index, "error", reason)
            return

        if not specs:
            self._total -= 1
            self._row(row, index, "skipped", "nothing found")
            return

        self._total += len(specs) - 1
        tally = self.run_tracks(specs, row=row, index=index)
        self._row(row, index, *_row_verdict(tally, len(specs),
                                            self._cancel.is_set()))

    def _resolve(self, row, url):
        """Probe one row's URL and turn it into TrackSpecs. Raises the typed
        YdlError a failed probe produced — the caller reports it."""
        genre = row.get("genre") or CrateLayout.NO_GENRE_VALUE
        platform = row.get("platform") or util.detect_platform(url)
        session = self._session()
        info = session.probe_metadata(url)
        if not info:
            raise YdlUnclassified("yt-dlp returned no metadata",
                                  intent="probe_metadata", target=url)
        is_collection = info.get("_type") in ("playlist", "channel")
        channel_id = info.get("channel_id") or ""
        channel_url = (channel_url_from_id(channel_id)
                       or info.get("channel_url")
                       or info.get("uploader_url") or "")
        if is_collection:
            entries = [e for e in session.list_channel(url)
                       if isinstance(e, dict)]
            collection_name = util.derive_collection_name(info)
        else:
            entries = [info]
            collection_name = ""

        channel_sub = row.get("channel_name") or (collection_name
                                                  if is_collection else None)
        save_dir = CrateLayout.channel_dir(
            self._platform_dir(platform), genre, channel_sub)
        os.makedirs(save_dir, exist_ok=True)

        word = ITEM_WORD.get(platform, "item")
        return [
            TrackSpec(
                row_id=row.get("id"),
                url=entry_url(entry, platform),
                title=entry.get("title") or f"{word.capitalize()} {i + 1}",
                save_dir=save_dir, genre=genre, platform=platform, entry=entry,
                channel_name=row.get("channel_name") or collection_name,
                channel_url=url if is_collection else "",
                channel_id=channel_id or None,
                # The downloads row keys on the collection URL; the
                # permanently-unavailable memory keys on the channel a one-off
                # track came from. For a collection they agree.
                suppress_channel_url=url if is_collection else channel_url,
            )
            for i, entry in enumerate(entries)
        ]

    # ── The per-track loop ────────────────────────────────────────────────────
    def run_tracks(self, tracks, *, skip_mode=None, ignore_skip_existing=False,
                   row=None, index=0):
        """Download already-resolved *tracks* — the reusable inner loop.

        *skip_mode* overrides the user's Skip-Existing setting with one of
        crate.SkipMode (the Watch List's own rule); *ignore_skip_existing*
        turns skipping off entirely, which is what a forced re-download is.
        *row* is the queue row these tracks came from, present only so the
        row's own progress detail can be updated as they settle.

        The policy is re-read per track, exactly as the monolith re-read its Tk
        variables, so a setting changed mid-batch reaches the very next track.
        """
        tracks = list(tracks)
        if self._session_ua is None:
            self._session_ua = pick_session_ua(self._settings.download_policy())
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
                        duration_reason(duration, policy.limit_minutes), "")
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
        cover_mode = (policy.cover_art_mode if policy.cover_art_enabled
                      else "off")
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
            cover_art=(cover_mode != "off" and cb_artwork.artwork_available()),
            target_kbps=(str(policy.bitrate_quality).split() or ["192"])[0],
            suppress_channel_url=spec.suppress_channel_url)
        return downloader.run(plan, _EventSink(self._emit, spec.title))

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
            self._log_line(skipped_line(title, path, reason))
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
        })

    def _finish(self):
        cancelled = self._cancel.is_set()
        counts = {"downloaded": self._downloaded, "skipped": self._skipped,
                  "errors": self._errors}
        self._log_line(separator_line(
            "CANCELLED BY USER" if cancelled else
            f"BATCH COMPLETE  —  {self._downloaded} downloaded, "
            f"{self._skipped} skipped, {self._errors} failed"))
        self._flush()
        self._emit("batch.finished", dict(counts, cancelled=cancelled))
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

    def _platform_dir(self, platform):
        return os.path.join(self._settings.get("base_dir"),
                            PLATFORM_SUBDIR.get(platform, platform))

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
        """Stamp title / source URL / genre onto a track. Never raises: a tag
        failure must not fail a download."""
        no_genre = CrateLayout.NO_GENRE_VALUE
        try:
            tagging.write_track_tags_any(
                path, title=title, source_url=url,
                genre=None if (genre or no_genre) == no_genre else genre)
        except Exception:
            pass

    def _harvest_art(self, audio_path, video_id, title, source_url=None,
                     genre=None):
        """Turn the thumbnail yt-dlp just wrote into cover art: the archival
        `.artwork/` sidecar plus the embedded front-cover frame. Returns
        (artwork_path, embedded, final_audio_path); never raises."""
        policy = self._settings.download_policy()
        mode = policy.cover_art_mode if policy.cover_art_enabled else "off"
        raw = _raw_thumbnail(audio_path)
        if mode == "off" or not raw or not cb_artwork.artwork_available():
            return None, False, audio_path
        try:
            art_dir = cb_artwork.thumbnail_dir(os.path.dirname(audio_path))
            art_path = art_dir and cb_artwork.ingest_thumbnail(
                raw, art_dir, video_id, mode)
            if not art_path:
                return None, False, audio_path
            final_path, embedded = cb_artwork.embed_cover_any(
                audio_path, art_path, self._ffmpeg_dir)
            if final_path != audio_path:
                # The Ogg container does not inherit the WebM's tags.
                self._tag(final_path, title, source_url, genre=genre)
            return art_path, embedded, final_path
        except Exception:
            return None, False, audio_path

    def _log_download(self, title, path, url, platform, genre,
                      quality="192 kbps MP3"):
        self._log_line(downloaded_line(title, path, url, platform, genre,
                                       quality))

    def _log_error(self, title, url, error):
        self._log_line(error_line(title, url, error))


def _fetch_failure_kind(exc):
    """The FETCH_FAILURE_TEXT key for a typed read-only failure."""
    for error_type, kind in FETCH_FAILURE_KIND.items():
        if isinstance(exc, error_type):
            return kind
    return "unknown"


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

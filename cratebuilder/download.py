"""One track, end to end: options, attempt ladder, tags, art, DB row."""
import os
import re
import time
import traceback
from dataclasses import dataclass

from cratebuilder import ydl
from cratebuilder.crate import CrateLayout
from cratebuilder.sidecar import canonical_channel_url
from cratebuilder.util import (
    classify_deferred_failure, classify_permanent_failure,
    download_result_facts, redact_ydl_opts,
)


# ── Records ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TrackPlan:
    """Everything a TrackDownloader needs to know about one track.

    Frozen and Tk-free: the caller resolves every live setting (the sleep
    range, the rotating User-Agent, the cover-art decision) into this record
    before handing it over, so the worker thread never reads a Tk variable.

    *expected_path* is the guessed on-disk path (CrateLayout.track_path); the
    download's own info dict is what settles the real name. *channel_url* is
    what the downloads row records — the collection URL, "" for a one-off
    track. *suppress_channel_url* is the channel the permanently-unavailable
    memory keys against, which for a one-off track is the channel it came from
    rather than "" (the two genuinely differ; see
    TrackDownloader._record_failure) — the caller must set it, since it is the
    only value that memory reads and nothing here falls back to *channel_url*
    on its behalf."""
    url: str
    title: str
    save_dir: str
    genre: str
    platform: str
    video_id: str = ""
    upload_date: str = ""
    thumbnail_url: str = ""
    channel_name: str = ""
    channel_url: str = ""
    channel_id: str | None = None
    expected_path: str = ""
    session_ua: str | None = None
    sleep_range: tuple | None = None
    cover_art: bool = False
    target_kbps: str = "192"
    suppress_channel_url: str = ""


@dataclass(frozen=True)
class Outcome:
    """What became of one track. *kind* is the whole verdict:

    downloaded  — the file is on disk, tagged, and in the downloads table.
    unavailable — permanently gone (DRM, removed, geo-blocked); remembered so
                  the Watch List stops offering it.
    deferred    — a premiere or scheduled stream that is not out yet. Not a
                  failure: nothing is remembered and no counter is a failure
                  counter, so the track stays pending for the next run.
    failed      — a real error. *reason* is the short UI string.
    cancelled   — the user cancelled during a retry backoff. Deliberately not
                  an error: nothing is logged and no counter moves."""
    kind: str
    reason: str = ""
    title: str = ""
    path: str = ""
    bitrate_text: str = ""
    quality_text: str = ""
    artwork_path: str | None = None
    artwork_embedded: bool = False


@dataclass(frozen=True)
class Failure:
    """How one download error was classified: the Outcome *kind* it maps to
    plus the short reason string shown in the queue and written to
    activity.log."""
    kind: str
    reason: str


# ── Sink ──────────────────────────────────────────────────────────────────────
class NullSink:
    """The do-nothing Sink, for callers with no UI to drive.

    The five events a TrackDownloader reports, in the order they can occur:

      started(title)                        — the attempt ladder is starting.
      bitrate_detected(source_abr, target)  — yt-dlp named the source stream's
                                              bitrate; *target* is the MP3
                                              bitrate this rung encodes at.
      progress(percent, speed_text)         — percent is 0..100 or None,
                                              speed_text "" when unknown.
      title_corrected(title)                — the real title differs from the
                                              placeholder the plan carried.
      finished()                            — yt-dlp finished fetching bytes;
                                              conversion follows."""

    def started(self, title):
        pass

    def progress(self, percent=None, speed_text=""):
        pass

    def bitrate_detected(self, source_abr, target_kbps):
        pass

    def title_corrected(self, title):
        pass

    def finished(self):
        pass


class _Silent:
    """Null debug/activity logger: every level swallowed."""

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass

    def debug(self, message):
        pass


class _NeverCancelled:
    """The default Canceller: sleeps out a backoff and never reports
    cancellation, which is exactly what the pre-Canceller code did."""

    def wait(self, timeout=None):
        if timeout:
            time.sleep(timeout)
        return False

    def is_set(self):
        return False


class _AbortRequested(Exception):
    """Raised inside the progress hook to abort the in-flight download the
    moment the canceller trips. yt-dlp propagates it out of the download
    call; _attempts recognises the tripped canceller and reports the rung
    cancelled rather than failed."""


class SkipOrCancel:
    """A Canceller over a threading.Event plus an extra predicate — the
    per-row Skip. Either one cancels. wait() polls the predicate in short
    slices so a Skip pressed during a retry backoff interrupts the sleep
    instead of waiting it out, exactly as the Event itself would."""

    def __init__(self, event, extra=None):
        self._event = event
        self._extra = extra or (lambda: False)

    def is_set(self):
        return self._event.is_set() or bool(self._extra())

    def wait(self, timeout=None):
        if not timeout:
            return self.is_set()
        deadline = time.monotonic() + timeout
        while True:
            if self._extra():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            if self._event.wait(min(0.2, remaining)):
                return True


# ── Progress text ─────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi(text):
    """Drop the ANSI colour escapes yt-dlp writes into its progress and error
    strings. Every string that reaches a Sink or a log passes through here."""
    return _ANSI_RE.sub('', text or '')


def parse_percent(text):
    """Read yt-dlp's "_percent_str" (" 42.3%", " N/A%") as a float, or None
    when it says nothing. Only consulted when the byte counts are missing —
    bytes are the truth, this is the fallback."""
    cleaned = strip_ansi(text).strip().rstrip('%').strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── Failure classification ────────────────────────────────────────────────────
# What YouTube actually says when it age-gates a video. The bare word "age" is
# deliberately NOT among them: it matches inside "webpage" and "message", and
# "Unable to download webpage" is one of yt-dlp's commonest transient wrappers.
AGE_GATE_MARKERS = (
    "confirm your age",
    "verify your age",
    "age-restricted",
    "age restricted",
    "age-gate",
    "age gate",
    "inappropriate for some users",
    "adult",
)


def looks_age_restricted(error_text):
    """Whether an error reads like YouTube's age gate.

    Two things ride on this: whether to drop authentication for one more
    attempt, and whether the track is reported to the user as "age-restricted".
    The original test was a bare `"age" in text` substring match, which fired on
    any error containing "webpage" — so a wobbling connection was announced as
    an age restriction and bought itself a second attempt ladder. Matching the
    phrases YouTube actually uses costs nothing and makes both answers honest.

    A non-transient failure is still required at the call site, which keeps the
    two conditions independent: a genuine age gate reported inside a transient
    wrapper stays classified as the network problem it also is."""
    text = (error_text or "").lower()
    return any(marker in text for marker in AGE_GATE_MARKERS)


def classify_download_failure(error_text, is_age=False):
    """Classify a failed download into the Outcome kind and the short reason.

    The order is load-bearing. Deferred is checked FIRST so a premiere the
    scan let through (its live_status missing because YouTube changed the
    channel markup) can never be read as "Removed" and filed in the
    permanently-unavailable memory, which would bury it for good — it fails
    today and succeeds by itself once it airs. Permanently-unavailable causes
    (SoundCloud DRM, removed/private 404s, geo-blocks) come next: they are
    nothing on our side, so they get an honest status and are counted apart
    from real errors. Everything after that is plain string matching, longest-
    standing markers first, falling back to the first 60 characters of the
    error.

    *is_age* is the verdict on the FIRST failure of this track, not on
    *error_text* — a track that age-gated and then failed its unauthenticated
    retry for some other reason still reports "age-restricted", because that is
    what the user has to fix. Pure; *error_text* must already be ANSI-stripped."""
    clean = error_text or ""
    lower = clean.lower()

    deferred = classify_deferred_failure(clean)
    if deferred:
        return Failure("deferred", deferred)
    permanent = classify_permanent_failure(clean)
    if permanent:
        return Failure("unavailable", permanent)

    if   "ffmpeg"        in lower: reason = "FFmpeg missing"
    elif "sign in"       in lower: reason = "login required"
    elif is_age:                   reason = "age-restricted"
    elif "unavailable"   in lower: reason = "unavailable"
    elif "private"       in lower: reason = "private"
    elif "copyright"     in lower: reason = "copyright claim"
    elif "members"       in lower: reason = "members only"
    elif "removed"       in lower: reason = "removed"
    elif "blocked"       in lower: reason = "blocked"
    elif "not available" in lower: reason = "format unavailable"
    else:                          reason = clean[:60]
    return Failure("failed", reason)


# Error wording that means the network wobbled rather than the track being
# unfetchable: Winsock 10054 resets, timeouts and broken connections, all
# common with VPNs and rate-limiting. These retry; everything else falls
# through on the first raise.
_TRANSIENT_MARKERS = (
    "10054", "connection reset", "connection broken", "connection aborted",
    "connectionreseterror", "timed out", "read timeout", "temporary failure",
    "remote end closed",
)

MAX_ATTEMPTS = 3


def looks_transient(error_text):
    """Whether an error is worth another attempt on the same rung."""
    lower = (error_text or "").lower()
    return any(marker in lower for marker in _TRANSIENT_MARKERS)


# ── Options ───────────────────────────────────────────────────────────────────
def rung_kbps(plan, authenticated, upgraded_kbps=None):
    """The MP3 bitrate one rung of the ladder encodes at — the bitrate rule,
    stated once.

    The auto-upgrade probe only runs with cookies on, so an upgraded bitrate
    describes an *authenticated* format ladder and means nothing to an
    unauthenticated attempt. An authenticated rung therefore gets the upgrade,
    an unauthenticated one the user's configured bitrate."""
    if authenticated and upgraded_kbps:
        return upgraded_kbps
    return plan.target_kbps


def download_opts_builder(plan, *, no_conversion=False, geo_bypass=False,
                          ffmpeg_dir=None, cookies=None, upgraded_kbps=None,
                          progress_hook=None):
    """Return the ONE yt-dlp options builder for *plan*: opts(authenticated).

    Every download attempt for this track comes out of the returned callable,
    whose only parameter is whether to authenticate. That is the whole point:
    the age-gate attempt used to be a second dict literal that silently drifted
    from the first — it lost ffmpeg_location (so a packaged build could not find
    FFmpeg and every age-gated track failed twice), ignored "Keep original
    format", and skipped the sleep throttle. With one literal those three
    cannot happen: authentication and the bitrate it implies are the only
    difference two attempts can have."""

    def opts(authenticated):
        built = {
            # Prefer highest-bitrate audio-only stream.
            # abr>=160 targets Opus 160k (itag 251) or better, falling back
            # to plain bestaudio. Deliberately NO muxed-"best" tier: when the
            # JS challenge breaks and no audio-only formats are offered, that
            # fallback silently downloaded the entire video (~83s a track for
            # the same audio). Better to fail as "format unavailable" — the
            # track stays pending and retries clean once yt-dlp is patched.
            "format":   "bestaudio[abr>=160]/bestaudio",
            "outtmpl":  os.path.join(plan.save_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [progress_hook] if progress_hook else [],
            "quiet":       True,
            "no_warnings": True,
        }
        # Point yt-dlp at the bundled FFmpeg (packaged build) so it does not
        # depend on FFmpeg being on PATH. None when run from source.
        if ffmpeg_dir:
            built["ffmpeg_location"] = ffmpeg_dir
        # Ask yt-dlp to save the thumbnail beside the audio. We embed it
        # ourselves rather than using the EmbedThumbnail postprocessor, so we
        # control the crop and keep the .artwork sidecar copy.
        if plan.cover_art:
            built["writethumbnail"] = True
        # Only add the FFmpeg MP3 postprocessor if conversion is enabled. When
        # "Keep original format" is checked, the file is saved as-is in
        # whatever container YouTube/SoundCloud served.
        if not no_conversion:
            built["postprocessors"] = [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": rung_kbps(plan, authenticated,
                                              upgraded_kbps),
            }]
        if geo_bypass:
            built["geo_bypass"] = True
        if plan.session_ua:
            built.setdefault("http_headers", {})["User-Agent"] = plan.session_ua
        if plan.sleep_range:
            s_min, s_max = plan.sleep_range
            built["sleep_interval"]     = s_min
            built["max_sleep_interval"] = s_max
        if authenticated:
            built.update(ydl.cookie_opts(cookies))
        built.update(ydl.js_runtime_opts())
        return built

    return opts


def download_with(opts, url):
    """The live runner: hand *opts* to yt-dlp and download *url*, returning the
    info dict. Mirrors ydl.extract_info with download=True. Imported lazily so
    importing this module (and every test that injects its own runner) never
    needs yt-dlp installed."""
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ytdl:
        return ytdl.extract_info(url, download=True)


# ── TrackDownloader ───────────────────────────────────────────────────────────
class TrackDownloader:
    """One track, end to end: build options, run the attempt ladder, tag,
    harvest cover art, write the downloads row, classify and record failures.

    Constructed once per track with its dependencies, then asked run(plan,
    sink) — the policy and cookie snapshots it carries are read at construction
    time, so building it per track is what lets a setting the user changes
    mid-batch reach the very next track. Everything that needs Tk or the
    batch's own bookkeeping stays
    with the caller: the per-entry loop, the pause gate, the duration limiter,
    the ownership/skip block, the redownload prompt and every counter. What the
    caller gets back is an Outcome; what it hears along the way are Sink events.

    Two policies are the module's, not the caller's:

      Transient retry — up to MAX_ATTEMPTS attempts per rung with 2s/4s
      backoff, waited through the Canceller so cancelling interrupts a retry
      instead of waiting it out.

      Age gate — an age-gated failure retries the whole rung with
      authentication dropped, because YouTube forces the main player (which
      demands age verification) for a signed-in request while yt-dlp's embedded
      player bypasses the gate for an anonymous one. That second rung gets the
      same transient retry as the first."""

    def __init__(self, *, runner, db, policy, canceller=None, cookies=None,
                 ffmpeg_dir=None, probe_formats=None, tag=None,
                 harvest_art=None, remember=None, log_download=None,
                 log_error=None, logger=None, debug=None):
        self._runner = runner
        self._db = db
        self._policy = policy
        self._canceller = canceller or _NeverCancelled()
        self._cookies = cookies
        self._ffmpeg_dir = ffmpeg_dir
        self._probe_formats = probe_formats
        self._tag = tag or (lambda path, title, url, genre=None: None)
        self._harvest_art = harvest_art or (
            lambda path, video_id, title, source_url=None, genre=None:
            (None, False, path))
        self._remember = remember or (lambda path: None)
        self._log_download = log_download or (
            lambda *a, **kw: None)
        self._log_error = log_error or (lambda *a, **kw: None)
        self._log = logger or _Silent()
        self._dbg = debug or _Silent()

    # ── The ladder ────────────────────────────────────────────────────────────
    def run(self, plan, sink=None):
        """Download one track and report what became of it.

        The ladder has two rungs — authenticated, then unauthenticated — and
        the second is only climbed when the first rung's failure reads like an
        age gate AND the first rung actually authenticated. Each rung is a
        transient-retry loop over the SAME options builder, so the two attempts
        differ by authentication and the bitrate that implies, and by nothing
        else. Never raises: every ending is an Outcome.

        "Never raises" is a guarantee the caller depends on, not a hope. The
        batch driver has no per-track guard of its own any more: a raise out of
        here returns no counts at all and abandons every remaining track in the
        batch. So the whole ladder runs inside this wrapper and a raise from
        anywhere in it — the DB row, the activity log, the options build —
        becomes one failed track and nothing more."""
        try:
            return self._run(plan, sink or NullSink())
        except Exception as exc:
            try:
                self._dbg.error(f"DOWNLOAD ABORT | {plan.title!r}  {exc}")
            except Exception:
                pass
            return Outcome(kind="failed", reason=str(exc)[:60])

    def _run(self, plan, sink):
        """The ladder itself. Everything that can raise lives here so run()'s
        one guard covers all of it."""
        sink.started(plan.title)

        authenticated = bool(ydl.cookie_opts(self._cookies))
        upgraded = self._upgrade_kbps(plan, authenticated)
        source_abr = [None]
        shown_title = [plan.title]
        rung_target = [rung_kbps(plan, authenticated, upgraded)]
        hook = self._make_hook(plan, sink, source_abr, shown_title, rung_target)
        opts_for = download_opts_builder(
            plan,
            no_conversion=bool(self._policy.no_conversion),
            geo_bypass=bool(self._policy.geo_bypass),
            ffmpeg_dir=self._ffmpeg_dir, cookies=self._cookies,
            upgraded_kbps=upgraded, progress_hook=hook)

        self._dbg.info(f"─── DOWNLOAD ─── {plan.title!r}  URL: {plan.url}")

        rung_auth = authenticated
        is_age = False
        seen_failure = False
        while True:
            rung_target[0] = rung_kbps(plan, rung_auth, upgraded)
            source_abr[0] = None
            opts = opts_for(rung_auth)
            self._dbg.info(f"YDL OPTS (download) | {redact_ydl_opts(opts)}")

            status, payload = self._attempts(plan, opts)
            if status == "ok":
                return self._record_success(plan, payload, sink, source_abr,
                                            shown_title, rung_target[0])
            if status == "cancelled":
                return Outcome(kind="cancelled", title=shown_title[0])

            raw = str(payload)
            clean = strip_ansi(raw).strip()
            if not seen_failure:
                seen_failure = True
                is_age = looks_age_restricted(clean)
                self._dbg.error(
                    f"DOWNLOAD FAIL | {plan.title!r}  URL: {plan.url}")
                self._dbg.error(f"DOWNLOAD FAIL | error: {clean}")
                self._dbg.debug(
                    f"DOWNLOAD FAIL | traceback:\n{_format_exc(payload)}")

            if is_age and rung_auth and not looks_transient(clean):
                self._dbg.info(
                    f"AGE-RETRY   | {plan.title!r}  "
                    f"Retrying without cookies to bypass age gate")
                self._log.info(
                    f"AGE-RETRY   | "
                    f"Title: {plan.title} | "
                    f"URL: {plan.url} | "
                    f"Retrying without cookies to bypass age gate")
                rung_auth = False
                continue

            return self._record_failure(plan, raw, clean, is_age, authenticated)

    def _attempts(self, plan, opts):
        """One rung: call the runner, retrying transient network errors with
        interruptible exponential backoff (2s, 4s).

        Returns ("ok", info) · ("cancelled", None) · ("error", exception).
        Cancelling — during a backoff, or mid-download via the hook's
        _AbortRequested — is an outcome, not an error: the rung stops and
        nothing is logged. The alternative was sleeping out the full delay
        (or finishing the whole track) and then filing a spurious ERROR line
        for a track the user cancelled or skipped."""
        attempt = 0
        while True:
            attempt += 1
            if self._canceller.is_set():
                return "cancelled", None
            try:
                return "ok", self._runner(opts, plan.url)
            except Exception as exc:
                if self._canceller.is_set():
                    return "cancelled", None
                if not (looks_transient(str(exc)) and attempt < MAX_ATTEMPTS):
                    return "error", exc
                delay = 2 ** attempt
                self._dbg.warning(
                    f"NET RETRY   | {plan.title!r}  "
                    f"attempt {attempt}/{MAX_ATTEMPTS-1} "
                    f"after transient error "
                    f"(sleeping {delay}s): "
                    f"{str(exc)[:120]}")
                self._log.info(
                    f"NET RETRY   | "
                    f"Title: {plan.title} | "
                    f"Attempt {attempt}/{MAX_ATTEMPTS-1} "
                    f"after network error, "
                    f"retrying in {delay}s")
                if self._canceller.wait(delay):
                    return "cancelled", None

    # ── Bitrate ───────────────────────────────────────────────────────────────
    def _upgrade_kbps(self, plan, authenticated):
        """The bitrate an authenticated attempt should encode at, probing the
        source's real format ladder first.

        If YouTube serves a higher-bitrate audio stream than the user's
        configured MP3 output (e.g. 256 kbps AAC on a Premium-authenticated
        account while the user selected 192), encode at that source bitrate
        instead of downgrading. Opt-in via the policy's bitrate_auto_upgrade:
        the probe is a real network round-trip (~4s) before every track, and
        on a free-tier account it can never find an upgrade — YouTube serves
        at most ~160 kbps Opus there — so it defaults off and the Settings
        checkbox says what it costs. Even opted in, only probe when the cookie
        settings really produced cookie options (*authenticated*) and
        conversion is actually happening, and skip it at the 320 kbps MP3
        ceiling, which no source can exceed. Probe failures are non-fatal."""
        target = plan.target_kbps
        if not (self._policy.bitrate_auto_upgrade
                and authenticated and self._probe_formats
                and not self._policy.no_conversion
                and 0 < _kbps_int(target) < 320):
            return target
        try:
            best_abr = 0
            for fmt in self._probe_formats(plan.url):
                # Audio-only streams have vcodec == "none"
                if fmt.get("vcodec") == "none":
                    abr_val = fmt.get("abr") or fmt.get("tbr")
                    if abr_val:
                        try:
                            best_abr = max(best_abr, int(abr_val))
                        except (TypeError, ValueError):
                            pass
            # Cap at 320 (MP3 ceiling). Upgrade only if we actually found a
            # higher bitrate than the user's.
            if best_abr > _kbps_int(target):
                upgraded = str(min(best_abr, 320))
                self._dbg.info(
                    f"BITRATE AUTO-UPGRADE | {plan.title!r}  "
                    f"source={best_abr}k > setting={target}k  "
                    f"→ encoding MP3 at {upgraded}k")
                return upgraded
        except Exception as probe_exc:
            self._dbg.warning(
                f"BITRATE PROBE FAIL | {plan.title!r}  {probe_exc}")
        return target

    # ── Progress ──────────────────────────────────────────────────────────────
    def _make_hook(self, plan, sink, source_abr, shown_title, rung_target):
        """Build the ONE yt-dlp progress hook for this track: raw hook dicts in,
        Sink events out.

        The same hook object serves every rung, reading the rung's own MP3
        bitrate out of *rung_target* — the old retry re-used a hook closed over
        the authenticated bitrate and so announced a bitrate it was not
        encoding at. *source_abr* is likewise reset per rung by the ladder, so
        the source bitrate reported for a download is the one that rung's own
        stream carried and not the failed rung's. Percent prefers the byte
        counts and falls back to yt-dlp's own percent string, which the old hook
        computed and then threw away."""
        def hook(d):
            if self._canceller.is_set():
                # The one interruption point yt-dlp offers mid-download: an
                # exception from a progress hook aborts the transfer now,
                # leaving a .part file yt-dlp resumes if ever re-attempted.
                raise _AbortRequested("cancelled mid-download")
            status = d.get("status")
            if status == "downloading":
                total = (d.get("total_bytes")
                         or d.get("total_bytes_estimate", 0))
                got = d.get("downloaded_bytes", 0)
                speed = strip_ansi(d.get("_speed_str", "")).strip()
                eta = strip_ansi(d.get("_eta_str", "")).strip()
                info = d.get("info_dict") or {}
                abr = info.get("abr") or info.get("tbr")
                if abr and source_abr[0] is None:
                    source_abr[0] = abr
                    sink.bitrate_detected(abr, rung_target[0])
                percent = ((got / total * 100) if total
                           else parse_percent(d.get("_percent_str", "")))
                speed_text = (f"{speed}  {eta}" if speed and eta else speed)
                if percent is not None or speed_text:
                    sink.progress(percent=percent, speed_text=speed_text)
                # The real title from yt-dlp's own filename, so the queue always
                # matches the saved file name.
                real_title = info.get("title", "")
                if real_title and real_title != shown_title[0]:
                    shown_title[0] = real_title
                    sink.title_corrected(real_title)
            elif status == "finished":
                sink.finished()
        return hook

    # ── Endings ───────────────────────────────────────────────────────────────
    def _record_success(self, plan, info, sink, source_abr, shown_title,
                        target_kbps):
        """The single success path: adopt the download's own facts, log, tag,
        harvest cover art, and write the downloads row.

        SoundCloud set flat-extraction returns no title or thumbnail, so the
        plan's title may still be the "Track N" placeholder and its
        expected_path a guess that matches nothing on disk. The info dict the
        download itself just returned carries the real values — adopt them so
        the log, tags, cover harvest, DB row and queue label all point at the
        file yt-dlp actually wrote."""
        r_title, r_path, r_thumb, r_vid = download_result_facts(info)
        title = plan.title
        path = plan.expected_path
        if r_title:
            title = r_title
            if r_title != shown_title[0]:
                shown_title[0] = r_title
                sink.title_corrected(r_title)
        if r_path and os.path.isfile(r_path):
            path = r_path

        quality = (f"{int(source_abr[0])} kbps src → {target_kbps} kbps MP3"
                   if source_abr[0] else f"{target_kbps} kbps MP3")
        self._dbg.info(f"DOWNLOAD OK   | {title!r}  quality={quality}")
        self._log_download(title, path, plan.url, plan.platform, plan.genre,
                           quality=quality)

        # Resolve the real path first, since yt-dlp's sanitiser may differ from
        # the expected one. The crate indexed the folder once, before the batch,
        # so tell it what we just wrote — otherwise a later entry whose title
        # normalises alike downloads again over the same file.
        real_path = CrateLayout.find_existing(plan.save_dir, title) or path
        self._remember(real_path)
        self._tag(real_path, title, plan.url, genre=plan.genre)
        # Cover art runs after tagging so the APIC frame is written onto the tag
        # mutagen has already created.
        video_id = plan.video_id or r_vid
        art_path, art_embedded, real_path = self._harvest_art(
            real_path, video_id, title, source_url=plan.url, genre=plan.genre)

        upload_date = (plan.upload_date
                       or (info or {}).get("upload_date", "")
                       or "")
        self._db.add_download(
            video_id=video_id,
            title=title,
            channel_name=plan.channel_name,
            channel_url=plan.channel_url,
            channel_id=plan.channel_id or None,
            platform=plan.platform, genre=plan.genre or "(none)",
            file_path=real_path,
            upload_date=upload_date,
            bitrate=quality,
            artwork_path=art_path,
            artwork_embedded=art_embedded,
            thumbnail_url=plan.thumbnail_url or r_thumb)

        bitrate_text = (f"{int(source_abr[0])}k → {target_kbps}k"
                        if source_abr[0] else f"→ {target_kbps}k")
        return Outcome(kind="downloaded", title=title, path=real_path,
                       bitrate_text=bitrate_text, quality_text=quality,
                       artwork_path=art_path, artwork_embedded=art_embedded)

    def _record_failure(self, plan, raw, clean, is_age, authenticated):
        """The single failure path: log the full error, classify it, and
        remember it only when it can never succeed.

        A deferred track is deliberately NOT remembered anywhere: it stays
        pending so the next run after it airs picks it up. A permanently
        unavailable one is remembered so the Watch List stops reporting a track
        that can never be downloaded as "new" on every scan."""
        self._dbg.error(
            f"FINAL ERROR | {plan.title!r}  "
            f"URL: {plan.url}  "
            f"cookies={authenticated}  "
            f"full_error: {raw}")
        self._log.error(
            f"ERROR       | "
            f"Title: {plan.title} | "
            f"URL: {plan.url} | "
            f"Full error: {raw}")

        failure = classify_download_failure(clean, is_age=is_age)
        if failure.kind == "unavailable":
            self._db.record_unavailable(
                platform=plan.platform,
                video_id=plan.video_id or "",
                channel_url=canonical_channel_url(plan.suppress_channel_url),
                title=plan.title,
                reason=failure.reason)
        self._log_error(plan.title, plan.url, failure.reason)
        return Outcome(kind=failure.kind, reason=failure.reason,
                       title=plan.title)


def _kbps_int(value):
    """A configured bitrate ("192", "192 kbps") as an int, 0 when unreadable."""
    try:
        return int(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return 0


def _format_exc(exc):
    """The traceback of an exception carried out of its except block."""
    return "".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__))

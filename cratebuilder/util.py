"""Pure helpers: config persistence, date/path/title normalisation.

No tkinter imports — safe to unit-test in isolation.
"""
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, date, timedelta

def detect_platform(url):
    """Return 'SoundCloud' for a soundcloud.com URL, else 'YouTube' (default)."""
    if url and re.search(r"soundcloud\.com", url, re.IGNORECASE):
        return "SoundCloud"
    return "YouTube"


def derive_collection_name(info):
    """Pick the channel/collection display name from a yt-dlp info dict.

    Fallback order: title -> uploader -> uploader_id/handle (leading '@'
    stripped) -> channel_id -> "". Each candidate is stripped and skipped when
    empty or whitespace-only, so a blank/whitespace title falls through to the
    next usable value. The legacy " - Videos" suffix is stripped from whichever
    candidate wins so the folder is just the channel name. Never raises;
    returns "" when the dict carries no usable name."""
    info = info or {}
    candidates = (
        info.get("title"),
        info.get("uploader"),
        (info.get("uploader_id") or "").lstrip("@"),
        info.get("channel_id"),
    )
    name = ""
    for cand in candidates:
        cand = (cand or "").strip()
        if cand:
            name = cand
            break
    if name.endswith(" - Videos"):
        name = name[:-len(" - Videos")].strip()
    return name


def canonical_channel_key(url, channel_id=None, platform=None):
    """Return a stable identity string for matching a channel across the
    different URL forms yt-dlp may report for it (@handle, /channel/UC…,
    …/videos, …/streams, etc.).

    When a YouTube UC channel_id is present it dominates: every form of the
    same channel collapses to "yt:<channel_id>". Otherwise the URL is
    normalised — host lower-cased, leading "www." dropped, query/fragment and
    trailing slash removed, and trailing collection path segments
    (/videos, /streams, /featured, /playlists) stripped — yielding
    "url:<normalized>". Deterministic and total: never raises."""
    cid = (channel_id or "").strip()
    if cid:
        return f"yt:{cid}"

    raw = (url or "").strip()
    if not raw:
        return "url:"
    try:
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "").rstrip("/")
        for seg in ("/videos", "/streams", "/featured", "/playlists"):
            if path.lower().endswith(seg):
                path = path[:-len(seg)]
                break
        norm = (host + path) if host else (path or raw)
        return f"url:{norm.rstrip('/').lower()}"
    except Exception:
        return f"url:{raw.lower()}"


def find_matching_watchlist_row(rows, url, channel_id=None, platform=None):
    """Return the first row in *rows* that identifies the same channel as the
    given (url, channel_id, platform), or None.

    Match priority (each tier scanned across all rows before the next):
    (a) a non-empty row channel_id equal to the argument's channel_id;
    (b) exact url equality; (c) canonical_channel_key parity (which collapses
    the different URL forms of one channel). *rows* is an iterable of dicts each
    carrying at least 'url', 'channel_id', 'platform'. Total: never raises on
    missing keys."""
    rows = list(rows or ())
    cid = (channel_id or "").strip()

    if cid:
        for row in rows:
            if ((row or {}).get("channel_id") or "").strip() == cid:
                return row

    for row in rows:
        if (row or {}).get("url") == url:
            return row

    want_key = canonical_channel_key(url, channel_id=cid, platform=platform)
    for row in rows:
        row = row or {}
        row_key = canonical_channel_key(
            row.get("url"), channel_id=row.get("channel_id"),
            platform=row.get("platform"))
        if row_key == want_key:
            return row

    return None


# ── Runtime data directory ────────────────────────────────────────────────────
def runtime_data_dir(script_path=None):
    """Directory for runtime artefacts (activity.log, debug.log, cratebuilder.db).

    Normally the directory the app script lives in — matching every existing
    Windows and per-user Linux install, where that folder is writable. When it
    is NOT writable (a system-wide Linux install, e.g. the .deb placing the app
    under /opt/dj-cratebuilder), fall back to a per-user data dir and create it:
    %LOCALAPPDATA%\\DJ-CrateBuilder on Windows, ~/.local/share/DJ-CrateBuilder
    elsewhere. *script_path* defaults to sys.argv[0]; it is a parameter only so
    tests can exercise both branches. Never raises — if even the fallback can't
    be created, the script dir is returned and the caller fails as before."""
    app_dir = os.path.dirname(os.path.abspath(script_path or sys.argv[0]))
    if os.access(app_dir, os.W_OK):
        return app_dir
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "DJ-CrateBuilder")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return app_dir
    return path


def _dir_is_writable(path):
    """True only if a file can actually be created in *path*.

    os.access() is useless for this on Windows — it checks the read-only
    attribute, not ACLs — so probe with a real exclusive create + unlink."""
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, f".cb_probe_{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def _is_under(path, root):
    """True if *path* is *root* or inside it. False on empty/cross-drive."""
    if not path or not root:
        return False
    try:
        return os.path.commonpath(
            [os.path.abspath(path), os.path.abspath(root)]
        ) == os.path.abspath(root)
    except ValueError:
        return False


def ensure_usable_tempdir(base_dir=None):
    """Guarantee this process a writable temp directory.

    An app launched from the Windows Run key starts with CWD =
    C:\\Windows\\System32; when the normal temp candidates fail their write
    probe, CPython's last-resort fallback caches the CWD as the process temp
    dir, and every later tempfile call (e.g. yt-dlp's format checking) dies
    with 'Permission denied ...System32\\tmpXXXX.tmp'. This validates the
    current temp dir with a real write probe — rejecting anything under
    %SystemRoot% outright — and, when it is unusable, relocates to a private
    dir under *base_dir* (default: %LOCALAPPDATA% on Windows, ~/.cache
    elsewhere), pointing tempfile.tempdir and TEMP/TMP/TMPDIR at it so child
    processes (FFmpeg, Node) inherit the fix.

    Returns (tempdir, relocated). Never raises: if the fallback can't be
    created either, the original dir is returned with relocated=False."""
    try:
        current = tempfile.gettempdir()
    except Exception:
        current = None
    if (current
            and not _is_under(current, os.environ.get("SystemRoot"))
            and _dir_is_writable(current)):
        return current, False
    if base_dir is None:
        if os.name == "nt":
            base_dir = (os.environ.get("LOCALAPPDATA")
                        or os.path.expanduser("~"))
        else:
            base_dir = os.path.join(os.path.expanduser("~"), ".cache")
    fallback = os.path.join(base_dir, "DJ-CrateBuilder", "Temp")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        return current, False
    if not _dir_is_writable(fallback):
        return current, False
    tempfile.tempdir = fallback
    for var in ("TEMP", "TMP", "TMPDIR"):
        os.environ[var] = fallback
    return fallback, True


# ── Config persistence ────────────────────────────────────────────────────────
CONFIG_NAME = ".dj_cratebuilder_config.json"
LEGACY_CONFIG_NAME = ".yt_dj_cratebuilder_config.json"

def _config_path():
    return os.path.join(os.path.expanduser("~"), CONFIG_NAME)

def _legacy_config_path():
    return os.path.join(os.path.expanduser("~"), LEGACY_CONFIG_NAME)

def default_base_dir():
    """The crate root a fresh install downloads into. Resolved live so a
    changed home directory is honoured."""
    return os.path.join(os.path.expanduser("~"), "Music", "DJ-CrateBuilder")

def load_config():
    p = _config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Migrate from old config name if it exists
    old_p = os.path.join(os.path.expanduser("~"), ".yt_dj_cratebuilder_config.json")
    if os.path.exists(old_p):
        try:
            with open(old_p, "r", encoding="utf-8") as f:
                data = json.load(f)
            save_config(data)   # write to new location
            return data
        except Exception:
            pass
    return {}

def save_config(data):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def today_yyyymmdd():
    return date.today().strftime("%Y%m%d")


def days_ago_yyyymmdd(days):
    d = date.today() - timedelta(days=int(days))
    return d.strftime("%Y%m%d")


def subtract_days_from_yyyymmdd(yyyymmdd, days):
    """Safely subtract *days* from a YYYYMMDD string, returning a new
    YYYYMMDD string. Returns the input unchanged if parsing fails."""
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        return (dt - timedelta(days=int(days))).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return yyyymmdd


def format_yyyymmdd_readable(yyyymmdd):
    """Convert '20260310' to 'March 10, 2026' (or return input if invalid)."""
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        return dt.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        return str(yyyymmdd)


def format_timestamp_relative(ts):
    """Convert a unix timestamp into a short 'X days ago' / 'Never' string."""
    if not ts:
        return "Never"
    try:
        diff = int(time.time() - float(ts))
        if diff < 60:          return "Just now"
        if diff < 3600:
            m = diff // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if diff < 86400:
            h = diff // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        if diff < 86400 * 30:
            d = diff // 86400
            return f"{d} day{'s' if d != 1 else ''} ago"
        if diff < 86400 * 365:
            mo = diff // (86400 * 30)
            return f"{mo} month{'s' if mo != 1 else ''} ago"
        y = diff // (86400 * 365)
        return f"{y} year{'s' if y != 1 else ''} ago"
    except Exception:
        return "Unknown"


def interval_label_to_seconds(value):
    """Map an interval dropdown label ('6 hours', '1 day', '2 days', '1 week',
    'Off') to seconds, or None for 'Off'/blank/unknown.

    The label is '<integer> <unit>' where unit is hour(s), day(s), or week(s)."""
    try:
        if not value or value.strip().lower() == "off":
            return None
        parts = value.strip().split()
        n = int(parts[0])
        unit = parts[1].lower() if len(parts) > 1 else "hours"
        if unit.startswith("week"):
            return n * 7 * 86400
        if unit.startswith("day"):
            return n * 86400
        if unit.startswith("hour"):
            return n * 3600
        return None
    except (ValueError, AttributeError, IndexError):
        return None


# ── Automation timer decisions ────────────────────────────────────────────────
# Pure functions taking *now* as an argument. ADR 0001 declined injecting a
# clock into the app's scheduling methods; these carry the arithmetic and the
# rules instead, so the decisions are testable without one.

# A scheduled run that finds a manual scan or download already in flight waits
# this long and asks again, rather than interrupting the user's own work.
BUSY_RETRY_MS = 60_000

# The post-scan settle poll: how often to look, and how many looks before
# giving up so a wedged scan cannot poll forever (150 × 2s ≈ 5 minutes).
SCAN_SETTLE_POLL_MS = 2000
SCAN_SETTLE_MAX_POLLS = 150


def next_run_delay_ms(interval_seconds, last_run_ts, now):
    """When the next scheduled auto-download is due, as (delay_ms, next_ts).

    Returns None when there is no interval ('Off'), which is the caller's
    signal to disarm the timer and show no next-run time.

    *last_run_ts* is the anchor the interval counts from — falsy (never run)
    counts as the epoch, so a first launch is immediately overdue. An overdue
    run is scheduled 1 second out rather than at once: it still has to reach
    the caller through the event loop, and firing from inside the arming call
    would re-enter the scheduler."""
    if not interval_seconds:
        return None
    now = int(now)
    elapsed = now - int(last_run_ts or 0)
    delay_ms = 1000 if elapsed >= interval_seconds else int(
        (interval_seconds - elapsed) * 1000)
    return delay_ms, now + delay_ms // 1000


def scan_settle_verdict(scans_active, polls_so_far):
    """What a post-scan settle poll should do: "proceed" · "wait" · "give_up".

    "give_up" means the scans never settled inside the cap; the caller stamps
    the run anchor anyway so the next cycle is a full interval away instead of
    retrying a wedged scan forever."""
    if scans_active <= 0:
        return "proceed"
    if polls_so_far + 1 <= SCAN_SETTLE_MAX_POLLS:
        return "wait"
    return "give_up"


def next_run_label(ts, prefix="⏰  Next auto-download:  "):
    """The Watch List's next-run line: *prefix* plus a local timestamp, "Off"
    when there is no next run, or "—" if the timestamp will not render.

    The hour is stripped of its leading zero by hand rather than with a
    platform-specific strftime flag ('%-I' is not portable to Windows)."""
    if not ts:
        return f"{prefix}Off"
    try:
        dt = datetime.fromtimestamp(ts)
        hour = dt.strftime("%I").lstrip("0") or "12"
        return (f"{prefix}{dt.strftime('%a %b')} {dt.day}, "
                f"{dt.strftime('%Y')}  ·  {hour}:{dt.strftime('%M %p')}")
    except (ValueError, OSError, OverflowError):
        return f"{prefix}—"


# SoundCloud routes that are site structure, not artist profiles. The first
# path segment of a soundcloud.com URL is the artist handle UNLESS it is one of
# these reserved words.
SC_RESERVED_ROUTES = frozenset({
    "search", "discover", "stream", "you", "upload", "settings", "pro",
    "tags", "popular", "charts", "people", "mobile", "pages", "jobs",
    "imprint", "community-guidelines", "terms-of-use", "notifications",
    "messages", "library", "feed", "stations", "tracks", "albums", "sets",
    "reposts", "comments", "likes", "following", "followers", "embed",
})


def soundcloud_profile_handle(url):
    """Reduce any soundcloud.com URL (a bare profile, a track, a /sets/ link,
    etc.) to the artist handle in its first path segment, or None when the URL
    is not a soundcloud.com URL or points at a reserved site route rather than
    an artist. Lower-cased so the same artist from different URL forms collapses
    to one identity. Never raises."""
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except (ValueError, AttributeError):
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"):
        return None
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    handle = parts[0].lower()
    if handle in SC_RESERVED_ROUTES:
        return None
    return handle


def merge_soundcloud_candidates(track_hits, web_hits, max_results=8):
    """Combine artist-profile candidates from a yt-dlp track search and an
    invisible web search into one deduped, ranked list.

    Each input is a list of dicts carrying at least a 'url' (and optionally a
    'title'). Both URL forms (track URLs from the audio search, profile/result
    URLs from the web search) are reduced to the artist handle via
    ``soundcloud_profile_handle`` and merged on that handle. A profile surfaced
    by BOTH sources is the strongest signal and ranks first; track-only next;
    web-only last. First-seen order is preserved within each rank tier.

    Returns up to ``max_results`` dicts:
        {handle, url, title, sources: [..], confidence: 'both'|'tracks'|'web'}
    """
    order = []          # handles in first-seen order
    by_handle = {}

    def _add(hit, source):
        url = (hit.get("url") or "").strip() if isinstance(hit, dict) else ""
        handle = soundcloud_profile_handle(url)
        if not handle:
            return
        rec = by_handle.get(handle)
        if rec is None:
            rec = {"handle": handle,
                   "url": f"https://soundcloud.com/{handle}",
                   "title": handle, "sources": set()}
            by_handle[handle] = rec
            order.append(handle)
        rec["sources"].add(source)
        # Prefer a real human title. Track hits carry the cleanest artist name;
        # otherwise take any non-empty title over the bare handle.
        title = (hit.get("title") or "").strip()
        if title and (rec["title"] == handle or source == "tracks"):
            rec["title"] = title

    for hit in track_hits or []:
        _add(hit, "tracks")
    for hit in web_hits or []:
        _add(hit, "web")

    def _rank(handle):
        s = by_handle[handle]["sources"]
        if "tracks" in s and "web" in s:
            return 0
        if "tracks" in s:
            return 1
        return 2

    ranked = sorted(order, key=_rank)   # stable → preserves first-seen order
    out = []
    for handle in ranked[:max_results]:
        rec = by_handle[handle]
        srcs = sorted(rec["sources"])
        out.append({
            "handle": rec["handle"],
            "url": rec["url"],
            "title": rec["title"],
            "sources": srcs,
            "confidence": "both" if len(srcs) == 2 else srcs[0],
        })
    return out


def scan_folder_newest_mp3(folder):
    if not folder or not os.path.isdir(folder):
        return 0, None
    newest = None
    count  = 0
    try:
        for name in os.listdir(folder):
            if not name.lower().endswith(".mp3"):
                continue
            full = os.path.join(folder, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            count += 1
            if newest is None or mtime > newest:
                newest = mtime
    except OSError:
        return 0, None
    if newest is None:
        return 0, None
    return count, datetime.fromtimestamp(newest).strftime("%Y%m%d")


def normalize_track_key(name):
    """Collapse a video title or .mp3 filename to a comparison key so a
    YouTube title and its saved (sanitised) filename match despite case,
    punctuation, spacing, or mangled special characters.

    Deliberately aggressive — only alphanumerics survive — because the chosen
    trade-off is to *hide* a track when a match is uncertain (avoid showing a
    track the user already owns) rather than risk re-listing it. A saved file
    like "1788-L - �THERSUIT.mp3" and the real title "1788-L - ÆTHERSUIT"
    both reduce to the same key once non-alphanumerics are stripped."""
    if not name:
        return ""
    s = re.sub(r"\.(mp3|m4a|opus|webm|wav|flac|aac)$", "", str(name),
               flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def safe_filename(name, strip=False):
    """Replace characters illegal in a filename ( \\ / * ? : " < > | ) with '_'.

    The default raw form mirrors the actual on-disk filename. With strip=True it
    also trims surrounding whitespace — used for folder names and for matching
    'legacy' files saved before yt-dlp's own sanitiser was adopted. Returns ''
    for empty/None input."""
    safe = re.sub(r'[\\/*?:"<>|]', "_", name or "")
    return safe.strip() if strip else safe


def push_mru(items, value, limit):
    """Return a new most-recently-used list with *value* at the front,
    de-duplicated and capped at *limit*. Does not mutate *items*."""
    rest = [x for x in (items or []) if x != value]
    return ([value] + rest)[:limit]


# Keys in a yt-dlp options dict whose values carry authentication material
# (a cookie file path leaks the user's home directory; the browser-cookie
# source names the profile) and must never be written to debug.log.
SENSITIVE_YDL_KEYS = ("cookiefile", "cookiesfrombrowser")


def build_cookie_opts(method, cookie_file, browser, profile):
    """Return the yt-dlp cookie option(s) for the user's settings as a dict to
    merge into an options dict. The 'Cookie File' method uses the file path
    only when it exists on disk; any other method reads cookies from the named
    browser (lower-cased), optionally scoped to a *profile*. Returns {} when no
    cookie source applies (e.g. a missing/blank cookie file). Callers decide
    whether cookies are enabled at all — this only formats the chosen source."""
    if method == "Cookie File":
        if cookie_file and os.path.exists(cookie_file):
            return {"cookiefile": cookie_file}
        return {}
    b = (browser or "").lower()
    return {"cookiesfrombrowser": (b, profile) if profile else (b,)}


def download_result_facts(info):
    """Distil yt-dlp's extract_info(download=True) return into the facts the
    download loop records: (title, filepath, thumbnail_url, video_id).

    SoundCloud set flat-extraction returns entries with no title/thumbnail, so
    the queue falls back to a "Track N" placeholder — the real values only
    exist on the full info dict the download itself returns. *filepath* is the
    post-postprocessor path (the final .mp3), read from requested_downloads;
    it is the authoritative on-disk name and may differ from any path guessed
    from a placeholder title. Any missing fact is None. Never raises."""
    if not isinstance(info, dict):
        return (None, None, None, None)
    if not info.get("title") and isinstance(info.get("entries"), list):
        entries = [e for e in info["entries"] if isinstance(e, dict)]
        if entries:
            info = entries[0]
    filepath = None
    try:
        rd = info.get("requested_downloads") or []
        if rd and isinstance(rd[0], dict):
            filepath = rd[0].get("filepath") or None
    except Exception:
        filepath = None
    return (info.get("title") or None, filepath,
            info.get("thumbnail") or None, info.get("id") or None)


# Error wording meaning "this exists but has not been released yet". Matched
# against yt-dlp's message ONLY, never a track title: "Premiere:" is an
# ordinary SoundCloud naming convention for tracks that are fully released,
# so matching titles would silently hide real music.
_DEFERRED_ERROR_MARKERS = (
    "premieres in",
    "premiere will begin",
    "live event will begin",
    "this live stream recording is not available",
    "is not yet available",
    "waiting for the live stream",
)


def classify_deferred_failure(error_text):
    """Classify a yt-dlp download error as not-released-yet, or None.

    A premiere or scheduled stream is neither a permanent failure nor a real
    error: it fails today and succeeds on its own once it airs. Callers use
    this to keep the track pending and report it apart from genuine failures,
    rather than burning it as an error or — worse — filing it in the
    permanently-unavailable memory, from which it would never return. Check
    this BEFORE classify_permanent_failure so an upcoming track can never be
    mistaken for a removed one. Returns the label "Not out yet", or None.
    Case-insensitive; safe on None/empty input."""
    text = (error_text or "").lower()
    if not text:
        return None
    if any(m in text for m in _DEFERRED_ERROR_MARKERS):
        return "Not out yet"
    return None


def classify_permanent_failure(error_text):
    """Classify a yt-dlp download error as permanently unavailable, or None.

    Permanent causes are tracks nothing on our side can recover: SoundCloud
    DRM (only encrypted formats served — undecryptable even with login),
    removed/private tracks (HTTP 404), and geo-restrictions. Returns a short
    user-facing status label ("DRM-protected", "Removed", "Geo-blocked") so
    callers can show an honest status instead of a generic error and skip
    retries that cannot succeed. Returns None for anything transient or
    unclassified. Case-insensitive; safe on None/empty input."""
    text = (error_text or "").lower()
    if not text:
        return None
    if "drm" in text:
        return "DRM-protected"
    if "http error 404" in text or "404 not found" in text:
        return "Removed"
    if ("geo restriction" in text or "geo-restricted" in text
            or "not available in your country" in text
            or "not available from your location" in text):
        return "Geo-blocked"
    return None


# Substrings that identify a yt-dlp read failure as permanent — the target
# itself is gone, private, or was never a valid URL — so the link genuinely
# needs fixing. Checked BEFORE the transient markers: yt-dlp wraps a 404 in its
# generic "Unable to download webpage" text, and the 404 is the real signal.
PERMANENT_ERROR_MARKERS = (
    "404", "does not exist", "not found", "unsupported url",
    "is not a valid url", "unable to recognize tab page",
    "incomplete youtube id", "no longer available", "has been removed",
    "has been terminated", "is private", "this playlist is private",
    "private video", "unavailable videos are hidden",
)

# Substrings that identify a yt-dlp read failure as transient — no network, a
# blocked or throttled request, a server-side wobble. The link is fine; retry
# later.
TRANSIENT_ERROR_MARKERS = (
    "getaddrinfo", "name resolution", "name or service not known",
    "nodename nor servname", "failed to resolve", "no address associated",
    "network is unreachable", "network unreachable", "unreachable network",
    "connection reset", "connection aborted", "connection refused",
    "connection broken", "remote end closed", "timed out", "timeout",
    "urlopen error", "ssl", "handshake", "incompleteread", "temporary failure",
    "unable to download webpage", "unable to download api page",
    "http error 429", "http error 500", "http error 502", "http error 503",
    "http error 504", "too many requests", "service unavailable",
    "errno 11001", "errno 11004", "errno 10054", "errno 10060", "errno 101",
)


def classify_ydl_error(message):
    """Classify a yt-dlp read-only failure message by how durable it is.

    Returns 'permanent' (the target is gone/private/never valid — asking again
    cannot help), 'transient' (network down, throttled, server wobble — the
    same call may well succeed later), or 'unknown' when the message matches
    neither and we shouldn't guess. The single home for these markers: both
    cratebuilder.ydl (typed errors) and sidecar.classify_scan_error (watchlist
    row status) read the verdict from here. Case-insensitive; safe on
    None/empty input."""
    text = (message or "").lower()
    if not text:
        return "unknown"
    if any(m in text for m in PERMANENT_ERROR_MARKERS):
        return "permanent"
    if any(m in text for m in TRANSIENT_ERROR_MARKERS):
        return "transient"
    return "unknown"


_WHITESPACE_RE = re.compile(r"\s+")

# Decoration yt-dlp writes for someone reading a terminal, which carries nothing
# once the message has to become a short label: its own severity prefix (often
# doubled, because the raised DownloadError already contains one), the
# "[extractor] <id>:" stamp, and the "[download] Got error:" progress-line
# wrapper that arrives glued on behind a carriage return.
_ERROR_NOISE_RE = re.compile(
    r"^(?:\s*(?:ERROR|WARNING)\s*:"
    r"|\s*\[download\]\s*Got error\s*:"
    r"|\s*\[[^\]]{1,20}\]\s*[\w.-]{1,40}\s*:)+", re.I)

_URL_RE = re.compile(r"\bhttps?://\S+")

# The CLI advice and issue-tracker pointer yt-dlp appends. True, and useless in
# a label — the untouched message is in activity.log for anyone who wants it.
# Applied BEFORE the URL is dropped: "See  https://…" stripped the other way
# round leaves a bare "See" that no longer matches anything.
_ADVICE_RE = re.compile(r"\b(?:Use|See|Try|Pass)\s+\S.*$", re.I)


def condense_error(error_text, limit=60):
    """An error message as something short enough to show in a label.

    Slicing the first N characters is how a queue row came to read "ERROR:
    unable to download video data: HTTP Error 403: Forbid" — a sentence cut
    mid-word with its tail off the edge of a widget that does not scroll.

    So: strip yt-dlp's decoration, drop the URL and the CLI advice it appends,
    keep the first sentence of what is left, and truncate that on a word
    boundary. What comes out is a phrase rather than a fragment. Nothing is
    lost by it — every caller writes the untouched error to the logs before
    ever asking for a label. Returns "failed" when the message was pure
    decoration, which is exactly what a bare "ERROR:" is."""
    text = _WHITESPACE_RE.sub(" ", error_text or "").strip()
    text = _ERROR_NOISE_RE.sub("", text)
    text = _URL_RE.sub("", _ADVICE_RE.sub("", text))
    text = _WHITESPACE_RE.sub(" ", text).strip(" .,;:-")
    if not text:
        return "failed"
    text = text.split(". ")[0].strip(" .,;:-")
    if len(text) <= limit:
        return text or "failed"
    cut = text[:limit - 1]
    # Trim back to a word only when the cut lands mid-word AND the word before
    # it ends late enough to be worth it. "nsig extraction failed" breaks after
    # "nsig", which reads as no message at all — keeping the clipped second
    # word says more than dropping it.
    if not text[limit - 1:limit].isspace():
        space = cut.rfind(" ")
        if space >= limit // 2:
            cut = cut[:space]
    return (cut.rstrip(" .,;:-") or text[:limit - 1]) + "…"


# What a failed metadata fetch means, keyed by the verdict YdlSession already
# reached about it. The session weighed the message against the marker lists AND
# against network reachability (a captive portal answers 404 for everything), so
# these are keyed off the error's type rather than re-reading its text.
FETCH_FAILURE_TEXT = {
    "offline":   "Can't reach the site",
    "permanent": "Link is gone, private, or not a valid URL",
    "unknown":   "Fetch failed",
}


def describe_fetch_failure(kind, error_text="", limit=44):
    """A failed metadata fetch as one sentence for the Current label.

    Says what kind of problem it is first — the part the user can act on — and
    appends the condensed detail in brackets so a support question still has
    something to go on. The old text was "Fetch failed (" plus the error's first
    100 characters, which named no cause and ran off the end of a single-line
    label that neither wraps nor scrolls."""
    lead = FETCH_FAILURE_TEXT.get(kind, FETCH_FAILURE_TEXT["unknown"])
    detail = condense_error(error_text, limit) if error_text else ""
    if not detail or detail == "failed":
        return lead
    return f"{lead} ({detail})"


# ── Window placement ─────────────────────────────────────────────────────────
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def parse_window_geometry(text):
    """A Tk geometry string as (width, height, x, y), or None if it isn't one.

    Only the full "WxH+X+Y" form is accepted — a size-only or position-only
    string tells us too little to place a window safely. X and Y are signed:
    a monitor arranged to the left of or above the primary one has negative
    coordinates, and reading those as corrupt is what makes a remembered
    window jump back to the primary display on every launch."""
    match = _GEOMETRY_RE.match((text or "").strip())
    if not match:
        return None
    width, height, x, y = match.groups()
    return int(width), int(height), int(x), int(y)


def format_window_geometry(width, height, x, y):
    """(width, height, x, y) as a Tk geometry string. Tk needs an explicit
    sign on both offsets, which "+%d" gives for negatives too."""
    return f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}"


def _overlap(a, b):
    """Area shared by two (x, y, w, h) rectangles."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    wide = min(ax + aw, bx + bw) - max(ax, bx)
    tall = min(ay + ah, by + bh) - max(ay, by)
    return wide * tall if wide > 0 and tall > 0 else 0


def fit_window_geometry(remembered, screens, min_size=(640, 620)):
    """Where a remembered window should actually open, given the monitors that
    exist right now. Returns a Tk geometry string, or None to let the caller
    fall back to its own default placement.

    A remembered position is only trustworthy while the desktop it was saved on
    still exists. Restore it blindly and a window saved on a second monitor
    opens off-screen the next time the laptop is undocked — visible to the
    window manager, unreachable with the mouse. So the position is checked
    against the real monitor rectangles rather than against a single screen
    size, which is also what keeps a monitor arranged to the LEFT of the
    primary one working: its coordinates are negative, and any check that
    treats negative as invalid throws that arrangement away.

    *screens* is a list of (x, y, w, h) rectangles, primary first. The window
    is placed on whichever it overlaps most; if it overlaps none of them —
    the monitor it was on is gone — it is centred on the primary instead. The
    size is clamped to that monitor, so a window sized on a 4K display still
    opens usable on a 1080p laptop panel, and the position is nudged until the
    whole window is inside it. That nudge can pull a deliberately straddled
    window fully onto one monitor; a window nobody can reach is the worse of
    the two outcomes."""
    parsed = parse_window_geometry(remembered)
    if not parsed or not screens:
        return None
    width, height, x, y = parsed
    min_w, min_h = min_size

    best = max(screens, key=lambda s: _overlap((x, y, width, height), s))
    if not _overlap((x, y, width, height), best):
        best = screens[0]
        sx, sy, sw, sh = best
        width = max(min_w, min(width, sw))
        height = max(min_h, min(height, sh))
        return format_window_geometry(
            width, height, sx + (sw - width) // 2, sy + (sh - height) // 2)

    sx, sy, sw, sh = best
    width = max(min_w, min(width, sw))
    height = max(min_h, min(height, sh))
    x = min(max(x, sx), sx + sw - width)
    y = min(max(y, sy), sy + sh - height)
    return format_window_geometry(width, height, x, y)


def redact_ydl_opts(opts):
    """Return a shallow copy of a yt-dlp options dict made safe for debug
    logging. Auth-bearing values (cookie file path, browser-cookie source)
    are replaced with '<redacted>' when set; the progress-hook callback list
    is summarised by count; every other key passes through unchanged. Falsy
    auth values are left as-is so the log still shows whether cookies were
    configured. Returns {} for None/empty input."""
    safe = {}
    for k, v in (opts or {}).items():
        if k in SENSITIVE_YDL_KEYS:
            safe[k] = "<redacted>" if v else v
        elif k == "progress_hooks":
            safe[k] = f"[{len(v)} hook(s)]"
        else:
            safe[k] = v
    return safe


# Container extension families, shared by the artwork and tagging modules so
# the two never drift. Cover art and text tags use a different mechanism in
# each family, which is what these tuples are dispatched on.
MP4_EXTS  = (".m4a", ".mp4", ".m4b")
OGG_EXTS  = (".opus", ".ogg", ".oga")
WEBM_EXTS = (".webm", ".mkv")

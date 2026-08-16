"""Channel-folder sidecar (cratebuilder.json) helpers + resolution predicate."""
import json
import os
import re
import time
import urllib.parse

from cratebuilder.util import today_yyyymmdd, normalize_track_key

CHANNEL_SIDECAR_NAME = "cratebuilder.json"


def channel_url_from_id(channel_id):
    """Build the canonical, spaceless scan URL from a YouTube channel_id."""
    if not channel_id:
        return ""
    return f"https://www.youtube.com/channel/{channel_id}/videos"


def channel_id_from_url(url):
    """Pull a UC… channel id straight out of a /channel/ URL, if present.
    Inverse of channel_url_from_id; returns None when no id is found."""
    m = re.search(r"/channel/(UC[\w-]+)", url or "")
    return m.group(1) if m else None


def read_channel_sidecar(folder):
    """Return the parsed cratebuilder.json dict for a channel folder, or None."""
    if not folder:
        return None
    path = os.path.join(folder, CHANNEL_SIDECAR_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_channel_sidecar(folder, *, channel_id, channel_url=None, handle=None,
                          display_name=None, platform="YouTube", genre=None):
    """Write/update cratebuilder.json in a channel folder. Best-effort:
    failures are returned as False rather than raised, so a sidecar write can
    never break a download. Existing keys are preserved and overlaid."""
    if not folder or not os.path.isdir(folder):
        return False
    existing = read_channel_sidecar(folder) or {}
    meta = dict(existing)
    if channel_id:
        meta["channel_id"] = channel_id
    meta["channel_url"] = channel_url or channel_url_from_id(channel_id) \
        or meta.get("channel_url", "")
    if handle:
        meta["handle"] = handle
    if display_name:
        meta["display_name"] = display_name
    if platform:
        meta["platform"] = platform
    if genre is not None:
        meta["genre"] = genre
    meta["updated"] = today_yyyymmdd()
    try:
        path = os.path.join(folder, CHANNEL_SIDECAR_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def is_unresolved_channel(ch):
    """True if a watchlist row has no usable scan identifier yet.

    All platforms: an explicit needs_resolve status, the unresolved://
    sentinel, or a space in the URL (a folder-name URL) is unresolved.
    SoundCloud additionally requires a soundcloud.com URL (usernames are
    stable; there is no channel-id resolution). YouTube keeps the historical
    permissive rule — any clean, space-free, non-sentinel URL is resolved —
    so legacy /c/ and /user/ channel URLs are not falsely flagged.

    A failed scan is deliberately NOT unresolved. 'error'/'offline' mean the
    last scan blew up — usually because the network was down — which says
    nothing about the stored link. Treating them as unresolved is what used to
    strand every card behind an orange "Fix Link" button after one offline
    scan. Only classify_scan_error's needs_resolve verdict (a 404 / dead
    channel) may move a row into that state."""
    url = (ch.get("url") or "")
    if (ch.get("status") == "needs_resolve"
            or url.startswith("unresolved://")
            or " " in url):
        return True
    platform = (ch.get("platform") or "YouTube")
    if platform == "SoundCloud":
        return "soundcloud.com" not in url.lower()
    return False


# Substrings that identify a scan failure as permanent — the channel itself is
# gone, private, or was never a valid target — so the link genuinely needs
# fixing. Checked BEFORE the transient markers: yt-dlp wraps a 404 in its
# generic "Unable to download webpage" text, and the 404 is the real signal.
_PERMANENT_ERROR_MARKERS = (
    "404", "does not exist", "not found", "unsupported url",
    "is not a valid url", "unable to recognize tab page",
    "incomplete youtube id", "no longer available", "has been removed",
    "has been terminated", "is private", "this playlist is private",
    "private video", "unavailable videos are hidden",
)

# Substrings that identify a scan failure as transient — no network, a blocked
# or throttled request, a server-side wobble. The link is fine; retry later.
_TRANSIENT_ERROR_MARKERS = (
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


def classify_scan_error(message):
    """Map a scan failure message onto the status the row should take.

    Returns 'offline' for a transient failure (network down, throttled,
    server wobble — the link is fine, retry later), 'needs_resolve' for a
    permanent one (channel gone/private/never valid — the link must be
    fixed), or 'error' when the message matches neither and we shouldn't
    guess. Only 'needs_resolve' surfaces the Fix Link button."""
    text = (message or "").lower()
    if not text:
        return "error"
    if any(m in text for m in _PERMANENT_ERROR_MARKERS):
        return "needs_resolve"
    if any(m in text for m in _TRANSIENT_ERROR_MARKERS):
        return "offline"
    return "error"


def watch_scan_url(platform, url):
    """Return the URL to hand yt-dlp for a *listing* scan of this entry.

    YouTube: ensure the /videos tab for an @handle or /channel. SoundCloud:
    ensure the /tracks tab for a user. Idempotent — never double-appends, and
    leaves playlist/other URLs untouched."""
    url = (url or "").rstrip("/")
    if not url:
        return url
    if platform == "SoundCloud":
        return url if url.endswith("/tracks") else url + "/tracks"
    # YouTube
    if "/videos" in url:
        return url
    last = url.split("/")[-1]
    if last.startswith("@") or "/channel/" in url:
        return url + "/videos"
    return url


# yt-dlp live_status values meaning "no finished file exists to fetch yet".
# is_upcoming: a scheduled premiere or stream. is_live: mid-broadcast, so a
# download would capture a partial set. post_live: just ended, still being
# processed into a VOD. 'was_live' is deliberately absent — a finished
# archived stream is an ordinary downloadable video.
UNRELEASED_LIVE_STATUSES = frozenset(("is_upcoming", "is_live", "post_live"))


def is_unreleased_entry(entry, now=None):
    """True when a flat-playlist *entry* is scheduled but not downloadable yet.

    Judged on yt-dlp's live_status, falling back to a release_timestamp still
    in the future. Deliberately NOT judged on two tempting signals: the title
    (SoundCloud uses "Premiere:" for ordinary released tracks, so matching it
    would hide real music) and a missing duration (plenty of flat entries
    simply omit it). Unparseable timestamps count as released — this filter
    hides tracks, so it errs toward showing them."""
    if (entry.get("live_status") or "") in UNRELEASED_LIVE_STATUSES:
        return True
    ts = entry.get("release_timestamp")
    if ts:
        try:
            return float(ts) > (now if now is not None else time.time())
        except (TypeError, ValueError):
            return False
    return False


def classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec,
                          platform, is_unavailable=None, now=None):
    """Bucket yt-dlp flat-playlist *entries* into new vs already-owned tracks.

    Pure (no DB / tkinter / filesystem): the DB membership check is injected as
    *is_downloaded(video_id) -> bool*, and *folder_keys* maps a normalised track
    key (see normalize_track_key) to the path of the matching .mp3 already on
    disk. *limit_sec* is the Time-Limiter ceiling in seconds, or None to disable
    duration filtering (a value of 0 preserves the original loop's degenerate
    behaviour of dropping every video with a positive duration).

    Drop rules (entry ends up in neither bucket): its video_id is already in the
    DB, or — when filtering is on — its duration exceeds *limit_sec*. Entries
    with no/zero duration are kept (the download step re-filters as a backstop).
    A surviving entry whose normalised title matches a folder key is 'on_disk'
    (a legacy file to backfill then hide); otherwise it is 'new'.

    *is_unavailable(video_id)* is the permanently-unavailable memory: it returns
    a reason string ("DRM-protected", "Removed", "Geo-blocked") for a track that
    can never be downloaded, or a falsy value otherwise. None (the default)
    means "never suppressed". It is consulted after the DB check — a track that
    has since downloaded is a download — and before every other rule, so a
    suppressed track is reported as unavailable no matter how it would
    otherwise have been bucketed.

    An entry that is_unreleased_entry judges scheduled-but-not-out (a premiere
    or an in-progress stream) goes to 'upcoming' and is kept out of 'new', so
    the pending count and the Download button only ever offer tracks that can
    actually be fetched. Nothing is remembered about it: once it airs, the next
    scan sees an ordinary entry and offers it normally. This is checked right
    after the suppression memory, before every rule that could bucket it
    elsewhere. *now* is the epoch seconds to judge release_timestamp against
    (defaults to the wall clock; injectable for tests).

    Returns {"new": [...], "on_disk": [...], "unavailable": [...],
    "upcoming": [...]} where each new item is {id, title, url, upload_date},
    each on_disk item is {id, title, upload_date, file_path}, each unavailable
    item is {id, title, reason}, and each upcoming item is
    {id, title, release_timestamp}. The id is "" when the entry has none."""
    new_entries = []
    on_disk = []
    unavailable = []
    upcoming = []
    for e in entries:
        vid_id = e.get("id")
        if vid_id and is_downloaded(vid_id):
            continue
        if vid_id and is_unavailable is not None:
            reason = is_unavailable(vid_id)
            if reason:
                unavailable.append({
                    "id":     vid_id,
                    "title":  e.get("title") or "",
                    "reason": reason,
                })
                continue
        if is_unreleased_entry(e, now):
            upcoming.append({
                "id":                vid_id or "",
                "title":             e.get("title") or "",
                "release_timestamp": e.get("release_timestamp") or None,
            })
            continue
        if limit_sec is not None:
            dur = e.get("duration")
            if dur and dur > limit_sec:
                continue
        title = e.get("title") or ""
        key = normalize_track_key(title)
        if key and key in folder_keys:
            on_disk.append({
                "id":          vid_id or "",
                "title":       title,
                "upload_date": e.get("upload_date") or "",
                "file_path":   folder_keys[key],
            })
            continue
        new_entries.append({
            "id":          vid_id or "",
            "title":       title,
            "url":         (e.get("url") or e.get("webpage_url")
                            or (f"https://www.youtube.com/watch?v={vid_id}"
                                if platform == "YouTube" else "")),
            "upload_date": e.get("upload_date") or "",
        })
    return {"new": new_entries, "on_disk": on_disk,
            "unavailable": unavailable, "upcoming": upcoming}


def watch_fetch_url(platform, url):
    """The listing URL to hand yt-dlp, URL-encoded so a handle containing
    spaces (e.g. "@BASS ENTITY") isn't truncated at the first whitespace
    (which otherwise yields a 404). This is the exact URL both the Watch List
    scan and a Watch List "Download New" feed to yt-dlp, so each blows through
    the channel's catalogue in a single extraction. Idempotent — the path is
    decoded before re-encoding, so an already percent-encoded stored URL is
    never double-encoded. Returns "" for empty url."""
    scan = watch_scan_url(platform, url)
    if not scan:
        return scan
    parsed = urllib.parse.urlsplit(scan)
    return urllib.parse.urlunsplit(parsed._replace(
        path=urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/@&")))


LISTING_TAB_SUFFIXES = ("/tracks", "/videos")


def canonical_channel_url(url):
    """Strip the listing-tab suffix watch_scan_url appends, if present, and
    percent-decode.

    The inverse of watch_scan_url/watch_fetch_url: it maps a channel's stored
    URL and the /tracks or /videos listing URL a scan or download was actually
    run against (which watch_fetch_url percent-encodes) onto one key.
    Everything that records or looks up per-channel state uses this, so a row
    written during a Watch List download is found again from the channel's
    plain URL, even when the handle contains characters quote() encodes (e.g.
    a space). Decoding an already-decoded URL is a no-op. Idempotent; returns
    "" for empty input."""
    u = urllib.parse.unquote((url or "").rstrip("/"))
    low = u.lower()
    for suffix in LISTING_TAB_SUFFIXES:
        if low.endswith(suffix):
            return u[:-len(suffix)]
    return u

"""Channel-folder sidecar (cratebuilder.json) helpers + resolution predicate."""
import json
import os
import re
import urllib.parse

from cratebuilder.util import today_yyyymmdd, classify_ydl_error

CHANNEL_SIDECAR_NAME = "cratebuilder.json"

# The url a watchlist row carries while it has no link yet: a unique sentinel
# per Platform/Genre/Channel folder, so UNIQUE(url) holds and nothing bogus is
# ever scanned. The monolith and cratebuilder.db keep their own copies of the
# literal (neither may import the other); this one is the package's.
UNRESOLVED_URL_PREFIX = "unresolved://"


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
            or url.startswith(UNRESOLVED_URL_PREFIX)
            or " " in url):
        return True
    platform = (ch.get("platform") or "YouTube")
    if platform == "SoundCloud":
        return "soundcloud.com" not in url.lower()
    return False


# How a yt-dlp durability verdict reads as a watchlist row status. The marker
# lists themselves live in util.classify_ydl_error — the one home shared with
# cratebuilder.ydl, so a scan and a probe can never disagree about a message.
_SCAN_STATUS_FOR = {
    "permanent": "needs_resolve",
    "transient": "offline",
    "unknown": "error",
}


def classify_scan_error(message):
    """Map a scan failure message onto the status the row should take.

    Returns 'offline' for a transient failure (network down, throttled,
    server wobble — the link is fine, retry later), 'needs_resolve' for a
    permanent one (channel gone/private/never valid — the link must be
    fixed), or 'error' when the message matches neither and we shouldn't
    guess. Only 'needs_resolve' surfaces the Fix Link button."""
    return _SCAN_STATUS_FOR[classify_ydl_error(message)]


NON_LISTING_TABS = ("/featured", "/shorts", "/releases")


def watch_scan_url(platform, url):
    """Return the URL to hand yt-dlp for a *listing* scan of this entry.

    YouTube: ensure the /videos tab for an @handle or /channel, replacing a
    NON_LISTING_TABS suffix if the stored URL carries one — /featured is the
    channel's Home page, which yt-dlp returns as a handful of curated shelves
    rather than the uploads. SoundCloud: ensure the /tracks tab for a user.
    Idempotent — never double-appends, and leaves playlist, /streams and
    other URLs untouched."""
    url = (url or "").rstrip("/")
    if not url:
        return url
    if platform == "SoundCloud":
        return url if url.endswith("/tracks") else url + "/tracks"
    # YouTube
    for tab in NON_LISTING_TABS:
        if url.lower().endswith(tab):
            url = url[:-len(tab)]
            break
    if "/videos" in url:
        return url
    parts = url.split("/")
    if parts[-1].startswith("@") or (len(parts) >= 2 and parts[-2] == "channel"):
        return url + "/videos"
    return url


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

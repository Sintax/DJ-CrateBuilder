"""Channel-folder sidecar (cratebuilder.json) helpers + resolution predicate."""
import json
import os

from cratebuilder.util import today_yyyymmdd

CHANNEL_SIDECAR_NAME = "cratebuilder.json"


def channel_url_from_id(channel_id):
    """Build the canonical, spaceless scan URL from a YouTube channel_id."""
    if not channel_id:
        return ""
    return f"https://www.youtube.com/channel/{channel_id}/videos"


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

    All platforms: explicit needs_resolve/error status, the unresolved://
    sentinel, or a space in the URL (a folder-name URL) is unresolved.
    SoundCloud additionally requires a soundcloud.com URL (usernames are
    stable; there is no channel-id resolution). YouTube keeps the historical
    permissive rule — any clean, space-free, non-sentinel URL is resolved —
    so legacy /c/ and /user/ channel URLs are not falsely flagged."""
    url = (ch.get("url") or "")
    if (ch.get("status") in ("needs_resolve", "error")
            or url.startswith("unresolved://")
            or " " in url):
        return True
    platform = (ch.get("platform") or "YouTube")
    if platform == "SoundCloud":
        return "soundcloud.com" not in url.lower()
    return False


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

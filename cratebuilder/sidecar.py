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
    """True if a watchlist row has no usable YouTube identifier yet —
    either explicitly flagged needs_resolve, left in error by a prior
    failed scan, or carrying a folder-name URL that can't resolve (a
    space, or our unresolved:// sentinel)."""
    url = (ch.get("url") or "")
    return (ch.get("status") in ("needs_resolve", "error")
            or url.startswith("unresolved://")
            or " " in url)

"""Durable watchlist channel-link store (cratebuilder_links.json).

A DB-independent JSON mirror of each watchlist channel's resolved URL, keyed by
folder identity (Platform/Genre/DisplayName) so it survives a lost or corrupted
cratebuilder.db. The SQLite watchlist stays the source of truth; this store is a
best-effort backup and the source for the 'previous link' prefill on Fix Link.
"""
import json
import os

LINKS_FILE_NAME = "cratebuilder_links.json"


def link_key(platform, genre, display_name):
    """Stable identity for a watchlist channel, independent of DB row ids —
    mirrors the on-disk folder path Platform/Genre/Channel."""
    parts = [(platform or "").strip(),
             (genre or "").strip(),
             (display_name or "").strip()]
    return "/".join(parts)


def load_links(path):
    """Return the parsed link map, or {} on any read/parse failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_link(path, platform, genre, display_name):
    """Return the stored URL for a channel identity, or '' if none is known."""
    entry = load_links(path).get(link_key(platform, genre, display_name))
    if isinstance(entry, dict):
        return entry.get("url", "") or ""
    return ""


def save_link(path, *, platform, genre, display_name, url,
              channel_id=None, updated=None):
    """Mirror one channel's URL into the store. Best-effort: returns True on a
    durable write, False on any failure (never raises — a mirror write can
    never break a resolve). An empty url is a no-op."""
    if not url:
        return False
    data = load_links(path)
    key = link_key(platform, genre, display_name)
    entry = dict(data.get(key) or {})
    entry["url"] = url
    if channel_id:
        entry["channel_id"] = channel_id
    entry["platform"] = platform or ""
    entry["genre"] = genre or ""
    entry["display_name"] = display_name or ""
    if updated:
        entry["updated"] = updated
    data[key] = entry
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False

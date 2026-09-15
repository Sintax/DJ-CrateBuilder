"""Sharing a Watch List between users: the list file's shape, and what counts as a clash."""
import json
import re
import unicodedata
from datetime import date
from urllib.parse import urlsplit

from .crate import CrateLayout
from . import util

FORMAT = "dj-cratebuilder-watchlist"
VERSION = 1
FIELDS = ("url", "display_name", "platform", "genre", "channel_id")

# A list file is another user's data, and a list ENTRY can also arrive
# straight from a client over the remote transport — so every value is
# treated as hostile until sanitize_entry has had it. The limits are
# generous for any real Watch List and small enough that a crafted file
# cannot exhaust memory or stall the picker.
MAX_IMPORT_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_TEXT = 200
MAX_URL = 2048
ALLOWED_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com",
    "on.soundcloud.com"})
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ShareError(ValueError):
    """The file is not a Watch List export, or is too damaged to read."""


def default_filename(today=None):
    """The name the Save dialog offers, dated so two exports do not collide."""
    stamp = (today or date.today()).isoformat()
    return f"DJ-CrateBuilder Watch List {stamp}.json"


def export_entries(rows):
    """The shareable half of each row: what identifies the channel and where
    the sender filed it — never counts, scan dates, errors or local paths.
    Unresolved placeholder rows are left out; there is no link to share."""
    entries = []
    for row in rows or ():
        url = (row.get("url") or "").strip()
        if not url or url.startswith("unresolved://"):
            continue
        entries.append({
            "url": url,
            "display_name": (row.get("display_name") or "").strip(),
            "platform": row.get("platform") or util.detect_platform(url),
            "genre": row.get("genre") or CrateLayout.NO_GENRE_VALUE,
            "channel_id": (row.get("channel_id") or "").strip() or None,
        })
    return entries


def dumps(rows):
    return json.dumps({"format": FORMAT, "version": VERSION,
                       "channels": export_entries(rows)},
                      indent=2, ensure_ascii=False) + "\n"


def clean_text(value, limit=MAX_TEXT):
    """*value* as one line of plain text: control characters and invisible
    formatting characters (bidi overrides, zero-width joiners) removed, so
    a name can neither forge a log line nor disguise itself on screen."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in ("Cc", "Cf"))
    return text.strip()[:limit]


def _link_ok(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https")
            and (parts.hostname or "").lower() in ALLOWED_HOSTS)


def sanitize_entry(item):
    """One raw channel record → (clean entry, None), or (None, reason) when
    it cannot be imported at all. Only a missing or non-web link drops the
    entry; a bad genre falls back to no-genre, a bad channel id is
    forgotten, and the platform is read off the link rather than trusted.
    The single gate for both the list file and `watchlist.import_entry`."""
    if not isinstance(item, dict):
        return None, "an entry that is not a channel record"
    url = clean_text(item.get("url"), MAX_URL)
    if not url or url.startswith("unresolved://"):
        return None, "an entry with no link"
    if not _link_ok(url):
        return None, ("a link that is not a YouTube or SoundCloud web "
                      f"address ({url[:60]})")
    genre = clean_text(item.get("genre"))
    if CrateLayout.genre_dir_name(genre) == CrateLayout.NO_GENRE_DIR:
        genre = ""
    channel_id = item.get("channel_id")
    channel_id = clean_text(channel_id, 64) if isinstance(channel_id, str) else ""
    if channel_id and not _CHANNEL_ID.match(channel_id):
        channel_id = ""
    return {
        "url": url,
        "display_name": clean_text(item.get("display_name")),
        "platform": util.detect_platform(url),
        "genre": genre or CrateLayout.NO_GENRE_VALUE,
        "channel_id": channel_id or None,
    }, None


def parse(text):
    """The channels in a list file, each normalised to FIELDS, as
    (entries, dropped): the clean entries and one plain-language reason per
    entry that was thrown out. Raises ShareError for anything that is not
    one of these files, is too big to be one, or is too damaged to read."""
    if isinstance(text, str) and text.startswith("﻿"):
        text = text[1:]
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError, RecursionError):
        raise ShareError("That file is not a DJ-CrateBuilder Watch List "
                         "export (it is not readable as JSON).")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ShareError("That file is not a DJ-CrateBuilder Watch List "
                         "export.")
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version > VERSION:
        raise ShareError(f"That list was exported by a newer build (format "
                         f"version {version}) — update the app to import it.")
    raw = data.get("channels")
    if not isinstance(raw, list):
        raise ShareError("That Watch List export carries no channel list.")
    if len(raw) > MAX_ENTRIES:
        raise ShareError(f"That list holds too many channels ({len(raw):,}; "
                         f"the limit is {MAX_ENTRIES:,}).")
    entries, dropped = [], []
    for item in raw:
        entry, reason = sanitize_entry(item)
        if entry is None:
            dropped.append(reason)
        else:
            entries.append(entry)
    return entries, dropped


def name_key(name):
    """One spelling of a channel name for clash checks: case-folded, trimmed,
    inner whitespace collapsed — the name is also the folder name, and the
    filesystem the crate lives on is case-insensitive."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def name_is_taken(name, existing_rows):
    key = name_key(name)
    return bool(key) and any(
        name_key((row or {}).get("display_name")) == key
        for row in existing_rows or ())


def find_conflict(entry, existing_rows):
    """The row *entry* clashes with, or None. A link match (channel id, exact
    link, or any spelling of the same link) wins over a name match; `kinds`
    says which of the two matched that row, so the caller can tell the user
    what is the same and what is not. A link clash means the channel can't
    be added a second time; a name-only clash can, under a new name."""
    rows = list(existing_rows or ())
    row = util.find_matching_watchlist_row(
        rows, entry.get("url"), channel_id=entry.get("channel_id"),
        platform=entry.get("platform"))
    kinds = []
    if row is not None:
        kinds.append("link")
    else:
        key = name_key(entry.get("display_name"))
        for candidate in rows:
            if key and name_key((candidate or {}).get("display_name")) == key:
                row = candidate
                break
    if row is None:
        return None
    key = name_key(entry.get("display_name"))
    if key and key == name_key(row.get("display_name")):
        kinds.append("name")
    return {"row": row, "kinds": kinds}

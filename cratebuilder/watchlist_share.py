"""Sharing a Watch List between users: the list file's shape, and add-vs-skip."""
import json
from datetime import date

from .crate import CrateLayout
from . import util

FORMAT = "dj-cratebuilder-watchlist"
VERSION = 1
FIELDS = ("url", "display_name", "platform", "genre", "channel_id")


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


def parse(text):
    """The channels in a list file, each normalised to FIELDS. Raises
    ShareError for anything that is not one of these files; an entry with no
    usable link is dropped rather than failing the whole import."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
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
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url.startswith("unresolved://"):
            continue
        platform = str(item.get("platform") or "").strip()
        entries.append({
            "url": url,
            "display_name": str(item.get("display_name") or "").strip(),
            "platform": platform or util.detect_platform(url),
            "genre": str(item.get("genre") or "").strip()
                     or CrateLayout.NO_GENRE_VALUE,
            "channel_id": str(item.get("channel_id") or "").strip() or None,
        })
    return entries


def plan_import(entries, existing_rows):
    """Split *entries* into (to_add, skipped). A channel already tracked —
    by channel id, exact link, or any spelling of the same link — is
    skipped, and so is a second copy of one channel inside the same file.
    Existing rows are never changed by an import."""
    seen = list(existing_rows or ())
    to_add, skipped = [], []
    for entry in entries:
        match = util.find_matching_watchlist_row(
            seen, entry["url"], channel_id=entry.get("channel_id"),
            platform=entry.get("platform"))
        if match is not None:
            skipped.append(entry)
            continue
        to_add.append(entry)
        seen.append(entry)
    return to_add, skipped

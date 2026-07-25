"""Channel-folder sidecar (cratebuilder.json) helpers + resolution predicate."""
import json
import os
import re
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


def classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec,
                          platform, is_unavailable=None):
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

    Returns {"new": [...], "on_disk": [...], "unavailable": [...]} where each new
    item is {id, title, url, upload_date}, each on_disk item is
    {id, title, upload_date, file_path}, and each unavailable item is
    {id, title, reason}. The id is "" when the entry has none."""
    new_entries = []
    on_disk = []
    unavailable = []
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
            "unavailable": unavailable}


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

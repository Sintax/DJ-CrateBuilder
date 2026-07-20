"""Rebuild the downloads table from disk: audio discovery + artwork reuse."""
import os
import re

from cratebuilder import artwork as _artwork

AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".oga", ".flac",
              ".wav", ".mp4", ".m4b")

# The two YouTube URL shapes _tag_track writes into the comment/WOAS fields.
# SoundCloud URLs carry no id in the path, so they deliberately do not match —
# those tracks fall through to the filename-stem sidecar lookup instead.
_YT_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})"),
)


def _source_url(path):
    """Read the source URL our tagger stamped on *path*, or None.

    Checks the ID3 COMM/WOAS frames on an MP3 and the equivalent comment field
    on MP4 and Ogg. Never raises.
    """
    lower = path.lower()
    try:
        if lower.endswith(".mp3"):
            from mutagen.id3 import ID3
            tags = ID3(path)
            for frame in tags.getall("WOAS"):
                if getattr(frame, "url", None):
                    return frame.url
            for frame in tags.getall("COMM"):
                text = (getattr(frame, "text", None) or [None])[0]
                if text:
                    return text
            return None
        if lower.endswith((".m4a", ".mp4", ".m4b")):
            from mutagen.mp4 import MP4
            tags = MP4(path).tags or {}
            vals = tags.get("\xa9cmt") or []
            return vals[0] if vals else None
        if lower.endswith((".opus", ".ogg", ".oga")):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            opener = OggOpus if lower.endswith(".opus") else OggVorbis
            vals = opener(path).get("comment") or []
            return vals[0] if vals else None
    except Exception:
        return None
    return None


def recover_video_id(path):
    """Recover a track's YouTube video id from the tags on the file itself.

    A rebuild derives every row from disk, so without this the video_id column
    is None for every track — which breaks the `<video_id>.jpg` artwork key and
    makes the backfill re-fetch art it already has, writing a second identical
    JPEG under the filename stem. Reading the source URL our own tagger wrote
    keeps the key stable across a rebuild.

    Returns the 11-character id, or None when the file carries no source URL or
    the URL is not a YouTube one. Never raises.
    """
    if not path or not os.path.isfile(path):
        return None
    url = _source_url(path)
    if not url:
        return None
    for pattern in _YT_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def index_artwork_dir(channel_dir):
    """Map sidecar stem -> JPEG path for one channel folder's `.artwork/`.

    Listed once per channel folder rather than once per track: a 5,000-track
    rebuild does one listdir per channel, not five thousand.

    Returns {} when the folder does not exist. Never raises.
    """
    if not channel_dir:
        return {}
    art_dir = os.path.join(str(channel_dir), _artwork.ARTWORK_DIR_NAME)
    if not os.path.isdir(art_dir):
        return {}
    index = {}
    try:
        for name in os.listdir(art_dir):
            stem, ext = os.path.splitext(name)
            if ext.lower() in (".jpg", ".jpeg"):
                index[stem] = os.path.join(art_dir, name)
    except OSError:
        return {}
    return index


def resolve_artwork(path, video_id, art_index, snapshot=None):
    """Find the cover art already on disk for *path*. Reads only.

    Resolution order, all local — no network, and nothing is ever written or
    deleted:
      1. `.artwork/<video_id>.jpg`
      2. `.artwork/<filename-stem>.jpg`  (art left by an earlier rebuild)
      3. art embedded in the file itself
      4. the pre-wipe database snapshot, keyed by exact file path
      5. nothing — left blank for the Fetch Missing Artwork button

    Returns (artwork_path, artwork_embedded, thumbnail_url), matching the three
    downloads columns. Never raises.
    """
    snap = (snapshot or {}).get(path) or (None, 0, None)

    if video_id and video_id in art_index:
        return art_index[video_id], snap[1], snap[2]

    stem_key = _artwork.artwork_key(None, path)
    if stem_key and stem_key in art_index:
        return art_index[stem_key], snap[1], snap[2]

    try:
        if _artwork.has_cover(path):
            return snap[0], 1, snap[2]
    except Exception:
        pass

    return snap

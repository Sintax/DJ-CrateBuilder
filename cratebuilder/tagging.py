"""ID3 tag writing for downloaded MP3s (title, encoder, source URL). Tk-free."""
import os

try:
    from mutagen.id3 import ID3, TIT2, TENC, COMM, WOAS, ID3NoHeaderError
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    ID3 = None

ENCODED_BY = "DJ-CrateBuilder"


def tagging_available():
    """True when the mutagen backend is importable (it is a runtime dep)."""
    return ID3 is not None


def write_track_tags(path, title=None, source_url=None,
                     encoded_by=ENCODED_BY, overwrite=False):
    """Write our standard ID3 tags onto the MP3 at *path*.

    Fields written:
      * Title       (TIT2)
      * Encoded by  (TENC) -> "DJ-CrateBuilder"
      * Source URL  -> Comment (COMM, blank description, so it shows in the
                       Windows Explorer Details pane) AND the WOAS
                       "official audio source" URL frame.

    With *overwrite* False (the default) a field is only written when it is
    absent, so a tag the user edited by hand is never clobbered — this is what
    makes the function safe to run as a bulk backfill over an existing library.
    Audio frames are never touched; mutagen rewrites only the ID3 header.

    Returns True if the file was changed, False otherwise (including when the
    path is not an .mp3, mutagen is unavailable, or the write fails). Never
    raises — a tagging failure must not break a download batch.
    """
    if ID3 is None:
        return False
    if not path or not path.lower().endswith(".mp3") or not os.path.isfile(path):
        return False
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        changed = False
        if title and (overwrite or not tags.getall("TIT2")):
            tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
            changed = True
        if encoded_by and (overwrite or not tags.getall("TENC")):
            tags.setall("TENC", [TENC(encoding=3, text=[encoded_by])])
            changed = True
        if source_url:
            if overwrite or not tags.getall("COMM"):
                tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="",
                                          text=[source_url])])
                changed = True
            if overwrite or not tags.getall("WOAS"):
                tags.setall("WOAS", [WOAS(url=source_url)])
                changed = True

        if changed:
            # ID3v2.3 is the most broadly readable variant on Windows Explorer.
            tags.save(path, v2_version=3)
        return changed
    except Exception:
        return False


def read_source_url(path):
    """Read the source URL back out of the ID3 tags on the MP3 at *path*.

    The inverse of the `source_url` half of `write_track_tags`, which stores the
    URL in two places: the WOAS ("official audio source") frame and a
    blank-description COMM frame. WOAS is checked first — it is the typed URL
    frame and carries the value verbatim. When it is absent (a tag written by
    another tool, or one where only the comment survived a re-encode) every COMM
    frame is scanned and the first value that looks like a URL wins.

    This is what makes the artwork backfill possible for legacy SoundCloud
    tracks: their thumbnail URL cannot be derived from a track id the way a
    YouTube one can, so the original page URL recovered from the file's own tags
    is the only way back to the art.

    Returns the URL string, or None (non-MP3, missing file, untagged file,
    mutagen unavailable, or no URL present). Never raises.
    """
    if ID3 is None:
        return None
    if not path or not path.lower().endswith(".mp3") or not os.path.isfile(path):
        return None
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return None

        for frame in tags.getall("WOAS"):
            url = (getattr(frame, "url", "") or "").strip()
            if url:
                return url

        for frame in tags.getall("COMM"):
            for value in (getattr(frame, "text", None) or []):
                text = (value or "").strip()
                if text.lower().startswith("http"):
                    return text
        return None
    except Exception:
        return None

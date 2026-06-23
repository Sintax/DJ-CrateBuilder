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

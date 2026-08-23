"""Which CrateBuilder genre each library track belongs to, read from its path,
and the bulk repair that realigns a track's tags with what we know about it."""
import os

from cratebuilder.crate import CrateLayout
from cratebuilder.rebuild import AUDIO_EXTS
from cratebuilder.tagging import set_track_genre, write_track_tags_any


def _tag_genre_for_dir(genre_dir):
    """The genre tag value a `<platform>/<genre_dir>/` folder implies.

    CrateLayout owns reading a folder name back as a genre; all this adds is
    the tag-write convention that no genre is written as '' so the frame gets
    cleared rather than filled with the in-app sentinel. Tracks in the no-genre
    bucket have no genre, which is a real answer — the tag is cleared, not
    skipped."""
    value = CrateLayout.genre_value(genre_dir)
    return "" if value == CrateLayout.NO_GENRE_VALUE else value


def iter_library_tracks(platform_dirs):
    """Yield (track_path, genre) for every audio file in a CrateBuilder library.

    Walks exactly the layout the downloader writes —
    `<platform_dir>/<genre>/<channel>/<track>` — rather than recursing blindly,
    so a stray folder the user dropped in the base directory is never mistaken
    for a genre and nothing outside the library is ever tagged.

    Sorted at every level, so a caller can count first and then re-walk for a
    progress bar and get the same order both times. Unreadable directories are
    skipped rather than raised.
    """
    for platform_dir in platform_dirs:
        if not platform_dir or not os.path.isdir(platform_dir):
            continue
        try:
            genre_dirs = sorted(os.listdir(platform_dir))
        except OSError:
            continue
        for genre_dir in genre_dirs:
            genre_path = os.path.join(platform_dir, genre_dir)
            if not os.path.isdir(genre_path):
                continue
            genre = _tag_genre_for_dir(genre_dir)
            try:
                channel_dirs = sorted(os.listdir(genre_path))
            except OSError:
                continue
            for channel_dir in channel_dirs:
                channel_path = os.path.join(genre_path, channel_dir)
                if not os.path.isdir(channel_path):
                    continue
                try:
                    names = sorted(os.listdir(channel_path))
                except OSError:
                    continue
                for name in names:
                    if not name.lower().endswith(AUDIO_EXTS):
                        continue
                    full = os.path.join(channel_path, name)
                    if os.path.isfile(full):
                        yield full, genre


def count_library_tracks(platform_dirs):
    """How many audio files `iter_library_tracks` would yield."""
    return sum(1 for _ in iter_library_tracks(platform_dirs))


def iter_channel_tracks(channel_dir, genre):
    """Yield (track_path, genre) for one channel folder's own audio files.

    The single-channel counterpart to `iter_library_tracks`, used after a
    Watch List genre change so only the folder that actually moved is
    retagged. Top level only — a channel folder's `.artwork/` sidecars are
    not tracks. Sorted, and unreadable folders yield nothing rather than
    raising.
    """
    if not channel_dir or not os.path.isdir(channel_dir):
        return
    try:
        names = sorted(os.listdir(channel_dir))
    except OSError:
        return
    for name in names:
        if not name.lower().endswith(AUDIO_EXTS):
            continue
        full = os.path.join(channel_dir, name)
        if os.path.isfile(full):
            yield full, genre


def count_channel_tracks(channel_dir):
    """How many audio files `iter_channel_tracks` would yield."""
    return sum(1 for _ in iter_channel_tracks(channel_dir, ""))


# ── repairing one track's tags ────────────────────────────────────────────────

def index_by_path(facts):
    """Re-key a {file_path: value} snapshot for case-insensitive lookup.

    The database stores each path exactly as it was written; the library walk
    builds its own from the directory listing. On Windows the two can differ
    in case for the same file, and a miss there costs a track its real title
    for no reason. Falsy paths are dropped — they match nothing."""
    return {os.path.normcase(str(path)): value
            for path, value in (facts or {}).items() if path}


def lookup_facts(facts, path):
    """The (title, video_id, platform) recorded for *path*, or empty strings.

    Takes the index `index_by_path` produced; a path the database has never
    heard of is a normal answer, not an error — a file the user dropped into
    a channel folder by hand still gets repaired, just from its own name."""
    if not facts or not path:
        return "", "", ""
    return facts.get(os.path.normcase(str(path)), ("", "", ""))


def title_from_filename(path):
    """A track's title as its file name spells it.

    The fallback when the database has no row for the file. yt-dlp's sanitiser
    wrote that name *from* the real title, so it is the same text with a
    handful of characters substituted — worse than the stored title, and far
    better than leaving the track untitled.

    A falsy path answers '' rather than being stringified: str(None) is
    "None", and stamping that onto a file as its title would be worse than
    leaving it blank."""
    if not path:
        return ""
    try:
        stem = os.path.splitext(os.path.basename(str(path)))[0]
    except Exception:
        return ""
    return stem.strip()


def source_url_for(platform, video_id):
    """The watch URL a track id implies, or '' when none can be built.

    Only a YouTube id maps back to a URL. A SoundCloud track is addressed by
    its permalink, which its numeric id is not part of, so nothing can be
    reconstructed for one — those tracks get their title and genre repaired
    and keep no source URL, which is honest."""
    if not video_id:
        return ""
    if str(platform or "").strip().lower() != "youtube":
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


def repair_track(path, genre, title=None, source_url=None):
    """Realign one track's tags with what CrateBuilder knows about it.

    Two writes, because the fields answer to different rules. The genre is
    *forced*: the folder a track is filed under is what CrateBuilder treats as
    its genre, so a tag disagreeing with the folder is simply wrong. Title,
    encoder and source URL are only ever *filled in when absent*, so a title
    the user edited by hand is never touched — which is what makes this safe
    to run over a whole library, repeatedly.

    Returns (genre_changed, fields_filled). Never raises: both writers
    swallow their own failures, so one unwritable file cannot stop a sweep."""
    genre_changed = set_track_genre(path, genre)
    fields_filled = write_track_tags_any(
        path, title=title or None, source_url=source_url or None,
        genre=None, overwrite=False)
    return genre_changed, fields_filled

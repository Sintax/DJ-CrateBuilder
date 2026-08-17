"""Which CrateBuilder genre each library track belongs to, read from its path."""
import os

from cratebuilder.crate import CrateLayout
from cratebuilder.rebuild import AUDIO_EXTS


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

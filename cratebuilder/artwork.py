"""Cover art: thumbnail sidecar ingest (Pillow) + ID3 APIC embedding. Tk-free."""
import ctypes
import os

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a runtime dep
    Image = None

try:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    ID3 = None

# The three values the `cover_art_mode` config key may take. 'crop' centre-crops
# the 16:9 source to a square (real album-art look), 'original' embeds it as-is,
# 'off' disables artwork entirely (no download, no sidecar, no embed).
COVER_ART_MODES = ("crop", "original", "off")
DEFAULT_COVER_ART_MODE = "crop"

# Sidecar folder name inside each channel folder. Dot-prefixed so it is hidden
# on Linux; on Windows we additionally set FILE_ATTRIBUTE_HIDDEN.
ARTWORK_DIR_NAME = ".artwork"
_FILE_ATTRIBUTE_HIDDEN = 0x02

JPEG_QUALITY = 90


def artwork_available():
    """True when both backends (Pillow, mutagen) are importable.

    Both are runtime deps; the check exists so callers can skip the artwork
    step cleanly on a stripped install rather than logging a failure per track.
    """
    return Image is not None and ID3 is not None


def thumbnail_dir(track_dir):
    """Return the sidecar artwork folder for a channel folder, creating it.

    The folder is `<track_dir>/.artwork/`. The dot prefix hides it on Linux and
    keeps it out of rekordbox/Serato folder scans; on Windows the hidden
    attribute is set on top of that so it does not clutter Explorer. Setting the
    attribute is best-effort — a failure there still yields a usable folder.

    Returns the folder path, or None when *track_dir* is falsy or the folder
    cannot be created. Never raises.
    """
    if not track_dir:
        return None
    path = os.path.join(track_dir, ARTWORK_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetFileAttributesW(
                str(path), _FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass
    return path


def _centre_square(img):
    """Centre-crop *img* to a square using its shorter side.

    A 1280x720 source yields 720x720 taken from the horizontal centre — the
    subject of a YouTube thumbnail is almost always centred, so this is the crop
    that loses the least.
    """
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def ingest_thumbnail(raw_path, art_dir, video_id, mode=DEFAULT_COVER_ART_MODE):
    """Convert whatever yt-dlp wrote into the sidecar JPEG for one track.

    *raw_path* is the thumbnail yt-dlp saved next to the audio file — `.webp`
    from YouTube, `.jpg` from SoundCloud, occasionally `.png`. It is opened with
    Pillow, flattened to RGB, shaped per *mode*, and written as JPEG quality 90
    to `<art_dir>/<video_id>.jpg`. The raw file is then removed (best-effort) so
    the crate folder is left with audio only.

    Modes:
      * "crop"     — centre-crop to a square on the shorter side (the default).
      * "original" — no geometry change; the source aspect is embedded as-is.
      * "off"      — do nothing at all and return None.

    Art is keyed by *video_id* because that id is stable, collision-free and
    already stored on the downloads row — a falsy id means we cannot match the
    art back to a track, so it is treated as a no-op.

    Returns the path to the written JPEG, or None on any failure (missing source,
    corrupt image, unwritable folder, Pillow absent). Never raises — an artwork
    failure must not fail a track.
    """
    if mode == "off":
        return None
    if Image is None:
        return None
    if not video_id or not raw_path or not art_dir:
        return None
    if not os.path.isfile(raw_path):
        return None

    out_path = os.path.join(art_dir, f"{video_id}.jpg")
    try:
        with Image.open(raw_path) as img:
            img.load()
            rgb = img.convert("RGB")
            if mode == "crop":
                rgb = _centre_square(rgb)
            rgb.save(out_path, "JPEG", quality=JPEG_QUALITY)
    except Exception:
        return None

    try:
        os.remove(raw_path)
    except OSError:
        pass
    return out_path


def embed_cover(audio_path, jpg_path):
    """Embed *jpg_path* as the front-cover APIC frame on the MP3 at *audio_path*.

    The frame is written as type 3 ("front cover"), mime `image/jpeg`, and the
    tag is saved as ID3 v2.3 to match `tagging.write_track_tags` — v2.3 is the
    variant Windows Explorer and Android players read most reliably. Any existing
    APIC frames are dropped first, so re-embedding replaces the art instead of
    accumulating duplicate frames.

    Only MP3s are handled. When "keep original format" is on the file is a
    `.webm`/`.m4a` whose cover art uses an entirely different container frame —
    that is a no-op here, not a failure.

    Returns True if the file was changed, False otherwise (non-MP3, missing
    audio or image, mutagen unavailable, or the write failed). Never raises.
    """
    if ID3 is None:
        return False
    if not audio_path or not audio_path.lower().endswith(".mp3"):
        return False
    if not os.path.isfile(audio_path):
        return False
    if not jpg_path or not os.path.isfile(jpg_path):
        return False
    try:
        with open(jpg_path, "rb") as fh:
            data = fh.read()
        if not data:
            return False

        try:
            tags = ID3(audio_path)
        except ID3NoHeaderError:
            tags = ID3()

        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                      data=data))
        tags.save(audio_path, v2_version=3)
        return True
    except Exception:
        return False


def has_cover(audio_path):
    """True when the MP3 at *audio_path* already carries an APIC frame.

    The predicate the phase-2 "fetch missing artwork" backfill filters on.
    Returns False for a non-MP3, a missing file, an untagged file, or when
    mutagen is unavailable. Never raises.
    """
    if ID3 is None:
        return False
    if not audio_path or not audio_path.lower().endswith(".mp3"):
        return False
    if not os.path.isfile(audio_path):
        return False
    try:
        return bool(ID3(audio_path).getall("APIC"))
    except Exception:
        return False

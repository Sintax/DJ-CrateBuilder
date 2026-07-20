"""Cover art: thumbnail sidecar ingest (Pillow) + ID3 APIC embedding. Tk-free."""
import base64
import ctypes
import os
import shutil
import subprocess
import urllib.request

from cratebuilder.util import safe_filename, MP4_EXTS, OGG_EXTS, WEBM_EXTS

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a runtime dep
    Image = None

try:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    ID3 = None

try:
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    MP4 = None

try:
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    Picture = None

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


def embed_cover_mp4(audio_path, jpg_path):
    """Embed *jpg_path* as the cover atom on the MP4/M4A at *audio_path*.

    MP4 stores cover art in the `covr` atom of the iTunes metadata box, not in
    an ID3 frame — a completely different mechanism from `embed_cover`. Assigning
    a single-element list replaces any existing art rather than appending, so
    re-embedding never accumulates duplicates.

    Returns True if the file was changed, False otherwise. Never raises.
    """
    if MP4 is None:
        return False
    if not audio_path or not audio_path.lower().endswith(MP4_EXTS):
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
        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True
    except Exception:
        return False


def embed_cover_ogg(audio_path, jpg_path):
    """Embed *jpg_path* as the cover picture on the Ogg file at *audio_path*.

    Ogg (Opus and Vorbis) carries art as a base64-encoded FLAC picture block in
    the `metadata_block_picture` Vorbis comment. The comment is overwritten
    wholesale, so re-embedding replaces the art.

    Returns True if the file was changed, False otherwise. Never raises.
    """
    if Picture is None:
        return False
    if not audio_path or not audio_path.lower().endswith(OGG_EXTS):
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

        pic = Picture()
        pic.data = data
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.depth = 24
        # Dimensions are advisory in a FLAC picture block; 0x0 is legal. Reading
        # them needs Pillow, so a stripped install still embeds working art.
        pic.width, pic.height = 0, 0
        if Image is not None:
            try:
                with Image.open(jpg_path) as im:
                    pic.width, pic.height = im.size
            except Exception:
                pass

        opener = OggOpus if audio_path.lower().endswith(".opus") else OggVorbis
        audio = opener(audio_path)
        audio["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
        return True
    except Exception:
        return False


# Windows: keep the FFmpeg console window from flashing on every remux.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# The remux is a lossless stream copy (`-c:a copy`) — no re-encode, so the
# encoded audio payload in the output is essentially identical to the
# source's, at any track length. That makes the check proportional to the
# source rather than a flat byte count: an absolute floor can't tell
# "truncated write of a 4 MB track" from "complete write of a tiny test
# fixture", but a check scaled to the source works at any file size. The
# ratio sits well under 1 because Matroska (WebM) framing carries noticeably
# more per-frame overhead than Ogg's — on real, non-silent audio the two
# containers end up within a couple of percent of each other, but on
# near-silent audio (the 1-second fixtures this suite uses) that overhead
# dominates and the true ratio can fall as low as ~0.42. 0.3 stays safely
# under that floor while still rejecting anything an order of magnitude too
# small — the actual truncated-write failure mode this guards against. The
# tiny absolute floor alongside it exists only to reject a zero-byte or
# near-zero output outright, in case the source itself is implausibly small.
_MIN_REMUX_SIZE_RATIO = 0.3
_MIN_REMUX_ABS_BYTES = 32


def _ffmpeg_exe(ffmpeg_dir=None):
    """Absolute path to ffmpeg, or None when it cannot be found.

    Prefers *ffmpeg_dir* (the bundled folder in a packaged build) and falls
    back to PATH, mirroring how the app points yt-dlp at FFmpeg.
    """
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if ffmpeg_dir:
        cand = os.path.join(str(ffmpeg_dir), name)
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg")


def _ffprobe_exe(ffmpeg_dir=None):
    """Absolute path to ffprobe, or None when it cannot be found.

    Mirrors `_ffmpeg_exe`: prefers *ffmpeg_dir* (the bundled folder in a
    packaged build) and falls back to PATH.
    """
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if ffmpeg_dir:
        cand = os.path.join(str(ffmpeg_dir), name)
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffprobe")


def _probe_audio_codec(audio_path, ffmpeg_dir=None):
    """Return the lower-cased codec name of *audio_path*'s first audio stream,
    or None on any failure (no ffprobe, no audio stream, a bad file). Never
    raises.
    """
    exe = _ffprobe_exe(ffmpeg_dir)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            return None
        name = proc.stdout.decode("utf-8", "ignore").strip().lower()
        return name or None
    except Exception:
        return None


def remux_webm_to_opus(audio_path, ffmpeg_dir=None):
    """Rewrap the Opus stream inside a WebM file into an Ogg container.

    WebM stores cover art as a Matroska attachment element, which mutagen cannot
    write at all. Ogg carries it as a Vorbis comment, which mutagen can. The
    audio is stream-copied (`-c:a copy`), so this is lossless and fast — no
    re-encode, the samples are bit-identical.

    `-c:a copy -f ogg` succeeds on ANY audio codec, not just Opus — yt-dlp's
    format selection can hand back a WebM whose audio is Vorbis (e.g. via a
    `best` fallback). Copying that into a file named `.opus` would produce an
    Ogg-framed file that is actually Vorbis, which `embed_cover_ogg` then opens
    with the wrong reader and silently fails. So the source's actual codec is
    probed first, and anything other than Opus is refused before FFmpeg ever
    runs.

    The source is deleted only after the output exists and is plausibly sized,
    so a failed remux leaves the original file intact.

    Returns the new `.opus` path, or None when FFmpeg is unavailable, the
    source audio is not Opus, or the remux failed. Never raises.
    """
    if not audio_path or not audio_path.lower().endswith(WEBM_EXTS):
        return None
    if not os.path.isfile(audio_path):
        return None

    exe = _ffmpeg_exe(ffmpeg_dir)
    if not exe:
        return None

    if _probe_audio_codec(audio_path, ffmpeg_dir) != "opus":
        return None

    out_path = os.path.splitext(audio_path)[0] + ".opus"
    tmp_path = out_path + ".part"
    try:
        proc = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", audio_path,
             "-vn", "-c:a", "copy", "-f", "ogg", tmp_path],
            capture_output=True, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg exited non-zero")
        src_size = os.path.getsize(audio_path)
        out_size = os.path.getsize(tmp_path)
        if out_size < _MIN_REMUX_ABS_BYTES or out_size < src_size * _MIN_REMUX_SIZE_RATIO:
            raise RuntimeError("remux output implausibly small")

        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None

    # The replace above is the point of no return: a valid .opus now exists at
    # out_path, so the conversion is complete. Deleting the source is best
    # effort tidy-up, not part of success — on Windows an AV scanner or a
    # lingering handle can make this fail even though nothing is actually
    # wrong, and that must not discard a completed remux.
    try:
        os.remove(audio_path)
    except OSError:
        pass
    return out_path


def embed_cover_any(audio_path, jpg_path, ffmpeg_dir=None):
    """Embed *jpg_path* into *audio_path*, whatever container it is in.

    Dispatches on extension: MP3 uses the ID3 APIC frame, MP4/M4A the `covr`
    atom, Ogg/Opus a Vorbis-comment picture block. WebM has no writable art
    mechanism, so it is first remuxed to Opus (lossless, see
    `remux_webm_to_opus`) and then embedded — which is why the audio path can
    change.

    Returns (final_audio_path, embedded). The path is the input path unless a
    remux occurred. Never raises.
    """
    if not audio_path:
        return audio_path, False
    if not jpg_path or not os.path.isfile(jpg_path):
        return audio_path, False

    lower = audio_path.lower()
    if lower.endswith(".mp3"):
        return audio_path, embed_cover(audio_path, jpg_path)
    if lower.endswith(MP4_EXTS):
        return audio_path, embed_cover_mp4(audio_path, jpg_path)
    if lower.endswith(OGG_EXTS):
        return audio_path, embed_cover_ogg(audio_path, jpg_path)
    if lower.endswith(WEBM_EXTS):
        remuxed = remux_webm_to_opus(audio_path, ffmpeg_dir)
        if not remuxed:
            return audio_path, False
        return remuxed, embed_cover_ogg(remuxed, jpg_path)
    return audio_path, False


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


def has_cover_any(audio_path):
    """True when *audio_path* already carries embedded cover art, whatever
    container it is in.

    Dispatches by extension the same way `embed_cover_any` does: MP3 delegates
    to `has_cover`, MP4/M4A checks the `covr` atom, Ogg/Opus checks the
    `metadata_block_picture` Vorbis comment. WebM has no readable art mechanism
    (mutagen cannot parse a Matroska attachment element), so it is always False
    here — the caller falls back to the remux-then-check path instead.

    Returns False for a missing file, an untagged file, a container mutagen
    cannot open, or when the relevant mutagen submodule is unavailable. Never
    raises.
    """
    if not audio_path:
        return False
    lower = audio_path.lower()
    if lower.endswith(".mp3"):
        return has_cover(audio_path)
    if not os.path.isfile(audio_path):
        return False
    try:
        if lower.endswith(MP4_EXTS):
            if MP4 is None:
                return False
            audio = MP4(audio_path)
            return bool(audio.tags and audio.tags.get("covr"))
        if lower.endswith(OGG_EXTS):
            if Picture is None:
                return False
            opener = OggOpus if lower.endswith(".opus") else OggVorbis
            audio = opener(audio_path)
            return bool(audio.get("metadata_block_picture"))
    except Exception:
        return False
    return False


def extract_cover(audio_path):
    """Return the raw bytes of the MP3's embedded cover image, or None.

    Prefers the front-cover frame (APIC type 3) and falls back to whatever
    picture is present. Lets the Database Viewer's preview still show art for a
    track whose sidecar JPEG has been deleted off disk. Never raises.
    """
    if ID3 is None:
        return None
    if not audio_path or not audio_path.lower().endswith(".mp3"):
        return None
    if not os.path.isfile(audio_path):
        return None
    try:
        frames = ID3(audio_path).getall("APIC")
    except Exception:
        return None
    if not frames:
        return None
    for frame in frames:
        if getattr(frame, "type", None) == 3 and getattr(frame, "data", None):
            return frame.data
    return getattr(frames[0], "data", None) or None


# ── backfill: locating art for a track that was downloaded before this feature ──

# YouTube thumbnail endpoints, best quality first. maxresdefault only exists for
# uploads whose source was at least 1280x720, so it 404s on a large slice of the
# older catalogue; hqdefault is generated for every video ever uploaded and is
# therefore the guaranteed fallback rather than an optional extra.
_YT_THUMB_TEMPLATES = (
    "https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
    "https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
)

# Fetch budget for a single thumbnail. Long enough for a slow CDN edge, short
# enough that a backfill over a few thousand tracks cannot wedge on one bad URL.
DOWNLOAD_TIMEOUT = 15


def artwork_key(video_id, file_path):
    """Return the stable filename stem for a track's sidecar JPEG.

    Fresh downloads are keyed by *video_id* — stable, collision-free, and already
    on the downloads row. Legacy rows rebuilt from disk have no video_id at all,
    so the audio file's own basename stem is used instead, sanitised through
    `util.safe_filename` because a track title can carry characters (`:`, `?`,
    `|`) that are legal in a tag but not in a filename.

    The fallback is deliberately derived from the *file* and not the title: it
    stays in step with the track as long as the file is not renamed, and it is
    what lets `existing_sidecar` find art we have already fetched.

    Returns None when neither source yields a usable stem. Never raises.
    """
    if video_id:
        return str(video_id)
    if not file_path:
        return None
    try:
        stem = os.path.splitext(os.path.basename(str(file_path)))[0]
    except Exception:
        return None
    key = safe_filename(stem, strip=True)
    return key or None


def youtube_thumbnail_urls(video_id):
    """Ordered candidate thumbnail URLs for a YouTube video id, best first.

    maxresdefault is the 1280x720 original and is what we want, but it is only
    generated for uploads that were HD to begin with — on an older or low-res
    video it 404s. hqdefault (480x360) exists for every video, so it is the
    always-present fallback the caller drops to.

    Returns () for a falsy id.
    """
    if not video_id:
        return ()
    return tuple(t.format(vid=video_id) for t in _YT_THUMB_TEMPLATES)


def thumbnail_url_candidates(platform, video_id, stored_url=None):
    """Ordered direct-fetch thumbnail URLs to try for one track. No network.

    Pure URL construction — the caller decides how many to actually fetch.

    Order:
      1. *stored_url* — the `thumbnail_url` column, when the row has one. It is
         the exact URL yt-dlp reported at download time, so it is always the
         best guess.
      2. For YouTube with a video_id, the `youtube_thumbnail_urls` candidates,
         which can be reconstructed from the id alone.

    De-duplicated with order preserved, so a stored_url that happens to be one of
    the ytimg URLs is not fetched twice.

    A SoundCloud track with no stored_url yields an empty tuple: its artwork URL
    is not derivable from the track id, and the caller must fall back to a yt-dlp
    metadata lookup against the source page URL. Never raises.
    """
    candidates = []
    if stored_url:
        candidates.append(str(stored_url))
    if platform and str(platform).strip().lower() == "youtube":
        candidates.extend(youtube_thumbnail_urls(video_id))

    seen = set()
    ordered = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return tuple(ordered)


def download_thumbnail(url, dest_path, opener=None, timeout=DOWNLOAD_TIMEOUT):
    """Fetch the image at *url* to *dest_path*. Returns dest_path or None.

    *opener* is the injection seam: a callable(url, timeout=...) returning a
    file-like with `.read()`, defaulting to `urllib.request.urlopen`. Tests pass
    a fake so the suite stays offline.

    The body is read fully before anything is written, so an empty or failed
    response leaves no zero-byte file behind for `existing_sidecar` to later
    mistake for real art — an important property when a maxresdefault 404 is the
    expected path for a large share of the catalogue.

    Returns None on any failure (HTTP error, timeout, empty body, unwritable
    destination). Never raises — a missing thumbnail must not fail a backfill.
    """
    if not url or not dest_path:
        return None
    fetch = opener or urllib.request.urlopen
    try:
        resp = fetch(url, timeout=timeout)
        try:
            data = resp.read()
        finally:
            closer = getattr(resp, "close", None)
            if callable(closer):
                closer()
        if not data:
            return None
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return dest_path
    except Exception:
        return None


def existing_sidecar(art_dir, key):
    """Return `<art_dir>/<key>.jpg` when that sidecar is already on disk.

    The backfill's first move for every track: art fetched on a previous run (or
    left behind by a re-encode that stripped the tag) can be re-embedded without
    touching the network at all.

    Returns None when *art_dir* or *key* is falsy or the file does not exist.
    Never raises.
    """
    if not art_dir or not key:
        return None
    try:
        path = os.path.join(str(art_dir), f"{key}.jpg")
        return path if os.path.isfile(path) else None
    except Exception:
        return None

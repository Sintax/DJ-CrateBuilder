"""Tests for cratebuilder.artwork: thumbnail ingest + ID3 APIC embedding."""
import os

import pytest

from cratebuilder import artwork

pytest.importorskip("PIL")
pytest.importorskip("mutagen")

from PIL import Image  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402

from tests.conftest import make_silent, requires_ffmpeg


# Same minimal silent MP3 frame tests/test_tagging.py uses: MPEG-1 Layer III,
# 128kbps, 44.1kHz. Enough for mutagen to attach and read back an ID3 tag.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path):
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    return str(path)


def _make_image(path, size=(1280, 720), fmt=None):
    img = Image.new("RGB", size, (10, 120, 200))
    img.save(str(path), fmt)
    return str(path)


# ── thumbnail_dir ─────────────────────────────────────────────────────────────
def test_thumbnail_dir_creates_dot_artwork(tmp_path):
    d = artwork.thumbnail_dir(str(tmp_path))
    assert d == os.path.join(str(tmp_path), ".artwork")
    assert os.path.isdir(d)


def test_thumbnail_dir_is_idempotent(tmp_path):
    first = artwork.thumbnail_dir(str(tmp_path))
    second = artwork.thumbnail_dir(str(tmp_path))
    assert first == second
    assert os.path.isdir(second)


def test_thumbnail_dir_falsy_input_returns_none():
    assert artwork.thumbnail_dir("") is None
    assert artwork.thumbnail_dir(None) is None


# ── ingest_thumbnail ──────────────────────────────────────────────────────────
def test_crop_mode_yields_centred_square(tmp_path):
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    out = artwork.ingest_thumbnail(raw, art_dir, "vid123", mode="crop")

    assert out == os.path.join(art_dir, "vid123.jpg")
    with Image.open(out) as img:
        assert img.size == (720, 720)


def test_crop_mode_uses_shorter_side_when_portrait(tmp_path):
    raw = _make_image(tmp_path / "raw.png", (400, 900), "PNG")
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    out = artwork.ingest_thumbnail(raw, art_dir, "tall", mode="crop")

    with Image.open(out) as img:
        assert img.size == (400, 400)


def test_original_mode_preserves_aspect(tmp_path):
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    out = artwork.ingest_thumbnail(raw, art_dir, "vid123", mode="original")

    with Image.open(out) as img:
        assert img.size == (1280, 720)


def test_off_mode_returns_none_and_writes_nothing(tmp_path):
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    assert artwork.ingest_thumbnail(raw, art_dir, "vid123", mode="off") is None
    assert os.listdir(art_dir) == []
    # The raw file is left alone — 'off' is a total no-op.
    assert os.path.isfile(raw)


def test_webp_source_is_converted_to_jpeg(tmp_path):
    raw = _make_image(tmp_path / "raw.webp", (1280, 720), "WEBP")
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    out = artwork.ingest_thumbnail(raw, art_dir, "wid", mode="crop")

    assert out.endswith(".jpg")
    assert os.path.isfile(out)
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert img.size == (720, 720)


def test_raw_file_is_deleted_after_ingest(tmp_path):
    raw = _make_image(tmp_path / "raw.webp", (1280, 720), "WEBP")
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    artwork.ingest_thumbnail(raw, art_dir, "vid123", mode="crop")

    assert not os.path.exists(raw)


def test_falsy_video_id_returns_none(tmp_path):
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    assert artwork.ingest_thumbnail(raw, art_dir, "", mode="crop") is None
    assert artwork.ingest_thumbnail(raw, art_dir, None, mode="crop") is None
    assert os.listdir(art_dir) == []


def test_corrupt_image_returns_none_without_raising(tmp_path):
    raw = tmp_path / "raw.jpg"
    raw.write_bytes(b"\xff\xd8\xff\xe0 definitely not a real jpeg")
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    assert artwork.ingest_thumbnail(str(raw), art_dir, "vid", mode="crop") is None
    assert os.listdir(art_dir) == []


def test_missing_raw_file_returns_none(tmp_path):
    art_dir = artwork.thumbnail_dir(str(tmp_path))
    ghost = str(tmp_path / "nope.webp")
    assert artwork.ingest_thumbnail(ghost, art_dir, "vid", mode="crop") is None


# ── embed_cover / has_cover ───────────────────────────────────────────────────
def test_embed_then_has_cover_round_trip(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))
    jpg = artwork.ingest_thumbnail(raw, art_dir, "vid123", mode="crop")

    assert artwork.has_cover(mp3) is False
    assert artwork.embed_cover(mp3, jpg) is True
    assert artwork.has_cover(mp3) is True

    frames = ID3(mp3).getall("APIC")
    assert len(frames) == 1
    assert frames[0].mime == "image/jpeg"
    assert frames[0].type == 3
    assert frames[0].desc == "Cover"
    with open(jpg, "rb") as fh:
        assert frames[0].data == fh.read()


def test_embed_cover_is_idempotent(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = _make_image(tmp_path / "cover.jpg", (500, 500))

    assert artwork.embed_cover(mp3, jpg) is True
    assert artwork.embed_cover(mp3, jpg) is True

    assert len(ID3(mp3).getall("APIC")) == 1


def test_embed_cover_on_non_mp3_returns_false(tmp_path):
    wav = tmp_path / "track.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")
    jpg = _make_image(tmp_path / "cover.jpg", (500, 500))

    assert artwork.embed_cover(str(wav), jpg) is False


def test_embed_cover_missing_inputs_return_false(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = _make_image(tmp_path / "cover.jpg", (500, 500))

    assert artwork.embed_cover(str(tmp_path / "ghost.mp3"), jpg) is False
    assert artwork.embed_cover(mp3, str(tmp_path / "ghost.jpg")) is False
    assert artwork.embed_cover(mp3, None) is False
    assert artwork.embed_cover(None, jpg) is False


def test_has_cover_false_for_non_mp3_and_missing(tmp_path):
    wav = tmp_path / "track.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")

    assert artwork.has_cover(str(wav)) is False
    assert artwork.has_cover(str(tmp_path / "ghost.mp3")) is False
    assert artwork.has_cover(None) is False


def test_has_cover_false_on_untagged_mp3(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    assert artwork.has_cover(mp3) is False


# ── has_cover_any ────────────────────────────────────────────────────────────
def test_has_cover_any_true_for_mp3(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    artwork.embed_cover(mp3, jpg)
    assert artwork.has_cover_any(mp3) is True


def test_has_cover_any_false_for_untagged_mp3(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    assert artwork.has_cover_any(mp3) is False


@requires_ffmpeg
def test_has_cover_any_true_for_mp4(tmp_path):
    audio = make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    artwork.embed_cover_mp4(audio, jpg)
    assert artwork.has_cover_any(audio) is True


@requires_ffmpeg
def test_has_cover_any_false_for_untagged_mp4(tmp_path):
    audio = make_silent(tmp_path / "t.m4a", "aac")
    assert artwork.has_cover_any(audio) is False


@requires_ffmpeg
def test_has_cover_any_true_for_ogg(tmp_path):
    audio = make_silent(tmp_path / "t.opus", "libopus")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    artwork.embed_cover_ogg(audio, jpg)
    assert artwork.has_cover_any(audio) is True


@requires_ffmpeg
def test_has_cover_any_false_for_untagged_ogg(tmp_path):
    audio = make_silent(tmp_path / "t.opus", "libopus")
    assert artwork.has_cover_any(audio) is False


def test_has_cover_any_false_for_missing_and_falsy(tmp_path):
    assert artwork.has_cover_any(str(tmp_path / "ghost.m4a")) is False
    assert artwork.has_cover_any(None) is False


# ── extract_cover ─────────────────────────────────────────────────────────────
def test_extract_cover_round_trips_embedded_bytes(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = _make_image(tmp_path / "cover.jpg", (500, 500))
    artwork.embed_cover(mp3, jpg)

    data = artwork.extract_cover(mp3)

    with open(jpg, "rb") as fh:
        assert data == fh.read()


def test_extract_cover_bytes_reopen_as_an_image(tmp_path):
    """The preview pane hands these bytes straight to Pillow — they must decode
    back to the same picture, at the same size."""
    import io

    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = _make_image(tmp_path / "cover.jpg", (640, 640))
    artwork.embed_cover(mp3, jpg)

    with Image.open(io.BytesIO(artwork.extract_cover(mp3))) as img:
        assert img.size == (640, 640)


def test_extract_cover_none_when_no_art(tmp_path):
    mp3 = _make_mp3(tmp_path / "track.mp3")
    wav = tmp_path / "track.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")

    assert artwork.extract_cover(mp3) is None            # untagged
    assert artwork.extract_cover(str(wav)) is None       # not an MP3
    assert artwork.extract_cover(str(tmp_path / "x.mp3")) is None  # missing
    assert artwork.extract_cover(None) is None


# ── artwork_key ───────────────────────────────────────────────────────────────
def test_artwork_key_prefers_video_id():
    assert artwork.artwork_key("dQw4w9WgXcQ", "/music/Some Track.mp3") == "dQw4w9WgXcQ"


def test_artwork_key_falls_back_to_sanitised_file_stem():
    # Legacy rows rebuilt from disk carry no video_id.
    key = artwork.artwork_key(None, os.path.join("music", "Artist - Track?.mp3"))
    assert key == "Artist - Track_"


def test_artwork_key_strips_extension_and_whitespace():
    assert artwork.artwork_key("", os.path.join("d", " Track .mp3")) == "Track"


def test_artwork_key_none_when_both_unusable():
    assert artwork.artwork_key(None, None) is None
    assert artwork.artwork_key("", "") is None
    assert artwork.artwork_key(None, "   .mp3") is None


# ── youtube_thumbnail_urls ────────────────────────────────────────────────────
def test_youtube_thumbnail_urls_order():
    urls = artwork.youtube_thumbnail_urls("abc123")
    assert urls == (
        "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
    )


def test_youtube_thumbnail_urls_empty_for_falsy_id():
    assert artwork.youtube_thumbnail_urls("") == ()
    assert artwork.youtube_thumbnail_urls(None) == ()


# ── thumbnail_url_candidates ──────────────────────────────────────────────────
def test_candidates_stored_url_comes_first():
    urls = artwork.thumbnail_url_candidates("YouTube", "abc123",
                                            "https://example.com/stored.jpg")
    assert urls[0] == "https://example.com/stored.jpg"
    assert urls[1:] == artwork.youtube_thumbnail_urls("abc123")


def test_candidates_youtube_without_stored_url():
    assert artwork.thumbnail_url_candidates("YouTube", "abc123") == \
        artwork.youtube_thumbnail_urls("abc123")


def test_candidates_deduplicate_preserving_order():
    stored = "https://i.ytimg.com/vi/abc123/hqdefault.jpg"
    urls = artwork.thumbnail_url_candidates("youtube", "abc123", stored)
    assert urls == (
        stored,
        "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
    )
    assert len(set(urls)) == len(urls)


def test_candidates_soundcloud_without_stored_url_is_empty():
    # SoundCloud art is not derivable from a track id — caller must fall back
    # to a yt-dlp metadata lookup.
    assert artwork.thumbnail_url_candidates("SoundCloud", "12345678") == ()


def test_candidates_soundcloud_with_stored_url():
    stored = "https://i1.sndcdn.com/artworks-xyz-t500x500.jpg"
    assert artwork.thumbnail_url_candidates("SoundCloud", "1234", stored) == (stored,)


def test_candidates_no_video_id_and_no_stored_url_is_empty():
    assert artwork.thumbnail_url_candidates("YouTube", None) == ()
    assert artwork.thumbnail_url_candidates(None, None, None) == ()


# ── download_thumbnail (offline: fake opener injected) ────────────────────────
class _FakeResponse:
    def __init__(self, data):
        self._data = data
        self.closed = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True


def _opener_returning(data, record=None):
    def _open(url, timeout=None):
        if record is not None:
            record.append((url, timeout))
        return _FakeResponse(data)
    return _open


def _opener_raising(exc):
    def _open(url, timeout=None):
        raise exc
    return _open


def test_download_thumbnail_writes_file(tmp_path):
    jpeg_bytes = b"\xff\xd8\xff\xe0 fake jpeg payload \xff\xd9"
    dest = str(tmp_path / "abc123.jpg")
    calls = []

    out = artwork.download_thumbnail("https://i.ytimg.com/vi/abc123/hqdefault.jpg",
                                     dest, opener=_opener_returning(jpeg_bytes, calls))

    assert out == dest
    with open(dest, "rb") as fh:
        assert fh.read() == jpeg_bytes
    assert calls == [("https://i.ytimg.com/vi/abc123/hqdefault.jpg",
                      artwork.DOWNLOAD_TIMEOUT)]


def test_download_thumbnail_honours_timeout_argument(tmp_path):
    dest = str(tmp_path / "a.jpg")
    calls = []
    artwork.download_thumbnail("http://x/a.jpg", dest,
                               opener=_opener_returning(b"data", calls), timeout=3)
    assert calls[0][1] == 3


def test_download_thumbnail_opener_raising_returns_none(tmp_path):
    dest = str(tmp_path / "abc123.jpg")

    out = artwork.download_thumbnail("http://x/404.jpg", dest,
                                     opener=_opener_raising(OSError("HTTP 404")))

    assert out is None
    assert not os.path.exists(dest)


def test_download_thumbnail_empty_body_leaves_no_file(tmp_path):
    # A 200 with an empty body must not leave a zero-byte sidecar behind for
    # existing_sidecar() to later mistake for real art.
    dest = str(tmp_path / "abc123.jpg")

    out = artwork.download_thumbnail("http://x/empty.jpg", dest,
                                     opener=_opener_returning(b""))

    assert out is None
    assert not os.path.exists(dest)


def test_download_thumbnail_unwritable_dest_returns_none(tmp_path):
    ghost = str(tmp_path / "no-such-dir" / "abc123.jpg")

    out = artwork.download_thumbnail("http://x/a.jpg", ghost,
                                     opener=_opener_returning(b"data"))

    assert out is None


def test_download_thumbnail_falsy_inputs_return_none(tmp_path):
    opener = _opener_returning(b"data")
    assert artwork.download_thumbnail("", str(tmp_path / "a.jpg"), opener=opener) is None
    assert artwork.download_thumbnail(None, str(tmp_path / "a.jpg"), opener=opener) is None
    assert artwork.download_thumbnail("http://x/a.jpg", "", opener=opener) is None
    assert artwork.download_thumbnail("http://x/a.jpg", None, opener=opener) is None


def test_download_thumbnail_closes_response(tmp_path):
    resp = _FakeResponse(b"data")
    dest = str(tmp_path / "a.jpg")

    artwork.download_thumbnail("http://x/a.jpg", dest,
                               opener=lambda url, timeout=None: resp)

    assert resp.closed is True


# ── existing_sidecar ──────────────────────────────────────────────────────────
def test_existing_sidecar_finds_existing_jpg(tmp_path):
    art_dir = artwork.thumbnail_dir(str(tmp_path))
    jpg = _make_image(os.path.join(art_dir, "vid123.jpg"), (500, 500))

    assert artwork.existing_sidecar(art_dir, "vid123") == jpg


def test_existing_sidecar_none_when_absent(tmp_path):
    art_dir = artwork.thumbnail_dir(str(tmp_path))

    assert artwork.existing_sidecar(art_dir, "vid123") is None
    assert artwork.existing_sidecar(art_dir, "") is None
    assert artwork.existing_sidecar(None, "vid123") is None


def test_existing_sidecar_pairs_with_artwork_key(tmp_path):
    # The pairing the backfill relies on: art ingested under artwork_key() is
    # found again by existing_sidecar() with no network.
    raw = _make_image(tmp_path / "raw.jpg", (1280, 720))
    art_dir = artwork.thumbnail_dir(str(tmp_path))
    key = artwork.artwork_key(None, str(tmp_path / "Artist - Track.mp3"))

    written = artwork.ingest_thumbnail(raw, art_dir, key, mode="crop")

    assert artwork.existing_sidecar(art_dir, key) == written


# ── embed_cover_mp4 / embed_cover_ogg ─────────────────────────────────────────
@requires_ffmpeg
def test_embed_cover_mp4_round_trips(tmp_path):
    from mutagen.mp4 import MP4
    audio = make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    assert artwork.embed_cover_mp4(audio, jpg) is True

    covers = MP4(audio).tags.get("covr")
    assert covers and len(bytes(covers[0])) > 0


@requires_ffmpeg
def test_embed_cover_ogg_round_trips(tmp_path):
    from mutagen.oggopus import OggOpus
    audio = make_silent(tmp_path / "t.opus", "libopus")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    assert artwork.embed_cover_ogg(audio, jpg) is True

    assert OggOpus(audio).get("metadata_block_picture")


@requires_ffmpeg
def test_embed_cover_mp4_replaces_rather_than_stacks(tmp_path):
    from mutagen.mp4 import MP4
    audio = make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    artwork.embed_cover_mp4(audio, jpg)
    artwork.embed_cover_mp4(audio, jpg)

    assert len(MP4(audio).tags.get("covr")) == 1


def test_embed_cover_mp4_rejects_wrong_extension(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    assert artwork.embed_cover_mp4(audio, jpg) is False


def test_embed_cover_ogg_missing_image_is_false(tmp_path):
    assert artwork.embed_cover_ogg(str(tmp_path / "nope.opus"),
                                   str(tmp_path / "nope.jpg")) is False


# ── remux_webm_to_opus / embed_cover_any ──────────────────────────────────────
@requires_ffmpeg
def test_remux_webm_to_opus_produces_opus_and_removes_source(tmp_path):
    webm = make_silent(tmp_path / "t.webm", "libopus")
    out = artwork.remux_webm_to_opus(webm)
    assert out is not None
    assert out.lower().endswith(".opus")
    assert os.path.isfile(out)
    assert not os.path.exists(webm)


@requires_ffmpeg
def test_remux_rejects_undersized_output_and_keeps_source(tmp_path, monkeypatch):
    # Force the proportional size check to reject a perfectly valid remux, so
    # the rejection branch is exercised deterministically regardless of the
    # real byte counts ffmpeg happens to produce for the fixture.
    webm = make_silent(tmp_path / "t.webm", "libopus")
    monkeypatch.setattr(artwork, "_MIN_REMUX_SIZE_RATIO", 100.0)

    out = artwork.remux_webm_to_opus(webm)

    assert out is None
    assert os.path.isfile(webm)
    part_path = os.path.splitext(webm)[0] + ".opus.part"
    assert not os.path.exists(part_path)


@requires_ffmpeg
def test_remux_invalid_webm_fails_and_keeps_source(tmp_path):
    webm = str(tmp_path / "t.webm")
    with open(webm, "wb") as fh:
        fh.write(b"not a real webm container at all")

    out = artwork.remux_webm_to_opus(webm)

    assert out is None
    assert os.path.isfile(webm)
    part_path = os.path.splitext(webm)[0] + ".opus.part"
    assert not os.path.exists(part_path)


@requires_ffmpeg
def test_remux_succeeds_even_if_source_delete_fails(tmp_path, monkeypatch):
    webm = make_silent(tmp_path / "t.webm", "libopus")
    real_remove = os.remove

    def _flaky_remove(path):
        if path == webm:
            raise OSError("locked by AV scanner")
        real_remove(path)

    monkeypatch.setattr(artwork.os, "remove", _flaky_remove)

    out = artwork.remux_webm_to_opus(webm)

    # os.replace already committed the conversion before the delete ran, so
    # a failure to delete the source must not be reported as a failed remux.
    assert out is not None
    assert out.lower().endswith(".opus")
    assert os.path.isfile(out)
    assert os.path.isfile(webm)


@requires_ffmpeg
def test_remux_rejects_non_opus_audio_and_keeps_source(tmp_path):
    # yt-dlp's format fallback can hand back a WebM whose audio is Vorbis, not
    # Opus. `-c:a copy -f ogg` would succeed on that too, producing an
    # Ogg-framed file mis-named `.opus` that is actually Vorbis. The codec
    # must be probed and refused before FFmpeg ever runs.
    webm = make_silent(tmp_path / "t.webm", "libvorbis")

    out = artwork.remux_webm_to_opus(webm)

    assert out is None
    assert os.path.isfile(webm)
    part_path = os.path.splitext(webm)[0] + ".opus.part"
    assert not os.path.exists(part_path)
    opus_path = os.path.splitext(webm)[0] + ".opus"
    assert not os.path.exists(opus_path)


@requires_ffmpeg
def test_embed_cover_any_webm_remuxes_then_embeds(tmp_path):
    from mutagen.oggopus import OggOpus
    webm = make_silent(tmp_path / "t.webm", "libopus")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    path, embedded = artwork.embed_cover_any(webm, jpg)

    assert embedded is True
    assert path.lower().endswith(".opus")
    assert OggOpus(path).get("metadata_block_picture")


@requires_ffmpeg
def test_embed_cover_any_m4a_keeps_path(tmp_path):
    audio = make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(audio, jpg)
    assert embedded is True
    assert path == audio


def test_embed_cover_any_mp3_uses_apic(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(audio, jpg)
    assert embedded is True
    assert path == audio
    assert ID3(audio).getall("APIC")


def test_embed_cover_any_unknown_extension_is_noop(tmp_path):
    audio = tmp_path / "t.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(str(audio), jpg)
    assert embedded is False
    assert path == str(audio)


def test_embed_cover_any_missing_image_is_noop(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    path, embedded = artwork.embed_cover_any(audio, None)
    assert embedded is False
    assert path == audio


# ── module constants ──────────────────────────────────────────────────────────
def test_mode_constants():
    assert artwork.COVER_ART_MODES == ("crop", "original", "off")
    assert artwork.DEFAULT_COVER_ART_MODE == "crop"
    assert artwork.DEFAULT_COVER_ART_MODE in artwork.COVER_ART_MODES

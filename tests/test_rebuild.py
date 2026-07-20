"""Tests for cratebuilder.rebuild: audio discovery + artwork reassociation."""
import os

import pytest

from cratebuilder import rebuild

pytest.importorskip("mutagen")

from mutagen.id3 import ID3, COMM, WOAS  # noqa: E402

_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path, source_url=None):
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    if source_url:
        tags = ID3()
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[source_url]))
        tags.add(WOAS(url=source_url))
        tags.save(str(path), v2_version=3)
    return str(path)


def _make_art(art_dir, stem):
    os.makedirs(art_dir, exist_ok=True)
    p = os.path.join(art_dir, f"{stem}.jpg")
    with open(p, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    return p


def test_audio_exts_covers_keep_original_formats():
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".flac", ".wav"):
        assert ext in rebuild.AUDIO_EXTS


def test_recover_video_id_from_youtube_watch_url(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3",
                  "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert rebuild.recover_video_id(p) == "dQw4w9WgXcQ"


def test_recover_video_id_from_short_url(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3", "https://youtu.be/dQw4w9WgXcQ")
    assert rebuild.recover_video_id(p) == "dQw4w9WgXcQ"


def test_recover_video_id_untagged_file_is_none(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    assert rebuild.recover_video_id(p) is None


def test_recover_video_id_soundcloud_url_is_none(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3",
                  "https://soundcloud.com/artist/some-track")
    assert rebuild.recover_video_id(p) is None


def test_resolve_artwork_prefers_video_id_sidecar(tmp_path):
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    expected = _make_art(art_dir, "dQw4w9WgXcQ")
    _make_art(art_dir, "Some Track")

    index = rebuild.index_artwork_dir(str(tmp_path))
    path, embedded, thumb = rebuild.resolve_artwork(
        audio, "dQw4w9WgXcQ", index)

    assert path == expected


def test_resolve_artwork_falls_back_to_filename_stem(tmp_path):
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    expected = _make_art(art_dir, "Some Track")

    index = rebuild.index_artwork_dir(str(tmp_path))
    path, embedded, thumb = rebuild.resolve_artwork(audio, None, index)

    assert path == expected


def test_resolve_artwork_uses_snapshot_when_no_sidecar(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    index = rebuild.index_artwork_dir(str(tmp_path))
    snapshot = {audio: ("/old/art.jpg", 1, "https://img/x.jpg")}

    path, embedded, thumb = rebuild.resolve_artwork(
        audio, None, index, snapshot=snapshot)

    assert path == "/old/art.jpg"
    assert embedded == 1
    assert thumb == "https://img/x.jpg"


def test_resolve_artwork_no_art_anywhere_is_blank(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    index = rebuild.index_artwork_dir(str(tmp_path))
    assert rebuild.resolve_artwork(audio, None, index) == (None, 0, None)


def test_resolve_artwork_never_writes_or_deletes(tmp_path):
    """The core guarantee: rebuild only reads. It must not touch the disk."""
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    _make_art(art_dir, "Some Track")

    def snapshot_tree():
        seen = {}
        for root, _dirs, files in os.walk(str(tmp_path)):
            for f in files:
                fp = os.path.join(root, f)
                seen[fp] = os.path.getsize(fp)
        return seen

    before = snapshot_tree()
    index = rebuild.index_artwork_dir(str(tmp_path))
    rebuild.resolve_artwork(audio, "dQw4w9WgXcQ", index)
    rebuild.resolve_artwork(audio, None, index)

    assert snapshot_tree() == before


def test_index_artwork_dir_missing_folder_is_empty(tmp_path):
    assert rebuild.index_artwork_dir(str(tmp_path)) == {}

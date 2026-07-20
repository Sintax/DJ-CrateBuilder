"""Tests for cratebuilder.tagging ID3 writing."""
import os
import struct

import pytest

from cratebuilder import tagging

mutagen = pytest.importorskip("mutagen")
from mutagen.id3 import ID3  # noqa: E402


# A minimal valid silent MP3 frame (MPEG-1 Layer III, 128kbps, 44.1kHz).
# Header bytes + zero padding for the frame body. Enough for mutagen to treat
# the file as an MP3 and attach an ID3 tag.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path):
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 4)


def test_writes_all_three_fields(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    url = "https://www.youtube.com/watch?v=abc123"

    changed = tagging.write_track_tags(p, title="Cool Track", source_url=url)
    assert changed is True

    tags = ID3(p)
    assert tags.getall("TIT2")[0].text[0] == "Cool Track"
    assert tags.getall("TENC")[0].text[0] == "DJ-CrateBuilder"
    # URL lives in both the Comment (Explorer-visible) and WOAS frame.
    assert tags.getall("COMM")[0].text[0] == url
    assert tags.getall("WOAS")[0].url == url


def test_comment_has_blank_description(tmp_path):
    # Windows Explorer only surfaces the COMM frame whose description is empty.
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="T", source_url="http://x")
    assert ID3(p).getall("COMM")[0].desc == ""


def test_no_overwrite_preserves_existing(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="Original", source_url="http://first")
    # Second pass without overwrite must not clobber existing fields.
    changed = tagging.write_track_tags(p, title="New", source_url="http://second")
    assert changed is False
    tags = ID3(p)
    assert tags.getall("TIT2")[0].text[0] == "Original"
    assert tags.getall("COMM")[0].text[0] == "http://first"


def test_overwrite_replaces(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="Original", source_url="http://first")
    changed = tagging.write_track_tags(p, title="New", source_url="http://second",
                                       overwrite=True)
    assert changed is True
    tags = ID3(p)
    assert tags.getall("TIT2")[0].text[0] == "New"
    assert tags.getall("COMM")[0].text[0] == "http://second"


def test_backfill_only_missing_fields(tmp_path):
    # A file with a title but no URL gets the URL added, title untouched.
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="Existing Title")
    changed = tagging.write_track_tags(p, title="Ignored",
                                       source_url="http://url")
    assert changed is True
    tags = ID3(p)
    assert tags.getall("TIT2")[0].text[0] == "Existing Title"
    assert tags.getall("WOAS")[0].url == "http://url"


def test_non_mp3_is_ignored(tmp_path):
    p = str(tmp_path / "track.webm")
    p_obj = tmp_path / "track.webm"
    p_obj.write_bytes(b"not an mp3")
    assert tagging.write_track_tags(p, title="T", source_url="http://x") is False


def test_missing_file_is_ignored(tmp_path):
    p = str(tmp_path / "ghost.mp3")
    assert tagging.write_track_tags(p, title="T", source_url="http://x") is False


def test_uses_default_encoder_constant(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="T", source_url="http://x")
    assert ID3(p).getall("TENC")[0].text[0] == tagging.ENCODED_BY


# ── read_source_url ───────────────────────────────────────────────────────────
def test_read_source_url_round_trip(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    url = "https://soundcloud.com/artist/some-track"
    tagging.write_track_tags(p, title="T", source_url=url)

    assert tagging.read_source_url(p) == url


def test_read_source_url_falls_back_to_comment(tmp_path):
    # A tag whose WOAS frame was lost (re-encode, third-party editor) still
    # yields the URL from the blank-description comment.
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    url = "https://www.youtube.com/watch?v=abc123"
    tagging.write_track_tags(p, title="T", source_url=url)

    tags = ID3(p)
    tags.delall("WOAS")
    tags.save(p, v2_version=3)
    assert not ID3(p).getall("WOAS")

    assert tagging.read_source_url(p) == url


def test_read_source_url_ignores_non_url_comment(tmp_path):
    from mutagen.id3 import COMM

    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    tagging.write_track_tags(p, title="T")

    tags = ID3(p)
    tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="",
                              text=["ripped by someone"])])
    tags.save(p, v2_version=3)

    assert tagging.read_source_url(p) is None


def test_read_source_url_untagged_mp3_returns_none(tmp_path):
    p = str(tmp_path / "track.mp3")
    _make_mp3(p)
    assert tagging.read_source_url(p) is None


def test_read_source_url_non_mp3_and_missing_return_none(tmp_path):
    webm = tmp_path / "track.webm"
    webm.write_bytes(b"not an mp3")

    assert tagging.read_source_url(str(webm)) is None
    assert tagging.read_source_url(str(tmp_path / "ghost.mp3")) is None
    assert tagging.read_source_url(None) is None
    assert tagging.read_source_url("") is None

"""Genre tagging: written on download, and repaired in bulk from the genre
folder each track is filed under."""
import os

import pytest

from cratebuilder import genrefix, tagging

mutagen = pytest.importorskip("mutagen")
from mutagen.id3 import ID3  # noqa: E402

# Same minimal silent frame test_tagging.py uses — enough for mutagen to treat
# the file as an MP3 and attach a tag.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    return str(path)


# ── write_track_tags: genre on the download path ──────────────────────────────

def test_download_writes_the_genre_frame(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.write_track_tags(p, title="T", source_url="http://x",
                             genre="Drum & Bass")
    assert ID3(p).getall("TCON")[0].text[0] == "Drum & Bass"


def test_download_without_a_genre_writes_no_frame(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.write_track_tags(p, title="T", source_url="http://x")
    assert not ID3(p).getall("TCON")


def test_download_backfill_does_not_clobber_an_existing_genre(tmp_path):
    """The download path runs with overwrite=False so a re-download or a
    skipped-file backfill never rewrites a tag the user curated."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.write_track_tags(p, title="T", genre="Original")
    tagging.write_track_tags(p, title="T", genre="Different")
    assert ID3(p).getall("TCON")[0].text[0] == "Original"


# ── read_track_genre / set_track_genre ────────────────────────────────────────

def test_read_genre_returns_none_when_untagged(tmp_path):
    assert tagging.read_track_genre(_make_mp3(tmp_path / "t.mp3")) is None


def test_read_genre_of_a_missing_file_is_none(tmp_path):
    assert tagging.read_track_genre(str(tmp_path / "nope.mp3")) is None


def test_set_genre_writes_and_reads_back(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    assert tagging.set_track_genre(p, "Techno") is True
    assert tagging.read_track_genre(p) == "Techno"


def test_set_genre_overwrites_a_different_value(tmp_path):
    """This is the whole point of the bulk fix — unlike write_track_tags it
    wins over what is already there."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "Wrong")
    assert tagging.set_track_genre(p, "Right") is True
    assert tagging.read_track_genre(p) == "Right"


def test_set_genre_is_a_noop_when_already_correct(tmp_path):
    """A second run must report no work and leave the file byte-identical, so
    the summary count means something and re-running is free."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "House")
    before = os.path.getmtime(p), os.path.getsize(p)
    assert tagging.set_track_genre(p, "House") is False
    assert (os.path.getmtime(p), os.path.getsize(p)) == before


def test_set_genre_empty_clears_the_frame(tmp_path):
    """Tracks under _No Genre carry no genre — the tag is removed, not blanked
    to a stray empty string."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "House")
    assert tagging.set_track_genre(p, "") is True
    assert not ID3(p).getall("TCON")
    assert tagging.read_track_genre(p) is None


def test_set_genre_empty_on_an_untagged_file_is_a_noop(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    assert tagging.set_track_genre(p, "") is False


def test_set_genre_on_a_missing_file_is_false(tmp_path):
    assert tagging.set_track_genre(str(tmp_path / "nope.mp3"), "X") is False


def test_set_genre_on_an_unsupported_container_is_false(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    assert tagging.set_track_genre(str(p), "X") is False


def test_set_genre_strips_surrounding_whitespace(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "  Techno  ")
    assert tagging.read_track_genre(p) == "Techno"


# ── genrefix: which genre a track belongs to ──────────────────────────────────

def test_no_genre_bucket_maps_to_no_genre():
    assert genrefix.genre_from_dir_name(genrefix.NO_GENRE_DIR) == ""
    assert genrefix.genre_from_dir_name("Drum & Bass") == "Drum & Bass"


def _library(tmp_path):
    yt = tmp_path / "YouTube"
    _make_mp3(yt / "Drum & Bass" / "DnB Portal" / "a.mp3")
    _make_mp3(yt / "Drum & Bass" / "DnB Portal" / "b.mp3")
    _make_mp3(yt / genrefix.NO_GENRE_DIR / "Misc" / "c.mp3")
    sc = tmp_path / "SoundCloud"
    _make_mp3(sc / "House" / "DJ Foo" / "d.mp3")
    return [str(yt), str(sc)]


def test_iter_library_tracks_pairs_each_file_with_its_folder_genre(tmp_path):
    found = dict(genrefix.iter_library_tracks(_library(tmp_path)))
    by_name = {os.path.basename(p): g for p, g in found.items()}
    assert by_name == {"a.mp3": "Drum & Bass", "b.mp3": "Drum & Bass",
                       "c.mp3": "", "d.mp3": "House"}


def test_iter_library_tracks_ignores_non_audio(tmp_path):
    dirs = _library(tmp_path)
    (tmp_path / "YouTube" / "House").mkdir(parents=True, exist_ok=True)
    (tmp_path / "YouTube" / "Drum & Bass" / "DnB Portal" / "notes.txt"
     ).write_text("x", encoding="utf-8")
    names = {os.path.basename(p)
             for p, _ in genrefix.iter_library_tracks(dirs)}
    assert "notes.txt" not in names


def test_iter_library_tracks_ignores_loose_files_outside_the_layout(tmp_path):
    """Only <platform>/<genre>/<channel>/<track> counts. A file dropped
    straight into a platform or genre folder is not a library track and must
    never be tagged."""
    dirs = _library(tmp_path)
    _make_mp3(tmp_path / "YouTube" / "loose.mp3")
    _make_mp3(tmp_path / "YouTube" / "House" / "loose2.mp3")
    names = {os.path.basename(p)
             for p, _ in genrefix.iter_library_tracks(dirs)}
    assert "loose.mp3" not in names and "loose2.mp3" not in names


def test_iter_library_tracks_skips_missing_platform_dirs(tmp_path):
    assert list(genrefix.iter_library_tracks(
        [str(tmp_path / "nope"), None])) == []


def test_count_matches_what_the_walk_yields(tmp_path):
    dirs = _library(tmp_path)
    assert genrefix.count_library_tracks(dirs) == 4
    assert len(list(genrefix.iter_library_tracks(dirs))) == 4


def test_walk_order_is_stable_across_passes(tmp_path):
    """The UI counts first, then re-walks to drive the progress bar; both
    passes have to agree or the bar lies."""
    dirs = _library(tmp_path)
    first = [p for p, _ in genrefix.iter_library_tracks(dirs)]
    second = [p for p, _ in genrefix.iter_library_tracks(dirs)]
    assert first == second


# ── genrefix: one channel folder, after a Watch List genre change ─────────────

def test_iter_channel_tracks_covers_only_that_folder(tmp_path):
    dirs = _library(tmp_path)
    folder = str(tmp_path / "YouTube" / "Drum & Bass" / "DnB Portal")
    got = list(genrefix.iter_channel_tracks(folder, "Drum & Bass"))
    assert {os.path.basename(p) for p, _ in got} == {"a.mp3", "b.mp3"}
    assert {g for _, g in got} == {"Drum & Bass"}
    # The rest of the library is untouched by a single-channel sweep.
    assert len(list(genrefix.iter_library_tracks(dirs))) == 4


def test_iter_channel_tracks_skips_the_artwork_sidecar_folder(tmp_path):
    folder = tmp_path / "YouTube" / "House" / "DJ Foo"
    _make_mp3(folder / "a.mp3")
    _make_mp3(folder / ".artwork" / "nested.mp3")
    got = list(genrefix.iter_channel_tracks(str(folder), "House"))
    assert [os.path.basename(p) for p, _ in got] == ["a.mp3"]


def test_iter_channel_tracks_missing_folder_yields_nothing(tmp_path):
    assert list(genrefix.iter_channel_tracks(str(tmp_path / "nope"), "X")) == []
    assert list(genrefix.iter_channel_tracks(None, "X")) == []
    assert genrefix.count_channel_tracks(str(tmp_path / "nope")) == 0


def test_channel_retag_after_a_genre_move(tmp_path):
    """The Watch List move relocates the files; this is the pass that makes the
    tag inside each one agree with where it now lives."""
    folder = tmp_path / "YouTube" / "Techno" / "DJ Foo"
    a = _make_mp3(folder / "a.mp3")
    b = _make_mp3(folder / "b.mp3")
    tagging.set_track_genre(a, "House")      # the genre it moved out of
    tagging.set_track_genre(b, "House")

    assert genrefix.count_channel_tracks(str(folder)) == 2
    fixed = sum(1 for p, g in genrefix.iter_channel_tracks(str(folder), "Techno")
                if tagging.set_track_genre(p, g))

    assert fixed == 2
    assert tagging.read_track_genre(a) == "Techno"
    assert tagging.read_track_genre(b) == "Techno"


def test_channel_retag_to_no_genre_clears_the_tag(tmp_path):
    folder = tmp_path / "YouTube" / genrefix.NO_GENRE_DIR / "DJ Foo"
    a = _make_mp3(folder / "a.mp3")
    tagging.set_track_genre(a, "House")
    for p, g in genrefix.iter_channel_tracks(str(folder), ""):
        tagging.set_track_genre(p, g)
    assert tagging.read_track_genre(a) is None


def test_end_to_end_repair_aligns_tags_with_folders(tmp_path):
    dirs = _library(tmp_path)
    # Seed a wrong tag and a correct one, leaving the rest untagged.
    tracks = dict(genrefix.iter_library_tracks(dirs))
    by_name = {os.path.basename(p): p for p in tracks}
    tagging.set_track_genre(by_name["a.mp3"], "Completely Wrong")
    tagging.set_track_genre(by_name["b.mp3"], "Drum & Bass")
    tagging.set_track_genre(by_name["c.mp3"], "Leftover")

    fixed = sum(1 for p, g in genrefix.iter_library_tracks(dirs)
                if tagging.set_track_genre(p, g))

    assert fixed == 3          # a (wrong), c (cleared), d (blank) — not b
    assert tagging.read_track_genre(by_name["a.mp3"]) == "Drum & Bass"
    assert tagging.read_track_genre(by_name["c.mp3"]) is None
    assert tagging.read_track_genre(by_name["d.mp3"]) == "House"

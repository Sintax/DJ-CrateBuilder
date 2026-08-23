"""Repair Track Tags: the sweep that realigns a library's tags.

Genre is forced to the folder a track is filed under; Title, Encoded-by and
the source URL are only ever filled in where a track is missing them. The
case that motivated it: tracks downloaded before tagging existed carry no
title at all, and the genre and artwork sweeps each gave them an ID3 tag
holding only their own field — so they show cover art and a genre in
Explorer, and a blank Title.
"""
import os

import pytest

from cratebuilder import genrefix, tagging
from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase

mutagen = pytest.importorskip("mutagen")
from mutagen.id3 import ID3  # noqa: E402

_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    return str(path)


def _library(tmp_path):
    yt = tmp_path / "YouTube"
    _make_mp3(yt / "Drum & Bass" / "DnB Portal" / "a.mp3")
    _make_mp3(yt / "Drum & Bass" / "DnB Portal" / "b.mp3")
    _make_mp3(yt / CrateLayout.NO_GENRE_DIR / "Misc" / "c.mp3")
    sc = tmp_path / "SoundCloud"
    _make_mp3(sc / "House" / "DJ Foo" / "d.mp3")
    return [str(yt), str(sc)]


def _frames(path):
    """The tag values this feature reads and writes."""
    tags = ID3(path)
    out = {}
    for frame in ("TIT2", "TENC", "TCON"):
        got = tags.getall(frame)
        values = (getattr(got[0], "text", None) or []) if got else []
        out[frame] = str(values[0]) if values else None
    woas = tags.getall("WOAS")
    out["WOAS"] = getattr(woas[0], "url", None) if woas else None
    return out


# ── repair_track ─────────────────────────────────────────────────────────────
def test_repair_gives_an_untagged_track_its_title(tmp_path):
    """The whole point. Explorer's Title column reads the tag, not the file
    name, so a correct filename is no help at all."""
    p = _make_mp3(tmp_path / "t.mp3")
    changed, filled = genrefix.repair_track(
        p, "Drum & Bass", title="Real Title",
        source_url="https://www.youtube.com/watch?v=abc")
    assert (changed, filled) == (True, True)
    got = _frames(p)
    assert got["TIT2"] == "Real Title"
    assert got["TCON"] == "Drum & Bass"
    assert got["TENC"] == tagging.ENCODED_BY
    assert got["WOAS"] == "https://www.youtube.com/watch?v=abc"


def test_repair_reproduces_the_real_world_broken_state(tmp_path):
    """The exact fingerprint found in the library: a genre and cover art and
    nothing else, because the genre and artwork sweeps each created a tag
    holding only their own field. The repair has to fill the rest in."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "Drum & Bass")     # what the genre sweep left
    assert _frames(p)["TIT2"] is None

    _changed, filled = genrefix.repair_track(p, "Drum & Bass",
                                             title="Recovered Title")
    assert filled is True
    assert _frames(p)["TIT2"] == "Recovered Title"


def test_repair_never_overwrites_a_title_you_edited(tmp_path):
    """Only the genre is forced. This is what makes the sweep safe to run
    over a whole library, repeatedly."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.write_track_tags(p, title="My Own Name")
    genrefix.repair_track(p, "House", title="Something Else")
    assert _frames(p)["TIT2"] == "My Own Name"


def test_repair_still_forces_the_genre_over_a_wrong_one(tmp_path):
    """The contract the button already had, kept intact by the widening."""
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "Completely Wrong")
    changed, _filled = genrefix.repair_track(p, "House", title="T")
    assert changed is True
    assert _frames(p)["TCON"] == "House"


def test_repair_of_an_already_complete_track_changes_nothing(tmp_path):
    """Re-running has to be free, or nobody runs it twice."""
    p = _make_mp3(tmp_path / "t.mp3")
    genrefix.repair_track(p, "House", title="T", source_url="https://x/y")
    assert genrefix.repair_track(
        p, "House", title="T", source_url="https://x/y") == (False, False)


def test_repair_without_a_source_url_still_writes_the_title(tmp_path):
    """A SoundCloud track has no reconstructable URL; it still gets a name."""
    p = _make_mp3(tmp_path / "t.mp3")
    _changed, filled = genrefix.repair_track(p, "Trance", title="SC Track",
                                             source_url="")
    assert filled is True
    assert _frames(p)["TIT2"] == "SC Track"
    assert _frames(p)["WOAS"] is None


def test_repair_of_a_no_genre_track_clears_the_genre_but_keeps_the_title(
        tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    tagging.set_track_genre(p, "Stale")
    genrefix.repair_track(p, "", title="Kept")
    got = _frames(p)
    assert got["TCON"] is None
    assert got["TIT2"] == "Kept"


def test_repair_of_a_missing_file_is_not_an_error(tmp_path):
    """One unwritable file must never stop a library-wide sweep."""
    assert genrefix.repair_track(str(tmp_path / "gone.mp3"), "House",
                                 title="T") == (False, False)


# ── the lookups that feed it ─────────────────────────────────────────────────
def test_title_from_filename_is_the_stem():
    assert genrefix.title_from_filename(
        os.path.join("x", "Artist - Track (Original Mix).mp3")) \
        == "Artist - Track (Original Mix)"


@pytest.mark.parametrize("path", ["", None])
def test_title_from_filename_of_nothing_is_empty(path):
    assert genrefix.title_from_filename(path) == ""


def test_source_url_is_built_only_for_youtube():
    assert genrefix.source_url_for("YouTube", "abc123") \
        == "https://www.youtube.com/watch?v=abc123"
    assert genrefix.source_url_for("youtube", "abc123").endswith("abc123")


@pytest.mark.parametrize("platform,vid", [
    ("SoundCloud", "123456"),      # a permalink, not derivable from the id
    ("YouTube", ""),
    ("", ""),
])
def test_source_url_is_empty_when_it_cannot_be_built(platform, vid):
    assert genrefix.source_url_for(platform, vid) == ""


def test_index_by_path_survives_windows_case_drift():
    """The database stores each path as written; the walk builds its own. A
    case mismatch would silently cost a track its real title."""
    index = genrefix.index_by_path(
        {os.path.join("C:", "Music", "Track.mp3"): ("T", "v", "YouTube")})
    assert genrefix.lookup_facts(
        index, os.path.join("c:", "music", "track.mp3").lower()) \
        == ("T", "v", "YouTube")


def test_lookup_of_an_unknown_path_is_blank_not_an_error():
    """A file the user dropped in by hand has no row, and still gets fixed."""
    assert genrefix.lookup_facts({}, os.path.join("x", "y.mp3")) == ("", "", "")
    assert genrefix.lookup_facts(None, None) == ("", "", "")


def test_index_by_path_drops_falsy_keys():
    assert genrefix.index_by_path(
        {"": ("T", "v", "p"), None: ("U", "w", "q")}) == {}


def test_index_by_path_of_nothing_is_empty():
    assert genrefix.index_by_path(None) == {}


# ── the database read behind it ──────────────────────────────────────────────
def _db_with(tmp_path, rows):
    db = DownloadsDatabase(str(tmp_path / "t.db"))
    for video_id, title, path, platform in rows:
        db.add_download(video_id=video_id, title=title, channel_name="C",
                        channel_url="https://yt/c", platform=platform,
                        genre="DnB", file_path=path, upload_date="",
                        bitrate="")
    return db


def test_track_facts_carry_the_title_id_and_platform(tmp_path):
    db = _db_with(tmp_path, [("v1", "Track One", "/x/a.mp3", "YouTube")])
    assert db.get_track_facts_by_path() == {
        "/x/a.mp3": ("Track One", "v1", "YouTube")}


def test_track_facts_skip_rows_with_no_file(tmp_path):
    """The key would be meaningless, and would collide across rows."""
    db = _db_with(tmp_path, [("v1", "Has File", "/x/a.mp3", "YouTube"),
                             ("v2", "No File", "", "YouTube")])
    assert list(db.get_track_facts_by_path()) == ["/x/a.mp3"]


def test_track_facts_are_read_in_one_query(tmp_path, monkeypatch):
    """Per file it would open a connection per track, through the same lock
    the UI thread takes to redraw — the sweep runs over the whole library."""
    db = _db_with(tmp_path, [(f"v{i}", f"T{i}", f"/x/{i}.mp3", "YouTube")
                             for i in range(30)])
    calls = []
    real = db._conn
    monkeypatch.setattr(db, "_conn", lambda: (calls.append(1), real())[1])
    facts = db.get_track_facts_by_path()
    assert len(facts) == 30
    assert len(calls) == 1


def test_track_facts_of_an_empty_db(tmp_path):
    assert DownloadsDatabase(str(tmp_path / "e.db")).get_track_facts_by_path() \
        == {}


# ── end to end over a library ────────────────────────────────────────────────
def test_a_sweep_titles_the_untagged_and_leaves_the_rest(tmp_path):
    """The untagged tracks gain their real titles, the hand-titled one keeps
    its own, and every track ends up on its folder's genre."""
    dirs = _library(tmp_path)
    tracks = dict(genrefix.iter_library_tracks(dirs))
    by_name = {os.path.basename(p): p for p in tracks}
    tagging.write_track_tags(by_name["b.mp3"], title="Hand Written")

    facts = genrefix.index_by_path({
        by_name["a.mp3"]: ("Proper Title A", "vidA", "YouTube"),
        by_name["b.mp3"]: ("Ignored", "vidB", "YouTube"),
    })

    filled = 0
    for path, genre in genrefix.iter_library_tracks(dirs):
        title, vid, platform = genrefix.lookup_facts(facts, path)
        _changed, was_filled = genrefix.repair_track(
            path, genre,
            title=title or genrefix.title_from_filename(path),
            source_url=genrefix.source_url_for(platform, vid))
        filled += bool(was_filled)

    assert filled == len(tracks)          # every file was missing something
    assert _frames(by_name["a.mp3"])["TIT2"] == "Proper Title A"
    assert _frames(by_name["b.mp3"])["TIT2"] == "Hand Written"
    # No database row -> the file's own name is the fallback.
    assert _frames(by_name["d.mp3"])["TIT2"] == "d"
    assert _frames(by_name["a.mp3"])["WOAS"] == \
        "https://www.youtube.com/watch?v=vidA"
    assert _frames(by_name["c.mp3"])["TCON"] is None      # no-genre folder
    assert _frames(by_name["d.mp3"])["TCON"] == "House"


def test_a_second_sweep_is_free(tmp_path):
    """Idempotence, end to end: nothing is rewritten the second time."""
    dirs = _library(tmp_path)

    def sweep():
        touched = 0
        for path, genre in genrefix.iter_library_tracks(dirs):
            changed, filled = genrefix.repair_track(
                path, genre, title=genrefix.title_from_filename(path))
            touched += bool(changed or filled)
        return touched

    assert sweep() > 0
    assert sweep() == 0


# ── the app wiring ───────────────────────────────────────────────────────────
def test_the_button_runs_the_repair(app):
    assert str(app._repair_tags_btn.cget("text")).strip() \
        == "🏷  Repair Track Tags".strip()


def test_the_repair_sweeps_the_library_and_titles_it(app, monkeypatch):
    """Drives the app's own sweep: confirm the dialog, run the worker inline,
    and check the files on disk."""
    base = app._base_dir
    yt = os.path.join(base, "YouTube")
    track = _make_mp3(os.path.join(yt, "Drum & Bass", "DnB Portal", "a.mp3"))
    app._db.add_download(video_id="vidA", title="Proper Title A",
                         channel_name="DnB Portal",
                         channel_url="https://yt/c", platform="YouTube",
                         genre="Drum & Bass", file_path=track,
                         upload_date="", bitrate="")

    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))
    import tkinter.messagebox as mb
    monkeypatch.setattr(mb, "askokcancel", lambda *a, **kw: True)
    monkeypatch.setattr(mb, "showinfo", lambda *a, **kw: None)

    app._repair_track_tags()
    app.update()

    got = _frames(track)
    assert got["TIT2"] == "Proper Title A"
    assert got["TCON"] == "Drum & Bass"
    assert got["WOAS"] == "https://www.youtube.com/watch?v=vidA"
    assert app._tag_repair_active is False


def test_a_watchlist_genre_move_stays_genre_only(app, monkeypatch):
    """That path exists to make files agree with a genre the user just
    changed. Titling is the library sweep's job, and passing no facts is
    what keeps the two apart."""
    folder = os.path.join(app._base_dir, "YouTube", "House", "DJ Foo")
    track = _make_mp3(os.path.join(folder, "a.mp3"))
    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))

    app._watchlist_retag_genre(folder, "House", "DJ Foo")
    app.update()

    got = _frames(track)
    assert got["TCON"] == "House"
    assert got["TIT2"] is None          # untouched by a genre move

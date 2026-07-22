"""Tests for DownloadsDatabase.move_channel_downloads — the transactional
prefix-rewrite used by the Watchlist Edit dialog to move a channel between
genre folders (and to auto-heal DB rows when a folder was already relocated on
disk out of band)."""
from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path):
    return DownloadsDatabase(str(tmp_path / "test.db"))


def _add_track(db, *, file_path, artwork_path=None, video_id="v1",
               genre="Rock", channel_name="Foo",
               channel_url="https://x", platform="YouTube"):
    db.add_download(
        video_id=video_id, title="t", channel_name=channel_name,
        channel_url=channel_url, platform=platform, genre=genre,
        file_path=file_path, upload_date="20260101", bitrate="192k",
        artwork_path=artwork_path)


def _all_downloads(db):
    with db._conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM downloads")]


def test_prefix_rewrite_moves_rows_under_channel(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://youtube.com/@foo", display_name="Foo",
        platform="YouTube", genre="Rock", scan_cutoff_date="20260101")
    old = r"C:\Music\YouTube\Rock\Foo"
    new = r"C:\Music\YouTube\Techno\Foo"
    _add_track(db, video_id="v1", file_path=old + r"\song1.mp3",
               artwork_path=old + r"\.artwork\v1.jpg")
    _add_track(db, video_id="v2", file_path=old + r"\song2.mp3",
               artwork_path=old + r"\.artwork\v2.jpg")

    rows = db.move_channel_downloads(
        wl_id=wid, old_dir=old, new_dir=new, new_genre="Techno")
    assert rows == 2

    paths = {d["file_path"] for d in _all_downloads(db)}
    assert paths == {new + r"\song1.mp3", new + r"\song2.mp3"}
    genres = {d["genre"] for d in _all_downloads(db)}
    assert genres == {"Techno"}
    assert db.get_watchlist_channel(wid)["genre"] == "Techno"


def test_prefix_rewrite_ignores_unrelated_rows(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://youtube.com/@foo", display_name="Foo",
        platform="YouTube", genre="Rock", scan_cutoff_date="20260101")
    old = r"C:\Music\YouTube\Rock\Foo"
    new = r"C:\Music\YouTube\Techno\Foo"
    _add_track(db, video_id="v1", file_path=old + r"\song.mp3")
    # Different channel under same genre — must not be rewritten.
    _add_track(db, video_id="v2", channel_name="Bar",
               file_path=r"C:\Music\YouTube\Rock\Bar\song.mp3")
    # A channel folder whose name starts with the same prefix — the trailing
    # separator anchor is what prevents this from being caught as a false match.
    _add_track(db, video_id="v3", channel_name="FooBar",
               file_path=r"C:\Music\YouTube\Rock\FooBar\song.mp3")

    db.move_channel_downloads(
        wl_id=wid, old_dir=old, new_dir=new, new_genre="Techno")

    by_id = {d["video_id"]: d for d in _all_downloads(db)}
    assert by_id["v1"]["file_path"] == new + r"\song.mp3"
    assert by_id["v1"]["genre"] == "Techno"
    # v2 and v3 must be untouched.
    assert by_id["v2"]["file_path"] == r"C:\Music\YouTube\Rock\Bar\song.mp3"
    assert by_id["v2"]["genre"] == "Rock"
    assert by_id["v3"]["file_path"] == r"C:\Music\YouTube\Rock\FooBar\song.mp3"
    assert by_id["v3"]["genre"] == "Rock"


def test_null_artwork_stays_null(tmp_path):
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://youtube.com/@foo", display_name="Foo",
        platform="YouTube", genre="Rock", scan_cutoff_date="20260101")
    old = r"C:\Music\YouTube\Rock\Foo"
    new = r"C:\Music\YouTube\Techno\Foo"
    _add_track(db, video_id="v1", file_path=old + r"\song.mp3",
               artwork_path=None)

    db.move_channel_downloads(
        wl_id=wid, old_dir=old, new_dir=new, new_genre="Techno")

    row = _all_downloads(db)[0]
    assert row["file_path"] == new + r"\song.mp3"
    assert row["artwork_path"] is None


def test_genre_only_patch_when_dirs_match(tmp_path):
    """Verify-on-open path: folder was already in the right physical place, we
    just need to reconcile the DB's genre column to match the folder location."""
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://youtube.com/@foo", display_name="Foo",
        platform="YouTube", genre="Rock", scan_cutoff_date="20260101")
    same = r"C:\Music\YouTube\Techno\Foo"
    _add_track(db, video_id="v1", genre="Rock", file_path=same + r"\song.mp3",
               artwork_path=same + r"\.artwork\v1.jpg")

    rows = db.move_channel_downloads(
        wl_id=wid, old_dir=same, new_dir=same, new_genre="Techno")
    assert rows == 1

    row = _all_downloads(db)[0]
    assert row["file_path"] == same + r"\song.mp3"
    assert row["artwork_path"] == same + r"\.artwork\v1.jpg"
    assert row["genre"] == "Techno"
    assert db.get_watchlist_channel(wid)["genre"] == "Techno"


def test_empty_or_missing_paths_are_noop(tmp_path):
    db = _new_db(tmp_path)
    assert db.move_channel_downloads(
        wl_id=1, old_dir="", new_dir=r"C:\x", new_genre="A") == 0
    assert db.move_channel_downloads(
        wl_id=1, old_dir=r"C:\x", new_dir="", new_genre="A") == 0


def test_unix_style_separators(tmp_path):
    """The helper must handle forward-slash paths (Linux/macOS installs) just
    as cleanly as backslash paths (Windows)."""
    db = _new_db(tmp_path)
    wid = db.add_watchlist_channel(
        url="https://youtube.com/@foo", display_name="Foo",
        platform="YouTube", genre="Rock", scan_cutoff_date="20260101")
    old = "/home/dj/Music/YouTube/Rock/Foo"
    new = "/home/dj/Music/YouTube/Techno/Foo"
    _add_track(db, file_path=old + "/song.mp3",
               artwork_path=old + "/.artwork/v1.jpg")

    db.move_channel_downloads(
        wl_id=wid, old_dir=old, new_dir=new, new_genre="Techno")
    row = _all_downloads(db)[0]
    assert row["file_path"] == new + "/song.mp3"
    assert row["artwork_path"] == new + "/.artwork/v1.jpg"

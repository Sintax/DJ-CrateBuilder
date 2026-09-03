"""CrateBuilderService.populate_watchlist_from_folders: the first-run fill.

The monolith arms it in __init__ (after(1200, _watchlist_populate_from_folders))
and the web port never inherited it: a reinstall that lost cratebuilder.db left
every crate folder on disk with no way back into the Watch List. The folder
walk and the rows it writes are WatchlistOps' and tested with it
(test_watchrun.py); this is the gate the service puts around it, and the
launch order it has to hold with the startup scan.
"""

from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import CrateBuilderService
from cratebuilder.settings import Settings
from cratebuilder.watchrun import WatchlistOps


def _build(tmp_path, *, database=False):
    """A service pointed entirely at tmp_path; the database file only when
    asked for, since whether one comes into existence is under test."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    db_path = tmp_path / "cratebuilder.db"
    if database:
        DownloadsDatabase(str(db_path))
    service = CrateBuilderService(
        settings=settings, db_path=str(db_path),
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"))
    return service, db_path


def _rows(db_path):
    return DownloadsDatabase(str(db_path)).get_all_watchlist_channels()


def _channel_folder(tmp_path, name, genre="House", platform="YouTube"):
    (tmp_path / "crate" / platform / genre / name).mkdir(parents=True,
                                                        exist_ok=True)


def test_a_machine_with_no_crate_and_no_database_gets_neither(tmp_path):
    service, db_path = _build(tmp_path)

    assert service.populate_watchlist_from_folders() == 0

    assert not db_path.exists()


def test_the_folders_fill_an_empty_watch_list_and_bring_the_database_to_life(
        tmp_path):
    _channel_folder(tmp_path, "Deep House Daily")
    _channel_folder(tmp_path, "Berlin Sets", genre="Techno",
                    platform="SoundCloud")
    service, db_path = _build(tmp_path)

    assert service.populate_watchlist_from_folders() == 2

    assert db_path.exists()
    assert sorted((r["platform"], r["genre"], r["display_name"], r["status"])
                  for r in _rows(db_path)) == [
        ("SoundCloud", "Techno", "Berlin Sets", "needs_resolve"),
        ("YouTube", "House", "Deep House Daily", "needs_resolve"),
    ]
    assert len(service.watchlist_list()) == 2


def test_a_watch_list_with_rows_is_left_alone_without_a_write(tmp_path):
    _channel_folder(tmp_path, "Fresh Finds")
    service, db_path = _build(tmp_path, database=True)
    DownloadsDatabase(str(db_path)).add_watchlist_channel(
        url="https://yt/c0", display_name="Channel 0", platform="YouTube",
        genre="House")

    assert service.populate_watchlist_from_folders() == 0

    assert [r["display_name"] for r in _rows(db_path)] == ["Channel 0"]
    assert service._watchlist_ops is None       # the writer was never built


def test_the_startup_scan_armed_next_sees_the_rows(tmp_path, monkeypatch):
    """The window fills first, then arms start_startup_scan — which arms
    nothing for an empty list. In that order the fresh rows get their launch
    scan; the other way round a first launch would never scan at all."""
    _channel_folder(tmp_path, "Deep House Daily")
    service, _ = _build(tmp_path)
    monkeypatch.setattr(service, "_startup_scan_wait", lambda: None)

    assert service.start_startup_scan() is None

    service.populate_watchlist_from_folders()
    thread = service.start_startup_scan()

    assert thread is not None
    thread.join(5)


def test_a_fill_that_blows_up_never_reaches_the_launch(tmp_path, monkeypatch):
    """A launch step: whatever goes wrong in it is not a reason to take the
    window down for a Watch List that was empty anyway."""
    _channel_folder(tmp_path, "Deep House Daily")
    service, _ = _build(tmp_path)

    def explode(self):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(WatchlistOps, "populate_from_folders", explode)

    assert service.populate_watchlist_from_folders() == 0

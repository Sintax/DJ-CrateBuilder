"""fs.watchlist_export / fs.watchlist_import through the service, with the
native dialog stubbed to a path under tmp_path."""
import json

import pytest

from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings
from cratebuilder import watchlist_share as share


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def _track(service, url, name, genre="Techno", cid=None, platform="YouTube"):
    return service._db_for_write().add_watchlist_channel(
        url=url, display_name=name, platform=platform, genre=genre,
        channel_id=cid)


def _dialog(monkeypatch, service, path):
    seen = []
    def fake(kind, **opts):
        seen.append((kind, opts))
        return path
    monkeypatch.setattr(service, "_file_dialog", fake)
    return seen


def test_export_refuses_an_empty_list(service, monkeypatch, tmp_path):
    _dialog(monkeypatch, service, str(tmp_path / "list.json"))
    with pytest.raises(CBError, match="empty"):
        service.call("fs.watchlist_export")


def test_a_cancelled_dialog_writes_and_adds_nothing(service, monkeypatch):
    _track(service, "https://www.youtube.com/@a", "A")
    _dialog(monkeypatch, service, None)
    assert service.call("fs.watchlist_export") == {"path": None, "count": 0}
    assert service.call("fs.watchlist_import") == {"path": None, "added": 0,
                                                   "skipped": 0}
    assert len(service._watchlist_rows()) == 1


def test_export_writes_the_shareable_list(service, monkeypatch, tmp_path):
    _track(service, "https://www.youtube.com/@a", "A", cid="UC1")
    _track(service, "https://soundcloud.com/b", "B", genre="House",
           platform="SoundCloud")
    out = tmp_path / "list.json"
    seen = _dialog(monkeypatch, service, str(out))
    res = service.call("fs.watchlist_export")
    assert res == {"path": str(out), "count": 2}
    assert seen[0][1]["save_filename"] == share.default_filename()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == share.FORMAT
    assert {c["url"] for c in data["channels"]} == {
        "https://www.youtube.com/@a", "https://soundcloud.com/b"}


def test_import_adds_only_the_channels_not_already_tracked(service, monkeypatch,
                                                           tmp_path):
    mine = _track(service, "https://www.youtube.com/@a", "My A", genre="Mine",
                  cid="UC1")
    theirs = tmp_path / "theirs.json"
    theirs.write_text(share.dumps([
        {"url": "https://www.youtube.com/@a-renamed", "display_name": "Their A",
         "platform": "YouTube", "genre": "Theirs", "channel_id": "UC1"},
        {"url": "https://www.youtube.com/@b", "display_name": "B",
         "platform": "YouTube", "genre": "Theirs", "channel_id": "UC2"},
    ]), encoding="utf-8")
    _dialog(monkeypatch, service, str(theirs))
    res = service.call("fs.watchlist_import")
    assert res == {"path": str(theirs), "added": 1, "skipped": 1}

    rows = {r["url"]: r for r in service._watchlist_rows()}
    assert set(rows) == {"https://www.youtube.com/@a", "https://www.youtube.com/@b"}
    # Mine is untouched — name, genre and id all as they were.
    assert rows["https://www.youtube.com/@a"]["display_name"] == "My A"
    assert rows["https://www.youtube.com/@a"]["genre"] == "Mine"
    assert rows["https://www.youtube.com/@a"]["id"] == mine
    # Theirs arrives with the sender's name and genre, not auto-added.
    added = rows["https://www.youtube.com/@b"]
    assert (added["display_name"], added["genre"], added["channel_id"],
            added["auto_added"]) == ("B", "Theirs", "UC2", 0)

    # A second import of the same file adds nothing more.
    assert service.call("fs.watchlist_import")["added"] == 0


def test_a_file_that_is_not_an_export_changes_nothing(service, monkeypatch,
                                                       tmp_path):
    _track(service, "https://www.youtube.com/@a", "A")
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")
    _dialog(monkeypatch, service, str(junk))
    with pytest.raises(CBError, match="not a DJ-CrateBuilder"):
        service.call("fs.watchlist_import")
    assert len(service._watchlist_rows()) == 1


def test_a_missing_file_is_a_readable_error(service, monkeypatch, tmp_path):
    _dialog(monkeypatch, service, str(tmp_path / "gone.json"))
    with pytest.raises(CBError, match="Couldn't read"):
        service.call("fs.watchlist_import")

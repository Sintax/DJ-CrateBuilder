"""fs.watchlist_export / fs.watchlist_import_read / watchlist.import_entry
through the service, with the native dialog stubbed to a path under tmp_path."""
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


def _theirs(tmp_path, entries):
    path = tmp_path / "theirs.json"
    path.write_text(share.dumps(entries), encoding="utf-8")
    return path


def _entry(url, name, genre="Theirs", cid=None, platform="YouTube"):
    return {"url": url, "display_name": name, "platform": platform,
            "genre": genre, "channel_id": cid}


def test_export_refuses_an_empty_list(service, monkeypatch, tmp_path):
    _dialog(monkeypatch, service, str(tmp_path / "list.json"))
    with pytest.raises(CBError, match="empty"):
        service.call("fs.watchlist_export")


def test_a_cancelled_dialog_writes_and_adds_nothing(service, monkeypatch):
    _track(service, "https://www.youtube.com/@a", "A")
    _dialog(monkeypatch, service, None)
    assert service.call("fs.watchlist_export") == {"path": None, "count": 0}
    assert service.call("fs.watchlist_import_read") == {"path": None,
                                                        "entries": []}
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


def test_export_writes_only_the_ticked_channels(service, monkeypatch, tmp_path):
    a = _track(service, "https://www.youtube.com/@a", "A", cid="UC1")
    _track(service, "https://soundcloud.com/b", "B", platform="SoundCloud")
    out = tmp_path / "some.json"
    _dialog(monkeypatch, service, str(out))
    res = service.call("fs.watchlist_export", {"ids": [a, "junk"]})
    assert res == {"path": str(out), "count": 1}
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [c["url"] for c in data["channels"]] == ["https://www.youtube.com/@a"]


def test_export_with_nothing_ticked_is_refused_before_the_dialog(
        service, monkeypatch, tmp_path):
    _track(service, "https://www.youtube.com/@a", "A")
    seen = _dialog(monkeypatch, service, str(tmp_path / "x.json"))
    with pytest.raises(CBError, match="Tick at least one"):
        service.call("fs.watchlist_export", {"ids": []})
    assert seen == []


def test_import_read_lists_the_file_and_flags_what_clashes(service, monkeypatch,
                                                           tmp_path):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    theirs = _theirs(tmp_path, [
        _entry("https://www.youtube.com/@a-renamed", "Their A", cid="UC1"),
        _entry("https://www.youtube.com/@elsewhere", "my a", cid="UC5"),
        _entry("https://www.youtube.com/@b", "B", cid="UC2"),
    ])
    _dialog(monkeypatch, service, str(theirs))
    res = service.call("fs.watchlist_import_read")
    assert res["path"] == str(theirs)
    tracked = [e["tracked"] for e in res["entries"]]
    assert tracked[0] == {"display_name": "My A",
                          "url": "https://www.youtube.com/@a", "kinds": ["link"]}
    assert tracked[1] == {"display_name": "My A",
                          "url": "https://www.youtube.com/@a", "kinds": ["name"]}
    assert tracked[2] is None
    # Reading the file changes nothing.
    assert len(service._watchlist_rows()) == 1


def test_import_entry_adds_a_new_channel_with_the_senders_name_and_genre(service):
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@b", "B", cid="UC2")})
    assert res["result"] == "added"
    row = service._watchlist_rows()[0]
    assert (row["display_name"], row["genre"], row["channel_id"],
            row["auto_added"]) == ("B", "Theirs", "UC2", 0)


def test_import_entry_reports_a_clash_and_writes_nothing(service):
    mine = _track(service, "https://www.youtube.com/@a", "My A", genre="Mine",
                  cid="UC1")
    by_link = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@a-renamed", "Their A", cid="UC1")})
    assert by_link == {"result": "conflict", "kinds": ["link"],
                       "existing": {"id": mine, "display_name": "My A",
                                    "url": "https://www.youtube.com/@a"},
                       "new_allowed": False}
    by_name = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@elsewhere", "MY A", cid="UC5")})
    assert by_name["kinds"] == ["name"] and by_name["new_allowed"] is True
    both = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@a", "my a")})
    assert both["kinds"] == ["link", "name"] and both["new_allowed"] is False
    rows = service._watchlist_rows()
    assert len(rows) == 1 and rows[0]["display_name"] == "My A"


def test_skip_leaves_the_existing_entry_alone(service):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@a-renamed", "Their A", cid="UC1"),
        "resolve": {"action": "skip"}})
    assert res == {"result": "skipped"}
    row = service._watchlist_rows()[0]
    assert (row["display_name"], row["url"]) == ("My A", "https://www.youtube.com/@a")


def test_overwrite_re_points_the_link_and_nothing_else(service):
    mine = _track(service, "https://www.youtube.com/@a", "My A", genre="Mine",
                  cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@moved", "Their A", genre="Theirs",
                        cid="UC1"),
        "resolve": {"action": "overwrite"}})
    assert res == {"result": "overwritten"}
    rows = service._watchlist_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == mine
    assert row["url"] == "https://www.youtube.com/@moved"
    assert row["channel_id"] == "UC1"
    assert (row["display_name"], row["genre"]) == ("My A", "Mine")


def test_new_adds_a_second_entry_under_the_typed_name(service):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    entry = _entry("https://www.youtube.com/@elsewhere", "My A", cid="UC5")
    res = service.call("watchlist.import_entry", {
        "entry": entry, "resolve": {"action": "new", "name": "My A (theirs)"}})
    assert res["result"] == "added"
    names = sorted(r["display_name"] for r in service._watchlist_rows())
    assert names == ["My A", "My A (theirs)"]


@pytest.mark.parametrize("name, hint", [
    ("", "Type a name"), ("  my a ", "already used")])
def test_new_is_refused_with_a_reason_when_the_name_wont_do(service, name, hint):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@elsewhere", "My A", cid="UC5"),
        "resolve": {"action": "new", "name": name}})
    assert res["result"] == "conflict" and hint in res["name_error"]
    assert len(service._watchlist_rows()) == 1


def test_new_is_refused_when_the_link_itself_is_already_tracked(service):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@a", "Their A"),
        "resolve": {"action": "new", "name": "Fresh"}})
    assert res["result"] == "conflict" and "already tracked" in res["name_error"]
    assert len(service._watchlist_rows()) == 1


def test_import_done_writes_one_summary_line(service):
    res = service.call("watchlist.import_done", {
        "path": "x.json", "added": 2, "overwritten": "1", "skipped": None})
    assert res == {"added": 2, "overwritten": 1, "skipped": 0}
    with open(service._log_path, encoding="utf-8") as fh:
        text = fh.read()
    assert "Imported 2 Watch List channels from x.json (1 re-pointed, 0 skipped)" \
        in text


def test_a_file_that_is_not_an_export_changes_nothing(service, monkeypatch,
                                                       tmp_path):
    _track(service, "https://www.youtube.com/@a", "A")
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")
    _dialog(monkeypatch, service, str(junk))
    with pytest.raises(CBError, match="not a DJ-CrateBuilder"):
        service.call("fs.watchlist_import_read")
    assert len(service._watchlist_rows()) == 1


def test_a_missing_file_is_a_readable_error(service, monkeypatch, tmp_path):
    _dialog(monkeypatch, service, str(tmp_path / "gone.json"))
    with pytest.raises(CBError, match="Couldn't read"):
        service.call("fs.watchlist_import_read")


def test_overwrite_keeps_a_resolved_channel_id_the_file_lacks(service):
    _track(service, "https://www.youtube.com/@a", "My A", genre="Mine", cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@a/videos", "My A"),
        "resolve": {"action": "overwrite"}})
    assert res == {"result": "overwritten"}
    row = service._watchlist_rows()[0]
    assert (row["url"], row["channel_id"]) == (
        "https://www.youtube.com/@a/videos", "UC1")


# ── the boundary holds for a hostile file and a hostile client alike ────────

def test_import_read_refuses_a_file_over_the_size_cap(service, monkeypatch, tmp_path):
    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b" " * (share.MAX_IMPORT_BYTES + 1) + b"}")
    _dialog(monkeypatch, service, str(big))
    with pytest.raises(CBError, match="too large"):
        service.call("fs.watchlist_import_read")


def test_import_read_refuses_a_file_that_is_not_utf8(service, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe\x00\x00 not text")
    _dialog(monkeypatch, service, str(bad))
    with pytest.raises(CBError):
        service.call("fs.watchlist_import_read")


def test_import_read_reports_what_it_dropped(service, monkeypatch, tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps({"format": share.FORMAT, "version": share.VERSION,
        "channels": [{"url": "https://youtu.be/ok"},
                     {"url": "file:///etc/passwd"}]}), encoding="utf-8")
    _dialog(monkeypatch, service, str(f))
    res = service.call("fs.watchlist_import_read")
    assert len(res["entries"]) == 1
    assert len(res["dropped"]) == 1


def test_import_entry_cleans_what_the_client_sends(service):
    res = service.call("watchlist.import_entry", {
        "entry": {"url": "https://www.youtube.com/@b", "display_name": "..",
                  "platform": r"..\..", "genre": r"..\..\Windows",
                  "channel_id": "UC/../x"}})
    assert res["result"] == "added"
    row = service._watchlist_rows()[0]
    assert row["platform"] == "YouTube"
    assert row["genre"] == "(none)"
    assert row["channel_id"] is None
    # ".." is kept as a display name (it is only text) but it never becomes a
    # folder: CrateLayout refuses it as a folder component.
    assert row["display_name"] == ".."


def test_import_entry_refuses_a_link_that_is_not_a_web_address(service):
    with pytest.raises(CBError, match="link"):
        service.call("watchlist.import_entry", {
            "entry": {"url": "file:///C:/Windows/system.ini"}})
    assert service._watchlist_rows() == []


def test_a_typed_name_for_a_new_entry_is_cleaned_too(service):
    _track(service, "https://www.youtube.com/@a", "Alpha", cid="UC1")
    res = service.call("watchlist.import_entry", {
        "entry": _entry("https://www.youtube.com/@other", "Alpha", cid="UC9"),
        "resolve": {"action": "new", "name": "Evil\nLine\u202e"}})
    assert res["result"] == "added"
    names = sorted(r["display_name"] for r in service._watchlist_rows())
    assert names == ["Alpha", "EvilLine"]

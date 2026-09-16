"""browser_receive: window mode, quiet mode, parking before the page is up,
the inbox RPCs, and what each of them emits."""
import sqlite3
from urllib.parse import quote

import pytest

from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import (BROWSER_INBOX, BROWSER_SEND, LOCAL, REMOTE,
                                  RECEIVE_MODE_QUIET, RECEIVE_MODE_WINDOW,
                                  CBError, CrateBuilderService)
from cratebuilder.settings import Settings


def _uri(kind, url):
    return f"djcrate://add?v=1&kind={kind}&url={quote(url, safe='')}"


CHANNEL = "https://soundcloud.com/someartist"
TRACK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def settings(tmp_path):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(tmp_path / "crate"))
    return s


@pytest.fixture
def service(settings, tmp_path):
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    svc.events_seen = []
    svc.events.subscribe(lambda t, p: svc.events_seen.append((t, p)))
    svc.brought_forward = 0

    def _forward():
        svc.brought_forward += 1

    svc.on_bring_forward = _forward
    yield svc
    svc.close()


def _of(service, event_type):
    return [p for t, p in service.events_seen if t == event_type]


def _ready(service):
    """The page's first snapshot, which is what makes live sends deliverable."""
    return service.call("state.snapshot", transport=LOCAL)


# ── window mode ──────────────────────────────────────────────────────────────

def test_a_send_before_the_page_is_up_is_parked_then_handed_over_once(service):
    assert service.browser_receive(_uri("channel", CHANNEL)) == {"action": "parked"}
    assert _of(service, BROWSER_SEND) == []
    assert service.brought_forward == 1
    first = _ready(service)
    assert first["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]
    assert first["browser"]["inbox_count"] == 0   # one send needs no overflow
    assert service.brought_forward == 2          # shown again as it is handed over
    second = _ready(service)
    assert second["browser"]["pending"] == []


def test_a_burst_before_the_page_is_up_hands_over_one_and_queues_the_rest(service):
    """The page opens a dialog per send and the second would close the first,
    so only the oldest is handed over — the rest go to the Browser Inbox."""
    second = "https://soundcloud.com/another"
    service.browser_receive(_uri("channel", CHANNEL))
    service.browser_receive(_uri("channel", second))
    service.browser_receive(_uri("track", TRACK))
    snap = _ready(service)
    assert snap["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]
    assert snap["browser"]["inbox_count"] == 2
    assert [(r["kind"], r["url"]) for r in service.call("browser.inbox_list")] == [
        ("channel", second), ("track", TRACK)]
    assert _of(service, BROWSER_INBOX) == [
        {"count": 2, "added": {"kind": "track", "url": TRACK}}]
    notes = _of(service, "notification")
    assert len(notes) == 1 and notes[0]["level"] == "info"
    assert "2 more sends" in notes[0]["body"]
    assert _ready(service)["browser"]["pending"] == []


def test_an_unreadable_inbox_count_never_costs_the_page_its_parked_sends(
        service, settings, monkeypatch):
    """The snapshot is the only hand-over point, so it must not fail after it
    has drained — nor before, over a number the page can live without."""
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("track", TRACK))       # brings a database into being
    settings.set("browser_receive_mode", RECEIVE_MODE_WINDOW)
    service.browser_receive(_uri("channel", CHANNEL))   # parks

    def _boom(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DownloadsDatabase, "inbox_count", _boom)
    snap = _ready(service)
    assert snap["browser"]["inbox_count"] == 0
    assert snap["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]


def test_a_send_after_the_page_is_up_is_emitted_live(service):
    _ready(service)
    assert service.browser_receive(_uri("track", TRACK)) == {"action": "opened"}
    assert _of(service, BROWSER_SEND) == [{"kind": "track", "url": TRACK}]
    assert _ready(service)["browser"]["pending"] == []


def test_a_remote_snapshot_never_drains_the_window_pending_list(service):
    service.browser_receive(_uri("channel", CHANNEL))
    remote = service.call("state.snapshot", transport=REMOTE)
    assert remote["browser"]["pending"] == []
    assert _ready(service)["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]


def test_window_mode_leaves_the_inbox_alone(service):
    _ready(service)
    service.browser_receive(_uri("channel", CHANNEL))
    assert service.browser_inbox_count() == 0
    assert _of(service, BROWSER_INBOX) == []


# ── quiet mode ───────────────────────────────────────────────────────────────

def test_quiet_mode_queues_and_pings_without_bringing_the_window_forward(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    _ready(service)
    assert service.browser_receive(_uri("channel", CHANNEL)) == {
        "action": "queued", "fresh": True}
    assert service.brought_forward == 0
    assert _of(service, BROWSER_SEND) == []
    assert _of(service, BROWSER_INBOX) == [
        {"count": 1, "added": {"kind": "channel", "url": CHANNEL}}]
    notes = _of(service, "notification")
    assert notes and notes[-1]["level"] == "info"
    assert service.browser_inbox_count() == 1


def test_quiet_mode_coalesces_a_duplicate_send(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("channel", CHANNEL))
    assert service.browser_receive(_uri("channel", CHANNEL)) == {
        "action": "queued", "fresh": False}
    assert service.browser_inbox_count() == 1
    assert _of(service, BROWSER_INBOX)[-1] == {"count": 1, "added": None}
    assert len(_of(service, "notification")) == 1    # no second ping


def test_quiet_mode_works_before_the_page_is_up(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("track", TRACK))
    snap = _ready(service)
    assert snap["browser"] == {"inbox_count": 1, "pending": []}


def test_an_unwritable_database_warns_instead_of_raising(service, settings,
                                                         monkeypatch):
    """A locked or corrupt database must not take down the listener thread —
    the send is refused out loud, the same way a bad URI is."""
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "_db_for_write", _boom)
    assert service.browser_receive(_uri("channel", CHANNEL)) == {"action": "rejected"}
    note = _of(service, "notification")[-1]
    assert note["level"] == "warn"
    assert "database is unavailable" in note["body"]
    assert _of(service, BROWSER_INBOX) == []
    assert _of(service, BROWSER_SEND) == []
    assert service.brought_forward == 0          # quiet stays quiet, even here


# ── the inbox RPCs ───────────────────────────────────────────────────────────

def test_inbox_list_take_and_remove(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("channel", CHANNEL))
    service.browser_receive(_uri("track", TRACK))
    rows = service.call("browser.inbox_list")
    assert [(r["kind"], r["url"]) for r in rows] == [
        ("channel", CHANNEL), ("track", TRACK)]
    assert set(rows[0]) == {"id", "kind", "url", "received_at"}
    taken = service.call("browser.inbox_take", {"id": rows[0]["id"]})
    assert taken == {"kind": "channel", "url": CHANNEL}
    assert service.browser_inbox_count() == 1
    assert _of(service, BROWSER_INBOX)[-1] == {"count": 1, "added": None}
    assert service.call("browser.inbox_remove", {"id": rows[1]["id"]}) == {"count": 0}
    assert service.call("browser.inbox_list") == []


def test_taking_a_row_that_is_gone_is_a_user_facing_error(service):
    with pytest.raises(CBError):
        service.call("browser.inbox_take", {"id": 12345})


def test_taking_an_unknown_id_from_a_real_inbox_is_a_user_facing_error(service,
                                                                       settings):
    """The database exists and has rows — only this id is missing."""
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("channel", CHANNEL))
    with pytest.raises(CBError):
        service.call("browser.inbox_take", {"id": 12345})
    assert service.browser_inbox_count() == 1       # nothing was dropped


def test_inbox_calls_without_a_database_are_empty_not_errors(service, tmp_path):
    assert service.call("browser.inbox_list") == []
    assert service.call("browser.inbox_remove", {"id": 1}) == {"count": 0}
    assert not (tmp_path / "cratebuilder.db").exists()


# ── contract §4: errors ──────────────────────────────────────────────────────

def test_a_newer_contract_warns_and_brings_the_window_forward(service):
    _ready(service)
    result = service.browser_receive(
        "djcrate://add?v=99&kind=track&url=https%3A%2F%2Fsoundcloud.com%2Fa%2Fb")
    assert result == {"action": "rejected"}
    note = _of(service, "notification")[-1]
    assert note["level"] == "warn" and "newer version" in note["body"]
    assert service.brought_forward == 1
    assert _of(service, BROWSER_SEND) == []


def test_a_bad_url_warns_with_the_unsupported_message(service):
    result = service.browser_receive(
        "djcrate://add?v=1&kind=track&url=https%3A%2F%2Fevil.example%2Fa")
    assert result == {"action": "rejected"}
    assert "supported YouTube or SoundCloud" in _of(service, "notification")[-1]["body"]


def test_junk_is_ignored_silently_and_never_raises(service):
    for junk in ("djcrate://frobnicate", "show", "", None, object()):
        assert service.browser_receive(junk) == {"action": "ignored"}
    assert service.events_seen == []
    assert service.brought_forward == 0


def test_a_throwing_bring_forward_hook_does_not_break_the_receive(service):
    def boom():
        raise RuntimeError("window already closing")
    service.on_bring_forward = boom
    _ready(service)
    assert service.browser_receive(_uri("channel", CHANNEL)) == {"action": "opened"}

"""The service's Folders Cleanup surface: db.cleanup_start / decide / cancel /
pending, the maintenance slot it runs on, and the refusals ahead of it."""
import os
import threading
import time

import pytest

from cratebuilder import remoteauth
from cratebuilder.cleanuprun import TASK, CleanupOps
from cratebuilder.service import (JOB_FINISHED, MAINTENANCE_JOB, CBError,
                                  CrateBuilderService)
from cratebuilder.settings import Settings

_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 413


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"),
                               log_path=str(tmp_path / "activity.log"))


def add_channel(service, tmp_path, name="Deep House Daily", with_file=True):
    db = service._db_for_write()
    cid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCabc/videos",
        display_name=name, platform="YouTube", genre="House",
        channel_id="UCabc")
    if with_file:
        folder = tmp_path / "crate" / "YouTube" / "House" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Track.mp3").write_bytes(_MP3)
    return cid


class BlockingSession:
    """A listing that waits until the test lets it go."""

    def __init__(self):
        self.gate = threading.Event()

    def list_channel(self, url, ignore_no_formats=False):
        self.gate.wait(10)
        return []


def test_the_calls_are_served_and_pending_is_readable(service):
    methods = service._methods()
    for name in ("db.cleanup_start", "db.cleanup_decide", "db.cleanup_cancel",
                 "db.cleanup_pending"):
        assert name in methods
    assert "db.cleanup_pending" in remoteauth.READ_METHODS
    assert not ({"db.cleanup_start", "db.cleanup_decide", "db.cleanup_cancel"}
                & remoteauth.READ_METHODS)
    assert service.call("db.cleanup_pending") == {"review": None}


def test_start_needs_at_least_one_channel(service):
    for empty in (None, [], ""):
        with pytest.raises(CBError, match="Tick at least one channel"):
            service.call("db.cleanup_start", {"channel_ids": empty})
    with pytest.raises(CBError, match="Not a channel id"):
        service.call("db.cleanup_start", {"channel_ids": ["abc"]})


def test_start_refuses_a_channel_the_viewer_greys_out(service, tmp_path):
    """The eligibility the checkboxes show is judged again on the host, so
    an ineligible channel cannot be sent up by a client that ignored it."""
    cid = add_channel(service, tmp_path, with_file=False)
    with pytest.raises(CBError, match="Folder missing"):
        service.call("db.cleanup_start", {"channel_ids": [cid]})
    with pytest.raises(CBError, match="no longer in the Watch List"):
        service.call("db.cleanup_start", {"channel_ids": [cid + 100]})


def test_start_refuses_while_a_download_runs(service, tmp_path):
    cid = add_channel(service, tmp_path)
    gate = threading.Event()
    service._start_job("batch", gate.wait, 5)
    try:
        with pytest.raises(CBError):
            service.call("db.cleanup_start", {"channel_ids": [cid]})
    finally:
        gate.set()


def test_a_run_holds_the_maintenance_slot_and_names_itself(service, tmp_path):
    cid = add_channel(service, tmp_path)
    session = BlockingSession()
    service._cleanup_ops = CleanupOps(
        service._settings, service._db_for_write, service.emit,
        session_factory=lambda cookies=None: session, decision_timeout=1.0)
    heard = []
    service.events.subscribe(lambda t, p: heard.append((t, p)))

    res = service.call("db.cleanup_start", {"channel_ids": [cid]})
    assert res["task"] == TASK and res["channels"] == 1
    assert service.snapshot()["running"]["maintenance"] is True
    assert service.snapshot()["running"]["maintenance_task"] == TASK
    with pytest.raises(CBError, match="already running"):
        service.call("db.cleanup_start", {"channel_ids": [cid]})
    with pytest.raises(CBError, match="already running"):
        service.call("db.rebuild")

    session.gate.set()
    deadline = time.time() + 5
    while service._job_running(MAINTENANCE_JOB) and time.time() < deadline:
        time.sleep(0.02)

    assert service._job_running(MAINTENANCE_JOB) is False
    assert service.snapshot()["running"]["maintenance_task"] is None
    finished = [p for t, p in heard if t == JOB_FINISHED]
    assert finished and finished[-1]["job"] == MAINTENANCE_JOB
    assert finished[-1]["ok"] is True
    assert os.path.exists(tmp_path / "crate" / "YouTube" / "House"
                          / "Deep House Daily" / "Track.mp3")


def test_decide_without_a_review_is_a_refusal(service):
    with pytest.raises(CBError, match="No channel is waiting"):
        service.call("db.cleanup_decide", {"action": "confirm", "paths": []})
    assert service.call("db.cleanup_cancel") == {"cancelled": True}

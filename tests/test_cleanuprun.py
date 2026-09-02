"""Folders Cleanup ‹Smart›, headless (cratebuilder.cleanuprun).

Every run here works a crate and a database built under tmp_path, lists a
canned channel instead of the network, and "trashes" into a list — nothing
may reach a real folder, the developer's database, or the Recycle Bin.
"""
import os
import threading
import time

import pytest

from cratebuilder.cleanuprun import TASK, CleanupOps
from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import CBError
from cratebuilder.settings import Settings

_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 413


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, type, payload):
        self.events.append((type, payload))

    def of(self, type):
        return [p for t, p in self.events if t == type]


class FakeSession:
    def __init__(self, listing=None, error=None, gate=None):
        self.listing = listing or []
        self.error = error
        self.gate = gate
        self.listed = []

    def list_channel(self, url, ignore_no_formats=False):
        self.listed.append(url)
        if self.gate is not None:
            self.gate.wait(10)
        if self.error:
            raise self.error
        return [dict(e) for e in self.listing]


# Five listed uploads keep the scan trustworthy against a three-file folder
# (is_scan_trustworthy wants scan >= max(folder // 2, 5)).
LISTING = [{"id": f"a{i}", "title": t, "url": f"https://y/{i}"}
           for i, t in enumerate(("One", "Two", "Three", "Four", "Five"), 1)]


class Harness:
    def __init__(self, tmp_path, session=None, trash=None, timeout=1.0):
        self.tmp_path = tmp_path
        self.settings = Settings(path=str(tmp_path / "config.json"))
        self.settings.set("base_dir", str(tmp_path / "crate"))
        self.db = DownloadsDatabase(str(tmp_path / "cratebuilder.db"))
        self.emit = Recorder()
        self.log = []
        self.trashed = []
        self.session = session or FakeSession(listing=LISTING)
        self.ops = CleanupOps(
            self.settings, lambda: self.db, self.emit,
            log_line=self.log.append,
            session_factory=lambda cookies=None: self.session,
            trash=trash or self._trash, decision_timeout=timeout)

    def _trash(self, path):
        self.trashed.append(path)
        os.remove(path)

    def add_channel(self, name="Deep House Daily", genre="House"):
        handle = "".join(ch for ch in name if ch.isalnum()) or "abc"
        return self.db.add_watchlist_channel(
            url=f"https://www.youtube.com/channel/UC{handle}/videos",
            display_name=name, platform="YouTube", genre=genre,
            channel_id=f"UC{handle}")

    def folder(self, name="Deep House Daily", genre="House"):
        return os.path.join(str(self.tmp_path / "crate"), "YouTube", genre, name)

    def track(self, filename, video_id=None, name="Deep House Daily",
              genre="House"):
        """A file in the channel folder, with a downloads row when *video_id*
        is given — that row is what makes a flag "strong"."""
        path = os.path.join(self.folder(name, genre), filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(_MP3 * 2)
        if video_id:
            self.db.backfill_downloads([{
                "video_id": video_id, "title": filename[:-4],
                "channel_name": name, "channel_url": "", "channel_id": None,
                "platform": "YouTube", "genre": genre, "file_path": path,
                "upload_date": "20240101", "ts": 1700000000, "bitrate": "192"}])
        return path

    def run_in_thread(self, cids):
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(self.ops.run(cids)), daemon=True)
        thread.start()
        return thread, result

    def wait_for(self, type, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.emit.of(type)
            if found:
                return found[-1]
            time.sleep(0.02)
        raise AssertionError(f"no {type} event within {timeout}s")

    def notes(self):
        return self.emit.of("notification")

    def phases(self):
        return [(p["name"], p["phase"]) for p in self.emit.of("cleanup.channel")]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# ── channels that never reach a review ───────────────────────────────────────

def test_a_clean_channel_is_reported_clean_and_never_reviewed(h):
    cid = h.add_channel()
    h.track("One.mp3", video_id="a1")
    h.track("Two.mp3", video_id="a2")

    result = h.ops.run([cid])

    assert result == {"removed": 0, "channels": 0, "skipped": 0,
                      "cancelled": False}
    assert h.phases() == [("Deep House Daily", "scanning"),
                          ("Deep House Daily", "clean")]
    assert h.emit.of("cleanup.review") == []
    assert h.trashed == []
    assert h.log[-1] == ("Folder Cleanup | YouTube / House / Deep House Daily: "
                         "0 removed, 2 kept, 0 errors — clean (nothing to remove)")
    note = h.notes()[-1]
    assert note["title"] == "Folders Cleanup complete"
    assert note["body"] == "0 files removed across 0 channels."
    assert note["level"] == "info"
    assert note["job"] == "maintenance" and note["task"] == TASK


def test_a_scan_that_cannot_be_trusted_skips_the_channel(tmp_path):
    """A listing far shorter than the folder is a broken scan, not a channel
    that pruned its catalogue — nothing may be offered for deletion."""
    h = Harness(tmp_path, session=FakeSession(listing=LISTING[:1]))
    cid = h.add_channel()
    for name in ("One", "Gone A", "Gone B", "Gone C", "Gone D", "Gone E"):
        h.track(f"{name}.mp3", video_id=f"v{name}")

    result = h.ops.run([cid])

    assert result["skipped"] == 1 and result["removed"] == 0
    assert h.phases()[-1] == ("Deep House Daily", "skipped")
    assert "scan returned too few videos" in h.log[-1]
    assert h.emit.of("cleanup.review") == []
    assert h.notes()[-1]["level"] == "warn"
    assert "1 channel skipped (see activity log)" in h.notes()[-1]["body"]


def test_a_scan_error_skips_the_channel(tmp_path):
    h = Harness(tmp_path, session=FakeSession(error=RuntimeError("HTTP 429")))
    cid = h.add_channel()
    h.track("Gone.mp3", video_id="zz")

    result = h.ops.run([cid])

    assert result["skipped"] == 1
    assert h.phases()[-1] == ("Deep House Daily", "skipped")
    assert h.emit.of("cleanup.channel")[-1]["note"] == "scan error: HTTP 429"
    assert "skipped (scan error: HTTP 429)" in h.log[-1]
    assert os.path.exists(h.folder() + os.sep + "Gone.mp3")


def test_a_channel_that_left_the_watch_list_is_skipped_quietly(h):
    result = h.ops.run([9999])
    assert result["skipped"] == 1
    assert h.phases() == []


# ── the review ───────────────────────────────────────────────────────────────

def test_the_review_offers_only_what_the_scan_no_longer_lists(h):
    cid = h.add_channel()
    kept = h.track("One.mp3", video_id="a1")
    strong = h.track("Gone Strong.mp3", video_id="zz")
    weak = h.track("Gone Weak.mp3")
    thread, _ = h.run_in_thread([cid])

    review = h.wait_for("cleanup.review")
    h.ops.decide("cancel")
    thread.join(5)

    assert review["name"] == "Deep House Daily"
    assert review["folder"] == h.folder()
    assert review["folder_count"] == 3
    assert (review["index"], review["total"]) == (0, 1)
    offered = {f["full_path"]: f for f in review["flagged"]}
    assert set(offered) == {strong, weak}
    assert kept not in offered
    assert offered[strong]["confidence"] == "strong"
    assert offered[strong]["video_id"] == "zz"
    assert offered[weak]["confidence"] == "weak"
    assert offered[weak]["reason"] == "No record this was ever on the channel"
    assert h.ops.pending_review() is None      # answered


def test_confirm_trashes_only_the_ticked_paths_it_offered(h):
    cid = h.add_channel()
    kept = h.track("One.mp3", video_id="a1")
    strong = h.track("Gone Strong.mp3", video_id="zz")
    weak = h.track("Gone Weak.mp3")
    thread, result = h.run_in_thread([cid])
    h.wait_for("cleanup.review")

    answer = h.ops.decide("confirm", [strong, kept, "C:/evil/anything.mp3"])
    thread.join(5)

    assert answer == {"accepted": True, "action": "confirm", "paths": 1}
    assert h.trashed == [strong]
    assert os.path.exists(kept) and os.path.exists(weak)
    assert {r["file_path"] for r in h.db.get_all_downloads()} == {kept}
    assert result == {"removed": 1, "channels": 1, "skipped": 0,
                      "cancelled": False}
    done = h.emit.of("cleanup.channel")[-1]
    assert done["phase"] == "done"
    assert (done["removed"], done["kept"], done["errors"]) == (1, 2, 0)
    assert h.log[-1] == ("Folder Cleanup | YouTube / House / Deep House Daily: "
                         "1 removed, 2 kept, 0 errors")
    assert h.notes()[-1]["body"] == "1 file removed across 1 channel."


def test_a_confirm_with_nothing_ticked_removes_nothing(h):
    cid = h.add_channel()
    strong = h.track("Gone Strong.mp3", video_id="zz")
    thread, result = h.run_in_thread([cid])
    h.wait_for("cleanup.review")

    h.ops.decide("confirm", [])
    thread.join(5)

    assert h.trashed == [] and os.path.exists(strong)
    assert result["channels"] == 1 and result["removed"] == 0
    assert "confirmed, nothing ticked" in h.log[-1]


def test_skip_leaves_the_channel_alone_and_moves_on(h):
    a = h.add_channel("Alpha")
    b = h.add_channel("Beta")
    gone_a = h.track("Gone A.mp3", video_id="za", name="Alpha")
    gone_b = h.track("Gone B.mp3", video_id="zb", name="Beta")
    thread, result = h.run_in_thread([a, b])

    first = h.wait_for("cleanup.review")
    assert first["name"] == "Alpha" and first["total"] == 2
    h.ops.decide("skip")
    deadline = time.time() + 5
    while len(h.emit.of("cleanup.review")) < 2 and time.time() < deadline:
        time.sleep(0.02)
    second = h.emit.of("cleanup.review")[-1]
    assert second["name"] == "Beta" and second["index"] == 1
    h.ops.decide("confirm", [gone_b])
    thread.join(5)

    assert os.path.exists(gone_a) and not os.path.exists(gone_b)
    assert result == {"removed": 1, "channels": 1, "skipped": 1,
                      "cancelled": False}
    assert any("skipped by user" in line for line in h.log)


def test_cancel_stops_after_the_channel_in_flight(h):
    a = h.add_channel("Alpha")
    b = h.add_channel("Beta")
    h.track("Gone A.mp3", video_id="za", name="Alpha")
    h.track("Gone B.mp3", video_id="zb", name="Beta")
    thread, result = h.run_in_thread([a, b])
    h.wait_for("cleanup.review")

    h.ops.decide("cancel")
    thread.join(5)

    assert result["cancelled"] is True
    assert len(h.emit.of("cleanup.review")) == 1     # Beta never scanned
    assert h.session.listed and len(h.session.listed) == 1
    note = h.notes()[-1]
    assert note["title"] == "Folders Cleanup cancelled"
    assert note["cancelled"] is True and note["level"] == "warn"
    assert h.trashed == []


def test_the_cancel_rpc_wakes_a_waiting_review(h):
    cid = h.add_channel()
    h.track("Gone.mp3", video_id="zz")
    thread, result = h.run_in_thread([cid])
    h.wait_for("cleanup.review")

    assert h.ops.cancel() == {"cancelled": True}
    thread.join(5)

    assert not thread.is_alive()
    assert result["cancelled"] is True
    assert h.ops.running is False


def test_a_review_nobody_answers_is_a_cancel(tmp_path):
    """A browser closed mid-review must not hold the maintenance slot."""
    h = Harness(tmp_path, timeout=0.2)
    cid = h.add_channel()
    gone = h.track("Gone.mp3", video_id="zz")

    result = h.ops.run([cid])

    assert result["cancelled"] is True
    assert os.path.exists(gone)


def test_a_decision_needs_a_pending_review_and_a_known_action(h):
    with pytest.raises(CBError, match="No channel is waiting"):
        h.ops.decide("confirm", [])
    cid = h.add_channel()
    h.track("Gone.mp3", video_id="zz")
    thread, _ = h.run_in_thread([cid])
    h.wait_for("cleanup.review")
    with pytest.raises(CBError, match="Unknown cleanup decision"):
        h.ops.decide("delete everything", [])
    assert h.ops.pending_review()["name"] == "Deep House Daily"
    h.ops.decide("cancel")
    thread.join(5)


def test_a_trash_that_refuses_is_an_error_not_a_kept_file(tmp_path):
    def refuse(path):
        raise PermissionError("locked")

    h = Harness(tmp_path, trash=refuse)
    cid = h.add_channel()
    gone = h.track("Gone.mp3", video_id="zz")
    thread, result = h.run_in_thread([cid])
    h.wait_for("cleanup.review")
    h.ops.decide("confirm", [gone])
    thread.join(5)

    done = h.emit.of("cleanup.channel")[-1]
    assert (done["removed"], done["kept"], done["errors"]) == (0, 0, 1)
    assert os.path.exists(gone)
    assert {r["file_path"] for r in h.db.get_all_downloads()} == {gone}
    assert result["removed"] == 0

"""WatchlistOps: scans, per-channel downloads and channel management."""

import contextlib
import json
import os
import re
import sqlite3

import pytest

from cratebuilder import links as cb_links
from cratebuilder import watchrun
from cratebuilder.batchrun import BatchRunner
from cratebuilder.crate import CrateLayout, classify_scan_entries
from cratebuilder.db import DownloadsDatabase
from cratebuilder.download import Outcome
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings
from cratebuilder.sidecar import read_channel_sidecar
from cratebuilder.watchrun import WatchlistOps
from cratebuilder.ydl import ChannelIdentity, YdlOffline, YdlPermanent


# ── Fakes ────────────────────────────────────────────────────────────────────
class Recorder:
    """Every event the ops emitted, in order."""

    def __init__(self):
        self.events = []

    def __call__(self, type, payload):
        self.events.append((type, payload))

    def of(self, type):
        return [p for t, p in self.events if t == type]

    def lines(self, level=None):
        return [p["text"] for p in self.of("scan.line")
                if level is None or p["level"] == level]


class FakeSession:
    """Canned listing / identity / search answers; never touches the network."""

    def __init__(self, listing=None, identity=None, candidates=None,
                 error=None):
        self.listing = listing or []
        self.identity = identity
        self.candidates = candidates or []
        self.error = error
        self.listed = []

    def list_channel(self, url, ignore_no_formats=False):
        self.listed.append(url)
        if self.error:
            raise self.error
        return [dict(e) for e in self.listing]

    def probe_identity(self, url):
        if self.error:
            raise self.error
        return self.identity or ChannelIdentity(
            channel_id="", handle="", channel_url="", title="",
            display_name="", raw={})

    def probe_formats(self, url):
        return []

    def probe_metadata(self, url):
        return {}

    def search_channels(self, name, max_results=3):
        if self.error:
            raise self.error
        return [dict(c) for c in self.candidates]

    def search_soundcloud_tracks(self, name, limit=20):
        if self.error:
            raise self.error
        return [dict(c) for c in self.candidates]


class FakeDownloader:
    """Writes the file the plan names and its downloads row, then reports it
    downloaded — the two effects of TrackDownloader.run the Watch List cares
    about, so the recorded channel_url is the real one a run would store."""

    def __init__(self, harness, kwargs):
        self.harness = harness
        self.kwargs = kwargs

    def run(self, plan, sink):
        self.harness.plans.append(plan)
        sink.started(plan.title)
        sink.progress(percent=50.0, speed_text="1.2MiB/s")
        if self.kwargs["canceller"].is_set():
            return Outcome(kind="cancelled", title=plan.title)
        sink.finished()
        path = plan.expected_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("audio")
        self.kwargs["remember"](path)
        outcome = self.harness.outcomes.get(
            plan.title, Outcome(kind="downloaded", title=plan.title, path=path))
        if outcome.kind == "downloaded":
            self.harness.db.add_download(
                video_id=plan.video_id, title=plan.title,
                channel_name=plan.channel_name, channel_url=plan.channel_url,
                channel_id=plan.channel_id, platform=plan.platform,
                genre=plan.genre, file_path=path,
                upload_date=plan.upload_date, bitrate="192 kbps MP3")
        return outcome


class Harness:
    """One WatchlistOps plus the fakes it was built from."""

    def __init__(self, tmp_path, session=None, spawn=None):
        self.tmp_path = tmp_path
        self.plans = []
        self.outcomes = {}
        self.log = []
        self.emit = Recorder()
        self.session = session or FakeSession()
        self.settings = _settings(tmp_path)
        self.db = DownloadsDatabase(str(tmp_path / "cratebuilder.db"))
        self.links_path = str(tmp_path / cb_links.LINKS_FILE_NAME)
        self.ops = WatchlistOps(
            self.settings, lambda: self.db, self.emit,
            links_path=self.links_path,
            session_factory=lambda cookies=None: self.session,
            runner_factory=self._runner,
            log_line=self.log.append,
            counts=lambda: {"downloads": 1},
            spawn=spawn or (lambda fn: fn()),
            network_probe=lambda: True,
            now=lambda: 0.0)

    def _runner(self, settings, db, emit, **kwargs):
        return BatchRunner(settings, db, emit,
                           downloader_factory=self._downloader, **kwargs)

    def _downloader(self, **kwargs):
        return FakeDownloader(self, kwargs)

    def add_channel(self, url="https://www.youtube.com/channel/UCabc/videos",
                    name="Deep House Daily", genre="House",
                    platform="YouTube", channel_id="UCabc"):
        return self.db.add_watchlist_channel(
            url=url, display_name=name, platform=platform, genre=genre,
            channel_id=channel_id)

    def folder(self, name="Deep House Daily", genre="House",
               platform="YouTube"):
        return os.path.join(str(self.tmp_path / "crate"), platform,
                            CrateLayout.genre_dir_name(genre), name)

    def row(self, cid):
        return self.db.get_watchlist_channel(cid)


class _FailingCursor:
    """Delegates every statement to the real connection, except the one whose
    SQL contains *needle* — that raises, the way a locked or corrupted database
    would."""

    def __init__(self, conn, needle):
        self._conn = conn
        self._needle = needle

    def execute(self, sql, *args, **kwargs):
        if self._needle in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def break_sql(db, needle):
    """Make one SQL statement fail for real, at the database layer.

    The method under test is never replaced — it runs in full, its transaction
    raises inside its own `with self._conn()`, that context manager rolls back,
    and the method's own `except` returns 0 having committed nothing. Every
    other statement, including the read-backs, goes through untouched."""
    real_conn = db._conn

    @contextlib.contextmanager
    def failing_conn():
        with real_conn() as conn:
            yield _FailingCursor(conn, needle)

    db._conn = failing_conn


def _settings(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.update({"base_dir": str(tmp_path / "crate"),
                     "skip_existing": False,
                     "limit_enabled": False,
                     "rotate_ua": False,
                     "sleep_enabled": False,
                     "cover_art_enabled": False})
    return settings


def _video_id(title):
    """A yt-dlp id derived from the title, so two channels in one test never
    accidentally share one (which would read as "already downloaded")."""
    return "id-" + "".join(ch for ch in title.lower() if ch.isalnum())


def _entries(*titles):
    return [{"id": _video_id(t), "title": t,
             "url": f"https://www.youtube.com/watch?v={_video_id(t)}",
             "upload_date": "20260101"}
            for t in titles]


# ── Pure helpers ─────────────────────────────────────────────────────────────
def test_pending_entries_reads_the_stored_list_and_refuses_anything_else():
    assert watchrun.pending_entries({}) == []
    assert watchrun.pending_entries({"pending_entries_json": "not json"}) == []
    assert watchrun.pending_entries({"pending_entries_json": '{"a": 1}'}) == []
    row = {"pending_entries_json": '[{"id": "a"}, 3]'}
    assert watchrun.pending_entries(row) == [{"id": "a"}]


def test_folder_helpers_tell_a_real_channel_folder_from_a_placeholder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert watchrun.folder_has_audio(str(empty)) is False
    assert watchrun.count_audio_files(str(empty)) == 0
    (empty / "Track.mp3").write_text("x", encoding="utf-8")
    assert watchrun.folder_has_audio(str(empty)) is True
    assert watchrun.count_audio_files(str(empty)) == 1
    assert watchrun.folder_has_audio(str(tmp_path / "nope")) is False


def test_track_specs_carry_the_listing_url_every_download_row_records(tmp_path):
    row = {"id": 4, "platform": "YouTube", "genre": "House",
           "url": "https://www.youtube.com/channel/UCabc",
           "display_name": "Deep House Daily", "channel_id": "UCabc"}
    specs = watchrun.track_specs(row, "/save", _entries("One"))
    assert len(specs) == 1
    spec = specs[0]
    assert spec.row_id == 4
    assert spec.title == "One"
    assert spec.save_dir == "/save"
    assert spec.channel_url.endswith("/videos")
    assert spec.suppress_channel_url == spec.channel_url
    assert spec.channel_id == "UCabc"


def test_scan_verdict_reads_the_error_type_not_its_message():
    assert watchrun.scan_verdict_for(YdlPermanent("gone")) == "needs_resolve"
    assert watchrun.scan_verdict_for(YdlOffline("down")) == "offline"
    assert watchrun.scan_verdict_for(ValueError("?")) is None


# ── Scan ─────────────────────────────────────────────────────────────────────
def test_scan_classifies_and_stores_the_pending_entries(tmp_path):
    listing = _entries("Already In DB", "Already On Disk", "Brand New")
    harness = Harness(tmp_path, FakeSession(listing=listing))
    cid = harness.add_channel()
    folder = harness.folder()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "Already On Disk.mp3"), "w",
              encoding="utf-8") as fh:
        fh.write("x")
    harness.db.add_download(
        video_id=_video_id("Already In DB"), title="Already In DB",
        channel_name="Deep House Daily",
        channel_url="https://www.youtube.com/channel/UCabc/videos",
        platform="YouTube", genre="House",
        file_path=os.path.join(folder, "Already In DB.mp3"),
        upload_date="20260101", bitrate="192 kbps MP3")

    harness.ops.run_scan([cid])

    row = harness.row(cid)
    assert row["pending_new_count"] == 1
    assert row["status"] == "found"
    stored = json.loads(row["pending_entries_json"])
    assert [e["title"] for e in stored] == ["Brand New"]
    # The already-on-disk track is backfilled so the next scan dedups by id.
    assert harness.db.is_video_downloaded(_video_id("Already On Disk"))


def test_scan_writes_the_pending_json_the_tkinter_app_also_writes(tmp_path):
    """The stored column must be byte-identical to what the monolith's own
    call produces: crate.classify_scan_entries' "new" list, handed to
    db.update_watchlist_scan_result unchanged."""
    listing = _entries("One", "Two")
    harness = Harness(tmp_path, FakeSession(listing=listing))
    cid = harness.add_channel()
    harness.ops.run_scan([cid])

    expected = classify_scan_entries(
        listing, is_downloaded=lambda vid: False, folder_keys={},
        limit_sec=None, platform="YouTube")["new"]
    assert harness.row(cid)["pending_entries_json"] == json.dumps(expected)

    # ...and the same helper, called directly with that list, writes the same
    # bytes — so a row written here is one the tkinter app can read back.
    other = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                name="Other", channel_id="UCxyz")
    harness.db.update_watchlist_scan_result(
        other, timestamp=1, pending_count=len(expected),
        pending_entries=expected, status="found")
    assert (harness.row(other)["pending_entries_json"]
            == harness.row(cid)["pending_entries_json"])
    assert all(set(e) == {"id", "title", "url", "upload_date"} for e in expected)


def test_scan_lines_read_the_way_the_design_shows_them(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A", "B")))
    cid = harness.add_channel()
    harness.ops.run_scan([cid])

    lines = harness.emit.lines()
    assert "SCAN Deep House Daily — enumerating uploads…" in lines
    assert ("SCAN Deep House Daily — 2 entries, 2 new since last scan"
            in lines)
    assert lines[-1] == "DONE Scan complete — 2 new across 1 channel"
    cards = harness.emit.of("watchlist.card")
    assert cards and cards[-1]["new_count"] == 2
    assert cards[-1]["name"] == "Deep House Daily"


def test_every_scan_line_matches_the_plans_normative_shape(tmp_path):
    """docs/specs/plans/webui-v2-implementation.md:94 —
    scan.line {ts: "HH:MM:SS", level: default|downloaded|skipped|error, text}.
    The level vocabulary is web/app.js's own log-line class names, so Task 9
    can hand these to Task 5's renderer with no translation."""
    listing = _entries("A")
    listing.append({"id": "up", "title": "Premiere",
                    "live_status": "is_upcoming"})
    harness = Harness(tmp_path, FakeSession(listing=listing))
    good = harness.add_channel()
    bad = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", channel_id=None)
    harness.ops.run_scan([good, bad])
    harness.ops.remove(bad)

    payloads = harness.emit.of("scan.line")
    assert len(payloads) >= 5
    for payload in payloads:
        assert set(payload) == {"ts", "level", "text"}
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", payload["ts"])
        assert payload["level"] in watchrun.LINE_LEVELS
        assert payload["text"] and payload["text"] == payload["text"].strip()
    # All four levels are actually reachable, and every line leads with its
    # keyword so a prefix filter cannot miss one.
    assert {p["level"] for p in payloads} == set(watchrun.LINE_LEVELS)
    assert all(p["text"].split()[0] in ("SCAN", "HELD", "ERROR", "DONE")
               for p in payloads)


def test_scan_holds_back_a_premiere_and_says_so(tmp_path):
    listing = _entries("Out Now")
    listing.append({"id": "up", "title": "Premiere",
                    "live_status": "is_upcoming"})
    harness = Harness(tmp_path, FakeSession(listing=listing))
    cid = harness.add_channel()
    harness.ops.run_scan([cid])

    assert harness.row(cid)["pending_new_count"] == 1
    assert any(line.startswith("HELD Deep House Daily — 1 premiere held back")
               for line in harness.emit.lines(watchrun.LINE_HELD))


def test_scan_refuses_an_unresolved_channel_before_touching_yt_dlp(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A")))
    cid = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", channel_id=None)
    harness.ops.run_scan([cid])

    assert harness.session.listed == []
    assert harness.row(cid)["status"] == "needs_resolve"
    assert any("channel id unresolved" in line
               for line in harness.emit.lines(watchrun.LINE_ERROR))


def test_a_transient_scan_failure_leaves_the_row_exactly_as_it_was(tmp_path):
    harness = Harness(tmp_path, FakeSession(error=YdlOffline("no route")))
    cid = harness.add_channel()
    harness.db.update_watchlist_scan_result(
        cid, timestamp=99, pending_count=3,
        pending_entries=[{"id": "keep"}], status="found")
    harness.ops.run_scan([cid])

    row = harness.row(cid)
    assert row["status"] == "offline"
    assert row["pending_new_count"] == 3
    assert row["last_scanned_timestamp"] == 99
    assert row["url"] == "https://www.youtube.com/channel/UCabc/videos"


def test_a_dead_channel_zeroes_the_pending_list_and_asks_for_a_fix(tmp_path):
    harness = Harness(tmp_path, FakeSession(error=YdlPermanent("404 gone")))
    cid = harness.add_channel()
    harness.db.update_watchlist_scan_result(
        cid, timestamp=99, pending_count=3,
        pending_entries=[{"id": "drop"}], status="found")
    harness.ops.run_scan([cid])

    row = harness.row(cid)
    assert row["status"] == "needs_resolve"
    assert row["pending_new_count"] == 0
    assert json.loads(row["pending_entries_json"]) == []
    assert any("Fix Link" in line for line in harness.emit.lines(watchrun.LINE_ERROR))


def test_scan_all_reports_one_total_across_every_channel(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A", "B")))
    first = harness.add_channel()
    second = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Neon Bass Radio", channel_id="UCxyz")
    harness.ops.run_scan([first, second])

    assert harness.emit.lines()[-1] == ("DONE Scan complete — 4 new across "
                                        "2 channels")


def test_cancelling_a_run_stops_before_the_next_channel(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A")))
    first = harness.add_channel()
    second = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Second", channel_id="UCxyz")

    original = harness.ops._scan_channel

    def scan_then_cancel(cid):
        result = original(cid)
        harness.ops.cancel_all()
        return result

    harness.ops._scan_channel = scan_then_cancel
    harness.ops.run_scan([first, second])

    assert harness.row(second)["status"] == "idle"
    assert any(line.startswith("SCAN Cancelled")
               for line in harness.emit.lines(watchrun.LINE_DEFAULT))


# ── Download ─────────────────────────────────────────────────────────────────
def _scanned(harness, titles=("Brand New",)):
    cid = harness.add_channel()
    harness.session.listing = _entries(*titles)
    harness.ops.run_scan([cid])
    harness.emit.events.clear()
    return cid


def test_download_new_consumes_the_pending_entries_and_clears_them(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("One", "Two"))
    assert harness.row(cid)["pending_new_count"] == 2

    harness.ops.run_download([cid])

    assert sorted(p.title for p in harness.plans) == ["One", "Two"]
    row = harness.row(cid)
    assert row["pending_new_count"] == 0
    assert json.loads(row["pending_entries_json"]) == []
    assert row["status"] == "idle"
    assert row["last_download_started"]
    assert row["total_downloaded"] == 2
    assert os.path.isfile(os.path.join(harness.folder(), "One.mp3"))
    assert harness.emit.lines()[-1].startswith("DONE Download complete — "
                                               "2 tracks downloaded")


def test_a_downloading_card_carries_the_progress_the_design_renders(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("One", "Two"))
    harness.ops.run_download([cid])

    cards = [c for c in harness.emit.of("watchlist.card") if c.get("progress")]
    progress = [c["progress"] for c in cards]
    assert progress[0]["total"] == 2
    assert any(p["title"] == "One" for p in progress)
    assert progress[-1]["done"] == 2
    assert progress[-1]["percent"] == 100
    # Every card raised mid-run says the channel is downloading, which is what
    # greys the card's other buttons out.
    assert all(c["status"] == "downloading" for c in cards)
    assert harness.emit.of("watchlist.card")[-1]["status"] == "idle"


def test_progress_frames_name_the_job_that_produced_them(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("One",))
    harness.ops.run_download([cid])

    frames = harness.emit.of("progress.overall") + harness.emit.of("progress.current")
    assert frames
    assert {f["job"] for f in frames} == {"watchlist"}


def test_nothing_can_join_a_run_that_is_not_a_download(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A")))
    cid = harness.add_channel()

    assert harness.ops.enqueue(cid) is None       # nothing running at all
    harness.ops._begin("scan")
    assert harness.ops.enqueue(cid) is None       # a scan owns the job
    harness.ops.run_download([cid])
    assert harness.ops.enqueue(cid) is None       # ...and the run has ended


def test_a_join_is_never_acknowledged_into_a_queue_that_is_discarded(tmp_path):
    """The two windows a join could be accepted into and then dropped: before
    run_download seeds its queue, and during the terminal flush."""
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("One",))
    second = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Second", channel_id="UCxyz")

    # _begin publishes the mode and the queue in one locked section, so a join
    # can only ever land in the queue the run actually iterates.
    harness.ops._begin("download", [(cid, False)])
    assert harness.ops.enqueue(second) == 2
    assert harness.ops._queue == [(cid, False), (second, False)]

    # ...and the run is closed to joins before the TERMINAL flush, which is
    # real work. A mid-run flush still accepts (the run is genuinely live);
    # only the last one, after _end, must refuse.
    during_flush = []
    harness.ops._flush = lambda: during_flush.append(harness.ops.enqueue(second))
    harness.ops.run_download([cid])
    assert during_flush[-1] is None
    assert during_flush[:-1] and all(p is not None for p in during_flush[:-1])


def test_an_unexpected_failure_names_the_channel_not_its_row_id(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    harness.ops._scan_channel = lambda c: (_ for _ in ()).throw(
        RuntimeError("boom"))

    harness.ops.run_scan([cid])

    errors = harness.emit.lines(watchrun.LINE_ERROR)
    assert errors == ["ERROR Deep House Daily — boom"]


def test_download_new_skips_a_track_the_channel_already_owns(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("Owned", "Fresh"))
    folder = harness.folder()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "Owned.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")

    harness.ops.run_download([cid])

    assert [p.title for p in harness.plans] == ["Fresh"]


def test_force_download_ignores_skipping_and_takes_the_whole_catalogue(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("Owned")))
    cid = harness.add_channel()
    folder = harness.folder()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "Owned.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")

    harness.ops.run_download([cid], force=True)

    assert [p.title for p in harness.plans] == ["Owned"]
    # A forced run never consumes the pending list — it did not read it.
    assert harness.session.listed


def test_a_failed_track_keeps_the_channel_pending_for_the_next_run(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = _scanned(harness, ("One",))
    harness.outcomes["One"] = Outcome(kind="error", title="One",
                                      reason="HTTP 403")

    harness.ops.run_download([cid])

    assert harness.row(cid)["pending_new_count"] == 1


def test_download_says_so_when_there_is_nothing_pending(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    harness.ops.run_download([cid])

    assert harness.plans == []
    assert any("nothing pending" in line
               for line in harness.emit.lines(watchrun.LINE_DEFAULT))


def test_a_second_channel_can_join_the_running_download_queue(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    first = _scanned(harness, ("One",))
    second = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Second", channel_id="UCxyz")
    harness.db.update_watchlist_scan_result(
        second, timestamp=1, pending_count=1,
        pending_entries=_entries("Joined"), status="found")

    joins = []

    original = harness.ops._download_channel

    def download_then_join(cid, force=False):
        if not joins:
            joins.append(harness.ops.enqueue(second))
        return original(cid, force=force)

    harness.ops._download_channel = download_then_join
    harness.ops.run_download([first])

    assert joins == [2]
    assert sorted(p.title for p in harness.plans) == ["Joined", "One"]


def test_a_channel_joining_a_forced_run_still_downloads_new_only(tmp_path):
    """force rides on the queue entry, not the run: Download New pressed while
    a Force Download is in flight must not re-take the joiner's catalogue."""
    harness = Harness(tmp_path, FakeSession(listing=_entries("Forced")))
    forced = harness.add_channel()
    joiner = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Joiner", channel_id="UCxyz")
    harness.db.update_watchlist_scan_result(
        joiner, timestamp=1, pending_count=1,
        pending_entries=_entries("Fresh"), status="found")
    joiner_folder = harness.folder(name="Joiner")
    os.makedirs(joiner_folder, exist_ok=True)
    with open(os.path.join(joiner_folder, "Owned.mp3"), "w",
              encoding="utf-8") as fh:
        fh.write("x")

    joined = []
    original = harness.ops._download_channel

    def join_then_download(cid, force=False):
        if not joined:
            joined.append(harness.ops.enqueue(joiner))
        return original(cid, force=force)

    harness.ops._download_channel = join_then_download
    harness.ops.run_download([forced], force=True)

    assert joined == [2]
    # The forced channel took its whole listing; the joiner took only its one
    # pending entry and left "Owned" alone.
    assert [p.title for p in harness.plans] == ["Forced", "Fresh"]
    assert harness.session.listed == [
        "https://www.youtube.com/channel/UCabc/videos"]


def test_cancelling_one_channel_leaves_the_rest_of_the_run_alone(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    first = _scanned(harness, ("One",))
    second = harness.add_channel(url="https://www.youtube.com/channel/UCxyz",
                                 name="Second", channel_id="UCxyz")
    harness.db.update_watchlist_scan_result(
        second, timestamp=1, pending_count=1,
        pending_entries=_entries("Two"), status="found")
    original = harness.ops._download_channel

    def cancel_second_then_download(cid, force=False):
        harness.ops.cancel(second)
        return original(cid, force=force)

    harness.ops._download_channel = cancel_second_then_download
    harness.ops.run_download([first, second])

    assert [p.title for p in harness.plans] == ["One"]
    assert harness.row(second)["pending_new_count"] == 1


# ── Channel management ───────────────────────────────────────────────────────
def test_add_names_the_channel_from_the_probe_and_mirrors_the_link(tmp_path):
    identity = ChannelIdentity(channel_id="UCnew", handle="@new",
                               channel_url="", title="New Channel",
                               display_name="New Channel", raw={})
    harness = Harness(tmp_path, FakeSession(identity=identity))

    result = harness.ops.add("https://www.youtube.com/@new", genre="Techno")

    row = harness.row(result["channel_id"])
    assert row["display_name"] == "New Channel"
    assert row["channel_id"] == "UCnew"
    assert row["genre"] == "Techno"
    assert cb_links.get_link(harness.links_path, "YouTube", "Techno",
                             "New Channel") == "https://www.youtube.com/@new"


def test_add_refuses_a_channel_already_tracked_under_another_url(tmp_path):
    identity = ChannelIdentity(channel_id="UCabc", handle="", channel_url="",
                               title="", display_name="Deep House Daily",
                               raw={})
    harness = Harness(tmp_path, FakeSession(identity=identity))
    harness.add_channel()

    with pytest.raises(CBError, match="already in the Watch List"):
        harness.ops.add("https://www.youtube.com/@deephousedaily")


def test_add_still_tracks_a_channel_the_probe_could_not_read(tmp_path):
    harness = Harness(tmp_path, FakeSession(error=YdlOffline("no route")))

    result = harness.ops.add("https://www.youtube.com/@offline")

    row = harness.row(result["channel_id"])
    assert row["display_name"] == "https://www.youtube.com/@offline"
    assert any("couldn't read the channel" in line
               for line in harness.emit.lines(watchrun.LINE_ERROR))


def test_remove_drops_the_row_and_leaves_the_folder_alone(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    folder = harness.folder()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "Keep.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")

    harness.ops.remove(cid)

    assert harness.row(cid) is None
    assert os.path.isfile(os.path.join(folder, "Keep.mp3"))


def test_forget_unavailable_clears_only_this_channel(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    harness.db.record_unavailable(
        video_id="v1", platform="YouTube",
        channel_url="https://www.youtube.com/channel/UCabc",
        title="Gone", reason="Removed")

    assert harness.ops.forget_unavailable(cid) == {"removed": 1}
    assert harness.db.count_unavailable_for_channel(
        "https://www.youtube.com/channel/UCabc") == 0


def test_details_reports_the_folder_and_both_counts(tmp_path):
    """The Edit dialog's three lazy reads. `tracks` is what a genre move would
    carry, so it must be the same count _change_genre reports as `moved`."""
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    folder = harness.folder()
    os.makedirs(folder, exist_ok=True)
    for name in ("One.mp3", "Two.mp3", "cover.jpg", "cratebuilder.json"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write("x")
    harness.db.record_unavailable(
        video_id="v1", platform="YouTube",
        channel_url="https://www.youtube.com/channel/UCabc",
        title="Gone", reason="Removed")

    assert harness.ops.details(cid) == {
        "folder": folder, "tracks": 2, "unavailable": 1}


def test_details_on_a_channel_with_no_folder_yet(tmp_path):
    """Never creates the folder, and answers zero rather than raising."""
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()

    details = harness.ops.details(cid)

    assert details["tracks"] == 0 and details["unavailable"] == 0
    assert not os.path.exists(details["folder"])


def test_details_refuses_a_channel_that_is_gone(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    with pytest.raises(CBError):
        harness.ops.details(9999)


def test_editing_the_genre_moves_the_folder_and_rewrites_the_rows(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    src = harness.folder()
    os.makedirs(src, exist_ok=True)
    track = os.path.join(src, "Track.mp3")
    with open(track, "w", encoding="utf-8") as fh:
        fh.write("x")
    harness.db.add_download(
        video_id="v1", title="Track", channel_name="Deep House Daily",
        channel_url="https://www.youtube.com/channel/UCabc/videos",
        platform="YouTube", genre="House", file_path=track,
        upload_date="20260101", bitrate="192 kbps MP3")
    cb_links.save_link(harness.links_path, platform="YouTube", genre="House",
                       display_name="Deep House Daily", url="https://x")

    result = harness.ops.edit(cid, genre="Techno")

    dst = harness.folder(genre="Techno")
    assert result["genre"]["moved"] == 1
    assert result["genre"]["rows"] == 1
    assert os.path.isfile(os.path.join(dst, "Track.mp3"))
    assert not os.path.exists(src)
    assert harness.row(cid)["genre"] == "Techno"
    rows = harness.db.get_all_downloads()
    assert rows[0]["genre"] == "Techno"
    assert rows[0]["file_path"] == os.path.join(dst, "Track.mp3")
    # The out-of-database mirrors are re-filed under the new genre.
    assert cb_links.get_link(harness.links_path, "YouTube", "House",
                             "Deep House Daily") == ""
    assert cb_links.get_link(harness.links_path, "YouTube", "Techno",
                             "Deep House Daily")
    assert read_channel_sidecar(dst)["genre"] == "Techno"


def test_a_failed_database_rewrite_rolls_the_folder_move_back(tmp_path):
    """The real move_channel_downloads runs — only its SQL is broken, so the
    transaction genuinely fails, its own error path runs and nothing commits.
    Replacing the method outright is what hid this: the method is what writes
    watchlist.genre, so mocking it away means the drift can never appear."""
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    src = harness.folder()
    os.makedirs(src, exist_ok=True)
    track = os.path.join(src, "Track.mp3")
    with open(track, "w", encoding="utf-8") as fh:
        fh.write("x")
    harness.db.add_download(
        video_id="v1", title="Track", channel_name="Deep House Daily",
        channel_url="https://www.youtube.com/channel/UCabc/videos",
        platform="YouTube", genre="House", file_path=track,
        upload_date="20260101", bitrate="192 kbps MP3")
    break_sql(harness.db, "UPDATE OR REPLACE downloads")

    with pytest.raises(CBError, match="rolled back"):
        harness.ops.edit(cid, genre="Techno")

    # Disk and database agree: both still say House.
    assert os.path.isfile(track)
    assert not os.path.exists(harness.folder(genre="Techno"))
    assert harness.row(cid)["genre"] == "House"
    assert harness.db.get_all_downloads()[0]["file_path"] == track
    # ...and the failure reached every client, not just the caller.
    assert any("rolled back" in line
               for line in harness.emit.lines(watchrun.LINE_ERROR))
    assert harness.emit.of("watchlist.card")[-1]["genre"] == "House"


def test_a_channel_of_unrecorded_files_moves_without_a_spurious_rollback(tmp_path):
    """Audio on disk with no downloads rows — legacy files, a rebuilt database,
    tracks dropped in by hand — makes move_channel_downloads return 0 while its
    transaction commits perfectly. Reading that zero as a failure is what made
    the folder go back while the row said the new genre."""
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    src = harness.folder()
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "Legacy.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert harness.db.get_all_downloads() == []

    result = harness.ops.edit(cid, genre="Techno")

    dst = harness.folder(genre="Techno")
    assert result["genre"]["rows"] == 0
    assert result["genre"]["moved"] == 1
    assert os.path.isfile(os.path.join(dst, "Legacy.mp3"))
    assert not os.path.exists(src)
    assert harness.row(cid)["genre"] == "Techno"


def test_a_genre_move_onto_an_existing_folder_is_refused(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    src = harness.folder()
    dst = harness.folder(genre="Techno")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(src, "Track.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")

    with pytest.raises(CBError, match="already exists"):
        harness.ops.edit(cid, genre="Techno")

    assert os.path.isfile(os.path.join(src, "Track.mp3"))
    assert harness.row(cid)["genre"] == "House"


def test_an_empty_channel_folder_still_travels_with_its_genre(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    src = harness.folder()
    os.makedirs(src, exist_ok=True)

    harness.ops.edit(cid, genre="Techno")

    assert harness.row(cid)["genre"] == "Techno"
    assert os.path.isdir(harness.folder(genre="Techno"))
    assert not os.path.exists(src)


def test_editing_the_url_canonicalises_a_channel_link(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel(url="https://www.youtube.com/@old",
                              channel_id=None)

    result = harness.ops.edit(cid, url="https://www.youtube.com/channel/UCnew")

    assert result["link"]["resolved"] is True
    row = harness.row(cid)
    assert row["channel_id"] == "UCnew"
    assert row["url"] == "https://www.youtube.com/channel/UCnew/videos"
    assert read_channel_sidecar(harness.folder())["channel_id"] == "UCnew"


def test_editing_the_url_probes_a_handle_for_its_channel_id(tmp_path):
    identity = ChannelIdentity(channel_id="UCprobe", handle="@probe",
                               channel_url="", title="", display_name="",
                               raw={})
    harness = Harness(tmp_path, FakeSession(identity=identity))
    cid = harness.add_channel(url="https://www.youtube.com/@old",
                              channel_id=None)

    harness.ops.edit(cid, url="https://www.youtube.com/@new")

    row = harness.row(cid)
    assert row["channel_id"] == "UCprobe"
    assert row["url"] == "https://www.youtube.com/@new"


def test_a_url_that_belongs_to_another_entry_is_refused_as_a_duplicate(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    harness.add_channel()
    other = harness.add_channel(url="unresolved://Garage Archive",
                                name="Garage Archive", channel_id=None)

    with pytest.raises(CBError, match="already track"):
        harness.ops.edit(other,
                         url="https://www.youtube.com/channel/UCabc/videos")

    assert harness.row(other)["url"] == "unresolved://Garage Archive"


# ── Fix Link ─────────────────────────────────────────────────────────────────
def test_resolve_candidates_marks_the_ones_that_would_duplicate(tmp_path):
    candidates = [
        {"title": "Deep House Daily", "channel_id": "UCabc",
         "url": "https://www.youtube.com/channel/UCabc/videos",
         "handle": "@dhd", "followers": 1200},
        {"title": "Garage Archive Official", "channel_id": "UCgar",
         "url": "https://www.youtube.com/channel/UCgar/videos",
         "handle": "@gar", "followers": 42},
    ]
    harness = Harness(tmp_path, FakeSession(candidates=candidates))
    harness.add_channel()
    cid = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", genre="UK Garage",
                              channel_id=None)

    first, second = harness.ops.resolve_candidates(cid)

    assert first["duplicate_of"]["name"] == "Deep House Daily"
    assert second["duplicate_of"] is None
    assert second["followers"] == 42


def test_resolve_apply_writes_the_row_the_sidecar_and_the_link_store(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", genre="UK Garage",
                              channel_id=None)

    result = harness.ops.resolve_apply(
        cid, resolved_url="https://www.youtube.com/channel/UCgar/videos")

    assert result["channel_id"] == "UCgar"
    row = harness.row(cid)
    assert row["url"] == "https://www.youtube.com/channel/UCgar/videos"
    assert row["status"] == "idle"
    folder = harness.folder(name="Garage Archive", genre="UK Garage")
    assert read_channel_sidecar(folder)["channel_id"] == "UCgar"
    assert cb_links.get_link(harness.links_path, "YouTube", "UK Garage",
                             "Garage Archive").endswith("/UCgar/videos")


def test_resolve_apply_refuses_a_channel_another_entry_already_owns(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    harness.add_channel()
    cid = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", channel_id=None)

    with pytest.raises(CBError, match="already track"):
        harness.ops.resolve_apply(
            cid, resolved_url="https://www.youtube.com/channel/UCabc/videos")


# ── Service dispatch ─────────────────────────────────────────────────────────
def _service(tmp_path, harness):
    service = CrateBuilderService(
        settings=harness.settings,
        db_path=str(tmp_path / "cratebuilder.db"),
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"))
    service._watchlist_ops = harness.ops
    return service


def test_the_dispatch_table_routes_every_watchlist_method(tmp_path):
    harness = Harness(tmp_path, FakeSession(listing=_entries("A")))
    cid = harness.add_channel()
    service = _service(tmp_path, harness)

    assert service.call("watchlist.list")[0]["name"] == "Deep House Daily"
    assert "job_id" in service.call("watchlist.scan", {"channel_id": cid})
    _drain(service)
    assert harness.row(cid)["pending_new_count"] == 1
    assert service.call("watchlist.cancel_all") == {"cancelled": True}
    assert set(service.call("watchlist.details", {"channel_id": cid})) == {
        "folder", "tracks", "unavailable"}


def test_an_unknown_channel_id_is_refused_by_name(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    service = _service(tmp_path, harness)

    with pytest.raises(CBError, match="no longer in the Watch List"):
        service.call("watchlist.scan", {"channel_id": 999})
    with pytest.raises(CBError, match="No channels to scan"):
        service.call("watchlist.scan_all")
    with pytest.raises(CBError, match="No new tracks pending"):
        service.call("watchlist.download_all_new")


def test_watchlist_methods_are_reachable_on_the_remote_transport(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    service = _service(tmp_path, harness)

    names = [m for m in service._methods() if m.startswith("watchlist.")]
    assert len(names) == 15
    for name in names:
        # The transport gate refuses before dispatch, so anything that gets
        # past it and fails on its own arguments has proven the point.
        try:
            service.call(name, {"channel_id": 999}, transport="remote")
        except CBError as exc:
            assert "only available in the app window" not in str(exc), name
    assert service.call("watchlist.list", transport="remote") == []


def test_a_genre_move_is_refused_while_either_download_runs(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel()
    service = _service(tmp_path, harness)
    src = harness.folder()
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "Track.mp3"), "w", encoding="utf-8") as fh:
        fh.write("x")

    for category in ("batch", "watchlist"):
        service._jobs[category] = 1
        try:
            with pytest.raises(CBError, match="folder can't be moved"):
                service.call("watchlist.edit",
                             {"channel_id": cid, "genre": "Techno"})
            # A link edit touches no files and stays allowed.
            service.call("watchlist.edit",
                         {"channel_id": cid,
                          "url": "https://www.youtube.com/channel/UCnew"})
        finally:
            service._jobs.pop(category, None)

    assert harness.row(cid)["genre"] == "House"
    assert os.path.isfile(os.path.join(src, "Track.mp3"))
    assert harness.row(cid)["channel_id"] == "UCnew"


def test_a_killed_frontend_leaves_no_row_stuck_mid_job(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    scanning = harness.add_channel()
    downloading = harness.add_channel(
        url="https://www.youtube.com/channel/UCxyz", name="Second",
        channel_id="UCxyz")
    harness.db.update_watchlist_status(scanning, "scanning")
    harness.db.update_watchlist_status(downloading, "downloading")

    # A fresh service is a fresh process: neither status can still be true.
    service = _service(tmp_path, harness)

    assert harness.row(scanning)["status"] == "idle"
    assert harness.row(downloading)["status"] == "idle"
    assert service.call("watchlist.list")[0]["status"] == "idle"


def test_the_startup_reset_never_brings_a_database_into_existence(tmp_path):
    db_path = tmp_path / "not-created" / "cratebuilder.db"
    CrateBuilderService(settings=_settings(tmp_path), db_path=str(db_path),
                        log_path=str(tmp_path / "activity.log"),
                        debug_log_path=str(tmp_path / "debug.log"))
    assert not db_path.exists()


def test_force_download_refuses_an_unresolved_channel(tmp_path):
    harness = Harness(tmp_path, FakeSession())
    cid = harness.add_channel(url="unresolved://Garage Archive",
                              name="Garage Archive", channel_id=None)
    service = _service(tmp_path, harness)

    with pytest.raises(CBError, match="isn't resolved yet"):
        service.call("watchlist.force_download", {"channel_id": cid})


def _drain(service, timeout=5.0):
    """Wait for the watchlist job thread to finish."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while service._job_running("watchlist") and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert not service._job_running("watchlist")

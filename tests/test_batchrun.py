"""BatchRunner: headless batch orchestration, its events and its controls."""

import os
import threading

import pytest

from cratebuilder import service as service_module
from cratebuilder import activitylog, util
from cratebuilder.batchrun import (BatchRunner, TrackSpec, entry_url, eta_text,
                                   pick_session_ua, resolve_sleep_range)
from cratebuilder.crate import SkipMode
from cratebuilder.db import DownloadsDatabase
from cratebuilder.download import Outcome
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings
from cratebuilder.sidecar import read_channel_sidecar, write_channel_sidecar
from cratebuilder.ydl import YdlPermanent


# ── Fakes ────────────────────────────────────────────────────────────────────
class Recorder:
    """Every event the runner emitted, in order."""

    def __init__(self):
        self.events = []

    def __call__(self, type, payload):
        self.events.append((type, payload))

    @property
    def types(self):
        return [t for t, _ in self.events]

    def of(self, type):
        return [p for t, p in self.events if t == type]


class FakeSession:
    """Canned probe / listing answers; never touches the network."""

    def __init__(self, probe, listing, error=None):
        self.probe = probe
        self.listing = listing
        self.error = error

    def probe_metadata(self, url):
        if self.error:
            raise self.error
        return dict(self.probe)

    def list_channel(self, url, ignore_no_formats=False):
        return [dict(e) for e in self.listing]

    def probe_formats(self, url):
        return []


class FakeDownloader:
    """Records its plan, drives the Sink, honours the canceller, and returns
    whatever Outcome the test scripted for that title."""

    def __init__(self, harness, kwargs):
        self.harness = harness
        self.kwargs = kwargs

    def run(self, plan, sink):
        self.harness.plans.append(plan)
        if self.harness.on_start:
            self.harness.on_start(plan)
        sink.started(plan.title)
        sink.bitrate_detected(160, plan.target_kbps)
        sink.progress(percent=50.0, speed_text="1.2MiB/s  00:03")
        if self.kwargs["canceller"].is_set():
            return Outcome(kind="cancelled", title=plan.title)
        sink.finished()
        outcome = self.harness.outcomes.get(
            plan.title, Outcome(kind="downloaded", title=plan.title,
                                path=os.path.join(plan.save_dir, plan.title),
                                bitrate_text="160k → 192k"))
        if self.harness.on_end:
            self.harness.on_end(plan)
        return outcome


class Harness:
    """One runner plus the fakes it was built from."""

    def __init__(self, tmp_path, probe, listing, error=None, settings=None):
        self.plans = []
        self.built = []
        self.sidecars = []
        self.outcomes = {}
        self.on_start = None
        self.on_end = None
        self.log = []
        self.emit = Recorder()
        self.settings = settings or _settings(tmp_path)
        self.db = DownloadsDatabase(str(tmp_path / "cratebuilder.db"))
        self.session = FakeSession(probe, listing, error)
        self.runner = BatchRunner(
            self.settings, self.db, self.emit,
            session_factory=lambda cookies=None: self.session,
            downloader_factory=self._make_downloader,
            write_sidecar=self._record_sidecar,
            log_line=self.log.append,
            counts=lambda: {"downloads": 7})

    def _record_sidecar(self, folder, **kwargs):
        """Records the stamp, then writes the real sidecar file."""
        self.sidecars.append(dict(kwargs, folder=folder))
        return write_channel_sidecar(folder, **kwargs)

    def _make_downloader(self, **kwargs):
        downloader = FakeDownloader(self, kwargs)
        self.built.append(downloader)
        return downloader

    @property
    def titles(self):
        return [p.title for p in self.plans]


def _settings(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.update({"base_dir": str(tmp_path / "crate"),
                     "skip_existing": False,
                     "limit_enabled": False,
                     "rotate_ua": False,
                     "sleep_enabled": False,
                     "cover_art_enabled": False})
    return settings


def _row(row_id=1, url="https://youtube.com/watch?v=aaa", genre="Techno",
         platform="YouTube", state="queued", **extra):
    return dict({"id": row_id, "url": url, "genre": genre,
                 "platform": platform, "state": state}, **extra)


TRACK_PROBE = {"_type": "video", "id": "aaa", "title": "One Track"}
LIST_PROBE = {"_type": "playlist", "title": "UKF", "channel_id": "UC123",
              "uploader_id": "@UKF"}


def _entries(*titles):
    return [{"id": f"id{i}", "title": t,
             "url": f"https://youtube.com/watch?v=id{i}"}
            for i, t in enumerate(titles)]


# ── Pure helpers ─────────────────────────────────────────────────────────────
def test_entry_url_prefers_the_entry_url_then_builds_a_watch_url():
    assert entry_url({"url": "u"}, "YouTube") == "u"
    assert entry_url({"id": "x"}, "YouTube") == "https://www.youtube.com/watch?v=x"
    assert entry_url({"id": "x"}, "SoundCloud") == "x"


def test_sleep_range_reads_the_preset_and_the_manual_pair(tmp_path):
    settings = _settings(tmp_path)
    settings.update({"sleep_enabled": True, "sleep_mode": "Auto",
                     "sleep_preset": "Moderate  (3–8 s)"})
    assert resolve_sleep_range(settings.download_policy()) == (3, 8)
    settings.update({"sleep_mode": "Manual", "sleep_min": 9, "sleep_max": 2})
    assert resolve_sleep_range(settings.download_policy()) == (9, 9)
    settings.set("sleep_enabled", False)
    assert resolve_sleep_range(settings.download_policy()) is None


def test_session_ua_is_one_agent_for_the_whole_batch(tmp_path):
    settings = _settings(tmp_path)
    assert pick_session_ua(settings.download_policy()) is None
    settings.set("rotate_ua", True)
    assert pick_session_ua(settings.download_policy()) in util.USER_AGENT_POOL


def test_over_limit_reason_matches_the_activity_log_wording():
    assert activitylog.over_limit(671, 8) == "exceeds limit (11:11 > 8:00)"


def test_eta_text_reads_as_time_left_and_says_nothing_when_it_cannot_tell():
    assert eta_text([], 5) == ""
    assert eta_text([10.0], 0) == ""
    assert eta_text([120.0, 120.0], 3) == "~6 min left"
    assert eta_text([2.0], 4) == "~8 sec left"


# ── Single track ─────────────────────────────────────────────────────────────
def test_single_track_downloads_and_emits_the_whole_sequence(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])

    assert harness.titles == ["One Track"]
    current = harness.emit.of("progress.current")
    assert current[0]["title"] == "One Track"
    assert current[-1]["percent"] == 100
    assert any(p["bitrate_text"] == "160k → 192k" for p in current)

    rows = harness.emit.of("queue.row")
    assert [r["state"] for r in rows] == ["active", "done"]
    assert rows[0]["id"] == 1 and rows[0]["index"] == 0

    overall = harness.emit.of("progress.overall")
    assert overall[0]["done"] == 0 and overall[0]["total"] == 1
    assert overall[-1] == {"done": 1, "total": 1, "downloaded": 1,
                           "skipped": 0, "errors": 0, "percent": 100,
                           "eta_text": ""}
    assert harness.emit.of("batch.finished")[-1] == {
        "downloaded": 1, "skipped": 0, "errors": 0, "cancelled": False}
    assert harness.emit.of("state.patch")[-1] == {"counts": {"downloads": 7}}


def test_a_one_off_track_saves_under_the_genre_with_no_channel_folder(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])
    expected = tmp_path / "crate" / "YouTube" / "Techno"
    assert harness.plans[0].save_dir == str(expected)
    assert expected.is_dir()


def test_the_downloader_is_built_with_the_real_database(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])
    assert harness.built[0].kwargs["db"] is harness.db


# ── Collections ──────────────────────────────────────────────────────────────
def test_a_playlist_expands_to_one_track_per_entry(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B", "C"))
    harness.runner.run([_row()])

    assert harness.titles == ["A", "B", "C"]
    assert harness.plans[0].save_dir == str(
        tmp_path / "crate" / "YouTube" / "Techno" / "UKF")
    assert harness.plans[0].channel_name == "UKF"
    assert harness.plans[0].channel_url == "https://youtube.com/watch?v=aaa"
    assert harness.emit.of("progress.overall")[-1]["total"] == 3
    assert harness.emit.of("queue.row")[-1] == {
        "id": 1, "index": 0, "state": "done",
        "title": "https://youtube.com/watch?v=aaa", "detail": "3 downloaded"}


def test_an_entry_without_a_title_gets_the_platform_item_word(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, [{"id": "z", "url": "https://z"}])
    harness.runner.run([_row()])
    assert harness.titles == ["Video 1"]


# ── Skips ────────────────────────────────────────────────────────────────────
def test_a_track_already_in_the_folder_is_skipped_when_the_policy_says_so(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B"))
    harness.settings.update({"skip_existing": True,
                             "skip_mode": "In Folder Only"})
    folder = tmp_path / "crate" / "YouTube" / "Techno" / "UKF"
    folder.mkdir(parents=True)
    (folder / "A.mp3").write_text("x", encoding="utf-8")

    harness.runner.run([_row()])

    assert harness.titles == ["B"]
    finished = harness.emit.of("batch.finished")[-1]
    assert (finished["downloaded"], finished["skipped"]) == (1, 1)
    assert any("Reason: already on disk" in line for line in harness.log)


def test_skipping_is_off_when_the_setting_is_off(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    folder = tmp_path / "crate" / "YouTube" / "Techno" / "UKF"
    folder.mkdir(parents=True)
    (folder / "A.mp3").write_text("x", encoding="utf-8")
    harness.runner.run([_row()])
    assert harness.titles == ["A"]


def test_the_duration_limiter_skips_the_long_entry(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, [
        {"id": "a", "title": "Short", "duration": 200},
        {"id": "b", "title": "Epic Set", "duration": 671},
    ])
    harness.settings.update({"limit_enabled": True, "limit_minutes": 8})

    harness.runner.run([_row()])

    assert harness.titles == ["Short"]
    assert any("Reason: exceeds limit (11:11 > 8:00)" in line
               for line in harness.log)
    assert harness.emit.of("batch.finished")[-1]["skipped"] == 1


def test_a_premiere_is_neither_downloaded_nor_an_error(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, [
        {"id": "a", "title": "Premiere", "live_status": "is_upcoming"},
        {"id": "b", "title": "Real"},
    ])
    harness.runner.run([_row()])

    assert harness.titles == ["Real"]
    finished = harness.emit.of("batch.finished")[-1]
    assert finished == {"downloaded": 1, "skipped": 0, "errors": 0,
                        "cancelled": False}


def test_a_queue_row_marked_skipped_is_passed_over(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row(1, state="skipped"), _row(2)])

    assert len(harness.titles) == 1
    first = harness.emit.of("queue.row")[0]
    assert (first["id"], first["state"]) == (1, "skipped")
    assert any("SKIPPED" in line and "skipped by user" in line
               for line in harness.log)


# ── Channel identity: sidecar + Watch List ───────────────────────────────────
def test_a_channel_row_stamps_the_folder_with_its_identity(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.runner.run([_row()])

    stamped = read_channel_sidecar(
        str(tmp_path / "crate" / "YouTube" / "Techno" / "UKF"))
    assert stamped["channel_id"] == "UC123"
    assert stamped["display_name"] == "UKF"
    assert stamped["handle"] == "@UKF"
    assert stamped["genre"] == "Techno"
    assert harness.sidecars[0]["platform"] == "YouTube"


def test_a_one_off_track_row_stamps_nothing(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])
    assert harness.sidecars == []
    assert read_channel_sidecar(
        str(tmp_path / "crate" / "YouTube" / "Techno")) is None


def test_a_collection_without_a_canonical_id_stamps_nothing(tmp_path):
    harness = Harness(tmp_path, {"_type": "playlist", "title": "Mixes"},
                      _entries("A"))
    harness.runner.run([_row()])
    assert harness.sidecars == []


def test_a_channel_row_is_auto_added_to_the_watch_list(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.runner.run([_row()])

    tracked = harness.db.get_all_watchlist_channels()
    assert len(tracked) == 1
    assert tracked[0]["display_name"] == "UKF"
    assert tracked[0]["channel_id"] == "UC123"
    assert tracked[0]["genre"] == "Techno"
    assert tracked[0]["auto_added"] == 1


def test_auto_add_is_off_when_the_setting_is_off(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.settings.set("auto_add_to_watchlist", False)
    harness.runner.run([_row()])
    assert harness.db.get_all_watchlist_channels() == []


def test_a_one_off_track_is_never_auto_added(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])
    assert harness.db.get_all_watchlist_channels() == []


def test_an_already_tracked_channel_is_backfilled_not_duplicated(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.db.add_watchlist_channel(
        url="https://youtube.com/watch?v=aaa", display_name="",
        platform="YouTube", genre="Techno")

    harness.runner.run([_row()])

    tracked = harness.db.get_all_watchlist_channels()
    assert len(tracked) == 1
    assert tracked[0]["channel_id"] == "UC123"     # backfilled
    assert tracked[0]["display_name"] == "UKF"     # backfilled


def test_a_watch_list_run_is_not_auto_added_again(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.runner.run([_row(channel_name="UKF")])
    assert harness.db.get_all_watchlist_channels() == []


def test_a_nameless_collection_never_becomes_a_blank_card(tmp_path):
    # Nothing to name it by — not even a channel id, which derive_collection_name
    # would otherwise fall back to.
    harness = Harness(tmp_path, {"_type": "playlist"}, _entries("A"))
    harness.runner.run([_row()])
    assert harness.db.get_all_watchlist_channels() == []


def test_the_watch_list_total_is_recounted_after_the_row_downloads(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    recounted = []
    real = harness.db.refresh_watchlist_total
    harness.db.refresh_watchlist_total = lambda wl_id: (recounted.append(wl_id),
                                                        real(wl_id))[1]

    harness.runner.run([_row()])

    assert recounted == [harness.db.get_all_watchlist_channels()[0]["id"]]


def test_nothing_is_recounted_when_the_row_downloaded_nothing(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.outcomes["A"] = Outcome(kind="failed", reason="rate-limited",
                                    title="A")
    recounted = []
    harness.db.refresh_watchlist_total = recounted.append

    harness.runner.run([_row()])
    assert recounted == []


# ── Failures ─────────────────────────────────────────────────────────────────
def test_a_failed_probe_fails_only_that_row(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [],
                      error=YdlPermanent("Video unavailable"))
    harness.runner.run([_row()])

    row = harness.emit.of("queue.row")[-1]
    assert row["state"] == "error"
    assert "gone" in row["detail"]
    assert harness.emit.of("batch.finished")[-1]["errors"] == 1
    assert any(line.startswith("ERROR") for line in harness.log)


def test_a_failed_track_counts_as_an_error_not_a_download(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B"))
    harness.outcomes["A"] = Outcome(kind="failed", reason="rate-limited",
                                    title="A")
    harness.runner.run([_row()])

    finished = harness.emit.of("batch.finished")[-1]
    assert (finished["downloaded"], finished["errors"]) == (1, 1)


def test_an_unavailable_track_counts_with_the_errors(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A"))
    harness.outcomes["A"] = Outcome(kind="unavailable", reason="Removed",
                                    title="A")
    harness.runner.run([_row()])
    assert harness.emit.of("batch.finished")[-1]["errors"] == 1


# ── Controls ─────────────────────────────────────────────────────────────────
def test_pause_holds_before_the_next_track(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B"))
    resumed = threading.Event()

    def resume():
        resumed.set()
        harness.runner.resume()

    def on_end(plan):
        if plan.title == "A":
            harness.runner.pause()
            threading.Timer(0.02, resume).start()

    def on_start(plan):
        if plan.title == "B":
            assert resumed.is_set(), "B started while the batch was paused"

    harness.on_end, harness.on_start = on_end, on_start
    harness.runner.run([_row()])
    assert harness.titles == ["A", "B"]


def test_cancel_stops_after_the_current_track_and_keeps_it(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B", "C"))
    harness.on_end = lambda plan: (harness.runner.cancel()
                                   if plan.title == "A" else None)

    harness.runner.run([_row()])

    assert harness.titles == ["A"]
    assert harness.emit.of("batch.finished")[-1] == {
        "downloaded": 1, "skipped": 0, "errors": 0, "cancelled": True}


def test_cancel_while_paused_releases_the_gate(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B"))

    def on_end(plan):
        harness.runner.pause()
        threading.Timer(0.02, harness.runner.cancel).start()

    harness.on_end = on_end
    harness.runner.run([_row()])
    assert harness.titles == ["A"]
    assert harness.emit.of("batch.finished")[-1]["cancelled"] is True


def test_skip_row_interrupts_the_running_row_and_moves_on(tmp_path):
    harness = Harness(tmp_path, LIST_PROBE, _entries("A", "B", "C"))
    harness.on_start = lambda plan: (harness.runner.skip_row(1)
                                     if plan.title == "B" else None)

    harness.runner.run([_row(1), _row(2, url="https://youtube.com/watch?v=bbb")])

    # B was interrupted by the canceller, C never started, and the next row ran.
    assert harness.titles == ["A", "B", "A", "B", "C"]
    rows = harness.emit.of("queue.row")
    assert [r["state"] for r in rows if r["id"] == 1][-1] == "skipped"
    assert [r["state"] for r in rows if r["id"] == 2][-1] == "done"


def test_skip_row_before_the_row_starts_passes_it_over(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.skip_row(2)
    harness.runner.run([_row(1), _row(2)])
    assert len(harness.titles) == 1


# ── run_tracks as the reusable entry point ───────────────────────────────────
def test_run_tracks_downloads_resolved_specs_without_probing(tmp_path):
    harness = Harness(tmp_path, {}, [], error=AssertionError("probed!"))
    spec = TrackSpec(row_id=None, url="https://t/1", title="Resolved",
                     save_dir=str(tmp_path / "crate" / "YouTube" / "Techno"),
                     genre="Techno", platform="YouTube",
                     entry={"id": "1", "title": "Resolved"})
    tally = harness.runner.run_tracks([spec])

    assert harness.titles == ["Resolved"]
    assert tally["downloaded"] == 1


def test_run_tracks_can_ignore_skip_existing_for_a_forced_download(tmp_path):
    harness = Harness(tmp_path, {}, [])
    harness.settings.update({"skip_existing": True,
                             "skip_mode": "In Folder Only"})
    folder = tmp_path / "crate" / "YouTube" / "Techno"
    folder.mkdir(parents=True)
    (folder / "Owned.mp3").write_text("x", encoding="utf-8")
    spec = TrackSpec(row_id=None, url="https://t/1", title="Owned",
                     save_dir=str(folder), genre="Techno", platform="YouTube",
                     entry={"id": "1", "title": "Owned"})

    assert harness.runner.run_tracks([spec])["skipped"] == 1
    assert harness.runner.run_tracks(
        [spec], ignore_skip_existing=True)["downloaded"] == 1


def test_run_tracks_honours_an_explicit_watch_list_skip_mode(tmp_path):
    harness = Harness(tmp_path, {}, [])          # skip_existing is off
    folder = tmp_path / "crate" / "YouTube" / "Techno"
    folder.mkdir(parents=True)
    (folder / "Owned.mp3").write_text("x", encoding="utf-8")
    spec = TrackSpec(row_id=None, url="https://t/1", title="Owned",
                     save_dir=str(folder), genre="Techno", platform="YouTube",
                     entry={"id": "1", "title": "Owned"})

    tally = harness.runner.run_tracks([spec], skip_mode=SkipMode.WATCH_LIST)
    assert tally["skipped"] == 1


# ── Activity log ─────────────────────────────────────────────────────────────
def test_the_downloader_logs_through_the_injected_log_line(tmp_path):
    harness = Harness(tmp_path, TRACK_PROBE, [])
    harness.runner.run([_row()])
    downloader = harness.built[0]
    downloader.kwargs["log_download"]("T", "C:/x.mp3", "https://u", "YouTube",
                                      "Techno", quality="192 kbps MP3")
    downloader.kwargs["log_error"]("T", "https://u", "rate-limited")

    assert harness.log[0].startswith("═")
    assert ("DOWNLOADED  | Platform: YouTube    | Genre: Techno            "
            "| Title: T | File: C:/x.mp3 | URL: https://u | "
            "Quality: 192 kbps MP3") in harness.log
    assert "ERROR       | Title: T | URL: https://u | Error: rate-limited" \
        in harness.log


def test_skipped_line_matches_the_monolith_column_widths():
    assert activitylog.skipped("T", "C:/x.mp3", "already on disk") == (
        "SKIPPED     | Reason: already on disk     | Title: T | "
        "File: C:/x.mp3")


# ── Service wiring ───────────────────────────────────────────────────────────
class BlockingRunner:
    """A BatchRunner stand-in that parks on the job thread until released."""

    def __init__(self, *args, **kwargs):
        self.started = threading.Event()
        self.release = threading.Event()
        self.paused = None
        self.cancelled = False
        self.skipped = []

    def run(self, rows):
        self.rows = rows
        self.started.set()
        self.release.wait(2)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def cancel(self):
        self.cancelled = True

    def skip_row(self, row_id):
        self.skipped.append(row_id)


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"),
                               log_path=str(tmp_path / "activity.log"))


@pytest.fixture
def running(service, monkeypatch):
    """A service with a real batch job in flight, parked and controllable."""
    runner = BlockingRunner()
    monkeypatch.setattr(service_module, "BatchRunner",
                        lambda *a, **kw: runner)
    service.batch_add("https://youtube.com/watch?v=a", "Techno")
    service.download_start()
    assert runner.started.wait(2)
    yield service, runner
    runner.release.set()


def test_starting_an_empty_queue_is_an_error(service):
    with pytest.raises(CBError):
        service.download_start()


def test_starting_an_all_skipped_queue_is_an_error(service):
    row = service.batch_add("https://youtube.com/watch?v=a")
    service.batch_skip(row["id"])
    with pytest.raises(CBError):
        service.download_start()


def test_start_reports_the_batch_as_running(running):
    service, _ = running
    assert service.snapshot()["running"]["batch"] is True


def test_the_queue_is_locked_while_a_batch_runs(running):
    service, _ = running
    row_id = service.batch_list()[0]["id"]
    for call in (lambda: service.batch_remove(row_id),
                 lambda: service.batch_move(row_id, 1),
                 service.batch_clear):
        with pytest.raises(CBError):
            call()


def test_a_row_added_mid_batch_reaches_the_running_runner(running):
    service, runner = running
    service.batch_add("https://youtube.com/watch?v=b")
    assert [r["url"] for r in runner.rows] == [
        "https://youtube.com/watch?v=a", "https://youtube.com/watch?v=b"]


def test_skip_during_a_run_marks_the_row_and_interrupts_it(running):
    service, runner = running
    row_id = service.batch_list()[0]["id"]
    assert service.batch_skip(row_id)["state"] == "skipped"
    assert runner.skipped == [row_id]
    # Skipping twice does not un-skip a row mid-run.
    assert service.batch_skip(row_id)["state"] == "skipped"


def test_pause_resume_and_cancel_route_to_the_runner(running):
    service, runner = running
    service.download_pause()
    assert runner.paused is True
    service.download_resume()
    assert runner.paused is False
    service.download_cancel()
    assert runner.cancelled is True


def test_controls_without_a_running_batch_are_errors(service):
    for call in (service.download_pause, service.download_resume,
                 service.download_cancel):
        with pytest.raises(CBError):
            call()


def test_log_line_writes_a_timestamped_activity_line(service, tmp_path):
    service.log_line("DOWNLOADED  | Title: T")
    written = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert written.endswith("| DOWNLOADED  | Title: T\n")
    assert written[:4].isdigit()

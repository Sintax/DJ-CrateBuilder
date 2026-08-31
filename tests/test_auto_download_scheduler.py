"""The periodic scan-all-then-download-new run.

`auto_download_interval` has been in the settings schema, rendered in Settings
and shown on the Overview since v2.0 shipped, and util has carried the three
decisions behind it — next_run_delay_ms, scan_settle_verdict, next_run_label —
with tests of their own and no caller anywhere. Nothing auto-downloaded.

This is the scheduler that calls them. The rules that matter most here are the
ones about NOT downloading: never at launch, never on a remote mount, never
over the top of something the user started, and never twice for one interval.

Every test drives the real methods with a real service; the interval is set in
seconds-scale labels where a wait is needed, never by faking a clock (ADR
0001), and the one long wait is cut short by the same wake event the app uses.
"""
import threading
import time

import pytest

from cratebuilder import util
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


@pytest.fixture
def make_service(tmp_path):
    """Services pointed entirely at tmp_path, closed when the test ends."""
    made = []

    def build(transport="local", **settings_values):
        settings = Settings(path=str(tmp_path / f"config{len(made)}.json"))
        settings.set("base_dir", str(tmp_path / "crate"))
        if settings_values:
            settings.update(settings_values)
        service = CrateBuilderService(
            transport=transport, settings=settings,
            db_path=str(tmp_path / "cratebuilder.db"),
            log_path=str(tmp_path / "activity.log"),
            debug_log_path=str(tmp_path / "debug.log"))
        made.append(service)
        return service

    yield build
    for service in made:
        service.close()


def add_channel(service, name="Channel", pending=0):
    db = service._db_for_write()
    row_id = db.add_watchlist_channel(
        url=f"https://www.youtube.com/@{name.replace(' ', '')}",
        display_name=name, platform="YouTube", genre="Drum n Bass")
    if pending:
        db.update_watchlist_scan_result(
            row_id, timestamp=int(time.time()), pending_count=pending,
            pending_entries=[], status="found")
    return row_id


# ── arming: the rules about when it must NOT run ─────────────────────────────

def test_a_constructed_service_arms_nothing(make_service):
    """The whole reason this is an explicit call: importing or snapshotting a
    service must never leave a thread that can start downloading."""
    service = make_service()
    assert service._auto_dl_thread is None
    assert service.next_auto_download()["ts"] is None


def test_a_remote_mount_never_schedules_downloads(make_service):
    """A browser elsewhere must not make this host start downloading on its
    own — the rule start_update_timer and start_startup_scan already follow."""
    service = make_service(transport="remote")
    assert service.start_auto_download_timer() is None
    assert service._auto_dl_thread is None


def test_arming_twice_leaves_one_thread(make_service):
    service = make_service()
    first = service.start_auto_download_timer()
    assert first is not None
    assert service.start_auto_download_timer() is None
    assert service._auto_dl_thread is first


def test_a_closed_service_arms_nothing(make_service):
    service = make_service()
    service.close()
    assert service.start_auto_download_timer() is None


def test_the_schedule_counts_from_this_launch_not_the_stored_anchor(
        make_service):
    """The monolith's rule, and the one that stops an app opened after a week
    away downloading the moment it is looked at. `watchlist_last_download` is
    an ancient timestamp here; the next run must still be a full interval out.
    """
    service = make_service(auto_download_interval="1 week",
                           watchlist_last_download=1)
    before = int(time.time())

    service.start_auto_download_timer()

    assert service._auto_dl_anchor >= before
    for _ in range(200):
        if service.next_auto_download()["ts"] is not None:
            break
        time.sleep(0.01)
    due = service.next_auto_download()["ts"]
    assert due is not None
    # A week out, give or take the moment it took to arm.
    assert due - before > 7 * 86400 - 60


def test_off_arms_no_run_and_says_so(make_service):
    service = make_service(auto_download_interval="Off")
    seen = []
    service.events.subscribe(
        lambda t, p: seen.append(p) if t == "automation.next_run" else None)

    service.start_auto_download_timer()
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)

    assert seen and seen[0]["ts"] is None
    assert seen[0]["text"].endswith("Off")


# ── the next-run announcement ────────────────────────────────────────────────

def test_the_next_run_is_announced_and_carried_in_the_snapshot(make_service):
    service = make_service(auto_download_interval="6 hours")
    seen = []
    service.events.subscribe(
        lambda t, p: seen.append(p) if t == "automation.next_run" else None)

    service.start_auto_download_timer()
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)

    assert seen, "the scheduler never said when it would run"
    assert seen[0] == service.snapshot()["next_auto_download"]
    assert seen[0]["text"] == util.next_run_label(seen[0]["ts"])


def test_the_same_next_run_is_never_announced_twice(make_service):
    """It is pushed to every socket; a repeat per poll would be noise."""
    service = make_service(auto_download_interval="6 hours")
    seen = []
    service.events.subscribe(
        lambda t, p: seen.append(p) if t == "automation.next_run" else None)

    service._publish_next_auto_download(1234)
    service._publish_next_auto_download(1234)
    service._publish_next_auto_download(5678)

    assert [p["ts"] for p in seen] == [1234, 5678]


# ── re-anchoring ─────────────────────────────────────────────────────────────

def test_download_all_new_restarts_the_interval(make_service):
    """The monolith's "Download All New stamps the anchor": whoever pressed
    it, the next scheduled run is a full interval from THIS download."""
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=3)
    service.start_auto_download_timer()
    service._auto_dl_anchor = 1          # pretend it is long overdue

    service.watchlist_download_all_new()

    assert service._auto_dl_anchor > 1


def test_a_refused_download_all_new_does_not_restart_the_interval(
        make_service):
    """Nothing pending is not a download, so it must not push the schedule
    out — otherwise a stray press could hold the scheduler off indefinitely."""
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=0)
    service.start_auto_download_timer()
    service._auto_dl_anchor = 1

    with pytest.raises(CBError):
        service.watchlist_download_all_new()

    assert service._auto_dl_anchor == 1


def test_changing_the_interval_re_arms_the_timer(make_service):
    """Stored and displayed but not re-armed is this feature's failure mode:
    the old interval would stay in force until the app restarted."""
    service = make_service(auto_download_interval="1 week")
    service.start_auto_download_timer()
    service._auto_dl_wake.clear()

    service.settings_set("auto_dl_interval", "6 hours")

    assert service._auto_dl_wake.is_set()


# ── deferring to the user ────────────────────────────────────────────────────

@pytest.mark.parametrize("category", ["batch", "watchlist"])
def test_a_scheduled_run_waits_for_work_the_user_started(make_service,
                                                         category):
    """It never interrupts: the monolith re-arms its tick every BUSY_RETRY_MS
    while a manual scan or download is in flight. A batch counts too — it owns
    the same yt-dlp session a scan would need."""
    service = make_service()
    hold = threading.Event()
    service._start_job(category, hold.wait)
    try:
        done = []
        waiter = threading.Thread(
            target=lambda: done.append(service._wait_until_idle()),
            daemon=True)
        waiter.start()
        waiter.join(0.5)
        assert not done, "it did not wait for the running job"

        service._auto_dl_wake.set()      # stand down rather than sleep 60s
        waiter.join(5)
    finally:
        hold.set()

    assert done == [False]


def test_waiting_ends_as_soon_as_the_host_is_idle(make_service):
    service = make_service()
    assert service._wait_until_idle() is True


def test_waiting_gives_up_when_the_service_closes(make_service):
    service = make_service()
    hold = threading.Event()
    service._start_job("batch", hold.wait)
    try:
        service.close()
        assert service._wait_until_idle() is False
    finally:
        hold.set()


# ── the settle poll ──────────────────────────────────────────────────────────

def test_the_settle_poll_proceeds_once_the_scan_releases_the_slot(
        make_service):
    service = make_service()
    assert service._wait_for_scan_to_settle() is True


def test_a_wedged_scan_is_given_up_on_and_the_interval_restarted(
        make_service, monkeypatch):
    """The cap exists so a stuck scan cannot be polled forever. Giving up has
    to advance the anchor as well, or the next cycle retries it immediately."""
    service = make_service()
    monkeypatch.setattr(util, "SCAN_SETTLE_MAX_POLLS", 2)
    monkeypatch.setattr(util, "SCAN_SETTLE_POLL_MS", 1)
    hold = threading.Event()
    service._start_job("watchlist", hold.wait)
    service._auto_dl_anchor = 1
    try:
        assert service._wait_for_scan_to_settle() is False
    finally:
        hold.set()

    assert service._auto_dl_anchor > 1
    with open(service._log_path, encoding="utf-8") as fh:
        assert "gave up waiting" in fh.read()


# ── a whole run ──────────────────────────────────────────────────────────────

def test_a_run_that_finds_nothing_says_so_and_waits_a_full_interval(
        make_service, monkeypatch):
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=0)
    service._auto_dl_anchor = 1
    # The scan itself is not what is under test here.
    monkeypatch.setattr(service, "watchlist_scan_all", lambda: {"job_id": 1})

    service._auto_download_run()

    assert service._auto_dl_anchor > 1
    with open(service._log_path, encoding="utf-8") as fh:
        log = fh.read()
    assert "Scheduled auto-download starting" in log
    assert "no new tracks" in log


def test_a_run_that_finds_tracks_downloads_them_and_announces_it(
        make_service, monkeypatch):
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=2)
    add_channel(service, "Channel B", pending=3)
    monkeypatch.setattr(service, "watchlist_scan_all", lambda: {"job_id": 1})
    started = []
    monkeypatch.setattr(service, "watchlist_download_all_new",
                        lambda: started.append(True))
    notes = []
    service.events.subscribe(
        lambda t, p: notes.append(p) if t == "notification" else None)

    service._auto_download_run()

    assert started == [True]
    assert notes and notes[-1]["title"] == "Watch List"
    assert "5 new tracks" in notes[-1]["body"]
    assert "2 channels" in notes[-1]["body"]


def test_a_run_closes_its_own_cycle_rather_than_trusting_the_download_to(
        make_service, monkeypatch):
    """watchlist_download_all_new re-anchors on its way in, so the success
    path looked covered. It is not the run's to assume: measured against the
    real window with that call stubbed, the cycle never closed and the
    scheduler fired twice in a row."""
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=2)
    monkeypatch.setattr(service, "watchlist_scan_all", lambda: {"job_id": 1})
    monkeypatch.setattr(service, "watchlist_download_all_new", lambda: None)
    service._auto_dl_anchor = 1

    service._auto_download_run()

    assert service._auto_dl_anchor > 1


def test_an_empty_watch_list_ends_the_cycle_without_a_download(
        make_service, monkeypatch):
    """watchlist_scan_all refuses with nothing to scan; that is a spent cycle,
    not a failure, and it must still push the next run out."""
    service = make_service(auto_download_interval="1 week")
    service._auto_dl_anchor = 1
    downloads = []
    monkeypatch.setattr(service, "watchlist_download_all_new",
                        lambda: downloads.append(True))

    service._auto_download_run()        # no channels at all

    assert downloads == []
    assert service._auto_dl_anchor > 1


def test_a_run_never_starts_while_the_user_is_downloading(make_service,
                                                          monkeypatch):
    """The end-to-end version of the busy rule: nothing is scanned and nothing
    is downloaded while a batch the user started holds the host."""
    service = make_service(auto_download_interval="1 week")
    add_channel(service, "Channel A", pending=2)
    scans, downloads = [], []
    monkeypatch.setattr(service, "watchlist_scan_all",
                        lambda: scans.append(True))
    monkeypatch.setattr(service, "watchlist_download_all_new",
                        lambda: downloads.append(True))
    hold = threading.Event()
    service._start_job("batch", hold.wait)
    try:
        runner = threading.Thread(target=service._auto_download_run,
                                  daemon=True)
        runner.start()
        runner.join(0.5)
        assert scans == [] and downloads == []
        service._auto_dl_wake.set()
        runner.join(5)
    finally:
        hold.set()

    assert scans == [] and downloads == []


def test_the_loop_fires_a_run_when_the_interval_elapses(make_service,
                                                        monkeypatch):
    """The timer really is a timer: armed overdue, the loop reaches a run
    without anything else prodding it."""
    service = make_service(auto_download_interval="6 hours")
    fired = threading.Event()
    monkeypatch.setattr(service, "_auto_download_run", fired.set)

    service.start_auto_download_timer()
    # Overdue by construction, so next_run_delay_ms hands back its 1s floor.
    service._auto_dl_anchor = 1
    service._auto_dl_wake.set()

    assert fired.wait(10), "the scheduled run never fired"


def test_closing_stops_the_loop(make_service):
    service = make_service(auto_download_interval="1 week")
    thread = service.start_auto_download_timer()

    service.close()

    thread.join(5)
    assert not thread.is_alive(), "the scheduler outlived the service"

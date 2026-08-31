"""CrateBuilderService: transport gating, batch queue, settings and snapshots."""

import threading

import pytest

from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import (JOB_FINISHED, JOB_STARTED, CBError,
                                  CrateBuilderService, version_info)
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    """A service pointed entirely at tmp_path — never the developer's config."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


# ── transport gating ─────────────────────────────────────────────────────────

def test_remote_transport_refuses_filesystem_and_updater(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    for method in ("fs.pick_folder", "update.check", "update.apply"):
        with pytest.raises(CBError):
            remote.call(method)


def test_local_transport_advertises_both_capabilities(service):
    caps = service.snapshot()["capabilities"]
    assert caps == {"update": True, "filesystem": True}


def test_remote_snapshot_advertises_neither(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    caps = remote.snapshot()["capabilities"]
    assert caps == {"update": False, "filesystem": False}


def test_unknown_transport_is_rejected():
    with pytest.raises(ValueError):
        CrateBuilderService(transport="carrier-pigeon")


def test_unknown_method_raises_user_facing_error(service):
    with pytest.raises(CBError):
        service.call("nope.not_a_method")


# ── batch queue ──────────────────────────────────────────────────────────────

def test_batch_add_and_list(service):
    row = service.batch_add("https://youtube.com/watch?v=a", "Techno")
    assert row["state"] == "queued"
    assert [r["url"] for r in service.batch_list()] == ["https://youtube.com/watch?v=a"]


def test_batch_add_rejects_blank_url(service):
    with pytest.raises(CBError):
        service.batch_add("   ")


def test_batch_add_defaults_to_the_no_genre_value(service):
    assert service.batch_add("https://x/y")["genre"] == "(none)"


def test_batch_move_reorders(service):
    first = service.batch_add("https://a")
    service.batch_add("https://b")
    service.batch_move(first["id"], 1)
    assert [r["url"] for r in service.batch_list()] == ["https://b", "https://a"]


def test_batch_move_past_the_end_is_a_noop(service):
    row = service.batch_add("https://a")
    service.batch_add("https://b")
    service.batch_move(row["id"], -5)
    assert [r["url"] for r in service.batch_list()] == ["https://a", "https://b"]


def test_batch_remove_and_clear(service):
    row = service.batch_add("https://a")
    service.batch_add("https://b")
    assert len(service.batch_remove(row["id"])) == 1
    assert service.batch_clear() == []


def test_batch_remove_unknown_row_raises(service):
    with pytest.raises(CBError):
        service.batch_remove(4242)


def test_batch_skip_toggles(service):
    row = service.batch_add("https://a")
    assert service.batch_skip(row["id"])["state"] == "skipped"
    assert service.batch_skip(row["id"])["state"] == "queued"


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_set_echoes_stored_value(service):
    assert service.settings_set("skip_existing", False)["value"] is False
    assert service.settings_get("skip_existing")["value"] is False


def test_settings_reject_unknown_key(service):
    with pytest.raises(CBError):
        service.settings_set("not_a_key", 1)
    with pytest.raises(CBError):
        service.settings_get("not_a_key")


def test_settings_all_skips_keys_the_schema_lacks(service):
    values = service.settings_all()
    assert "skip_existing" in values
    assert "log_limit" in values          # bound to log_max_mb, which exists
    # Contract-only keys with nowhere to live are still skipped; the three
    # remote_* toggles are not among them any more — they are backed by
    # cratebuilder_remote.json (see REMOTE_SETTINGS_KEYS).
    assert "notify_scan_found" not in values
    assert values["remote_enabled"] is False


# ── library ──────────────────────────────────────────────────────────────────

def test_library_stats_without_a_database_creates_nothing(service, tmp_path):
    stats = service.library_stats()
    assert stats["available"] is False
    assert stats["downloads"] == 0
    assert not (tmp_path / "cratebuilder.db").exists()


def test_library_stats_counts_an_existing_database(tmp_path):
    db_path = tmp_path / "cratebuilder.db"
    DownloadsDatabase(str(db_path))
    svc = CrateBuilderService(settings=Settings(path=str(tmp_path / "c.json")),
                              db_path=str(db_path))
    stats = svc.library_stats()
    assert stats["available"] is True
    assert stats["downloads"] == 0


def test_genres_reads_the_crate_tree(service, tmp_path):
    (tmp_path / "crate" / "YouTube" / "Techno").mkdir(parents=True)
    (tmp_path / "crate" / "YouTube" / "_No Genre").mkdir(parents=True)
    assert service.genres() == ["(none)", "Techno"]


def test_genres_survives_a_missing_crate_root(service):
    assert service.genres() == []


# ── snapshot / strings ───────────────────────────────────────────────────────

def test_snapshot_carries_everything_the_shell_needs(service):
    snap = service.snapshot()
    for key in ("app", "host", "counts", "library", "batch", "watchlist",
                "settings", "settings_path", "platform", "genres", "capabilities"):
        assert key in snap


def test_ui_strings_expose_the_shared_registry(service):
    strings = service.ui_strings()
    assert strings["tooltips"]["wl.scan_all"]
    assert any(e["key"] == "skip_existing" for e in strings["settings_keys"])


def test_version_info_parses_the_monolith():
    info = version_info()
    assert info["version"] == "2.0"
    assert isinstance(info["build"], int)


def test_version_info_missing_file_is_not_fatal(tmp_path):
    assert version_info(str(tmp_path / "gone.py")) == {"version": None, "build": None}


# ── job lifecycle: the one event that cannot report a stale `running` ─────────
# A run's own terminal events (batch.finished, the Watch List's closing DONE
# scan line) are emitted from inside the job body, while _start_job still holds
# the category's slot — so a client that resyncs on one of those can be told the
# job is still running and re-arm a run that has already ended. job.finished is
# emitted after the slot is released, which is what makes it safe to resync on.

@pytest.mark.parametrize("category", ["batch", "watchlist"])
def test_job_finished_is_emitted_after_the_slot_is_released(service, category):
    seen = []
    done = threading.Event()

    def on_event(type, payload):
        if type != JOB_FINISHED:
            return
        # Asked from the handler, exactly as a frontend would: both the job
        # registry and a full snapshot must already say the category is free.
        seen.append((payload,
                     service._job_running(category),
                     service.snapshot()["running"][category]))
        done.set()

    service.events.subscribe(on_event)
    service._start_job(category, lambda: None)

    assert done.wait(10), "job.finished was never emitted"
    payload, registry_running, snapshot_running = seen[0]
    assert payload == {"job": category, "ok": True, "error": None}
    assert registry_running is False
    assert snapshot_running is False


def test_an_event_emitted_from_inside_a_run_still_reports_it_as_running(service):
    """The behaviour job.finished exists to work around, pinned so it cannot
    quietly change and leave the client's reasoning stale."""
    inside = []
    done = threading.Event()

    def on_event(type, payload):
        if type == "batch.finished":
            inside.append(service.snapshot()["running"]["batch"])
        elif type == JOB_FINISHED:
            done.set()

    service.events.subscribe(on_event)
    service._start_job(
        "batch", lambda: service.emit("batch.finished", {"cancelled": False}))
    assert done.wait(10)
    assert inside == [True]


def test_a_raising_job_reports_the_failure_rather_than_dying_quietly(service):
    """A run that raises has no terminal event of its own, so job.finished is
    the only place its failure can be told — and a frontend that took it at
    face value would settle a dead job as a success."""
    seen = []
    done = threading.Event()

    def on_event(type, payload):
        if type in (JOB_FINISHED, "notification"):
            seen.append((type, payload))
        if type == JOB_FINISHED:
            done.set()

    def boom():
        raise RuntimeError("the run blew up")

    service.events.subscribe(on_event)
    service._start_job("batch", boom)
    assert done.wait(10)
    assert service._job_running("batch") is False

    types = [t for t, _ in seen]
    assert types == ["notification", JOB_FINISHED], \
        "the failure is announced before the slot is declared free"
    note = seen[0][1]
    assert note["level"] == "error"
    assert note["title"] == "Downloads"
    assert note["body"] == "RuntimeError: the run blew up"
    finished = seen[1][1]
    assert finished["job"] == "batch"
    assert finished["ok"] is False
    assert finished["error"] == "RuntimeError: the run blew up"


def test_a_cberror_from_a_job_is_reported_as_its_own_message(service):
    """CBError messages are written to be read by the user, so they are not
    dressed with an exception type the way an unexpected crash is."""
    seen = []
    done = threading.Event()
    service.events.subscribe(
        lambda t, p: (seen.append((t, p)), done.set()) if t == JOB_FINISHED
        else None)

    def refuse():
        raise CBError("The library folder is on a drive that went away.")

    service._start_job("watchlist", refuse, title="Watch List scan")
    assert done.wait(10)
    assert seen[0][1]["error"] == "The library folder is on a drive that went away."


@pytest.mark.parametrize("category", ["batch", "watchlist", "maintenance"])
def test_job_started_is_emitted_with_the_slot_already_taken(service, category):
    """The mirror of job.finished's guarantee. A frontend resyncing on this
    must not be handed a snapshot that still says nothing is running, or it
    would re-open the controls it just closed."""
    seen = []
    started = threading.Event()

    def on_event(type, payload):
        if type != JOB_STARTED:
            return
        # Asked from the handler, exactly as a frontend would.
        seen.append((payload,
                     service._job_running(category),
                     service.snapshot()["running"][category]))
        started.set()

    service.events.subscribe(on_event)
    hold = threading.Event()
    service._start_job(category, hold.wait)
    try:
        assert started.wait(10), "job.started was never emitted"
    finally:
        hold.set()

    payload, registry_running, snapshot_running = seen[0]
    assert payload["job"] == category
    assert registry_running is True
    assert snapshot_running is True


def test_a_refused_start_announces_nothing(service):
    """A second job in the same category never took a slot, so it must not
    tell the frontend one opened — that would arm controls for a run that
    does not exist."""
    seen = []
    service.events.subscribe(
        lambda t, p: seen.append(t) if t == JOB_STARTED else None)
    hold = threading.Event()
    service._start_job("batch", hold.wait)
    try:
        with pytest.raises(CBError):
            service._start_job("batch", lambda: None)
    finally:
        hold.set()

    assert seen == [JOB_STARTED], "only the start that actually claimed a slot"


def test_a_guard_that_refuses_announces_nothing(service):
    seen = []
    service.events.subscribe(
        lambda t, p: seen.append(t) if t == JOB_STARTED else None)

    def refuse():
        raise CBError("not right now")

    with pytest.raises(CBError):
        service._start_job("batch", lambda: None, guard=refuse)
    assert seen == []


def test_a_job_started_by_the_host_alone_still_announces_itself(service):
    """The whole point: nothing here is a client call. The launch scan and the
    tray's Scan Now reach _start_job directly, and used to leave every open
    frontend's Watch List controls reading idle for the length of the run."""
    seen = []
    done = threading.Event()

    def on_event(type, payload):
        if type in (JOB_STARTED, JOB_FINISHED):
            seen.append((type, payload["job"]))
        if type == JOB_FINISHED:
            done.set()

    service.events.subscribe(on_event)
    service._start_job("watchlist", lambda: None)

    assert done.wait(10)
    assert seen == [(JOB_STARTED, "watchlist"), (JOB_FINISHED, "watchlist")]


def test_a_job_guard_refuses_the_start_before_the_slot_is_taken(service):
    """The guard runs under the job lock, so what it checks cannot change
    between the check and the slot being claimed."""
    def refuse():
        raise CBError("not right now")

    with pytest.raises(CBError, match="not right now"):
        service._start_job("batch", lambda: None, guard=refuse)
    assert service._job_running("batch") is False
    # The slot is untouched, so a start with no guard still works.
    service._start_job("batch", lambda: None)


# ── F9: the isolation guard in tests/conftest.py is structural ──────────────

def test_a_bare_service_lands_in_the_sandbox_not_the_developers_install():
    """Every fixture in the suite passes explicit tmp paths, but nothing
    stopped the next test from writing `CrateBuilderService()` bare — which
    would probe the real cratebuilder.db beside the checkout and, through
    `remote_state`, write the real cratebuilder_remote.json. The autouse
    `_isolate_service_paths` fixture is what makes that impossible; this is
    what proves the fixture is doing it.
    """
    import os

    from cratebuilder import util
    from cratebuilder.service import app_dir, repo_root

    bare = CrateBuilderService()
    sandbox = os.path.realpath(app_dir())
    assert os.path.realpath(bare._db_path).startswith(sandbox)
    assert os.path.realpath(bare._log_path).startswith(sandbox)
    assert os.path.realpath(bare._debug_log_path).startswith(sandbox)
    assert os.path.realpath(bare._links_path).startswith(sandbox)
    assert os.path.realpath(bare._remote_path).startswith(sandbox)
    assert not os.path.realpath(bare._db_path).startswith(
        os.path.realpath(repo_root()) + os.sep)
    # And the two paths Settings() would reach on its own, which are HOME's,
    # not app_dir's — the second seam the fixture closes.
    home = os.path.realpath(os.path.expanduser("~"))
    assert "service_sandbox" in home, "HOME still points at a real profile"
    assert os.path.realpath(util._config_path()).startswith(home)
    assert os.path.realpath(util.default_base_dir()).startswith(home)

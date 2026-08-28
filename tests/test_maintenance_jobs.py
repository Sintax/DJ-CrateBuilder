"""The four database maintenance jobs: rebuild, de-dup, tag repair, artwork.

Everything here runs against a database and a crate tree built under the
test's own tmp_path — no test may reach the developer's real cratebuilder.db
or Music folder, least of all the two jobs that clear and rewrite rows.
"""
import os
import re
import threading

import pytest

from cratebuilder import maintenance
from cratebuilder.db import DownloadsDatabase
from cratebuilder.maintenance import (TASK_DEDUPE, TASK_FETCH_ARTWORK,
                                      TASK_REBUILD, TASK_REPAIR_TAGS,
                                      MaintenanceOps)
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings

# The same minimal silent frame tests/test_genre_tags.py uses — enough for
# mutagen to treat the file as an MP3 and attach a tag, with no encoder.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def make_mp3(path):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    return str(path)


class Recorder:
    """Collects every event a run publishes, in order."""

    def __init__(self):
        self.events = []

    def __call__(self, type, payload):
        self.events.append((type, payload))

    def of(self, type):
        return [p for t, p in self.events if t == type]

    def last(self, type):
        frames = self.of(type)
        return frames[-1] if frames else None


@pytest.fixture
def crate(tmp_path):
    """base/<Platform>/<Genre>/<Channel>/ with two tracks under YouTube."""
    root = tmp_path / "crate"
    make_mp3(root / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track A.mp3")
    make_mp3(root / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track B.mp3")
    make_mp3(root / "SoundCloud" / "House" / "DJ Foo" / "Track C.mp3")
    return root


@pytest.fixture
def settings(tmp_path, crate):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(crate))
    return s


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cratebuilder.db")


@pytest.fixture
def ops(settings, db_path):
    rec = Recorder()
    ops = MaintenanceOps(settings, lambda: DownloadsDatabase(db_path), rec,
                         sleep=lambda _s: None)
    ops.recorder = rec
    return ops


@pytest.fixture
def service(settings, db_path):
    return CrateBuilderService(settings=settings, db_path=db_path)


def seed_download(db, path, **extra):
    row = {"video_id": extra.get("video_id"), "title": os.path.basename(path),
           "channel_name": "DnB Portal", "channel_url": "", "channel_id": None,
           "platform": "YouTube", "genre": "Drum & Bass", "file_path": path,
           "upload_date": "20240101", "ts": 1700000000, "bitrate": "192"}
    row.update({k: v for k, v in extra.items() if k != "video_id"})
    db.backfill_downloads([row])


# ══════════════════════════════════════════════════════════════════════════════
# db.rebuild
# ══════════════════════════════════════════════════════════════════════════════

def test_rebuild_ingests_the_seeded_disk_tree(ops, db_path, crate):
    result = ops.run_rebuild()
    assert result["indexed"] == 3
    db = DownloadsDatabase(db_path)
    paths = {r["file_path"] for r in db.get_all_downloads()}
    assert paths == {
        str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track A.mp3"),
        str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track B.mp3"),
        str(crate / "SoundCloud" / "House" / "DJ Foo" / "Track C.mp3"),
    }


def test_rebuild_reads_the_genre_and_platform_off_the_folder(ops, db_path):
    ops.run_rebuild()
    rows = {r["title"]: r for r in DownloadsDatabase(db_path).get_all_downloads()}
    assert rows["Track A"]["genre"] == "Drum & Bass"
    assert rows["Track A"]["platform"] == "YouTube"
    assert rows["Track C"]["platform"] == "SoundCloud"


def test_rebuild_replaces_rows_that_are_no_longer_on_disk(ops, db_path, crate):
    db = DownloadsDatabase(db_path)
    seed_download(db, str(crate / "YouTube" / "Gone" / "Ghost" / "old.mp3"))
    ops.run_rebuild()
    titles = {r["title"] for r in DownloadsDatabase(db_path).get_all_downloads()}
    assert "old" not in titles
    assert len(titles) == 3


def test_an_empty_scan_leaves_the_table_alone(tmp_path, db_path):
    """An unmounted drive scans as empty — clearing on that result would wipe
    the user's whole history behind a success message."""
    s = Settings(path=str(tmp_path / "c.json"))
    s.set("base_dir", str(tmp_path / "not-here"))
    db = DownloadsDatabase(db_path)
    seed_download(db, str(tmp_path / "kept.mp3"))
    rec = Recorder()
    ops = MaintenanceOps(s, lambda: DownloadsDatabase(db_path), rec)

    result = ops.run_rebuild()

    assert result["indexed"] == 0
    assert len(DownloadsDatabase(db_path).get_all_downloads()) == 1
    assert rec.last("notification")["level"] == "warn"


def test_a_cancelled_rebuild_never_clears_the_table(ops, db_path, crate,
                                                    monkeypatch):
    """The clear only happens once the walk is complete, so cancelling leaves
    the existing rows exactly as they were."""
    db = DownloadsDatabase(db_path)
    seed_download(db, str(crate / "YouTube" / "Old" / "Chan" / "kept.mp3"))
    real = maintenance.rebuild.index_artwork_dir
    monkeypatch.setattr(maintenance.rebuild, "index_artwork_dir",
                        lambda d: (ops.cancel(), real(d))[1])

    result = ops.run_rebuild()

    assert result["cancelled"] is True
    kept = DownloadsDatabase(db_path).get_all_downloads()
    assert [r["title"] for r in kept] == ["kept.mp3"]


def test_a_stale_cancel_never_kills_the_next_run(ops, db_path):
    """Starting a run clears the flag — a cancel left over from the previous
    job must not stop the one the user just asked for."""
    ops.cancel()
    assert ops.run_rebuild()["indexed"] == 3


def test_rebuild_reports_determinate_progress_stamped_maintenance(ops):
    ops.run_rebuild()
    frames = ops.recorder.of("progress.overall")
    assert frames, "expected progress frames"
    assert all(f["job"] == "maintenance" for f in frames)
    assert all(f["task"] == TASK_REBUILD for f in frames)
    # Two channel folders, three tracks: the bar counts folders.
    assert frames[-1]["done"] == frames[-1]["total"] == 2
    assert frames[-1]["percent"] == 100
    assert frames[-1]["found"] == 3


def test_rebuild_names_the_channel_folder_in_flight(ops):
    ops.run_rebuild()
    titles = [f["title"] for f in ops.recorder.of("progress.current")]
    assert "DnB Portal" in titles and "DJ Foo" in titles


# ══════════════════════════════════════════════════════════════════════════════
# db.dedupe
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def duplicated(db_path, crate):
    """Three rows across two files: one file logged twice.

    The unique index is what a fresh database gets and what a de-dup exists to
    restore, so it has to be dropped before the duplicate can be planted — the
    same state an older database is actually in."""
    db = DownloadsDatabase(db_path)
    a = str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track A.mp3")
    b = str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track B.mp3")
    with db._conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_dl_file_path_unique")
        for path, vid in ((a, "aaaaaaaaaaa"), (a, None), (b, "bbbbbbbbbbb")):
            conn.execute(
                "INSERT INTO downloads (video_id, title, channel_name, "
                "platform, genre, file_path, upload_date, download_timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (vid, os.path.basename(path), "DnB Portal", "YouTube",
                 "Drum & Bass", path, "20240101", 1700000000))
    return db


def test_dedupe_counts_before_it_collapses(ops, duplicated):
    preview = ops.preview(TASK_DEDUPE)
    assert preview == {"task": TASK_DEDUPE, "files": 1, "extra": 1}


def test_dedupe_collapses_the_duplicate_rows(ops, duplicated, db_path):
    result = ops.run_dedupe()
    assert result["removed"] == 1
    assert result["groups"] == 1
    rows = DownloadsDatabase(db_path).get_all_downloads()
    assert len(rows) == 2


def test_dedupe_reports_the_tally_as_a_notification(ops, duplicated):
    ops.run_dedupe()
    note = ops.recorder.last("notification")
    assert note["title"] == "Remove Duplicates"
    assert "1 redundant row" in note["body"]
    assert set(note) >= {"level", "title", "body", "at"}


def test_dedupe_preview_refuses_a_clean_database(ops, db_path, crate):
    seed_download(DownloadsDatabase(db_path),
                  str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track A.mp3"))
    with pytest.raises(CBError, match="No duplicate rows"):
        ops.preview(TASK_DEDUPE)


# ══════════════════════════════════════════════════════════════════════════════
# db.repair_tags
# ══════════════════════════════════════════════════════════════════════════════

def test_repair_tags_writes_the_folder_genre_onto_every_track(ops, crate):
    pytest.importorskip("mutagen")
    from mutagen.id3 import ID3

    result = ops.run_repair_tags()

    assert result["done"] == 3
    track = str(crate / "YouTube" / "Drum & Bass" / "DnB Portal" / "Track A.mp3")
    assert ID3(track).getall("TCON")[0].text[0] == "Drum & Bass"


def test_repair_tags_keeps_what_it_wrote_before_a_cancel(ops, crate):
    """The Cancel tooltip's promise: each file is complete the moment it is
    saved, so a cancel midway keeps every tag already written."""
    pytest.importorskip("mutagen")
    from mutagen.id3 import ID3

    seen = []
    real = maintenance.genrefix.repair_track

    def cancel_after_first(path, genre, **kw):
        out = real(path, genre, **kw)
        seen.append(path)
        if len(seen) == 1:
            ops.cancel()
        return out

    maintenance.genrefix.repair_track = cancel_after_first
    try:
        result = ops.run_repair_tags()
    finally:
        maintenance.genrefix.repair_track = real

    assert result["cancelled"] is True
    assert result["done"] == 1
    assert ID3(seen[0]).getall("TCON"), "the repaired tag survives the cancel"
    assert len(seen) == 1, "nothing was processed after the cancel"


def test_repair_tags_counts_a_failing_write_without_stopping(ops):
    pytest.importorskip("mutagen")
    real = maintenance.genrefix.repair_track
    calls = []

    def one_bad_apple(path, genre, **kw):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("locked")
        return real(path, genre, **kw)

    maintenance.genrefix.repair_track = one_bad_apple
    try:
        result = ops.run_repair_tags()
    finally:
        maintenance.genrefix.repair_track = real

    assert result["errors"] == 1
    assert result["done"] == 3


def test_repair_tags_preview_refuses_an_empty_library(tmp_path, db_path):
    s = Settings(path=str(tmp_path / "c.json"))
    s.set("base_dir", str(tmp_path / "empty"))
    ops = MaintenanceOps(s, lambda: DownloadsDatabase(db_path), Recorder())
    with pytest.raises(CBError, match="no audio files"):
        ops.preview(TASK_REPAIR_TAGS)


def test_repair_tags_progress_is_stamped_maintenance(ops):
    ops.run_repair_tags()
    frames = ops.recorder.of("progress.overall")
    assert all(f["job"] == "maintenance" for f in frames)
    assert all(f["task"] == TASK_REPAIR_TAGS for f in frames)
    assert frames[-1]["done"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# db.fetch_artwork
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def artless(ops, db_path, crate):
    """Two rows with no cover art, both files present on disk."""
    db = DownloadsDatabase(db_path)
    folder = crate / "YouTube" / "Drum & Bass" / "DnB Portal"
    for name, vid in (("Track A.mp3", "aaaaaaaaaaa"), ("Track B.mp3", "bbbbbbbbbbb")):
        seed_download(db, str(folder / name), video_id=vid)
    return db


def test_fetch_artwork_walks_every_row_with_no_art(ops, artless, monkeypatch):
    monkeypatch.setattr(maintenance.cb_artwork, "has_cover_any", lambda p: False)
    monkeypatch.setattr(maintenance.cb_artwork, "artwork_available", lambda: True)
    monkeypatch.setattr(maintenance.cb_artwork, "download_thumbnail",
                        lambda url, dest, **kw: False)

    result = ops.run_fetch_artwork()

    assert result["total"] == 2
    assert result["done"] == 2
    assert result["not_found"] == 2


def test_fetch_artwork_repairs_a_row_whose_file_already_has_art(ops, artless,
                                                                monkeypatch):
    monkeypatch.setattr(maintenance.cb_artwork, "has_cover_any", lambda p: True)

    result = ops.run_fetch_artwork()

    assert result["repaired"] == 2
    assert result["embedded"] == 0


def test_fetch_artwork_skips_the_track_in_flight_on_request(ops, artless,
                                                            monkeypatch):
    """Skip abandons the row being fetched and moves straight on — the next
    row starts with a cleared flag, so only one row is ever skipped."""
    monkeypatch.setattr(maintenance.cb_artwork, "has_cover_any", lambda p: False)

    seen = []

    def slow_fetch(url, dest, **kw):
        seen.append(url)
        if len(seen) == 1:
            ops.skip()      # the modal's Skip button, mid-download
            return False
        return False

    monkeypatch.setattr(maintenance.cb_artwork, "download_thumbnail", slow_fetch)

    result = ops.run_fetch_artwork()

    assert result["skipped"] == 1
    assert result["done"] == 2, "the run carried on to the next row"
    assert result["not_found"] == 1


def test_fetch_artwork_counts_a_row_whose_file_has_gone(ops, db_path, crate,
                                                        monkeypatch):
    seed_download(DownloadsDatabase(db_path), str(crate / "vanished.mp3"))
    monkeypatch.setattr(maintenance.cb_artwork, "has_cover_any", lambda p: False)

    result = ops.run_fetch_artwork()

    assert result["missing"] == 1


def test_fetch_artwork_preview_refuses_when_cover_art_is_off(ops, artless,
                                                              settings):
    settings.set("cover_art_enabled", False)
    with pytest.raises(CBError, match="Cover Art is switched off"):
        ops.preview(TASK_FETCH_ARTWORK)


def test_fetch_artwork_preview_refuses_a_fully_covered_library(ops, db_path,
                                                               monkeypatch):
    monkeypatch.setattr(maintenance.cb_artwork, "artwork_available", lambda: True)
    DownloadsDatabase(db_path)
    with pytest.raises(CBError, match="already has cover art"):
        ops.preview(TASK_FETCH_ARTWORK)


def test_fetch_artwork_cancel_stops_after_the_row_in_flight(ops, artless,
                                                            monkeypatch):
    monkeypatch.setattr(maintenance.cb_artwork, "has_cover_any", lambda p: False)
    calls = []

    def cancel_on_first(url, dest, **kw):
        calls.append(url)
        ops.cancel()
        return False

    monkeypatch.setattr(maintenance.cb_artwork, "download_thumbnail",
                        cancel_on_first)

    result = ops.run_fetch_artwork()

    assert result["cancelled"] is True
    assert result["done"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# The service surface: one job at a time, and the download guard
# ══════════════════════════════════════════════════════════════════════════════

def test_every_maintenance_method_is_dispatchable(service):
    methods = service._methods()
    for name in ("db.rebuild", "db.dedupe", "db.repair_tags",
                 "db.fetch_artwork", "db.maintenance_preview",
                 "db.maintenance_cancel", "db.maintenance_skip"):
        assert name in methods


def test_a_second_maintenance_job_is_refused(service):
    gate = threading.Event()
    service._maintenance_ops = _StubOps(gate)
    service.call("db.rebuild")
    try:
        with pytest.raises(CBError, match="already running"):
            service.call("db.repair_tags")
    finally:
        gate.set()


def test_the_running_flag_reaches_the_snapshot(service):
    gate = threading.Event()
    service._maintenance_ops = _StubOps(gate)
    assert service.snapshot()["running"]["maintenance"] is False
    service.call("db.rebuild")
    try:
        running = service.snapshot()["running"]
        assert running["maintenance"] is True
        # Which job, so a frontend that reloaded mid-run can reopen the right
        # progress dialog instead of guessing.
        assert running["maintenance_task"] == "db.rebuild"
    finally:
        gate.set()


def test_the_snapshot_names_no_task_before_anything_has_run(service):
    running = service.snapshot()["running"]
    assert running["maintenance"] is False
    assert running["maintenance_task"] is None


@pytest.mark.parametrize("task", ["db.rebuild", "db.dedupe", "db.fetch_artwork"])
def test_the_table_rewriting_jobs_wait_for_a_download(service, task):
    """_set_download_lock disables these three for the length of any batch or
    Watch List download; the same three are refused here."""
    gate = threading.Event()
    service._start_job("batch", gate.wait)
    try:
        with pytest.raises(CBError, match="A download is running"):
            service.call(task)
    finally:
        gate.set()


def test_repair_tags_is_allowed_while_a_download_runs(service, crate):
    """The monolith deliberately leaves Repair Track Tags out of the download
    lock — it rewrites tags inside files and never touches a row."""
    gate = threading.Event()
    service._start_job("batch", gate.wait)
    try:
        assert service.maintenance_preview("db.repair_tags")["total"] == 3
    finally:
        gate.set()


def test_a_watchlist_run_blocks_the_rewriting_jobs_too(service):
    gate = threading.Event()
    service._start_job("watchlist", gate.wait)
    try:
        with pytest.raises(CBError, match="A download is running"):
            service.call("db.rebuild")
    finally:
        gate.set()


def test_an_unknown_task_is_refused_by_name(service):
    with pytest.raises(CBError, match="Unknown maintenance job"):
        service.maintenance_start("db.reformat_everything")
    with pytest.raises(CBError, match="Unknown maintenance job"):
        service.call("db.maintenance_preview", {"task": "db.nope"})


def test_cancel_is_safe_when_nothing_is_running(service):
    assert service.call("db.maintenance_cancel")["cancelled"] is True
    assert service.call("db.maintenance_skip")["skipped"] is True


def test_job_finished_names_the_maintenance_category(service, crate):
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    done = threading.Event()
    service.events.subscribe(
        lambda t, p: done.set() if t == "job.finished" else None)

    service.call("db.repair_tags")
    assert done.wait(20), "the job never announced itself finished"

    finished = [p for t, p in seen if t == "job.finished"]
    assert finished[-1] == {"job": "maintenance"}
    assert service.snapshot()["running"]["maintenance"] is False


# ══════════════════════════════════════════════════════════════════════════════
# web/app.js — the confirm copy and the long-job dialog's paint
# ══════════════════════════════════════════════════════════════════════════════

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _slice(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


def _run_node(tmp_path, name, source):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


def test_every_task_quotes_a_tooltip_the_registry_actually_has(app_js):
    """The confirm dialog's help text is the registry's, never a second copy
    written here — a key that has drifted would silently show nothing."""
    from cratebuilder import ui_strings
    keys = re.findall(r"^\s+tt: '([^']+)',", app_js, re.M)
    assert len(keys) == 4
    for key in keys:
        assert key in ui_strings.TOOLTIPS, key


def test_the_four_buttons_name_the_four_service_methods(app_js, service):
    tasks = re.findall(r"^    '(db\.[a-z_]+)': \{$", app_js, re.M)
    assert set(tasks) == {"db.rebuild", "db.dedupe", "db.repair_tags",
                          "db.fetch_artwork"}
    methods = service._methods()
    for task in tasks:
        assert task in methods


def test_confirm_copy_quotes_the_hosts_counts(app_js, tmp_path):
    """Every number in a confirm dialog comes from the preview the host just
    measured, pluralised on that same number."""
    result = _run_node(tmp_path, "maintcopy.mjs", "\n".join([
        "const num = (n) => Number(n || 0).toLocaleString();",
        "const TOOLTIPS = {};",
        _slice(app_js, "  const MAINT_TASKS = {", "  const MAINT_BUSY_REASON"),
        """
console.log(JSON.stringify({
  rebuild: MAINT_TASKS['db.rebuild'].confirm({ rows: 1204 }).join(' '),
  rebuild_one: MAINT_TASKS['db.rebuild'].confirm({ rows: 1 }).join(' '),
  dedupe: MAINT_TASKS['db.dedupe'].confirm({ files: 3, extra: 4 }).join(' '),
  dedupe_one: MAINT_TASKS['db.dedupe'].confirm({ files: 1, extra: 1 }).join(' '),
  tags: MAINT_TASKS['db.repair_tags'].confirm(
    { total: 12, no_genre_dir: '_No Genre' }).join(' '),
  art: MAINT_TASKS['db.fetch_artwork'].confirm({ total: 2 }).join(' '),
  art_one: MAINT_TASKS['db.fetch_artwork'].confirm({ total: 1 }).join(' '),
}));
""",
    ]))
    assert "1,204 rows it holds now are replaced" in result["rebuild"]
    assert "1 row it holds now is replaced" in result["rebuild_one"]
    assert "4 redundant rows across 3 files" in result["dedupe"]
    assert "1 redundant row across 1 file" in result["dedupe_one"]
    assert "Repair the tags on 12 tracks?" in result["tags"]
    assert "'_No Genre' have theirs cleared" in result["tags"]
    assert "2 tracks have no cover art" in result["art"]
    assert "1 track has no cover art" in result["art_one"]


def test_the_dialog_paints_the_bar_and_counts_from_the_hosts_frames(app_js,
                                                                    tmp_path):
    """The bar is determinate and clamped: a percent the host never sends, or
    one outside 0–100, must not paint a bar wider than the dialog."""
    result = _run_node(tmp_path, "maintpaint.mjs", "\n".join([
        "const num = (n) => Number(n || 0).toLocaleString();",
        "const TOOLTIPS = {};",
        _slice(app_js, "  const MAINT_TASKS = {", "  const MAINT_BUSY_REASON"),
        "const mt = { view: null, overall: null, current: null, note: null };",
        _slice(app_js, "  function maintPaint()", "  /* The host released"),
        """
function refs() {
  const cell = () => ({ textContent: null });
  return { fill: { style: {} }, item: cell(), counts: cell(), tally: cell() };
}
function paint(task, overall, current, note) {
  const r = refs();
  mt.view = { task, refs: r };
  mt.overall = overall; mt.current = current; mt.note = note;
  maintPaint();
  return { width: r.fill.style.width, item: r.item.textContent,
           counts: r.counts.textContent, tally: r.tally.textContent };
}
console.log(JSON.stringify({
  start: paint('db.repair_tags', null, null, null),
  mid: paint('db.repair_tags',
             { done: 704, total: 1204, percent: 58, genres: 312, filled: 9 },
             { title: 'Fracture Point.mp3', note: 'Drum & Bass' }, null),
  one: paint('db.rebuild', { done: 1, total: 1, percent: 100, found: 3 },
             { title: 'DnB Portal' }, null),
  over: paint('db.rebuild', { done: 9, total: 2, percent: 450 }, null, null),
  done: paint('db.fetch_artwork',
              { done: 2, total: 2, percent: 100, embedded: 2 },
              { title: 'Track B' }, { body: '2 embedded, 0 none found.' }),
}));
""",
    ]))
    assert result["start"] == {"width": "0%", "item": None,
                              "counts": "Starting…", "tally": ""}
    assert result["mid"]["width"] == "58%"
    assert result["mid"]["item"] == "Fracture Point.mp3 — Drum & Bass"
    assert result["mid"]["counts"] == "704 of 1,204 tracks"
    assert result["mid"]["tally"] == "312 genres • 9 filled in"
    assert result["one"]["counts"] == "1 of 1 channel folder"
    assert result["over"]["width"] == "100%", "a bad percent is clamped"
    # Once the summary lands it takes over the item line — the last track's
    # name would otherwise stand as if it were still being worked on.
    assert result["done"]["item"] == "2 embedded, 0 none found."


class _StubOps:
    """A MaintenanceOps stand-in whose every run blocks until released."""

    task = "db.rebuild"

    def __init__(self, gate):
        self._gate = gate

    def preview(self, task):
        return {"task": task}

    def _blocking(self):
        self._gate.wait()

    run_rebuild = run_dedupe = run_repair_tags = run_fetch_artwork = _blocking

    def cancel(self):
        return {"cancelled": True, "task": None}

    def skip(self):
        return {"skipped": True, "task": None}

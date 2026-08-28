"""CrateBuilderService: the Database viewer's db.groups/db.query/db.export_csv/
db.artwork_preview/fs.reveal surface — contract-id mapping, watch-list folder +
cleanup eligibility, artwork on_disk derivation, and the off-DB-lock rules for
CSV export and artwork preview."""
import os
import tempfile

import pytest

import cratebuilder.service as service_module
from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings

# Same minimal silent MP3 frame test_artwork.py uses — enough for mutagen to
# attach and read back an ID3 tag.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path):
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    return str(path)


@pytest.fixture
def dbsvc(tmp_path):
    """(service, db) backed by a REAL on-disk database — unlike
    test_service.py's `service` fixture, whose db_path never gets a file, so
    _db() would return None for every one of these tests."""
    db_path = tmp_path / "cratebuilder.db"
    db = DownloadsDatabase(str(db_path))
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    svc = CrateBuilderService(settings=settings, db_path=str(db_path))
    return svc, db


def _add_download(db, **kw):
    kw.setdefault("channel_url", f"https://yt/{kw.get('channel_name', 'c')}")
    kw.setdefault("platform", "YouTube")
    kw.setdefault("genre", "House")
    kw.setdefault("upload_date", "20260101")
    kw.setdefault("bitrate", "320 kbps MP3")
    kw.setdefault("file_path", f"/x/{kw.get('video_id', kw.get('title'))}.mp3")
    db.add_download(**kw)


# ── db.groups ────────────────────────────────────────────────────────────────

def test_db_groups_no_database_reports_unavailable(dbsvc):
    svc, _db = dbsvc
    svc._db_path = "does/not/exist.db"
    assert svc.db_groups(None, {}) == {"available": False, "groups": []}


def test_db_groups_unknown_preset_raises(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.db_groups("Not A Real Preset", {})


def test_db_groups_drills_down(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1", platform="YouTube")
    _add_download(db, video_id="b", title="B", channel_name="C2", platform="SoundCloud")

    top = svc.db_groups("Platform › Channel", {})
    assert top["available"] is True
    assert "levels" not in top     # the client derives depth from the preset
    assert {g["key"] for g in top["groups"]} == {"YouTube", "SoundCloud"}

    second = svc.db_groups("Platform › Channel", {"platform": "YouTube"})
    assert {g["key"]: g["count"] for g in second["groups"]} == {"C1": 1}


# ── db.query: downloads ──────────────────────────────────────────────────────

def test_db_query_downloads_maps_contract_ids(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="Sundown Terrace", channel_name="Deep House",
                  genre="House", platform="YouTube", upload_date="20260519",
                  bitrate="320 kbps MP3", file_path="/x/a.mp3")
    with db._conn() as conn:
        conn.execute("UPDATE downloads SET download_timestamp = ? WHERE video_id = 'a'",
                     (1700000000,))

    res = svc.db_query("downloads", {}, {}, 0, 50)
    assert res["total"] == 1
    row = res["rows"][0]
    assert row["title"] == "Sundown Terrace"
    assert row["channel"] == "Deep House"
    assert row["genre"] == "House"
    assert row["platform"] == "YouTube"
    assert row["upload"] == "20260519"
    assert row["bitrate"] == "320 kbps MP3"
    assert row["file_path"] == "/x/a.mp3"
    assert row["downloaded"]        # formatted "%Y-%m-%d %H:%M", non-empty


def test_db_query_downloads_blanks_normalise_to_none_and_unknown(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1",
                  genre="", platform="")
    row = svc.db_query("downloads", {}, {}, 0, 50)["rows"][0]
    assert row["genre"] == "(none)"
    assert row["platform"] == "(unknown)"


def test_db_query_downloads_page_size_caps_the_payload(dbsvc):
    svc, db = dbsvc
    for i in range(25):
        _add_download(db, video_id=f"v{i}", title=f"T{i}", channel_name="C1")
    res = svc.db_query("downloads", {}, {}, 0, 10)
    assert res["total"] == 25
    assert len(res["rows"]) == 10


def test_db_query_downloads_sort_col_translates_to_raw_column(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="Zebra")
    _add_download(db, video_id="b", title="B", channel_name="Amber")
    res = svc.db_query("downloads", {}, {"col": "channel", "desc": False}, 0, 50)
    assert [r["channel"] for r in res["rows"]] == ["Amber", "Zebra"]


def test_db_query_clamps_an_oversized_limit_server_side(dbsvc, monkeypatch):
    """The "never one payload" property has to be the server's, not a
    convention the client happens to follow."""
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1")
    seen = {}
    real_query = db.query_downloads

    def spy(*a, **k):
        seen["limit"] = k.get("limit")
        return real_query(*a, **k)

    monkeypatch.setattr(db, "query_downloads", spy)
    monkeypatch.setattr(svc, "_db", lambda: db)
    svc.db_query("downloads", {}, {}, 0, 1_000_000)
    assert seen["limit"] == service_module.MAX_DB_PAGE_SIZE


def test_db_query_unmapped_sort_column_raises_instead_of_defaulting(dbsvc):
    """A header that renders its own sort arrow while the query ordered by
    something else is a lie the user can't see through."""
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1")
    with pytest.raises(CBError):
        svc.db_query("artwork", {}, {"col": "on_disk"}, 0, 50)
    with pytest.raises(CBError):
        svc.db_query("downloads", {}, {"col": "not_a_column"}, 0, 50)
    with pytest.raises(CBError):
        svc.db_query("watchlist", {}, {"col": "not_a_column"}, 0, 50)
    # a blank col still means "the table's default", not an error
    assert svc.db_query("downloads", {}, {"col": ""}, 0, 50)["total"] == 1


def test_db_query_want_total_false_skips_the_count(dbsvc, monkeypatch):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1")
    monkeypatch.setattr(svc, "_db", lambda: db)
    monkeypatch.setattr(db, "count_artwork_rows",
                        lambda *a, **k: pytest.fail("counted on a load-more"))
    res = svc.db_query("artwork", {}, {}, 0, 50, want_total=False)
    assert res["total"] is None
    assert len(res["rows"]) == 1


def test_db_query_unknown_table_raises(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.db_query("not_a_table", {}, {}, 0, 50)


def test_db_query_bad_group_key_raises_not_swallowed(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1")
    with pytest.raises(CBError):
        svc.db_query("downloads", {"group_key": "video_id", "group_value": "a"},
                     {}, 0, 50)


# ── db.query: watchlist ──────────────────────────────────────────────────────

def test_db_query_watchlist_eligible_row_has_no_reason(dbsvc):
    svc, db = dbsvc
    cid = db.add_watchlist_channel(
        url="https://youtube.com/@chan", display_name="Deep House Daily",
        platform="YouTube", genre="House", channel_id="UCxyz")
    folder = svc._channel_folder("YouTube", "House", "Deep House Daily")
    os.makedirs(folder)
    with open(os.path.join(folder, "track.mp3"), "wb") as fh:
        fh.write(b"x")

    row = next(r for r in svc.db_query("watchlist", {}, {}, 0, 50)["rows"]
              if r["id"] == cid)
    assert row["eligible"] is True
    assert row["ineligible_reason"] == ""
    assert row["folder"] == folder
    assert row["channel"] == "Deep House Daily"


def test_db_query_watchlist_unresolved_channel_is_ineligible(dbsvc):
    svc, db = dbsvc
    db.add_watchlist_channel(
        url="unresolved://needs-a-real-link", display_name="Garage Archive",
        platform="YouTube", genre="UK Garage")
    row = svc.db_query("watchlist", {}, {}, 0, 50)["rows"][0]
    assert row["eligible"] is False
    assert "resolved" in row["ineligible_reason"].lower()
    assert row["link"] == ""      # sentinel blanked


def test_db_query_watchlist_downloading_channel_is_ineligible(dbsvc):
    svc, db = dbsvc
    cid = db.add_watchlist_channel(
        url="https://youtube.com/@chan", display_name="Neon Bass Radio",
        platform="YouTube", genre="Drum n Bass", channel_id="UCabc",
        status="downloading")
    row = next(r for r in svc.db_query("watchlist", {}, {}, 0, 50)["rows"]
              if r["id"] == cid)
    assert row["eligible"] is False
    assert "downloading" in row["ineligible_reason"].lower()


def test_db_query_watchlist_missing_folder_is_ineligible(dbsvc):
    svc, db = dbsvc
    db.add_watchlist_channel(
        url="https://youtube.com/@chan", display_name="Never Downloaded",
        platform="YouTube", genre="House", channel_id="UCdef")
    row = svc.db_query("watchlist", {}, {}, 0, 50)["rows"][0]
    assert row["eligible"] is False
    assert "folder" in row["ineligible_reason"].lower()


def test_db_query_watchlist_search_filters_by_channel_name(dbsvc):
    svc, db = dbsvc
    db.add_watchlist_channel(url="https://a", display_name="Deep House Daily",
                             platform="YouTube", genre="House")
    db.add_watchlist_channel(url="https://b", display_name="Techno Warehouse",
                             platform="YouTube", genre="Techno")
    res = svc.db_query("watchlist", {"search": "deep"}, {}, 0, 50)
    assert [r["channel"] for r in res["rows"]] == ["Deep House Daily"]


def test_db_query_watchlist_sort_by_pending(dbsvc):
    svc, db = dbsvc
    a = db.add_watchlist_channel(url="https://a", display_name="A",
                                 platform="YouTube", genre="House")
    b = db.add_watchlist_channel(url="https://b", display_name="B",
                                 platform="YouTube", genre="House")
    db.update_watchlist_scan_result(a, timestamp=1, pending_count=1,
                                    pending_entries=[], status="idle")
    db.update_watchlist_scan_result(b, timestamp=1, pending_count=9,
                                    pending_entries=[], status="idle")
    res = svc.db_query("watchlist", {}, {"col": "pending", "desc": True}, 0, 50)
    assert [r["channel"] for r in res["rows"]] == ["B", "A"]


# ── db.query: artwork ─────────────────────────────────────────────────────────

def test_db_query_artwork_on_disk_states(dbsvc, tmp_path):
    svc, db = dbsvc
    on_disk_jpg = tmp_path / "ok.jpg"
    on_disk_jpg.write_bytes(b"fake-jpeg")

    _add_download(db, video_id="a", title="On Disk", channel_name="C",
                  artwork_path=str(on_disk_jpg))
    _add_download(db, video_id="b", title="Broken", channel_name="C",
                  artwork_path=str(tmp_path / "gone.jpg"))
    _add_download(db, video_id="c", title="No Art", channel_name="C")

    rows = {r["title"]: r for r in
            svc.db_query("artwork", {"filter_name": "All tracks"}, {}, 0, 50)["rows"]}
    assert rows["On Disk"]["on_disk"] is True
    assert rows["Broken"]["on_disk"] is False
    assert rows["No Art"]["on_disk"] is None


def test_db_query_artwork_defaults_to_all_tracks_filter(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C")
    res = svc.db_query("artwork", {}, {}, 0, 50)
    assert res["total"] == 1


# ── db.export_csv ─────────────────────────────────────────────────────────────

def test_db_export_csv_downloads_returns_csv_text_and_writes_no_file(dbsvc, tmp_path):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A Track", channel_name="Chan",
                  file_path="/x/a.mp3")
    before = set(os.listdir(tempfile.gettempdir()))
    result = svc.db_export_csv("downloads", {}, {})
    assert result["rows"] == 1
    assert "path" not in result            # nothing is left on the host's disk
    assert result["filename"] == "cratebuilder_downloads.csv"
    assert "A Track" in result["csv"]
    assert "Title,Channel,Genre,Platform" in result["csv"]
    leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
              if n.startswith("cratebuilder_")]
    assert leaked == []


def test_db_export_csv_watchlist_and_artwork_tables_work(dbsvc):
    svc, db = dbsvc
    db.add_watchlist_channel(url="https://a", display_name="Chan A",
                             platform="YouTube", genre="House")
    wl = svc.db_export_csv("watchlist", {}, {})
    assert "Chan A" in wl["csv"]

    _add_download(db, video_id="a", title="A", channel_name="Chan",
                  artwork_embedded=1)
    art = svc.db_export_csv("artwork", {}, {})
    assert "Yes" in art["csv"]   # embedded formatted as Yes/No, not True/False


def test_db_export_csv_unknown_table_raises(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.db_export_csv("not_a_table", {}, {})


def test_db_export_csv_builds_the_text_after_the_db_lock_is_released(dbsvc, monkeypatch):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="Chan")
    monkeypatch.setattr(svc, "_db", lambda: db)

    lock_states = []
    import cratebuilder.service as service_module
    real_writer = service_module.csv.writer

    def spy_writer(*a, **k):
        lock_states.append(db._lock.locked())
        return real_writer(*a, **k)

    monkeypatch.setattr(service_module.csv, "writer", spy_writer)
    svc.db_export_csv("downloads", {}, {})
    assert lock_states == [False]


def test_db_export_csv_neutralises_formula_cells(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="=cmd|'/c calc'!A1",
                  channel_name="@SUM(1)")
    csv_text = svc.db_export_csv("downloads", {}, {})["csv"]
    assert "'=cmd|'" in csv_text          # leading ' added, so Excel sees text
    assert "'@SUM(1)" in csv_text
    assert not csv_text.splitlines()[1].startswith("=")


def test_db_export_csv_artwork_skips_the_on_disk_stat(dbsvc, monkeypatch, tmp_path):
    """The artwork export has no On Disk column, so it must not stat a row."""
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="Chan",
                  artwork_path=str(tmp_path / "cover.jpg"))
    import cratebuilder.service as service_module
    stats = []
    real_isfile = service_module.os.path.isfile
    monkeypatch.setattr(service_module.os.path, "isfile",
                        lambda p: (stats.append(p), real_isfile(p))[1])
    svc.db_export_csv("artwork", {}, {})
    assert str(tmp_path / "cover.jpg") not in stats


# ── db.artwork_preview ───────────────────────────────────────────────────────

def test_db_artwork_preview_reads_the_rows_sidecar_file(dbsvc, tmp_path):
    svc, db = dbsvc
    jpg = tmp_path / "cover.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    _add_download(db, video_id="a", title="A", channel_name="C",
                  artwork_path=str(jpg))
    row_id = svc.db_query("artwork", {}, {}, 0, 5)["rows"][0]["id"]

    result = svc.db_artwork_preview(row_id)
    assert result["data_url"].startswith("data:image/jpeg;base64,")
    assert result["size"] == jpg.stat().st_size


def test_db_artwork_preview_unknown_row_is_empty(dbsvc):
    svc, _db = dbsvc
    assert svc.db_artwork_preview(None) == {"data_url": None}
    assert svc.db_artwork_preview(4242) == {"data_url": None}
    assert svc.db_artwork_preview("not-an-id") == {"data_url": None}


def test_db_artwork_preview_row_with_nothing_to_show_is_empty(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C")
    row_id = svc.db_query("artwork", {}, {}, 0, 5)["rows"][0]["id"]
    assert svc.db_artwork_preview(row_id) == {"data_url": None}


def test_db_artwork_preview_falls_back_to_embedded_cover(dbsvc, tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("mutagen")
    from PIL import Image
    from cratebuilder import artwork

    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = tmp_path / "cover.jpg"
    Image.new("RGB", (64, 64), (10, 120, 200)).save(str(jpg))
    artwork.embed_cover(mp3, str(jpg))

    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C", file_path=mp3)
    row_id = svc.db_query("artwork", {}, {}, 0, 5)["rows"][0]["id"]

    result = svc.db_artwork_preview(row_id)   # no sidecar path at all
    assert result["data_url"].startswith("data:image/jpeg;base64,")
    assert result["width"] == 64 and result["height"] == 64
    assert "embedded artwork" in result["note"]


def test_db_artwork_preview_refuses_an_oversized_file(dbsvc, tmp_path, monkeypatch):
    svc, db = dbsvc
    monkeypatch.setattr(service_module, "MAX_PREVIEW_BYTES", 64)
    big = tmp_path / "huge.jpg"
    big.write_bytes(b"\xff\xd8" + b"x" * 500)
    _add_download(db, video_id="a", title="A", channel_name="C",
                  artwork_path=str(big))
    row_id = svc.db_query("artwork", {}, {}, 0, 5)["rows"][0]["id"]

    result = svc.db_artwork_preview(row_id)
    assert result["data_url"] is None
    assert "too large" in result["note"].lower()


# ── db.artwork_preview: no client-supplied path reaches open() ───────────────

def test_db_artwork_preview_cannot_read_an_out_of_tree_file(dbsvc, tmp_path):
    """The pre-fix signature took a path; a caller could name any file at
    all. Keyed by row id, a path is not even expressible."""
    svc, _db = dbsvc
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SENSITIVE-NOT-AN-IMAGE")
    assert svc.db_artwork_preview(str(secret)) == {"data_url": None}


def test_db_artwork_preview_on_remote_cannot_read_an_arbitrary_file(tmp_path):
    secret = tmp_path / "config.json"
    secret.write_bytes(b"SENSITIVE-NOT-AN-IMAGE")
    db_path = tmp_path / "cratebuilder.db"
    DownloadsDatabase(str(db_path))
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(db_path))

    for params in ({"path": str(secret)}, {"id": str(secret)},
                   {"id": str(secret), "file_path": str(secret)}):
        assert remote.call("db.artwork_preview", params) == {"data_url": None}


# ── fs.reveal ─────────────────────────────────────────────────────────────────

def _crate_file(svc, name, body=b"x"):
    """A real file inside the service's own crate folder."""
    base = svc._settings.get("base_dir")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, name)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def test_fs_reveal_is_local_only(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    with pytest.raises(CBError):
        remote.call("fs.reveal", {"path": str(tmp_path)})


def test_fs_reveal_rejects_blank_path(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.fs_reveal("", "folder")


def test_fs_reveal_open_mode_uses_os_open(dbsvc, monkeypatch):
    svc, _db = dbsvc
    target = _crate_file(svc, "track.mp3")
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    result = svc.fs_reveal(target, "open")
    assert result == {"opened": True}
    assert calls == [target]


def test_fs_reveal_open_mode_refuses_an_executable(dbsvc, monkeypatch):
    """mode="open" is ShellExecute — on an .exe that is "run this program"."""
    svc, _db = dbsvc
    payload = _crate_file(svc, "payload.exe")
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    for name in ("payload.exe", "payload.bat", "payload.lnk", "payload.ps1"):
        target = _crate_file(svc, name)
        with pytest.raises(CBError):
            svc.fs_reveal(target, "open")
    assert calls == []
    assert os.path.isfile(payload)          # nothing was launched or removed


def test_fs_reveal_refuses_a_path_the_library_does_not_own(dbsvc, tmp_path,
                                                           monkeypatch):
    svc, _db = dbsvc
    outsider = tmp_path / "elsewhere" / "track.mp3"
    outsider.parent.mkdir()
    outsider.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    with pytest.raises(CBError):
        svc.fs_reveal(str(outsider), "open")
    with pytest.raises(CBError):
        svc.fs_reveal(str(outsider), "folder")
    assert calls == []


def test_fs_reveal_allows_a_recorded_row_outside_todays_crate_folder(
        dbsvc, tmp_path, monkeypatch):
    """base_dir is a setting the user can change; rows written before the
    move must still open."""
    svc, db = dbsvc
    old_crate = tmp_path / "old-crate"
    old_crate.mkdir()
    track = old_crate / "track.mp3"
    track.write_bytes(b"x")
    _add_download(db, video_id="a", title="A", channel_name="C",
                  file_path=str(track))
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    assert svc.fs_reveal(str(track), "open") == {"opened": True}
    assert calls == [str(track)]


def test_fs_reveal_open_mode_missing_file_raises(dbsvc):
    svc, _db = dbsvc
    base = svc._settings.get("base_dir")
    os.makedirs(base, exist_ok=True)
    with pytest.raises(CBError):
        svc.fs_reveal(os.path.join(base, "gone.mp3"), "open")


def test_fs_reveal_folder_mode_falls_back_to_nearest_existing_dir(
        dbsvc, monkeypatch):
    svc, _db = dbsvc
    base = svc._settings.get("base_dir")
    os.makedirs(base, exist_ok=True)
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    # win32's explorer /select branch only fires for an existing path;
    # a nonexistent one falls through to the directory-walk-up branch.
    missing = os.path.join(base, "ghost", "track.mp3")
    result = svc.fs_reveal(missing, "folder")
    assert result == {"opened": True}
    assert calls == [base]


def test_fs_reveal_folder_mode_selects_in_explorer_on_windows(dbsvc, monkeypatch):
    """The explorer /select, branch: a three-token argv LIST, never a
    shell string — metacharacters in a track's path must stay inert."""
    svc, _db = dbsvc
    target = _crate_file(svc, "a & b.mp3")
    monkeypatch.setattr(service_module.sys, "platform", "win32")
    seen = {}

    def fake_popen(args, *a, **k):
        seen["args"] = args
        seen["kwargs"] = k
        return None

    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(svc, "_os_open",
                        lambda p: pytest.fail("should have used explorer"))
    assert svc.fs_reveal(target, "folder") == {"opened": True}
    assert isinstance(seen["args"], list)
    assert seen["args"] == ["explorer", "/select,", os.path.normpath(target)]
    assert "shell" not in seen["kwargs"]

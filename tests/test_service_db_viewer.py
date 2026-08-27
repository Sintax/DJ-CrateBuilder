"""CrateBuilderService: the Database viewer's db.groups/db.query/db.export_csv/
db.artwork_preview/fs.reveal surface — contract-id mapping, watch-list folder +
cleanup eligibility, artwork on_disk derivation, and the off-DB-lock rules for
CSV export and artwork preview."""
import os

import pytest

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
    assert svc.db_groups(None, {}) == {"available": False, "groups": [], "levels": 0}


def test_db_groups_unknown_preset_raises(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.db_groups("Not A Real Preset", {})


def test_db_groups_reports_the_preset_depth_and_drills_down(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="C1", platform="YouTube")
    _add_download(db, video_id="b", title="B", channel_name="C2", platform="SoundCloud")

    top = svc.db_groups("Platform › Channel", {})
    assert top["available"] is True
    assert top["levels"] == 2
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

def test_db_export_csv_downloads_writes_a_real_file(dbsvc):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A Track", channel_name="Chan",
                  file_path="/x/a.mp3")
    result = svc.db_export_csv("downloads", {}, {})
    assert result["rows"] == 1
    assert os.path.isfile(result["path"])
    with open(result["path"], encoding="utf-8", newline="") as fh:
        on_disk_text = fh.read()
    assert on_disk_text == result["csv"]
    assert "A Track" in result["csv"]
    assert "Title,Channel,Genre,Platform" in result["csv"]
    os.remove(result["path"])


def test_db_export_csv_watchlist_and_artwork_tables_work(dbsvc):
    svc, db = dbsvc
    db.add_watchlist_channel(url="https://a", display_name="Chan A",
                             platform="YouTube", genre="House")
    wl = svc.db_export_csv("watchlist", {}, {})
    assert "Chan A" in wl["csv"]
    os.remove(wl["path"])

    _add_download(db, video_id="a", title="A", channel_name="Chan",
                  artwork_embedded=1)
    art = svc.db_export_csv("artwork", {}, {})
    assert "Yes" in art["csv"]   # embedded formatted as Yes/No, not True/False
    os.remove(art["path"])


def test_db_export_csv_unknown_table_raises(dbsvc):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.db_export_csv("not_a_table", {}, {})


def test_db_export_csv_writes_after_the_db_lock_is_released(dbsvc, monkeypatch):
    svc, db = dbsvc
    _add_download(db, video_id="a", title="A", channel_name="Chan")
    monkeypatch.setattr(svc, "_db", lambda: db)

    lock_states = []
    import cratebuilder.service as service_module
    real_tmp = service_module.tempfile.NamedTemporaryFile

    def spy_tmp(*a, **k):
        lock_states.append(db._lock.locked())
        return real_tmp(*a, **k)

    monkeypatch.setattr(service_module.tempfile, "NamedTemporaryFile", spy_tmp)
    result = svc.db_export_csv("downloads", {}, {})
    assert lock_states == [False]
    os.remove(result["path"])


# ── db.artwork_preview ───────────────────────────────────────────────────────

def test_db_artwork_preview_reads_a_sidecar_file(dbsvc, tmp_path):
    svc, _db = dbsvc
    jpg = tmp_path / "cover.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    result = svc.db_artwork_preview(str(jpg), None)
    assert result["data_url"].startswith("data:image/jpeg;base64,")
    assert result["size"] == jpg.stat().st_size


def test_db_artwork_preview_missing_everything_is_empty(dbsvc):
    svc, _db = dbsvc
    assert svc.db_artwork_preview("", "") == {"data_url": None}
    assert svc.db_artwork_preview("/nope.jpg", "/nope.mp3") == {"data_url": None}


def test_db_artwork_preview_falls_back_to_embedded_cover(dbsvc, tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("mutagen")
    from PIL import Image
    from cratebuilder import artwork

    mp3 = _make_mp3(tmp_path / "track.mp3")
    jpg = tmp_path / "cover.jpg"
    Image.new("RGB", (64, 64), (10, 120, 200)).save(str(jpg))
    artwork.embed_cover(mp3, str(jpg))

    svc, _db = dbsvc
    result = svc.db_artwork_preview("", mp3)   # no sidecar path at all
    assert result["data_url"].startswith("data:image/jpeg;base64,")
    assert result["width"] == 64 and result["height"] == 64
    assert "embedded artwork" in result["note"]


# ── fs.reveal ─────────────────────────────────────────────────────────────────

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


def test_fs_reveal_open_mode_uses_os_open(dbsvc, tmp_path, monkeypatch):
    svc, _db = dbsvc
    target = tmp_path / "track.mp3"
    target.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    result = svc.fs_reveal(str(target), "open")
    assert result == {"opened": True}
    assert calls == [str(target)]


def test_fs_reveal_open_mode_missing_file_raises(dbsvc, tmp_path):
    svc, _db = dbsvc
    with pytest.raises(CBError):
        svc.fs_reveal(str(tmp_path / "gone.mp3"), "open")


def test_fs_reveal_folder_mode_falls_back_to_nearest_existing_dir(
        dbsvc, tmp_path, monkeypatch):
    svc, _db = dbsvc
    calls = []
    monkeypatch.setattr(svc, "_os_open", lambda p: calls.append(p))
    # win32's explorer /select branch only fires for an existing path;
    # a nonexistent one falls through to the directory-walk-up branch.
    missing = tmp_path / "ghost" / "track.mp3"
    result = svc.fs_reveal(str(missing), "folder")
    assert result == {"opened": True}
    assert calls == [str(tmp_path)]

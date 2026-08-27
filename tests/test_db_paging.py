import pytest

from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path):
    return DownloadsDatabase(str(tmp_path / "test.db"))


def _add(db, *, title, channel_name, platform="YouTube", genre="House",
         upload_date="20260101", bitrate="320", ts=None, artwork_path=None,
         artwork_embedded=0):
    video_id = title.replace(" ", "_")
    db.add_download(
        video_id=video_id, title=title, channel_name=channel_name,
        channel_url=f"https://yt/{channel_name}", platform=platform,
        genre=genre, file_path=f"/x/{video_id}.mp3", upload_date=upload_date,
        bitrate=bitrate, artwork_path=artwork_path,
        artwork_embedded=artwork_embedded)
    if ts is not None:
        with db._conn() as conn:
            conn.execute(
                "UPDATE downloads SET download_timestamp = ? WHERE video_id = ?",
                (ts, video_id))


# ── count_downloads / query_downloads ───────────────────────────────────────

def test_count_and_query_downloads_no_filter(tmp_path):
    db = _new_db(tmp_path)
    for i in range(5):
        _add(db, title=f"Track {i}", channel_name="Chan", ts=1000 + i)
    assert db.count_downloads() == 5
    rows = db.query_downloads(limit=100, offset=0)
    assert len(rows) == 5
    # dict rows, not sqlite3.Row
    assert type(rows[0]) is dict


def test_query_downloads_filters_platform_genre_and_group_key(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1", platform="YouTube", genre="House")
    _add(db, title="B", channel_name="C2", platform="SoundCloud", genre="DnB")
    _add(db, title="C", channel_name="C1", platform="YouTube", genre="DnB")

    assert db.count_downloads({"platform": "YouTube"}) == 2
    assert db.count_downloads({"platform": "SoundCloud"}) == 1
    assert db.count_downloads({"genre": "DnB"}) == 2
    assert db.count_downloads(
        {"platform": "YouTube", "genre": "DnB"}) == 1

    rows = db.query_downloads(
        {"group_key": "channel_name", "group_value": "C1"})
    assert {r["title"] for r in rows} == {"A", "C"}


def test_query_downloads_search_filter_matches_title_and_channel(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="Deep House Mix", channel_name="Alpha")
    _add(db, title="Trance Set", channel_name="Beta House")
    _add(db, title="Techno", channel_name="Gamma")

    hits = db.query_downloads({"search": "house"})
    assert {r["title"] for r in hits} == {"Deep House Mix", "Trance Set"}
    assert db.count_downloads({"search": "house"}) == 2


def test_query_downloads_search_filter_escapes_like_wildcards(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="100% Pure", channel_name="Chan")
    _add(db, title="100 Percent Pure", channel_name="Chan")

    # A literal '%' in the search text must not act as a SQL LIKE wildcard.
    hits = db.query_downloads({"search": "100%"})
    assert [r["title"] for r in hits] == ["100% Pure"]


def test_query_downloads_group_value_unknown_bucket_matches_blank(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="No Platform", channel_name="Chan", platform="")
    _add(db, title="Has Platform", channel_name="Chan", platform="YouTube")

    rows = db.query_downloads(
        {"group_key": "platform", "group_value": "(unknown)"})
    assert [r["title"] for r in rows] == ["No Platform"]


def test_query_downloads_unknown_group_key_raises(tmp_path):
    db = _new_db(tmp_path)
    with pytest.raises(ValueError):
        db.query_downloads({"group_key": "video_id", "group_value": "x"})
    with pytest.raises(ValueError):
        db.count_downloads({"group_key": "1); DROP TABLE downloads;--"})


def test_query_downloads_order_by_whitelist_rejects_unknown_column(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1")
    # video_id is a real column, but it is deliberately not whitelisted.
    with pytest.raises(ValueError):
        db.query_downloads(order_by="video_id")
    # A blatant injection attempt must be rejected the same way.
    with pytest.raises(ValueError):
        db.query_downloads(order_by="id; DROP TABLE downloads;--")
    # A whitelisted column still works after the rejection.
    rows = db.query_downloads(order_by="title", descending=False)
    assert [r["title"] for r in rows] == ["A"]


def test_query_downloads_paging_no_gaps_no_duplicates(tmp_path):
    db = _new_db(tmp_path)
    total = 25
    for i in range(total):
        _add(db, title=f"Track {i:02d}", channel_name="Chan", ts=1000 + i)

    assert db.count_downloads() == total

    page_size = 7
    seen_ids = []
    offset = 0
    while True:
        page = db.query_downloads(
            order_by="download_timestamp", descending=False,
            limit=page_size, offset=offset)
        if not page:
            break
        seen_ids.extend(r["id"] for r in page)
        offset += page_size

    assert len(seen_ids) == total          # nothing dropped
    assert len(set(seen_ids)) == total     # nothing duplicated

    # Exact boundary: limit == total returns everything in one page, and the
    # very next offset returns nothing.
    exact = db.query_downloads(limit=total, offset=0)
    assert len(exact) == total
    past_end = db.query_downloads(limit=page_size, offset=total)
    assert past_end == []

    # Concatenated pages equal the un-paged order exactly (order preserved
    # across the boundary, not just the same set).
    full = db.query_downloads(
        order_by="download_timestamp", descending=False, limit=total,
        offset=0)
    assert [r["id"] for r in full] == seen_ids


# ── group_downloads ──────────────────────────────────────────────────────────

def test_group_downloads_each_preset_returns_sane_counts(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1", platform="YouTube", genre="House")
    _add(db, title="B", channel_name="C2", platform="YouTube", genre="DnB")
    _add(db, title="C", channel_name="C1", platform="SoundCloud", genre="House")

    for preset in DownloadsDatabase.GROUP_PRESETS:
        groups = db.group_downloads(preset)
        assert sum(g["count"] for g in groups) == 3
        for g in groups:
            assert set(g.keys()) == {"key", "label", "count"}
            assert g["key"] == g["label"]


def test_group_downloads_channel_preset_buckets_by_channel(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1")
    _add(db, title="B", channel_name="C1")
    _add(db, title="C", channel_name="C2")

    groups = {g["key"]: g["count"] for g in db.group_downloads("Channel")}
    assert groups == {"C1": 2, "C2": 1}


def test_group_downloads_blank_values_bucket_as_unknown_and_none(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1", platform="", genre="")
    _add(db, title="B", channel_name="C1", platform="YouTube", genre="House")

    plat_groups = {g["key"]: g["count"]
                   for g in db.group_downloads("Platform › Channel")}
    assert plat_groups["(unknown)"] == 1
    assert plat_groups["YouTube"] == 1

    genre_groups = {g["key"]: g["count"]
                    for g in db.group_downloads("Genre › Channel")}
    assert genre_groups["(none)"] == 1
    assert genre_groups["House"] == 1


def test_group_downloads_drill_down_pins_earlier_levels(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1", platform="YouTube", genre="House")
    _add(db, title="B", channel_name="C2", platform="YouTube", genre="DnB")
    _add(db, title="C", channel_name="C3", platform="SoundCloud", genre="House")

    top = db.group_downloads("Platform › Genre › Channel")
    yt = next(g for g in top if g["key"] == "YouTube")
    assert yt["count"] == 2

    # Drill into platform=YouTube: next ungrouped level is genre.
    second = db.group_downloads(
        "Platform › Genre › Channel",
        {"group_key": "platform", "group_value": "YouTube"})
    assert {g["key"]: g["count"] for g in second} == {"House": 1, "DnB": 1}

    # Fully pinned (platform + genre both given): the only remaining level
    # (channel_name) still breaks out.
    third = db.group_downloads(
        "Platform › Genre › Channel",
        {"platform": "YouTube", "genre": "House"})
    assert {g["key"]: g["count"] for g in third} == {"C1": 1}


def test_group_downloads_all_levels_pinned_returns_empty(tmp_path):
    db = _new_db(tmp_path)
    _add(db, title="A", channel_name="C1", platform="YouTube", genre="House")

    groups = db.group_downloads(
        "Channel", {"group_key": "channel_name", "group_value": "C1"})
    assert groups == []


def test_group_downloads_unknown_preset_raises(tmp_path):
    db = _new_db(tmp_path)
    with pytest.raises(ValueError):
        db.group_downloads("Not A Real Preset")


def test_group_downloads_unknown_group_key_raises(tmp_path):
    db = _new_db(tmp_path)
    with pytest.raises(ValueError):
        db.group_downloads(
            "Channel", {"group_key": "video_id", "group_value": "x"})


# ── query_watchlist_rows ─────────────────────────────────────────────────────

def test_query_watchlist_rows_live_join_counts(tmp_path):
    db = _new_db(tmp_path)
    db.add_watchlist_channel(
        url="https://yt/busy", display_name="Busy Channel",
        platform="YouTube", genre="House")
    db.add_watchlist_channel(
        url="https://yt/quiet", display_name="Quiet Channel",
        platform="YouTube", genre="House")

    for i in range(3):
        db.add_download(
            video_id=f"busy{i}", title=f"T{i}", channel_name="Busy Channel",
            channel_url="https://yt/busy", platform="YouTube", genre="House",
            file_path=f"/x/busy{i}.mp3", upload_date="20260101",
            bitrate="320")

    rows = {r["display_name"]: r for r in db.query_watchlist_rows()}
    assert rows["Busy Channel"]["download_count"] == 3
    assert rows["Quiet Channel"]["download_count"] == 0
    # total_downloaded (the stored column) is still present alongside it.
    assert "total_downloaded" in rows["Busy Channel"]
    # sorted by display_name
    names = [r["display_name"] for r in db.query_watchlist_rows()]
    assert names == sorted(names, key=str.lower)


def test_query_watchlist_rows_empty(tmp_path):
    db = _new_db(tmp_path)
    assert db.query_watchlist_rows() == []


# ── query_artwork_rows / count_artwork_rows ─────────────────────────────────

def test_artwork_filters_partition_correctly(tmp_path):
    db = _new_db(tmp_path)

    # A sidecar file that actually exists on disk.
    ok_path = tmp_path / "ok.jpg"
    ok_path.write_bytes(b"fake-jpeg")

    _add(db, title="Embedded No Path", channel_name="Chan",
         artwork_path=None, artwork_embedded=1)
    _add(db, title="Sidecar On Disk", channel_name="Chan",
         artwork_path=str(ok_path), artwork_embedded=0)
    _add(db, title="Broken Sidecar", channel_name="Chan",
         artwork_path=str(tmp_path / "gone.jpg"), artwork_embedded=0)
    _add(db, title="No Artwork At All", channel_name="Chan",
         artwork_path=None, artwork_embedded=0)
    _add(db, title="Embedded With Broken Path", channel_name="Chan",
         artwork_path=str(tmp_path / "also_gone.jpg"), artwork_embedded=1)

    def titles(filter_name):
        return {r["title"] for r in
                db.query_artwork_rows(filter_name, limit=100, offset=0)}

    assert titles("All tracks") == {
        "Embedded No Path", "Sidecar On Disk", "Broken Sidecar",
        "No Artwork At All", "Embedded With Broken Path"}

    assert titles("Has artwork") == {
        "Embedded No Path", "Sidecar On Disk", "Broken Sidecar",
        "Embedded With Broken Path"}

    assert titles("Missing artwork") == {"No Artwork At All"}

    assert titles("Embedded only") == {
        "Embedded No Path", "Embedded With Broken Path"}

    # Sidecar missing on disk keys off a real filesystem check, and (per the
    # monolith's own _art_state) "broken" wins over "embedded".
    assert titles("Sidecar missing on disk") == {
        "Broken Sidecar", "Embedded With Broken Path"}

    for name in DownloadsDatabase.ARTWORK_FILTERS:
        assert db.count_artwork_rows(name) == len(titles(name))


def test_query_artwork_rows_paging(tmp_path):
    db = _new_db(tmp_path)
    for i in range(12):
        _add(db, title=f"Track {i:02d}", channel_name="Chan",
             artwork_embedded=1)

    assert db.count_artwork_rows("Embedded only") == 12
    page1 = db.query_artwork_rows("Embedded only", limit=5, offset=0)
    page2 = db.query_artwork_rows("Embedded only", limit=5, offset=5)
    page3 = db.query_artwork_rows("Embedded only", limit=5, offset=10)
    ids = [r["id"] for r in page1 + page2 + page3]
    assert len(ids) == 12
    assert len(set(ids)) == 12
    assert db.query_artwork_rows("Embedded only", limit=5, offset=12) == []


def test_query_artwork_rows_unknown_filter_raises(tmp_path):
    db = _new_db(tmp_path)
    with pytest.raises(ValueError):
        db.query_artwork_rows("Not A Real Filter")
    with pytest.raises(ValueError):
        db.count_artwork_rows("Not A Real Filter")

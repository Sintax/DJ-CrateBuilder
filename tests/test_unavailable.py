"""Tests for the permanently-unavailable track memory (unavailable_tracks)."""
from cratebuilder.db import DownloadsDatabase, GEO_RECHECK_SECONDS, is_suppressed


def _new_db(tmp_path, name="test.db"):
    return DownloadsDatabase(str(tmp_path / name))


def _rows(db):
    with db._conn() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM unavailable_tracks")]


def test_unavailable_table_exists_and_reinit_is_idempotent(tmp_path):
    db = _new_db(tmp_path)
    assert _rows(db) == []
    # Re-opening the same file must not raise and must keep the table usable.
    db2 = DownloadsDatabase(str(tmp_path / "test.db"))
    assert _rows(db2) == []


def test_record_unavailable_inserts_first_failure(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(
        platform="SoundCloud", video_id="123", channel_url="https://sc/ukg",
        title="Some Track", reason="DRM-protected", now=1000)
    assert ok is True
    row = _rows(db)[0]
    assert row["attempts"] == 1
    assert row["first_failed"] == 1000
    assert row["last_failed"] == 1000
    assert row["reason"] == "DRM-protected"
    assert row["title"] == "Some Track"
    assert row["channel_url"] == "https://sc/ukg"


def test_record_unavailable_second_failure_bumps_attempts(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="https://sc/ukg", title="T",
                          reason="Removed", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="https://sc/ukg", title="T",
                          reason="Removed", now=2000)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["first_failed"] == 1000   # unchanged
    assert rows[0]["last_failed"] == 2000    # refreshed


def test_record_unavailable_updates_reason_on_refailure(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="", title="T",
                          reason="Geo-blocked", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="", title="T",
                          reason="Removed", now=2000)
    assert _rows(db)[0]["reason"] == "Removed"


def test_same_id_on_two_platforms_is_two_rows(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="abc",
                          channel_url="", title="A",
                          reason="DRM-protected", now=1000)
    db.record_unavailable(platform="YouTube", video_id="abc",
                          channel_url="", title="A",
                          reason="Removed", now=1000)
    assert len(_rows(db)) == 2


def test_record_unavailable_ignores_empty_video_id(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(platform="YouTube", video_id="",
                               channel_url="", title="T",
                               reason="Removed", now=1000)
    assert ok is False
    assert _rows(db) == []


def test_record_unavailable_ignores_empty_reason(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(platform="YouTube", video_id="v1",
                               channel_url="", title="T",
                               reason="", now=1000)
    assert ok is False
    assert _rows(db) == []


DAY = 24 * 3600


def test_geo_recheck_window_is_seven_days():
    assert GEO_RECHECK_SECONDS == 7 * DAY


def test_is_suppressed_drm_after_one_failure():
    assert is_suppressed("DRM-protected", 1, 1000, 1000) is True


def test_is_suppressed_removed_needs_two_failures():
    assert is_suppressed("Removed", 1, 1000, 1000) is False
    assert is_suppressed("Removed", 2, 1000, 1000) is True


def test_is_suppressed_removed_never_expires():
    assert is_suppressed("Removed", 2, 1000, 1000 + 999 * DAY) is True


def test_is_suppressed_geo_needs_two_failures_and_expires():
    # One failure is never enough.
    assert is_suppressed("Geo-blocked", 1, 1000, 1000) is False
    # Two failures, fresh -> suppressed.
    assert is_suppressed("Geo-blocked", 2, 1000, 1000 + 6 * DAY) is True
    # Two failures, older than the window -> eligible again.
    assert is_suppressed("Geo-blocked", 2, 1000, 1000 + 8 * DAY) is False


def test_is_suppressed_unknown_reason_is_never_suppressed():
    assert is_suppressed("Something New", 99, 1000, 1000) is False


def test_get_suppressed_reasons_maps_id_to_reason(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="drm",
                          channel_url="", title="D",
                          reason="DRM-protected", now=1000)
    assert db.get_suppressed_reasons("SoundCloud", now=1000) == {
        "drm": "DRM-protected"}


def test_get_suppressed_reasons_honours_the_two_strike_rule(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="YouTube", video_id="gone",
                          channel_url="", title="G",
                          reason="Removed", now=1000)
    assert db.get_suppressed_reasons("YouTube", now=1000) == {}
    db.record_unavailable(platform="YouTube", video_id="gone",
                          channel_url="", title="G",
                          reason="Removed", now=2000)
    assert db.get_suppressed_reasons("YouTube", now=2000) == {
        "gone": "Removed"}


def test_get_suppressed_reasons_expires_geo_after_the_window(tmp_path):
    db = _new_db(tmp_path)
    for ts in (1000, 2000):
        db.record_unavailable(platform="YouTube", video_id="geo",
                              channel_url="", title="G",
                              reason="Geo-blocked", now=ts)
    assert db.get_suppressed_reasons("YouTube", now=2000 + 6 * DAY) == {
        "geo": "Geo-blocked"}
    assert db.get_suppressed_reasons("YouTube", now=2000 + 8 * DAY) == {}


def test_get_suppressed_reasons_is_scoped_to_one_platform(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="x",
                          channel_url="", title="X",
                          reason="DRM-protected", now=1000)
    assert db.get_suppressed_reasons("YouTube", now=1000) == {}


def test_get_suppressed_reasons_empty_on_fresh_db(tmp_path):
    assert _new_db(tmp_path).get_suppressed_reasons("YouTube") == {}

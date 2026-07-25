"""Tests for the permanently-unavailable track memory (unavailable_tracks)."""
from cratebuilder.db import DownloadsDatabase


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

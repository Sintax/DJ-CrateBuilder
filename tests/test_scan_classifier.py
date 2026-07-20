"""Contract tests for sidecar.classify_scan_entries — the pure watchlist
scan/dedup classifier extracted from MP3DownloaderApp._watchlist_scan_channel.

It buckets yt-dlp flat-playlist entries into 'new' vs 'on_disk' (already owned,
to be backfilled) and drops entries already in the DB or over the time limit.
No DB / tkinter / filesystem — the DB check is injected, folder state is a dict.
"""
from cratebuilder import sidecar


def _never_downloaded(_vid):
    return False


def test_new_entry_passes_through():
    out = sidecar.classify_scan_entries(
        [{"id": "v1", "title": "Fresh Track", "url": "https://yt/v1",
          "upload_date": "20260101"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out["on_disk"] == []
    assert out["new"] == [{"id": "v1", "title": "Fresh Track",
                           "url": "https://yt/v1", "upload_date": "20260101"}]


def test_already_in_db_is_dropped():
    out = sidecar.classify_scan_entries(
        [{"id": "v1", "title": "Owned"}],
        is_downloaded=lambda vid: vid == "v1", folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out["new"] == []
    assert out["on_disk"] == []


def test_on_disk_match_goes_to_backfill_bucket():
    # "My Track!" normalises to the same key as the saved file.
    key = sidecar.normalize_track_key("My Track!")
    out = sidecar.classify_scan_entries(
        [{"id": "v1", "title": "My Track!", "upload_date": "20251212"}],
        is_downloaded=_never_downloaded,
        folder_keys={key: r"C:\Music\My Track_.mp3"}, limit_sec=None,
        platform="YouTube")
    assert out["new"] == []
    assert out["on_disk"] == [{"id": "v1", "title": "My Track!",
                               "upload_date": "20251212",
                               "file_path": r"C:\Music\My Track_.mp3"}]


def test_time_limit_drops_long_videos_but_keeps_short_and_unknown():
    entries = [
        {"id": "long", "title": "Long", "duration": 7200},   # 120 min > 60
        {"id": "short", "title": "Short", "duration": 1800},  # 30 min <= 60
        {"id": "live", "title": "Live", "duration": None},    # unknown -> kept
        {"id": "zero", "title": "Zero", "duration": 0},       # 0 -> kept
    ]
    out = sidecar.classify_scan_entries(
        entries, is_downloaded=_never_downloaded, folder_keys={},
        limit_sec=3600, platform="YouTube")
    kept = {e["id"] for e in out["new"]}
    assert kept == {"short", "live", "zero"}


def test_limit_none_disables_duration_filter():
    out = sidecar.classify_scan_entries(
        [{"id": "long", "title": "Long", "duration": 99999}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert [e["id"] for e in out["new"]] == ["long"]


def test_limit_zero_drops_all_positive_duration():
    # Degenerate config (limiter on, 0 minutes) is preserved verbatim from the
    # original loop: every video with a positive duration is filtered out.
    out = sidecar.classify_scan_entries(
        [{"id": "a", "title": "A", "duration": 1},
         {"id": "b", "title": "B", "duration": None}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=0,
        platform="YouTube")
    assert [e["id"] for e in out["new"]] == ["b"]


def test_url_fallback_prefers_url_then_webpage_then_constructed():
    entries = [
        {"id": "a", "title": "A", "url": "https://direct/a"},
        {"id": "b", "title": "B", "webpage_url": "https://page/b"},
        {"id": "c", "title": "C"},
    ]
    out = sidecar.classify_scan_entries(
        entries, is_downloaded=_never_downloaded, folder_keys={},
        limit_sec=None, platform="YouTube")
    urls = {e["id"]: e["url"] for e in out["new"]}
    assert urls["a"] == "https://direct/a"
    assert urls["b"] == "https://page/b"
    assert urls["c"] == "https://www.youtube.com/watch?v=c"


def test_non_youtube_no_url_yields_empty_string():
    out = sidecar.classify_scan_entries(
        [{"id": "x", "title": "X"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud")
    assert out["new"][0]["url"] == ""


def test_on_disk_entry_without_id_still_classified_on_disk():
    key = sidecar.normalize_track_key("No ID Track")
    out = sidecar.classify_scan_entries(
        [{"title": "No ID Track"}],
        is_downloaded=_never_downloaded,
        folder_keys={key: r"C:\Music\No ID Track.mp3"}, limit_sec=None,
        platform="YouTube")
    assert out["new"] == []
    assert out["on_disk"][0]["id"] == ""
    assert out["on_disk"][0]["file_path"] == r"C:\Music\No ID Track.mp3"


def test_empty_entries():
    out = sidecar.classify_scan_entries(
        [], is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out == {"new": [], "on_disk": []}


def test_classify_scan_entries_delegator(cb):
    # The monolith's _watchlist_scan_channel uses the same extracted function,
    # not a private copy.
    assert cb.classify_scan_entries is sidecar.classify_scan_entries

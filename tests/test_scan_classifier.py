"""Contract tests for crate.classify_scan_entries — the pure watchlist
scan/dedup classifier extracted from MP3DownloaderApp._watchlist_scan_channel.

It buckets yt-dlp flat-playlist entries into 'new' vs 'on_disk' (already owned,
to be backfilled) and drops entries already in the DB or over the time limit.
No DB / tkinter / filesystem — the DB check is injected, folder state is a dict.
"""
from cratebuilder import crate
from cratebuilder.util import normalize_track_key


def _never_downloaded(_vid):
    return False


def test_new_entry_passes_through():
    out = crate.classify_scan_entries(
        [{"id": "v1", "title": "Fresh Track", "url": "https://yt/v1",
          "upload_date": "20260101"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out["on_disk"] == []
    assert out["new"] == [{"id": "v1", "title": "Fresh Track",
                           "url": "https://yt/v1", "upload_date": "20260101"}]


def test_already_in_db_is_dropped():
    out = crate.classify_scan_entries(
        [{"id": "v1", "title": "Owned"}],
        is_downloaded=lambda vid: vid == "v1", folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out["new"] == []
    assert out["on_disk"] == []


def test_on_disk_match_goes_to_backfill_bucket():
    # "My Track!" normalises to the same key as the saved file.
    key = normalize_track_key("My Track!")
    out = crate.classify_scan_entries(
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
    out = crate.classify_scan_entries(
        entries, is_downloaded=_never_downloaded, folder_keys={},
        limit_sec=3600, platform="YouTube")
    kept = {e["id"] for e in out["new"]}
    assert kept == {"short", "live", "zero"}


def test_limit_none_disables_duration_filter():
    out = crate.classify_scan_entries(
        [{"id": "long", "title": "Long", "duration": 99999}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert [e["id"] for e in out["new"]] == ["long"]


def test_limit_zero_drops_all_positive_duration():
    # Degenerate config (limiter on, 0 minutes) is preserved verbatim from the
    # original loop: every video with a positive duration is filtered out.
    out = crate.classify_scan_entries(
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
    out = crate.classify_scan_entries(
        entries, is_downloaded=_never_downloaded, folder_keys={},
        limit_sec=None, platform="YouTube")
    urls = {e["id"]: e["url"] for e in out["new"]}
    assert urls["a"] == "https://direct/a"
    assert urls["b"] == "https://page/b"
    assert urls["c"] == "https://www.youtube.com/watch?v=c"


def test_non_youtube_no_url_yields_empty_string():
    out = crate.classify_scan_entries(
        [{"id": "x", "title": "X"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud")
    assert out["new"][0]["url"] == ""


def test_on_disk_entry_without_id_still_classified_on_disk():
    key = normalize_track_key("No ID Track")
    out = crate.classify_scan_entries(
        [{"title": "No ID Track"}],
        is_downloaded=_never_downloaded,
        folder_keys={key: r"C:\Music\No ID Track.mp3"}, limit_sec=None,
        platform="YouTube")
    assert out["new"] == []
    assert out["on_disk"][0]["id"] == ""
    assert out["on_disk"][0]["file_path"] == r"C:\Music\No ID Track.mp3"


def test_empty_entries():
    out = crate.classify_scan_entries(
        [], is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out == {"new": [], "on_disk": [], "unavailable": [], "upcoming": []}


def test_the_classification_kernel_is_the_channel_crate_method(cb):
    # Replaces the old identity assertion (`cb.classify_scan_entries is
    # sidecar.classify_scan_entries`), which broke when classification moved to
    # cratebuilder.crate. What is worth pinning here is that the module-level
    # kernel these tests call is not a second implementation: it must BE
    # ChannelCrate.classify, and the monolith must be bound to the same class.
    # (That the real scan routes through it is covered end to end by
    # tests/test_crate_paths.py::test_the_scan_buckets_a_mixed_listing_through_the_crate,
    # which drives _watchlist_scan_channel itself.)
    calls = []

    class Spy(crate.ChannelCrate):
        def classify(self, entries, now=None):
            calls.append(entries)
            return super().classify(entries, now=now)

    assert cb.ChannelCrate is crate.ChannelCrate
    entries = [{"id": "fresh", "title": "Fresh", "upload_date": "20260101"}]
    out = crate.classify_scan_entries(
        entries, is_downloaded=_never_downloaded, folder_keys={},
        limit_sec=None, platform="YouTube",
        _crate_class=Spy)
    assert calls == [entries]
    assert [e["id"] for e in out["new"]] == ["fresh"]


def test_suppressed_entry_goes_to_unavailable_not_new():
    out = crate.classify_scan_entries(
        [{"id": "drm1", "title": "Locked Track"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud",
        is_unavailable=lambda vid: "DRM-protected" if vid == "drm1" else None)
    assert out["new"] == []
    assert out["unavailable"] == [{"id": "drm1", "title": "Locked Track",
                                   "reason": "DRM-protected"}]


def test_unsuppressed_entry_still_passes_through():
    out = crate.classify_scan_entries(
        [{"id": "ok1", "title": "Fine"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud", is_unavailable=lambda _vid: None)
    assert [e["id"] for e in out["new"]] == ["ok1"]
    assert out["unavailable"] == []


def test_downloaded_takes_precedence_over_suppressed():
    # Once a track has downloaded it is a download, full stop — it must not
    # reappear in the unavailable bucket.
    out = crate.classify_scan_entries(
        [{"id": "v1", "title": "Owned"}],
        is_downloaded=lambda vid: vid == "v1", folder_keys={}, limit_sec=None,
        platform="YouTube", is_unavailable=lambda _vid: "Removed")
    assert out["new"] == []
    assert out["on_disk"] == []
    assert out["unavailable"] == []


def test_suppression_is_skipped_for_entries_without_an_id():
    # No id means nothing could have been recorded against it.
    out = crate.classify_scan_entries(
        [{"title": "No ID"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube", is_unavailable=lambda _vid: "Removed")
    assert [e["title"] for e in out["new"]] == ["No ID"]
    assert out["unavailable"] == []


def test_suppression_beats_the_duration_filter_and_folder_match():
    # A suppressed track is reported as unavailable regardless of how it
    # would otherwise have been bucketed.
    key = normalize_track_key("On Disk")
    out = crate.classify_scan_entries(
        [{"id": "a", "title": "Too Long", "duration": 99999},
         {"id": "b", "title": "On Disk"}],
        is_downloaded=_never_downloaded,
        folder_keys={key: r"C:\Music\On Disk.mp3"}, limit_sec=60,
        platform="YouTube", is_unavailable=lambda _vid: "DRM-protected")
    assert out["new"] == []
    assert out["on_disk"] == []
    assert {e["id"] for e in out["unavailable"]} == {"a", "b"}

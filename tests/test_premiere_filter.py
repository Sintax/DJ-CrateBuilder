"""Premieres and in-progress streams must never be offered as downloadable.

Two independent defences are covered here: the scan-side live_status filter
(the primary one) and the download-side error matcher (the backstop for when
YouTube's markup changes and live_status stops arriving)."""
import pytest

from cratebuilder.crate import (UNRELEASED_LIVE_STATUSES,
                                classify_scan_entries, is_unreleased_entry)
from cratebuilder.util import classify_deferred_failure, classify_permanent_failure

NOW = 1_700_000_000


def _classify(entries, *, downloaded=(), folder_keys=None, limit_sec=None,
              unavailable=None, now=NOW):
    return classify_scan_entries(
        entries,
        is_downloaded=lambda v: v in downloaded,
        folder_keys=folder_keys or {},
        limit_sec=limit_sec,
        platform="YouTube",
        is_unavailable=(unavailable or {}).get,
        now=now)


# ── is_unreleased_entry ───────────────────────────────────────────────────────

@pytest.mark.parametrize("status", sorted(UNRELEASED_LIVE_STATUSES))
def test_unreleased_live_statuses_are_held_back(status):
    assert is_unreleased_entry({"live_status": status}, NOW) is True


@pytest.mark.parametrize("status", ["was_live", "not_live", "", None])
def test_finished_and_ordinary_videos_are_released(status):
    """was_live is a finished archive — an ordinary downloadable video."""
    assert is_unreleased_entry({"live_status": status}, NOW) is False


def test_a_future_release_timestamp_holds_a_track_back():
    assert is_unreleased_entry({"release_timestamp": NOW + 3600}, NOW) is True


def test_a_past_release_timestamp_does_not():
    assert is_unreleased_entry({"release_timestamp": NOW - 3600}, NOW) is False


def test_an_unparseable_timestamp_errs_toward_showing_the_track():
    """This filter HIDES tracks, so garbage input must not swallow music."""
    assert is_unreleased_entry({"release_timestamp": "soon"}, NOW) is False


def test_a_missing_duration_is_not_evidence():
    """Flat entries routinely omit duration; treating that as 'upcoming'
    would hide great swathes of a SoundCloud listing."""
    assert is_unreleased_entry({"title": "Some Track", "duration": None},
                               NOW) is False


def test_the_word_premiere_in_a_title_is_not_evidence():
    """SoundCloud uses 'Premiere:' for ordinary released tracks — matching
    titles would silently hide real music."""
    entry = {"title": "Premiere: Hamdi 'Counting (Sammy Virji Remix)'"}
    assert is_unreleased_entry(entry, NOW) is False


# ── classify_scan_entries: the upcoming bucket ────────────────────────────────

def test_a_premiere_is_bucketed_upcoming_not_new():
    out = _classify([
        {"id": "aaa", "title": "Real Track"},
        {"id": "bbb", "title": "Beats for Love 2026",
         "live_status": "is_upcoming", "release_timestamp": NOW + 604800},
    ])
    assert [e["id"] for e in out["new"]] == ["aaa"]
    assert [e["id"] for e in out["upcoming"]] == ["bbb"]


def test_the_upcoming_item_carries_its_air_time():
    out = _classify([{"id": "bbb", "title": "Set",
                      "live_status": "is_upcoming",
                      "release_timestamp": NOW + 60}])
    assert out["upcoming"][0] == {"id": "bbb", "title": "Set",
                                  "release_timestamp": NOW + 60}


def test_an_upcoming_entry_without_an_id_is_still_held_back():
    out = _classify([{"title": "No id", "live_status": "is_upcoming"}])
    assert out["new"] == []
    assert out["upcoming"][0]["id"] == ""


def test_an_already_downloaded_track_wins_over_upcoming():
    """If it is in the DB we own it — whatever the listing now claims."""
    out = _classify([{"id": "aaa", "live_status": "is_upcoming"}],
                    downloaded={"aaa"})
    assert out["new"] == [] and out["upcoming"] == []


def test_a_suppressed_track_is_still_reported_unavailable():
    """The permanent memory is checked first, so a track that is both
    remembered-dead and marked live keeps its honest reason."""
    out = _classify([{"id": "aaa", "live_status": "is_upcoming"}],
                    unavailable={"aaa": "Removed"})
    assert [e["id"] for e in out["unavailable"]] == ["aaa"]
    assert out["upcoming"] == []


def test_upcoming_beats_the_time_limiter():
    """A premiere has no duration, so it would otherwise pass the limiter
    and land in 'new'."""
    out = _classify([{"id": "bbb", "live_status": "is_upcoming"}],
                    limit_sec=600)
    assert out["new"] == [] and len(out["upcoming"]) == 1


def test_upcoming_beats_the_on_disk_title_match():
    """A scheduled re-run of an owned title must not backfill a downloads
    row pointing the new id at the old file."""
    out = _classify(
        [{"id": "bbb", "title": "Owned Track", "live_status": "is_upcoming"}],
        folder_keys={"ownedtrack": "C:/x/Owned Track.mp3"})
    assert out["on_disk"] == [] and len(out["upcoming"]) == 1


def test_an_ordinary_scan_reports_an_empty_upcoming_bucket():
    out = _classify([{"id": "aaa", "title": "Real Track"}])
    assert out["upcoming"] == []
    assert len(out["new"]) == 1


def test_a_live_broadcast_is_held_back_until_it_ends():
    """Downloading mid-stream yields a partial set; was_live gets it whole."""
    live = _classify([{"id": "ccc", "live_status": "is_live"}])
    assert len(live["upcoming"]) == 1
    ended = _classify([{"id": "ccc", "live_status": "was_live"}])
    assert len(ended["new"]) == 1


# ── classify_deferred_failure: the download-side backstop ─────────────────────

@pytest.mark.parametrize("message", [
    # Verbatim from debug.log, 2026-08-16.
    "ERROR: [youtube] 5dwkWgpd8hc: Premieres in 7 days",
    "ERROR: [youtube] QIJat7z7hbc: Premieres in 8 hours",
    "ERROR: [youtube] abc: This live event will begin in 3 hours.",
    "ERROR: [youtube] abc: Premiere will begin shortly",
    "ERROR: Requested format is not yet available",
])
def test_scheduled_failures_are_deferred(message):
    assert classify_deferred_failure(message) == "Not out yet"


@pytest.mark.parametrize("message", [
    "ERROR: [soundcloud] 1950080779: This video is DRM protected",
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "ERROR: [youtube] abc: Video unavailable",
    "ERROR: [youtube] abc: This video is not available",
    "",
    None,
])
def test_other_failures_are_not_deferred(message):
    assert classify_deferred_failure(message) is None


def test_a_removed_video_is_not_confused_with_a_scheduled_one():
    """'not available' must not read as 'not yet available'."""
    gone = "ERROR: [youtube] abc: This video is not available"
    assert classify_deferred_failure(gone) is None
    assert classify_permanent_failure(gone) is None


def test_deferred_and_permanent_never_both_claim_a_premiere():
    """A premiere filed as permanently unavailable would be suppressed for
    good and never downloaded once it aired — the worst outcome available."""
    msg = "ERROR: [youtube] 5dwkWgpd8hc: Premieres in 7 days"
    assert classify_deferred_failure(msg) == "Not out yet"
    assert classify_permanent_failure(msg) is None


def test_deferred_matching_is_case_insensitive():
    assert classify_deferred_failure("PREMIERES IN 2 DAYS") == "Not out yet"

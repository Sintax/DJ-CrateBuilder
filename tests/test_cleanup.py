"""Tests for the pure Folders Cleanup matching core."""
from cratebuilder.cleanup import is_scan_trustworthy


def test_zero_scan_is_untrustworthy():
    assert is_scan_trustworthy(0, 20) is False


def test_far_below_half_is_untrustworthy():
    # 3 scanned vs 40 on disk — almost certainly a partial extraction
    assert is_scan_trustworthy(3, 40) is False


def test_at_least_half_is_trustworthy():
    assert is_scan_trustworthy(20, 40) is True
    assert is_scan_trustworthy(21, 40) is True


def test_small_folder_honours_floor_of_five():
    # folder of 4 files: floor is max(4//2, 5) = 5, so need >=5 scanned
    assert is_scan_trustworthy(4, 4) is False
    assert is_scan_trustworthy(5, 4) is True


def test_empty_folder_is_trivially_trustworthy():
    # nothing on disk to wrongly flag; any scan count is fine
    assert is_scan_trustworthy(0, 0) is False  # 0 scan still blocked
    assert is_scan_trustworthy(10, 0) is True


from cratebuilder.cleanup import classify_local_files


def _entry(vid, title):
    return {"id": vid, "title": title}


def _ff(name, full, size=1000, mtime=111):
    return (name, full, size, mtime)


SCAN = [
    _entry("aaa", "Artist - Track One"),
    _entry("bbb", "Artist - Track Two"),
    _entry("ccc", "Artist - Track Three"),
]


def test_kept_by_video_id():
    files = [_ff("Track One.mp3", "/f/Track One.mp3")]
    db = {"/f/Track One.mp3": "aaa"}
    assert classify_local_files(SCAN, files, db) == []


def test_kept_by_title_when_id_absent_or_changed():
    # file has a DB id that's NOT on the channel, but the title still matches a
    # current entry (re-upload under a new id) -> kept.
    files = [_ff("Artist - Track Two.mp3", "/f/Artist - Track Two.mp3")]
    db = {"/f/Artist - Track Two.mp3": "zzz"}  # zzz not in scan, title is
    assert classify_local_files(SCAN, files, db) == []


def test_strong_flag_id_in_db_gone_from_channel():
    files = [_ff("Old Removed Track.mp3", "/f/Old Removed Track.mp3")]
    db = {"/f/Old Removed Track.mp3": "ddd"}  # ddd gone, title not on channel
    out = classify_local_files(SCAN, files, db)
    assert len(out) == 1
    assert out[0]["confidence"] == "strong"
    assert out[0]["full_path"] == "/f/Old Removed Track.mp3"
    assert out[0]["video_id"] == "ddd"


def test_weak_flag_no_db_row():
    files = [_ff("Some Random Mix.mp3", "/f/Some Random Mix.mp3")]
    db = {}  # no DB row at all
    out = classify_local_files(SCAN, files, db)
    assert len(out) == 1
    assert out[0]["confidence"] == "weak"
    assert out[0]["video_id"] is None


def test_unicode_mangled_filename_still_matches():
    # title normalisation collapses to the same key despite mangled glyphs
    scan = [_entry("eee", "1788-L - ÆTHERSUIT")]
    files = [_ff("1788-L - �THERSUIT.mp3", "/f/x.mp3")]
    assert classify_local_files(scan, files, {}) == []


def test_size_and_mtime_passed_through():
    files = [_ff("Gone.mp3", "/f/Gone.mp3", size=4321, mtime=999)]
    out = classify_local_files(SCAN, files, {"/f/Gone.mp3": "ddd"})
    assert out[0]["size_bytes"] == 4321
    assert out[0]["mtime"] == 999
    assert out[0]["filename"] == "Gone.mp3"


def test_none_valued_db_entry_is_weak_like_absent():
    # an explicit None value must behave identically to a missing key
    files = [_ff("Untracked.mp3", "/f/Untracked.mp3")]
    out = classify_local_files(SCAN, files, {"/f/Untracked.mp3": None})
    assert len(out) == 1
    assert out[0]["confidence"] == "weak"
    assert out[0]["video_id"] is None


def test_empty_scan_flags_every_file():
    # the dangerous degenerate case: an empty scan must NOT silently keep files
    # (is_scan_trustworthy gates this upstream, but the unit contract is explicit)
    files = [_ff("A.mp3", "/f/A.mp3"), _ff("B.mp3", "/f/B.mp3")]
    out = classify_local_files([], files, {})
    assert len(out) == 2
    assert all(f["confidence"] == "weak" for f in out)


def test_empty_folder_returns_nothing():
    assert classify_local_files(SCAN, [], {}) == []

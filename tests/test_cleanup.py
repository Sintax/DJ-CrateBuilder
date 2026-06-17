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

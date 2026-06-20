"""Behaviour tests for cratebuilder.util.safe_filename.

Single source for turning a title / channel name into a filename-safe string
(characters illegal on Windows -> '_'). Pins both the raw form (mirrors the
on-disk filename, no strip) and the stripped form used for folder names and
legacy existence matching.
"""
from cratebuilder import util


def test_safe_filename_replaces_illegal_chars():
    assert util.safe_filename(r'a/b\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_safe_filename_keeps_ordinary_text():
    assert util.safe_filename("Drum & Bass 2024 (Mix)") == "Drum & Bass 2024 (Mix)"


def test_safe_filename_no_strip_by_default():
    assert util.safe_filename("  spaced  ") == "  spaced  "


def test_safe_filename_strip_option():
    assert util.safe_filename("  spaced  ", strip=True) == "spaced"
    # strip applies after replacement; interior underscores are preserved
    assert util.safe_filename(" a:b ", strip=True) == "a_b"


def test_safe_filename_handles_none_and_empty():
    assert util.safe_filename("") == ""
    assert util.safe_filename(None) == ""
    assert util.safe_filename(None, strip=True) == ""

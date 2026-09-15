"""cratebuilder.watchlist_share: the list file's shape, and what counts as a clash."""
import json
from datetime import date

import pytest

from cratebuilder import watchlist_share as share


def _row(url, name="Chan", platform="YouTube", genre="Techno", cid=None, **extra):
    row = {"url": url, "display_name": name, "platform": platform,
           "genre": genre, "channel_id": cid, "total_downloaded": 42,
           "last_scanned_timestamp": 123, "last_error": "boom",
           "pending_entries_json": "[{}]"}
    row.update(extra)
    return row


def test_the_default_filename_is_dated():
    assert share.default_filename(date(2026, 9, 11)) == \
        "DJ-CrateBuilder Watch List 2026-09-11.json"


def test_export_carries_only_the_shareable_fields():
    text = share.dumps([_row("https://www.youtube.com/@a", cid="UC1")])
    data = json.loads(text)
    assert data["format"] == share.FORMAT and data["version"] == share.VERSION
    assert data["channels"] == [{
        "url": "https://www.youtube.com/@a", "display_name": "Chan",
        "platform": "YouTube", "genre": "Techno", "channel_id": "UC1"}]
    for private in ("total_downloaded", "last_scanned", "last_error",
                    "pending_entries", "file_path"):
        assert private not in text


def test_export_skips_unresolved_placeholders_and_fills_blanks():
    entries = share.export_entries([
        _row("unresolved://abc"),
        _row("https://soundcloud.com/x", platform="", genre=None, cid=" "),
    ])
    assert len(entries) == 1
    assert entries[0]["platform"] == "SoundCloud"
    assert entries[0]["genre"] == "(none)"
    assert entries[0]["channel_id"] is None


def test_a_round_trip_reads_back_what_was_written():
    rows = [_row("https://www.youtube.com/@a", cid="UC1"),
            _row("https://soundcloud.com/b", platform="SoundCloud", genre="House")]
    assert share.parse(share.dumps(rows)) == (share.export_entries(rows), [])


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"format": "other"}',
                                  '{"format": "dj-cratebuilder-watchlist"}'])
def test_a_file_that_is_not_an_export_is_refused(text):
    with pytest.raises(share.ShareError):
        share.parse(text)


def test_a_newer_format_version_is_refused_with_an_update_hint():
    text = json.dumps({"format": share.FORMAT, "version": share.VERSION + 1,
                       "channels": []})
    with pytest.raises(share.ShareError, match="newer build"):
        share.parse(text)


def test_damaged_entries_are_dropped_rather_than_failing_the_file():
    text = json.dumps({"format": share.FORMAT, "version": 1, "channels": [
        "junk", {"display_name": "no url"}, {"url": "unresolved://x"},
        {"url": " https://www.youtube.com/@ok ", "genre": ""},
    ]})
    entries, dropped = share.parse(text)
    assert entries == [{"url": "https://www.youtube.com/@ok", "display_name": "",
                        "platform": "YouTube", "genre": "(none)",
                        "channel_id": None}]
    assert len(dropped) == 3


# ── a hostile file cannot reach the disk, the browser, or the log ────────────

def _file(channels):
    return json.dumps({"format": share.FORMAT, "version": share.VERSION,
                       "channels": channels})


def test_only_youtube_and_soundcloud_web_links_are_accepted():
    entries, dropped = share.parse(_file([
        {"url": "file:///C:/Windows/system.ini"},
        {"url": "javascript:alert(1)"},
        {"url": "https://evil.example.com/@z"},
        {"url": "https://www.youtube.com@evil.example.com/@z"},
        {"url": "https://xn--yutube-wqa.com/@z"},
        {"url": "ftp://soundcloud.com/x"},
        {"url": "https://www.youtube.com/@fine"},
        {"url": "https://on.soundcloud.com/abc"},
    ]))
    assert [e["url"] for e in entries] == ["https://www.youtube.com/@fine",
                                           "https://on.soundcloud.com/abc"]
    assert len(dropped) == 6
    assert all("link" in reason for reason in dropped)


def test_the_platform_comes_from_the_link_not_the_file():
    entries, _ = share.parse(_file([
        {"url": "https://soundcloud.com/a", "platform": "..\\.."},
        {"url": "https://www.youtube.com/@b", "platform": "SoundCloud"},
    ]))
    assert [e["platform"] for e in entries] == ["SoundCloud", "YouTube"]


def test_a_genre_that_could_walk_the_tree_is_reset_and_the_entry_kept():
    entries, dropped = share.parse(_file([
        {"url": "https://soundcloud.com/a", "genre": "..\\..\\Windows"},
        {"url": "https://soundcloud.com/b", "genre": "../etc"},
        {"url": "https://soundcloud.com/c", "genre": ".."},
        {"url": "https://soundcloud.com/d", "genre": "House"},
    ]))
    assert [e["genre"] for e in entries] == ["(none)", "(none)", "(none)", "House"]
    assert dropped == []


def test_control_and_invisible_characters_are_stripped_and_text_is_capped():
    entries, _ = share.parse(_file([
        {"url": "https://youtu.be/x",
         "display_name": "Bad\x00Name\nLINE FORGED\u202eevil\u200b",
         "genre": "G" * 500}]))
    assert entries[0]["display_name"] == "BadNameLINE FORGEDevil"
    assert len(entries[0]["genre"]) == share.MAX_TEXT


def test_a_malformed_channel_id_is_forgotten_but_the_entry_kept():
    entries, _ = share.parse(_file([
        {"url": "https://youtu.be/x", "channel_id": "UC../..\\evil"},
        {"url": "https://youtu.be/y", "channel_id": "UCabc_-123"},
        {"url": "https://youtu.be/z", "channel_id": 12345},
    ]))
    assert [e["channel_id"] for e in entries] == [None, "UCabc_-123", None]


def test_non_string_fields_are_coerced_not_crashed():
    entries, _ = share.parse(_file([
        {"url": ["https://youtu.be/x"], "display_name": {"a": 1}},
        {"url": "https://youtu.be/y", "display_name": 42, "genre": None},
    ]))
    assert len(entries) == 1
    assert entries[0]["display_name"] == "42"


def test_a_deeply_nested_file_is_refused_not_crashed():
    text = "[" * 100000 + "]" * 100000
    with pytest.raises(share.ShareError):
        share.parse(text)


def test_too_many_entries_are_refused():
    many = [{"url": "https://youtu.be/x"}] * (share.MAX_ENTRIES + 1)
    with pytest.raises(share.ShareError, match="too many"):
        share.parse(_file(many))


def test_a_byte_order_mark_is_tolerated():
    entries, _ = share.parse("\ufeff" + _file([{"url": "https://youtu.be/x"}]))
    assert len(entries) == 1


def test_sanitize_entry_is_what_the_import_boundary_uses():
    entry, reason = share.sanitize_entry({"url": "https://youtu.be/x",
                                          "genre": "../x"})
    assert reason is None and entry["genre"] == "(none)"
    entry, reason = share.sanitize_entry({"url": "data:text/html,hi"})
    assert entry is None and "link" in reason
    entry, reason = share.sanitize_entry("junk")
    assert entry is None


def test_size_and_count_limits_are_the_agreed_values():
    assert share.MAX_IMPORT_BYTES == 4 * 1024 * 1024
    assert share.MAX_ENTRIES == 5000
    assert share.MAX_TEXT == 200



def _existing():
    return [
        {"id": 1, "url": "https://www.youtube.com/@a", "channel_id": "UC1",
         "platform": "YouTube", "display_name": "Alpha"},
        {"id": 2, "url": "https://www.youtube.com/channel/UC2", "channel_id": "",
         "platform": "YouTube", "display_name": "Beta Beats"},
    ]


def test_name_key_ignores_case_edges_and_inner_spacing():
    assert share.name_key("  Beta   BEATS ") == "beta beats"
    assert share.name_key(None) == ""


def test_name_is_taken_matches_any_spelling_of_an_existing_name():
    assert share.name_is_taken("beta beats", _existing())
    assert share.name_is_taken("ALPHA ", _existing())
    assert not share.name_is_taken("Gamma", _existing())
    assert not share.name_is_taken("", _existing())


def test_no_conflict_for_a_channel_that_is_genuinely_new():
    entry = {"url": "https://www.youtube.com/@new", "channel_id": "UC9",
             "platform": "YouTube", "display_name": "Gamma"}
    assert share.find_conflict(entry, _existing()) is None


def test_a_link_clash_is_found_by_id_link_or_spelling():
    by_id = {"url": "https://www.youtube.com/@aa", "channel_id": "UC1",
             "platform": "YouTube", "display_name": "Other"}
    by_spelling = {"url": "https://www.youtube.com/channel/UC2/videos",
                   "channel_id": None, "platform": "YouTube",
                   "display_name": "Other"}
    hit = share.find_conflict(by_id, _existing())
    assert hit["row"]["id"] == 1 and hit["kinds"] == ["link"]
    hit = share.find_conflict(by_spelling, _existing())
    assert hit["row"]["id"] == 2 and hit["kinds"] == ["link"]


def test_a_name_clash_is_reported_when_the_links_differ():
    entry = {"url": "https://www.youtube.com/@elsewhere", "channel_id": "UC7",
             "platform": "YouTube", "display_name": "beta beats"}
    hit = share.find_conflict(entry, _existing())
    assert hit["row"]["id"] == 2 and hit["kinds"] == ["name"]


def test_a_clash_on_both_link_and_name_names_both():
    entry = {"url": "https://www.youtube.com/@a", "channel_id": None,
             "platform": "YouTube", "display_name": "ALPHA"}
    hit = share.find_conflict(entry, _existing())
    assert hit["row"]["id"] == 1 and hit["kinds"] == ["link", "name"]


def test_the_link_clash_wins_when_the_name_matches_a_different_row():
    entry = {"url": "https://www.youtube.com/@a", "channel_id": "UC1",
             "platform": "YouTube", "display_name": "Beta Beats"}
    hit = share.find_conflict(entry, _existing())
    assert hit["row"]["id"] == 1 and hit["kinds"] == ["link"]

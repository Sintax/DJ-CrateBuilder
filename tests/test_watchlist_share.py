"""cratebuilder.watchlist_share: the list file's shape, and add-vs-skip."""
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
    assert share.parse(share.dumps(rows)) == share.export_entries(rows)


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
    entries = share.parse(text)
    assert entries == [{"url": "https://www.youtube.com/@ok", "display_name": "",
                        "platform": "YouTube", "genre": "(none)",
                        "channel_id": None}]


def test_plan_import_skips_tracked_channels_by_id_link_or_spelling():
    existing = [
        {"url": "https://www.youtube.com/@a", "channel_id": "UC1", "platform": "YouTube"},
        {"url": "https://www.youtube.com/channel/UC2", "channel_id": "", "platform": "YouTube"},
    ]
    entries = [
        # same id, different link
        {"url": "https://www.youtube.com/@aa", "channel_id": "UC1", "platform": "YouTube"},
        # exact link
        {"url": "https://www.youtube.com/channel/UC2", "channel_id": None, "platform": "YouTube"},
        # another spelling of the same channel link
        {"url": "https://www.youtube.com/channel/UC2/videos", "channel_id": None, "platform": "YouTube"},
        # genuinely new
        {"url": "https://www.youtube.com/@new", "channel_id": "UC9", "platform": "YouTube"},
        # the same new channel twice in one file
        {"url": "https://www.youtube.com/@new-again", "channel_id": "UC9", "platform": "YouTube"},
    ]
    to_add, skipped = share.plan_import(entries, existing)
    assert [e["url"] for e in to_add] == ["https://www.youtube.com/@new"]
    assert len(skipped) == 4
    # Existing rows are read, never rewritten.
    assert existing[1]["channel_id"] == ""

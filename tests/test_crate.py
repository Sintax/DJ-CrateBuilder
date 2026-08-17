"""Tests for cratebuilder.crate — CrateLayout naming, ChannelCrate ownership.

The new home of the two questions the app used to answer in several places:
"where does this track live?" (CrateLayout) and "do we already have this?"
(ChannelCrate / Ownership / skip_decision). Two behaviours here deliberately
differ from what the app did before the extraction — the whitespace-only genre
and the retired 40-char prefix tier — and each is called out in the test that
asserts it. test_crate_paths.py drives the same rules through the real app.

No network, no real config, no real library: every path lives under tmp_path.
"""
import os

import pytest

from cratebuilder import crate
from cratebuilder.crate import (ChannelCrate, CrateLayout, Ownership,
                                OwnershipKind, SkipDecision, SkipMode,
                                skip_decision)
from cratebuilder.util import normalize_track_key, safe_filename

try:
    from yt_dlp.utils import sanitize_filename as ytdl_sanitize
except ImportError:                                   # pragma: no cover
    ytdl_sanitize = None

NOW = 1_700_000_000


def _never_downloaded(_vid):
    return False


def _mp3(directory, name):
    p = directory / f"{name}.mp3"
    p.write_bytes(b"\x00")
    return str(p)


# ══════════════════════════════════════════════════════════════════════════════
# CrateLayout — the "(none)" ↔ "_No Genre" translation, both directions
# ══════════════════════════════════════════════════════════════════════════════
def test_a_real_genre_is_its_own_folder_name():
    assert CrateLayout.genre_dir_name("Drum & Bass") == "Drum & Bass"


@pytest.mark.parametrize("genre", [None, "", "(none)"])
def test_every_no_genre_spelling_becomes_the_no_genre_folder(genre):
    assert CrateLayout.genre_dir_name(genre) == "_No Genre"


def test_a_whitespace_only_genre_now_becomes_the_no_genre_folder():
    # INTENDED DIVERGENCE. The old app builder produced a folder component made
    # of spaces — a path Win32 then refused to create at all, failing the whole
    # URL. The DB viewer's twin already normalised it; both sides now agree.
    # A PADDED genre is a different case and stays verbatim — see
    # test_a_padded_real_genre_is_kept_verbatim.
    assert CrateLayout.genre_dir_name("  ") == "_No Genre"
    assert CrateLayout.genre_dir_name("\t\n") == "_No Genre"


def test_a_padded_real_genre_is_kept_verbatim():
    # Only the whitespace-ONLY case is normalised. A padded genre is a folder
    # that exists on disk and a value stored in the DB; renaming it here would
    # point every path at a folder the user does not have.
    assert CrateLayout.genre_dir_name("  DnB  ") == "  DnB  "
    assert CrateLayout.genre_dir_name(" DnB") == " DnB"


def test_the_no_genre_folder_reads_back_as_the_in_app_value():
    assert CrateLayout.genre_value("_No Genre") == "(none)"
    assert CrateLayout.genre_value("DnB") == "DnB"


@pytest.mark.parametrize("dir_name", [None, "", "   "])
def test_a_blank_genre_folder_reads_back_as_the_in_app_value(dir_name):
    assert CrateLayout.genre_value(dir_name) == "(none)"


@pytest.mark.parametrize("genre", ["DnB", "(none)", None, "", "  "])
def test_genre_translation_round_trips(genre):
    # The two directions compose: a genre written to disk and read back is the
    # in-app value again, and "(none)" is the one representative of no-genre.
    assert CrateLayout.genre_value(CrateLayout.genre_dir_name(genre)) == \
        (genre if genre and genre.strip() and genre != "(none)" else "(none)")


# ── the channel folder ────────────────────────────────────────────────────────
def test_channel_dir_is_platform_genre_channel(tmp_path):
    pdir = str(tmp_path / "YouTube")
    assert CrateLayout.channel_dir(pdir, "DnB", "Chan") == \
        os.path.join(pdir, "DnB", "Chan")


@pytest.mark.parametrize("channel", [None, "", "   "])
def test_channel_dir_without_a_usable_channel_stops_at_the_genre(tmp_path,
                                                                channel):
    pdir = str(tmp_path / "YouTube")
    assert CrateLayout.channel_dir(pdir, "DnB", channel) == \
        os.path.join(pdir, "DnB")


def test_channel_dir_sanitises_and_strips_the_channel_name(tmp_path):
    pdir = str(tmp_path / "YouTube")
    raw = '  UKF: Drum\\Bass / Mix*Q? "x" <y> |z|  '
    got = CrateLayout.channel_dir(pdir, "DnB", raw)
    assert got == os.path.join(pdir, "DnB", 'UKF_ Drum_Bass _ Mix_Q_ _x_ _y_ _z_')
    assert os.path.basename(got) == safe_filename(raw, strip=True)


def test_a_channel_of_only_illegal_characters_still_gets_a_folder(tmp_path):
    # Each illegal character maps to "_", so the name is not empty and a real
    # folder is named — unlike a whitespace-only name, which is dropped.
    pdir = str(tmp_path / "YouTube")
    assert CrateLayout.channel_dir(pdir, "DnB", "?:|") == \
        os.path.join(pdir, "DnB", "___")
    assert CrateLayout.channel_dir_name("   ") == ""


def test_a_no_genre_channel_lands_in_the_no_genre_folder(tmp_path):
    pdir = str(tmp_path / "YouTube")
    assert CrateLayout.channel_dir(pdir, "(none)", "Chan") == \
        os.path.join(pdir, "_No Genre", "Chan")


def test_naming_never_touches_the_disk(tmp_path):
    pdir = str(tmp_path / "YouTube")
    path = CrateLayout.channel_dir(pdir, "DnB", "Chan")
    assert not os.path.exists(path)
    assert not os.path.exists(pdir)
    CrateLayout.track_path(path, "Some Track")
    assert not os.path.exists(path)


# ── the track's file name ─────────────────────────────────────────────────────
def test_track_file_name_is_the_apps_guess_at_what_ytdlp_writes():
    assert CrateLayout.track_file_name("DnB Mix?") == "DnB Mix_.mp3"
    # Surrounding whitespace is deliberately kept — the expected_path the
    # download loop builds today does the same, and find_existing reconciles it.
    assert CrateLayout.track_file_name("  Padded  ") == "  Padded  .mp3"


def test_track_path_joins_the_directory(tmp_path):
    assert CrateLayout.track_path(str(tmp_path), "Track") == \
        os.path.join(str(tmp_path), "Track.mp3")


# ══════════════════════════════════════════════════════════════════════════════
# CrateLayout.find_existing — two EXACT tiers, no prefix scan
# ══════════════════════════════════════════════════════════════════════════════
def test_find_existing_tier1_prefers_the_ytdlp_sanitised_name(tmp_path):
    if ytdl_sanitize is None:                          # pragma: no cover
        pytest.skip("yt_dlp.utils.sanitize_filename unavailable")
    title = "DnB Mix?"
    ytdl_name = ytdl_sanitize(title, restricted=False)      # 'DnB Mix？'
    legacy_name = safe_filename(title, strip=True)          # 'DnB Mix_'
    assert ytdl_name != legacy_name
    ytdl_path = _mp3(tmp_path, ytdl_name)
    _mp3(tmp_path, legacy_name)
    assert CrateLayout.find_existing(str(tmp_path), title) == ytdl_path


def test_find_existing_tier2_finds_the_legacy_regex_name(tmp_path):
    legacy_path = _mp3(tmp_path, safe_filename("DnB Mix?", strip=True))
    assert CrateLayout.find_existing(str(tmp_path), "DnB Mix?") == legacy_path


def test_find_existing_tier2_finds_a_padded_title(tmp_path):
    # yt-dlp keeps surrounding spaces; safe_filename(strip=True) removes them,
    # so only tier 2 finds this file.
    stripped_path = _mp3(tmp_path, "Padded Title")
    assert CrateLayout.find_existing(str(tmp_path), "  Padded Title  ") == \
        stripped_path


def test_find_existing_no_longer_prefix_matches_a_remix(tmp_path):
    # THE RETIRED TIER. The deleted _file_exists_on_disk answered this query
    # with the Cutline Remix, because both titles share their first 40
    # characters — so the original was never downloaded. The exact tiers cannot
    # make that mistake.
    remix = _mp3(tmp_path, "Aurora Skyline Sessions Vol 3 Extended Journey - "
                           "Cascade (Cutline Remix)")
    original = "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade"
    assert len(original) > 40
    assert CrateLayout.find_existing(str(tmp_path), original) is None
    assert normalize_track_key(original) != \
        normalize_track_key(os.path.basename(remix))


def test_find_existing_no_longer_claims_a_short_title_from_a_longer_file(
        tmp_path):
    # The same retired tier's worst case: for a title under 40 characters the
    # "prefix" was the whole title, so ANY longer file starting with it counted
    # as ownership.
    _mp3(tmp_path, "Cascade (VIP Edit)")
    assert CrateLayout.find_existing(str(tmp_path), "Cascade") is None


def test_find_existing_ignores_non_mp3_audio(tmp_path):
    (tmp_path / "Some Track.webm").write_bytes(b"\x00")
    (tmp_path / "Some Track.m4a").write_bytes(b"\x00")
    assert CrateLayout.find_existing(str(tmp_path), "Some Track") is None


def test_find_existing_returns_none_for_a_missing_directory(tmp_path):
    assert CrateLayout.find_existing(str(tmp_path / "nope"), "Track") is None


# ══════════════════════════════════════════════════════════════════════════════
# ChannelCrate.index_folder — the snapshot
# ══════════════════════════════════════════════════════════════════════════════
def test_the_index_maps_normalized_keys_to_full_paths(tmp_path):
    path = _mp3(tmp_path, "My Track_")
    index = ChannelCrate.index_folder(str(tmp_path))
    assert index == {normalize_track_key("My Track!"): path}


def test_the_index_holds_mp3_only(tmp_path):
    (tmp_path / "Some Track.webm").write_bytes(b"\x00")
    (tmp_path / "cratebuilder.json").write_text("{}", encoding="utf-8")
    assert ChannelCrate.index_folder(str(tmp_path)) == {}


def test_the_index_drops_titles_with_no_alphanumerics(tmp_path):
    # A title of "???" saves as "___" (legacy) or "？？？" (yt-dlp); both key to
    # "" and would otherwise match every other such title.
    _mp3(tmp_path, "___")
    _mp3(tmp_path, "？？？")
    assert ChannelCrate.index_folder(str(tmp_path)) == {}


def test_a_missing_folder_indexes_empty(tmp_path):
    assert ChannelCrate.index_folder(str(tmp_path / "nope")) == {}
    assert ChannelCrate.index_folder("") == {}


def test_the_index_is_snapshotted_at_construction(tmp_path):
    crate_obj = ChannelCrate(str(tmp_path), is_downloaded=_never_downloaded)
    assert crate_obj.owns({"id": "v1", "title": "Late Arrival"}) == \
        Ownership.new()
    _mp3(tmp_path, "Late Arrival")
    # The same crate answers from its snapshot; a fresh one sees the new file.
    assert crate_obj.owns({"id": "v1", "title": "Late Arrival"}) == \
        Ownership.new()
    later = ChannelCrate(str(tmp_path), is_downloaded=_never_downloaded)
    assert later.owns({"id": "v1", "title": "Late Arrival"}).kind is \
        OwnershipKind.OWNED_ON_DISK


# ══════════════════════════════════════════════════════════════════════════════
# ChannelCrate.owns — one Ownership fact per entry
# ══════════════════════════════════════════════════════════════════════════════
def _crate(**kw):
    kw.setdefault("is_downloaded", _never_downloaded)
    return ChannelCrate("", folder_keys=kw.pop("folder_keys", {}), **kw)


def test_owns_new_when_nothing_knows_about_it():
    own = _crate().owns({"id": "v1", "title": "Fresh"})
    assert own == Ownership.new()
    assert own.kind is OwnershipKind.NEW
    assert own.is_owned is False


def test_owns_owned_by_id_when_the_database_has_the_video_id():
    own = _crate(is_downloaded=lambda vid: vid == "v1").owns(
        {"id": "v1", "title": "Owned"})
    assert own.kind is OwnershipKind.OWNED_BY_ID
    assert own.in_db is True and own.is_owned is True
    assert own.path == ""          # the file itself is gone


def test_owns_owned_on_disk_by_exact_normalized_key():
    key = normalize_track_key("My Track!")
    own = _crate(folder_keys={key: r"C:\Music\My Track_.mp3"}).owns(
        {"id": "v1", "title": "My Track!"})
    assert own == Ownership.owned_on_disk(r"C:\Music\My Track_.mp3")
    assert own.is_owned is True


def test_owns_does_not_prefix_match_on_disk():
    # The retired tier, again: a remix on disk is not the original.
    remix = "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade " \
            "(Cutline Remix)"
    own = _crate(folder_keys={normalize_track_key(remix): "C:/x/" + remix +
                              ".mp3"}).owns(
        {"id": "v1", "title": "Aurora Skyline Sessions Vol 3 Extended "
                              "Journey - Cascade"})
    assert own.kind is OwnershipKind.NEW


def test_owns_unavailable_carries_the_reason():
    own = _crate(suppressed_reason={"v1": "Removed"}.get).owns(
        {"id": "v1", "title": "Gone"})
    assert own == Ownership.unavailable("Removed")
    assert own.reason == "Removed"


def test_owns_upcoming_for_a_premiere():
    own = _crate().owns({"id": "v1", "title": "Set",
                         "live_status": "is_upcoming"})
    assert own.kind is OwnershipKind.UPCOMING


def test_owns_upcoming_for_a_future_release_timestamp():
    own = _crate().owns({"id": "v1", "release_timestamp": NOW + 60}, now=NOW)
    assert own.kind is OwnershipKind.UPCOMING
    past = _crate().owns({"id": "v1", "release_timestamp": NOW - 60}, now=NOW)
    assert past.kind is OwnershipKind.NEW


def test_owns_too_long_only_when_a_ceiling_is_set():
    entry = {"id": "v1", "title": "Long", "duration": 7200}
    assert _crate(limit_sec=3600).owns(entry).kind is OwnershipKind.TOO_LONG
    assert _crate(limit_sec=None).owns(entry).kind is OwnershipKind.NEW
    # An unknown or zero duration is not evidence of anything.
    assert _crate(limit_sec=3600).owns(
        {"id": "v1", "title": "Live", "duration": None}).kind is \
        OwnershipKind.NEW


def test_owns_checks_the_database_before_every_other_story():
    # Owning a track outranks the suppression memory and a live_status: the
    # scan's order, and the one canonical order.
    own = _crate(is_downloaded=lambda _v: True,
                 suppressed_reason={"v1": "Removed"}.get).owns(
        {"id": "v1", "title": "Owned", "live_status": "is_upcoming"})
    assert own.kind is OwnershipKind.OWNED_BY_ID


def test_owns_ignores_the_suppression_memory_for_an_entry_with_no_id():
    own = _crate(suppressed_reason=lambda _v: "Removed").owns(
        {"title": "No id"})
    assert own.kind is OwnershipKind.NEW


def test_a_withheld_verdict_still_carries_the_on_disk_evidence():
    # The headline says "unavailable"/"upcoming", but the file IS in the folder.
    # skip_decision reads the evidence, so a download batch still skips it —
    # which is what the app does today.
    key = normalize_track_key("Gone")
    own = _crate(folder_keys={key: "C:/x/Gone.mp3"},
                 suppressed_reason={"v1": "Removed"}.get).owns(
        {"id": "v1", "title": "Gone"})
    assert own.kind is OwnershipKind.UNAVAILABLE
    assert own.path == "C:/x/Gone.mp3" and own.is_owned is True


def test_owns_reads_a_real_folder(tmp_path):
    path = _mp3(tmp_path, "Real File")
    crate_obj = ChannelCrate(str(tmp_path), is_downloaded=_never_downloaded)
    assert crate_obj.owns({"id": "v1", "title": "Real File"}) == \
        Ownership.owned_on_disk(path)
    assert crate_obj.track_path("Real File") == path
    assert crate_obj.track_path("Nothing Here") == ""


# ══════════════════════════════════════════════════════════════════════════════
# skip_decision — pure, evidence-driven; the Tk prompt stays at the caller
# ══════════════════════════════════════════════════════════════════════════════
_ON_DISK = Ownership.owned_on_disk("C:/x/Track.mp3")
_IN_DB_ONLY = Ownership.owned_by_id()
_IN_DB_AND_DISK = Ownership.owned_by_id(path="C:/x/Track.mp3")
_NEW = Ownership.new()


@pytest.mark.parametrize("mode,ownership,expected", [
    # "In Database ~ In Folder": either evidence skips.
    (SkipMode.DB_AND_FOLDER, _ON_DISK,        SkipDecision.SKIP),
    (SkipMode.DB_AND_FOLDER, _IN_DB_AND_DISK, SkipDecision.SKIP),
    (SkipMode.DB_AND_FOLDER, _NEW,            SkipDecision.DOWNLOAD),
    # "In Folder Only" ignores the database entirely.
    (SkipMode.FOLDER_ONLY,   _ON_DISK,        SkipDecision.SKIP),
    (SkipMode.FOLDER_ONLY,   _IN_DB_AND_DISK, SkipDecision.SKIP),
    (SkipMode.FOLDER_ONLY,   _IN_DB_ONLY,     SkipDecision.DOWNLOAD),
    (SkipMode.FOLDER_ONLY,   _NEW,            SkipDecision.DOWNLOAD),
    # "In Database Only" ignores the folder entirely.
    (SkipMode.DB_ONLY,       _IN_DB_AND_DISK, SkipDecision.SKIP),
    (SkipMode.DB_ONLY,       _ON_DISK,        SkipDecision.DOWNLOAD),
    (SkipMode.DB_ONLY,       _NEW,            SkipDecision.DOWNLOAD),
])
def test_skip_decision_truth_table(mode, ownership, expected):
    assert skip_decision(ownership, mode) is expected


@pytest.mark.parametrize("mode", [SkipMode.DB_AND_FOLDER, SkipMode.DB_ONLY])
def test_the_database_without_the_file_asks_the_user(mode):
    # "The database says we have it but the file is gone" — the only case that
    # needs a human. The prompt itself stays at the caller.
    assert skip_decision(_IN_DB_ONLY, mode) is SkipDecision.CONFIRM_REDOWNLOAD


def test_only_the_database_consulting_modes_can_ask():
    assert skip_decision(_IN_DB_ONLY, SkipMode.FOLDER_ONLY) is \
        SkipDecision.DOWNLOAD
    assert skip_decision(_IN_DB_ONLY, SkipMode.WATCH_LIST) is SkipDecision.SKIP


@pytest.mark.parametrize("ownership,expected", [
    (_ON_DISK,        SkipDecision.SKIP),
    (_IN_DB_ONLY,     SkipDecision.SKIP),
    (_IN_DB_AND_DISK, SkipDecision.SKIP),
    (_NEW,            SkipDecision.DOWNLOAD),
])
def test_the_watch_list_skips_on_either_evidence(ownership, expected):
    assert skip_decision(ownership, SkipMode.WATCH_LIST) is expected


def test_the_watch_list_is_a_mode_of_its_own_not_a_skip_mode_variant():
    # A Watch List batch ignores the Skip-Existing dropdown: it always skips
    # what it owns, by either evidence, and never prompts. Each of the three
    # user modes disagrees with that on at least one case, which is why the WL
    # rule cannot be expressed as one of them.
    owned = (_ON_DISK, _IN_DB_ONLY, _IN_DB_AND_DISK)
    assert {skip_decision(o, SkipMode.WATCH_LIST) for o in owned} == \
        {SkipDecision.SKIP}
    assert skip_decision(_IN_DB_ONLY, SkipMode.FOLDER_ONLY) is \
        SkipDecision.DOWNLOAD
    assert skip_decision(_IN_DB_ONLY, SkipMode.DB_ONLY) is \
        SkipDecision.CONFIRM_REDOWNLOAD
    assert skip_decision(_ON_DISK, SkipMode.DB_ONLY) is SkipDecision.DOWNLOAD


@pytest.mark.parametrize("mode", [SkipMode.DB_AND_FOLDER, SkipMode.FOLDER_ONLY,
                                 SkipMode.DB_ONLY, SkipMode.WATCH_LIST])
@pytest.mark.parametrize("ownership", [
    Ownership.upcoming(),
    Ownership.unavailable("Removed"),
    Ownership.too_long(),
])
def test_withheld_buckets_are_still_downloaded(mode, ownership):
    # DELIBERATE, and the whole reason skip_decision reads evidence rather than
    # the headline. Ownership reports "upcoming" and "unavailable" honestly —
    # the scan uses them to keep a premiere or a dead track out of "new" — but
    # the download path has NO pre-flight check for either today: it attempts
    # the track and lets classify_deferred_failure / classify_permanent_failure
    # sort out the failure afterwards. Mapping them to SKIP here would be a
    # behaviour change smuggled in by a refactor. Acting on them pre-flight is
    # a separate, deliberate decision for later.
    assert skip_decision(ownership, mode) is SkipDecision.DOWNLOAD


@pytest.mark.parametrize("mode", ["", None, "Off", "Something Legacy"])
def test_an_unrecognised_mode_downloads(mode):
    # Matches today's `should_skip = False` fall-through, which is also what a
    # batch with skip-checking switched off does.
    assert skip_decision(_ON_DISK, mode) is SkipDecision.DOWNLOAD
    assert skip_decision(_IN_DB_ONLY, mode) is SkipDecision.DOWNLOAD


def test_the_mode_strings_are_the_ones_the_ui_stores():
    # The Skip-Existing dropdown hands over plain strings; the enum compares
    # equal to them so a Tk var can be passed straight in.
    assert skip_decision(_ON_DISK, "In Folder Only") is SkipDecision.SKIP
    assert skip_decision(_ON_DISK, "In Database Only") is SkipDecision.DOWNLOAD
    assert skip_decision(_ON_DISK, "In Database ~ In Folder") is \
        SkipDecision.SKIP
    assert SkipDecision.CONFIRM_REDOWNLOAD == "confirm-redownload"


# ══════════════════════════════════════════════════════════════════════════════
# ChannelCrate.classify — the scan's four buckets, absorbed from sidecar
# ══════════════════════════════════════════════════════════════════════════════
def test_classify_buckets_a_mixed_listing_from_a_real_folder(tmp_path):
    on_disk_path = _mp3(tmp_path, "Legacy Track")
    crate_obj = ChannelCrate(
        str(tmp_path), platform="YouTube",
        is_downloaded=lambda vid: vid == "owned",
        suppressed_reason={"gone": "Removed"}.get, limit_sec=3600)
    out = crate_obj.classify([
        {"id": "owned", "title": "Owned"},
        {"id": "gone", "title": "Gone"},
        {"id": "soon", "title": "Soon", "live_status": "is_upcoming",
         "release_timestamp": NOW + 60},
        {"id": "long", "title": "Long", "duration": 7200},
        {"id": "legacy", "title": "Legacy Track", "upload_date": "20251212"},
        {"id": "fresh", "title": "Fresh", "upload_date": "20260101"},
    ], now=NOW)
    assert out["new"] == [{"id": "fresh", "title": "Fresh",
                           "url": "https://www.youtube.com/watch?v=fresh",
                           "upload_date": "20260101"}]
    assert out["on_disk"] == [{"id": "legacy", "title": "Legacy Track",
                               "upload_date": "20251212",
                               "file_path": on_disk_path}]
    assert out["unavailable"] == [{"id": "gone", "title": "Gone",
                                   "reason": "Removed"}]
    assert out["upcoming"] == [{"id": "soon", "title": "Soon",
                                "release_timestamp": NOW + 60}]


def test_classify_matches_the_pure_kernel(tmp_path):
    # classify_scan_entries is the same rule over an index taken elsewhere —
    # one implementation, two entry points.
    _mp3(tmp_path, "Legacy Track")
    entries = [{"id": "legacy", "title": "Legacy Track"},
               {"id": "fresh", "title": "Fresh"}]
    crate_obj = ChannelCrate(str(tmp_path), is_downloaded=_never_downloaded)
    assert crate_obj.classify(entries) == crate.classify_scan_entries(
        entries, is_downloaded=_never_downloaded,
        folder_keys=crate_obj.folder_keys, limit_sec=None, platform="YouTube")


def test_classify_leaves_the_soundcloud_url_empty_when_the_entry_has_none():
    out = ChannelCrate("", platform="SoundCloud",
                       is_downloaded=_never_downloaded).classify(
        [{"id": "x", "title": "X"}])
    assert out["new"][0]["url"] == ""

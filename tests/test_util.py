import re
import datetime as _dt

from cratebuilder import util


def test_normalize_strips_audio_extensions():
    assert util.normalize_track_key("My Track.mp3") == util.normalize_track_key("My Track")
    for ext in ("m4a", "opus", "webm", "wav", "flac", "aac"):
        assert util.normalize_track_key(f"Song.{ext}") == "song"


def test_normalize_collapses_punctuation_and_case():
    assert util.normalize_track_key("Drum & Bass!! (2024)") == "drumbass2024"
    assert util.normalize_track_key("A_B-C") == "abc"


def test_normalize_handles_empty_and_none():
    assert util.normalize_track_key("") == ""
    assert util.normalize_track_key(None) == ""


def test_today_yyyymmdd_format():
    # today_yyyymmdd() returns date.strftime("%Y%m%d"), i.e. a compact 8-digit
    # YYYYMMDD with NO dashes (e.g. "20260530"), not an ISO "YYYY-MM-DD" string.
    val = util.today_yyyymmdd()
    assert re.fullmatch(r"\d{8}", val)
    # parses as a real date
    _dt.datetime.strptime(val, "%Y%m%d")


def test_detect_platform():
    from cratebuilder.util import detect_platform
    assert detect_platform("https://soundcloud.com/artist") == "SoundCloud"
    assert detect_platform("https://www.youtube.com/@chan") == "YouTube"
    assert detect_platform("") == "YouTube"  # default
    assert detect_platform(None) == "YouTube"


def test_push_mru_prepends_new_value():
    assert util.push_mru(["a", "b", "c"], "d", 6) == ["d", "a", "b", "c"]
    assert util.push_mru([], "a", 6) == ["a"]


def test_push_mru_dedupes_and_moves_to_front():
    assert util.push_mru(["a", "b", "c"], "b", 6) == ["b", "a", "c"]
    assert util.push_mru(["a", "b"], "a", 6) == ["a", "b"]


def test_push_mru_caps_at_limit():
    assert util.push_mru(["a", "b", "c", "d", "e", "f"], "g", 6) == \
        ["g", "a", "b", "c", "d", "e"]


def test_push_mru_does_not_mutate_input():
    items = ["a", "b"]
    util.push_mru(items, "c", 6)
    assert items == ["a", "b"]


def test_push_mru_handles_none_list():
    assert util.push_mru(None, "a", 6) == ["a"]


def test_redact_ydl_opts_redacts_cookiefile():
    out = util.redact_ydl_opts({"cookiefile": r"C:\Users\djsin\cookies.txt"})
    assert out["cookiefile"] == "<redacted>"


def test_redact_ydl_opts_redacts_cookiesfrombrowser():
    out = util.redact_ydl_opts({"cookiesfrombrowser": ("chrome", "Default", None, None)})
    assert out["cookiesfrombrowser"] == "<redacted>"


def test_redact_ydl_opts_keeps_falsy_auth_values():
    # An unset cookie value must stay falsy so the log still shows cookies
    # were NOT configured — redacting None/"" would imply they were.
    out = util.redact_ydl_opts({"cookiefile": None, "cookiesfrombrowser": ""})
    assert out["cookiefile"] is None
    assert out["cookiesfrombrowser"] == ""


def test_redact_ydl_opts_summarizes_progress_hooks():
    out = util.redact_ydl_opts({"progress_hooks": [lambda d: None, lambda d: None]})
    assert out["progress_hooks"] == "[2 hook(s)]"


def test_redact_ydl_opts_passes_through_other_keys():
    opts = {"format": "bestaudio", "outtmpl": "%(title)s.%(ext)s", "quiet": True}
    out = util.redact_ydl_opts(opts)
    assert out["format"] == "bestaudio"
    assert out["outtmpl"] == "%(title)s.%(ext)s"
    assert out["quiet"] is True


def test_redact_ydl_opts_does_not_mutate_input():
    opts = {"cookiefile": "secret.txt", "format": "bestaudio"}
    util.redact_ydl_opts(opts)
    assert opts == {"cookiefile": "secret.txt", "format": "bestaudio"}


def test_redact_ydl_opts_handles_none():
    assert util.redact_ydl_opts(None) == {}


def test_build_cookie_opts_file_method_existing(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    out = util.build_cookie_opts("Cookie File", str(f), "Firefox", "")
    assert out == {"cookiefile": str(f)}


def test_build_cookie_opts_file_method_missing_file():
    out = util.build_cookie_opts("Cookie File", r"C:\nope\cookies.txt", "Firefox", "")
    assert out == {}


def test_build_cookie_opts_file_method_empty_path():
    assert util.build_cookie_opts("Cookie File", "", "Firefox", "") == {}


def test_build_cookie_opts_browser_with_profile():
    out = util.build_cookie_opts("From Browser", "", "Chrome", "Default")
    assert out == {"cookiesfrombrowser": ("chrome", "Default")}


def test_build_cookie_opts_browser_without_profile():
    out = util.build_cookie_opts("From Browser", "", "Firefox", "")
    assert out == {"cookiesfrombrowser": ("firefox",)}


def test_build_cookie_opts_delegator(cb):
    # The monolith's _apply_cookie_opts uses the same extracted helper, so the
    # metadata / probe / download / scan cookie blocks share one source.
    assert cb.build_cookie_opts is util.build_cookie_opts


# ── derive_collection_name ────────────────────────────────────────────────────
def test_derive_collection_name_title_wins():
    assert util.derive_collection_name(
        {"title": "Real Title", "uploader": "Up", "uploader_id": "@h",
         "channel_id": "UC1"}) == "Real Title"


def test_derive_collection_name_falls_back_to_uploader():
    assert util.derive_collection_name(
        {"title": "", "uploader": "Uploader Name",
         "uploader_id": "@h", "channel_id": "UC1"}) == "Uploader Name"


def test_derive_collection_name_falls_back_to_handle_with_at_stripped():
    assert util.derive_collection_name(
        {"uploader_id": "@MyHandle", "channel_id": "UC1"}) == "MyHandle"


def test_derive_collection_name_falls_back_to_channel_id():
    assert util.derive_collection_name(
        {"channel_id": "UCabc123"}) == "UCabc123"


def test_derive_collection_name_empty_when_nothing():
    assert util.derive_collection_name({}) == ""
    assert util.derive_collection_name(None) == ""


def test_derive_collection_name_strips_videos_suffix():
    assert util.derive_collection_name(
        {"title": "Some Channel - Videos"}) == "Some Channel"
    # Suffix stripping applies to whichever fallback wins, not just title.
    assert util.derive_collection_name(
        {"uploader": "Up Chan - Videos"}) == "Up Chan"


def test_derive_collection_name_whitespace_title_falls_back_to_uploader():
    # A whitespace-only title must not short-circuit the chain — it should be
    # skipped so the next usable candidate (uploader) wins.
    assert util.derive_collection_name(
        {"title": "   ", "uploader": "Uploader Name"}) == "Uploader Name"


def test_derive_collection_name_returns_stripped_value():
    assert util.derive_collection_name(
        {"title": "  Padded Title  "}) == "Padded Title"


# ── canonical_channel_key ─────────────────────────────────────────────────────
def test_canonical_channel_key_collapses_forms_with_channel_id():
    # @handle, /channel/UC…, and …/videos forms for the same channel all
    # collapse to the same key when a UC channel_id is present.
    key_handle = util.canonical_channel_key(
        "https://www.youtube.com/@SomeHandle", channel_id="UCxyz")
    key_channel = util.canonical_channel_key(
        "https://www.youtube.com/channel/UCxyz", channel_id="UCxyz")
    key_videos = util.canonical_channel_key(
        "https://www.youtube.com/channel/UCxyz/videos", channel_id="UCxyz")
    assert key_handle == key_channel == key_videos == "yt:UCxyz"


def test_canonical_channel_key_url_normalization_without_channel_id():
    # No channel_id -> URL is normalized: host lower-cased, www. dropped,
    # trailing /videos etc. stripped, query/fragment/trailing slash removed.
    base = util.canonical_channel_key(
        "https://www.YouTube.com/@SomeHandle/videos")
    assert base == util.canonical_channel_key(
        "https://youtube.com/@SomeHandle/")
    assert base == util.canonical_channel_key(
        "http://www.youtube.com/@SomeHandle/streams?foo=bar#frag")
    assert base.startswith("url:")


def test_canonical_channel_key_is_total_and_deterministic():
    # Never throws on odd input.
    assert util.canonical_channel_key(None) == util.canonical_channel_key(None)
    assert util.canonical_channel_key("") is not None
    assert util.canonical_channel_key(
        "not a url", channel_id="") is not None


# ── find_matching_watchlist_row ───────────────────────────────────────────────
def test_find_matching_watchlist_row_by_channel_id():
    rows = [
        {"url": "https://www.youtube.com/@other", "channel_id": "UCother",
         "platform": "YouTube"},
        {"url": "https://www.youtube.com/channel/UCabc", "channel_id": "UCabc",
         "platform": "YouTube"},
    ]
    match = util.find_matching_watchlist_row(
        rows, "https://youtube.com/@something/videos", channel_id="UCabc")
    assert match is rows[1]


def test_find_matching_watchlist_row_by_exact_url():
    rows = [
        {"url": "https://www.youtube.com/@chan", "channel_id": "",
         "platform": "YouTube"},
    ]
    match = util.find_matching_watchlist_row(
        rows, "https://www.youtube.com/@chan")
    assert match is rows[0]


def test_find_matching_watchlist_row_by_canonical_key_with_channel_id():
    # Row stored under the /channel/UC… form; lookup arrives as the @handle
    # /videos form — different URLs, same channel_id collapses them.
    rows = [
        {"url": "https://www.youtube.com/channel/UCabc", "channel_id": "UCabc",
         "platform": "YouTube"},
    ]
    match = util.find_matching_watchlist_row(
        rows, "https://youtube.com/@handle/videos", channel_id="UCabc")
    assert match is rows[0]


def test_find_matching_watchlist_row_by_canonical_key_no_channel_id():
    # No channel_id on either side: URLs differ only by /videos + www, which
    # the canonical key normalizes away.
    rows = [
        {"url": "https://www.youtube.com/@handle/videos", "channel_id": "",
         "platform": "YouTube"},
    ]
    match = util.find_matching_watchlist_row(
        rows, "https://youtube.com/@handle")
    assert match is rows[0]


def test_find_matching_watchlist_row_returns_none_when_nothing_matches():
    rows = [
        {"url": "https://www.youtube.com/@one", "channel_id": "UCone",
         "platform": "YouTube"},
    ]
    assert util.find_matching_watchlist_row(
        rows, "https://www.youtube.com/@two", channel_id="UCtwo") is None
    assert util.find_matching_watchlist_row([], "https://x") is None


def test_find_matching_watchlist_row_total_on_missing_keys():
    # Rows lacking url/channel_id/platform (or being None) must not raise.
    rows = [None, {}, {"url": "https://www.youtube.com/@chan"}]
    match = util.find_matching_watchlist_row(
        rows, "https://www.youtube.com/@chan")
    assert match is rows[2]
    assert util.find_matching_watchlist_row(
        [None, {}], "https://nope") is None


# ── SoundCloud profile-handle extraction ──────────────────────────────────────
def test_soundcloud_profile_handle_from_profile_and_track_urls():
    h = util.soundcloud_profile_handle
    assert h("https://soundcloud.com/vicksleek") == "vicksleek"
    # A track URL still resolves to the artist in its first path segment.
    assert h("https://soundcloud.com/vicksleek/some-track-name") == "vicksleek"
    # www / m subdomains and trailing junk are tolerated.
    assert h("https://www.soundcloud.com/Vicksleek/") == "vicksleek"
    assert h("https://m.soundcloud.com/vicksleek/sets/ep") == "vicksleek"


def test_soundcloud_profile_handle_rejects_non_profiles():
    h = util.soundcloud_profile_handle
    assert h("https://soundcloud.com/search?q=vicksleek") is None
    assert h("https://soundcloud.com/tags/house") is None
    assert h("https://soundcloud.com/") is None
    assert h("https://youtube.com/@vicksleek") is None
    assert h("") is None
    assert h(None) is None


# ── Cross-source candidate merge ───────────────────────────────────────────────
def test_merge_soundcloud_candidates_ranks_both_sources_first():
    track_hits = [
        {"url": "https://soundcloud.com/artist-a/track-1", "title": "Artist A"},
        {"url": "https://soundcloud.com/artist-b/track-9", "title": "Artist B"},
    ]
    web_hits = [
        {"url": "https://soundcloud.com/artist-b"},      # overlaps -> 'both'
        {"url": "https://soundcloud.com/artist-c"},      # web-only
    ]
    out = util.merge_soundcloud_candidates(track_hits, web_hits)
    handles = [c["handle"] for c in out]
    # artist-b is confirmed by both sources -> first.
    assert handles[0] == "artist-b"
    assert out[0]["confidence"] == "both"
    # All three distinct artists present; track-only outranks web-only.
    assert set(handles) == {"artist-a", "artist-b", "artist-c"}
    assert handles.index("artist-a") < handles.index("artist-c")
    # Human title from the track hit is preferred over the bare handle.
    assert out[0]["title"] == "Artist B"


def test_merge_soundcloud_candidates_dedupes_and_caps():
    track_hits = [{"url": f"https://soundcloud.com/a{i}/t"} for i in range(10)]
    # Duplicate of a0 in a different URL form must collapse, not double-count.
    web_hits = [{"url": "https://soundcloud.com/a0"}]
    out = util.merge_soundcloud_candidates(track_hits, web_hits, max_results=4)
    assert len(out) == 4
    assert out[0]["handle"] == "a0" and out[0]["confidence"] == "both"
    assert len({c["handle"] for c in out}) == 4


def test_merge_soundcloud_candidates_handles_empty():
    assert util.merge_soundcloud_candidates([], []) == []
    assert util.merge_soundcloud_candidates(None, None) == []

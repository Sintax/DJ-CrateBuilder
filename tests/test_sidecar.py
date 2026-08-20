import pytest

from cratebuilder import sidecar


def test_channel_url_from_id():
    assert sidecar.channel_url_from_id("UC123") == \
        "https://www.youtube.com/channel/UC123/videos"
    assert sidecar.channel_url_from_id("") == ""


def test_sidecar_write_then_read(tmp_path):
    folder = tmp_path / "ChannelX"
    folder.mkdir()
    ok = sidecar.write_channel_sidecar(
        str(folder), channel_id="UCabc", handle="@chanx",
        display_name="Chan X", genre="DnB")
    assert ok is True
    data = sidecar.read_channel_sidecar(str(folder))
    assert data["channel_id"] == "UCabc"
    assert data["channel_url"] == "https://www.youtube.com/channel/UCabc/videos"


def test_read_sidecar_missing_returns_none(tmp_path):
    assert sidecar.read_channel_sidecar(str(tmp_path / "nope")) is None


def test_is_unresolved_truth_table():
    f = sidecar.is_unresolved_channel
    assert f({"status": "needs_resolve", "url": "x"}) is True
    assert f({"status": "idle", "url": "unresolved://YouTube/x"}) is True
    assert f({"status": "idle", "url": "has space"}) is True
    assert f(
        {"status": "idle", "url": "https://www.youtube.com/channel/UC/videos"}) is False


def test_the_monolith_keeps_no_private_copy_of_these_answers(cb):
    """Replaces two identity tests that asserted App._is_unresolved_channel and
    App._channel_id_from_url still forwarded to the module functions.

    Those staticmethods are gone — a one-line forward is not a seam, and a test
    that it forwards proves nothing a caller can observe. The module functions
    themselves are covered directly by the tests around this one. What is still
    worth pinning is that deleting them left no second answer behind: the app
    must ask cratebuilder rather than re-derive a platform, an unresolved
    verdict or a channel id of its own."""
    import inspect
    source = inspect.getsource(cb)
    for gone in ("def _detect_platform", "def _is_unresolved_channel",
                 "def _channel_id_from_url"):
        assert gone not in source, f"{gone} is back in the monolith"
    # Still asked, just no longer wrapped.
    for asked in ("detect_platform(", "is_unresolved_channel(",
                  "channel_id_from_url("):
        assert asked in source


def test_is_unresolved_platform_aware():
    from cratebuilder.sidecar import is_unresolved_channel
    # YouTube: clean canonical URL resolved; spaced one unresolved.
    assert is_unresolved_channel(
        {"platform": "YouTube", "status": "idle",
         "url": "https://www.youtube.com/channel/UCx/videos"}) is False
    assert is_unresolved_channel(
        {"platform": "YouTube", "status": "idle",
         "url": "https://www.youtube.com/@A B"}) is True
    # SoundCloud: a soundcloud.com URL is resolved (no channel-id needed)…
    assert is_unresolved_channel(
        {"platform": "SoundCloud", "status": "idle",
         "url": "https://soundcloud.com/artist"}) is False
    # …but a non-soundcloud / sentinel / bad-status one is unresolved.
    assert is_unresolved_channel(
        {"platform": "SoundCloud", "status": "needs_resolve",
         "url": "unresolved://SoundCloud/x"}) is True
    assert is_unresolved_channel(
        {"platform": "SoundCloud", "status": "idle",
         "url": "https://example.com/not-sc"}) is True


def test_failed_scan_never_makes_a_good_link_unresolved():
    # A scan that blew up (offline, rate-limited, transient extractor fault)
    # must not strand a perfectly good canonical URL as "needs Fix Link".
    f = sidecar.is_unresolved_channel
    good = "https://www.youtube.com/channel/UCx/videos"
    assert f({"status": "error", "url": good}) is False
    assert f({"status": "offline", "url": good}) is False
    assert f({"platform": "SoundCloud", "status": "offline",
              "url": "https://soundcloud.com/artist"}) is False
    # …but a bad URL is still unresolved whatever the status says.
    assert f({"status": "offline", "url": "unresolved://YouTube/x"}) is True


def test_classify_scan_error_transient():
    f = sidecar.classify_scan_error
    # yt-dlp wraps a DNS failure like this when the connection is down.
    assert f("ERROR: Unable to download webpage: <urlopen error "
             "[Errno 11001] getaddrinfo failed>") == "offline"
    assert f("Temporary failure in name resolution") == "offline"
    assert f("[Errno 101] Network is unreachable") == "offline"
    assert f("The read operation timed out") == "offline"
    assert f("Remote end closed connection without response") == "offline"
    assert f("HTTP Error 503: Service Unavailable") == "offline"
    assert f("HTTP Error 429: Too Many Requests") == "offline"


def test_classify_scan_error_permanent():
    f = sidecar.classify_scan_error
    assert f("ERROR: [youtube:tab] @gone: This channel does not exist.") \
        == "needs_resolve"
    assert f("HTTP Error 404: Not Found") == "needs_resolve"
    assert f("ERROR: Unsupported URL: https://example.com/x") == "needs_resolve"
    assert f("This playlist is private") == "needs_resolve"
    assert f("This account has been terminated") == "needs_resolve"


def test_classify_scan_error_permanent_wins_over_transient_wrapper():
    # yt-dlp prefixes a 404 with its generic "Unable to download webpage"
    # wrapper; the 404 is the real signal and must not read as offline.
    assert sidecar.classify_scan_error(
        "ERROR: Unable to download webpage: HTTP Error 404: Not Found") \
        == "needs_resolve"


def test_classify_scan_error_unknown_falls_back():
    assert sidecar.classify_scan_error("something we've never seen") == "error"
    assert sidecar.classify_scan_error("") == "error"
    assert sidecar.classify_scan_error(None) == "error"


def test_watch_scan_url():
    from cratebuilder.sidecar import watch_scan_url
    assert watch_scan_url(
        "YouTube", "https://www.youtube.com/@chan"
        ) == "https://www.youtube.com/@chan/videos"
    assert watch_scan_url(
        "YouTube", "https://www.youtube.com/channel/UCx/videos"
        ) == "https://www.youtube.com/channel/UCx/videos"
    assert watch_scan_url(
        "SoundCloud", "https://soundcloud.com/artist"
        ) == "https://soundcloud.com/artist/tracks"
    assert watch_scan_url(
        "SoundCloud", "https://soundcloud.com/artist/tracks"
        ) == "https://soundcloud.com/artist/tracks"
    assert watch_scan_url("YouTube", "") == ""


@pytest.mark.parametrize("tab", ["featured", "shorts", "releases"])
def test_watch_scan_url_replaces_non_listing_tabs(tab):
    from cratebuilder.sidecar import watch_scan_url
    assert watch_scan_url(
        "YouTube", f"https://www.youtube.com/@chan/{tab}"
        ) == "https://www.youtube.com/@chan/videos"
    assert watch_scan_url(
        "YouTube", f"https://www.youtube.com/channel/UCx/{tab}/"
        ) == "https://www.youtube.com/channel/UCx/videos"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@chan/streams",
    "https://www.youtube.com/channel/UCx/streams",
    "https://www.youtube.com/@chan/playlists",
    "https://www.youtube.com/playlist?list=PLx",
])
def test_watch_scan_url_leaves_deliberate_listings_alone(url):
    from cratebuilder.sidecar import watch_scan_url
    assert watch_scan_url("YouTube", url) == url


def test_watch_fetch_url():
    from cratebuilder.sidecar import watch_fetch_url
    # Canonical/spaceless URLs pass straight through (just the /videos tab).
    assert watch_fetch_url(
        "YouTube", "https://www.youtube.com/channel/UCx"
        ) == "https://www.youtube.com/channel/UCx/videos"
    assert watch_fetch_url(
        "SoundCloud", "https://soundcloud.com/artist"
        ) == "https://soundcloud.com/artist/tracks"
    # A handle containing a space is percent-encoded so yt-dlp doesn't
    # truncate it at the whitespace (the bug that produced 404s).
    assert watch_fetch_url(
        "YouTube", "https://www.youtube.com/@BASS ENTITY"
        ) == "https://www.youtube.com/@BASS%20ENTITY/videos"
    # Empty stays empty.
    assert watch_fetch_url("YouTube", "") == ""


def test_watch_fetch_url_is_idempotent():
    from cratebuilder.sidecar import watch_fetch_url
    # Applying watch_fetch_url twice must equal applying it once, or a stored
    # URL that is already percent-encoded gets double-encoded on the next
    # scan (garbling the path yt-dlp receives).
    cases = [
        ("YouTube", "https://www.youtube.com/channel/UCx"),
        ("YouTube", "https://www.youtube.com/@BASS ENTITY"),
        ("SoundCloud", "https://soundcloud.com/%D0%BC%D1%83%D0%B7"),
    ]
    for platform, url in cases:
        once  = watch_fetch_url(platform, url)
        twice = watch_fetch_url(platform, once)
        assert twice == once


def test_watch_fetch_url_key_agreement_for_percent_encoded_stored_url():
    # A stored URL that already contains a % (e.g. a Cyrillic handle) must
    # canonicalise to the same key whether read from the plain stored URL or
    # from the fetch URL watch_fetch_url hands to yt-dlp.
    base = "https://soundcloud.com/%D0%BC%D1%83%D0%B7"
    fetched = sidecar.watch_fetch_url("SoundCloud", base)
    assert sidecar.canonical_channel_url(fetched) == \
        sidecar.canonical_channel_url(base)


def test_channel_id_from_url():
    from cratebuilder.sidecar import channel_id_from_url
    # pulls the UC… id out of a /channel/ URL (word chars + hyphens)
    assert channel_id_from_url(
        "https://www.youtube.com/channel/UCabc123_-/videos") == "UCabc123_-"
    # round-trips with the inverse builder
    assert channel_id_from_url(sidecar.channel_url_from_id("UCxyz")) == "UCxyz"
    # no /channel/ segment, or empty/None -> None
    assert channel_id_from_url("https://www.youtube.com/@handle") is None
    assert channel_id_from_url("") is None
    assert channel_id_from_url(None) is None


def test_canonical_channel_url_strips_soundcloud_tracks_tab():
    assert (sidecar.canonical_channel_url(
        "https://soundcloud.com/ukg2025/tracks")
        == "https://soundcloud.com/ukg2025")


def test_canonical_channel_url_strips_youtube_videos_tab():
    assert (sidecar.canonical_channel_url(
        "https://www.youtube.com/@UKFDnB/videos")
        == "https://www.youtube.com/@UKFDnB")


def test_canonical_channel_url_is_idempotent():
    once = sidecar.canonical_channel_url(
        "https://soundcloud.com/ukg2025/tracks")
    assert sidecar.canonical_channel_url(once) == once


def test_canonical_channel_url_strips_trailing_slash():
    assert (sidecar.canonical_channel_url("https://soundcloud.com/ukg2025/")
            == "https://soundcloud.com/ukg2025")


def test_canonical_channel_url_leaves_plain_urls_alone():
    assert (sidecar.canonical_channel_url("https://soundcloud.com/ukg2025")
            == "https://soundcloud.com/ukg2025")


def test_canonical_channel_url_does_not_strip_a_mid_path_segment():
    # Only a trailing listing tab is a tab; "/videos" elsewhere is part of the
    # channel's own path and must survive.
    assert (sidecar.canonical_channel_url("https://example.com/videos/thing")
            == "https://example.com/videos/thing")


def test_canonical_channel_url_matches_watch_fetch_url_round_trip():
    # The invariant the whole fix rests on: whatever watch_fetch_url produces
    # for a channel must canonicalise back to that channel's stored URL.
    for platform, base in (("SoundCloud", "https://soundcloud.com/ukg2025"),
                           ("YouTube", "https://www.youtube.com/@UKFDnB"),
                           ("YouTube", "https://www.youtube.com/@BASS ENTITY")):
        fetched = sidecar.watch_fetch_url(platform, base)
        assert sidecar.canonical_channel_url(fetched) == base


def test_canonical_channel_url_handles_empty_and_none():
    assert sidecar.canonical_channel_url("") == ""
    assert sidecar.canonical_channel_url(None) == ""


def test_canonical_channel_url_decodes_percent_encoded_handle():
    # watch_fetch_url percent-encodes the path (e.g. a space in a handle) so
    # the encoded listing URL and the plain stored URL must canonicalise to
    # the identical string, or count/forget lookups miss the recorded rows.
    base = "https://www.youtube.com/@BASS ENTITY"
    fetched = sidecar.watch_fetch_url("YouTube", base)
    assert fetched == "https://www.youtube.com/@BASS%20ENTITY/videos"
    assert sidecar.canonical_channel_url(fetched) == base


def test_canonical_channel_url_key_agreement_for_space_handle():
    # Pins key agreement, not just a string shape: the write-side key (from
    # the encoded fetch URL) and the read-side key (from the plain stored
    # URL) must be the exact same string.
    base = "https://www.youtube.com/@BASS ENTITY"
    fetched = sidecar.watch_fetch_url("YouTube", base)
    write_key = sidecar.canonical_channel_url(fetched)
    read_key = sidecar.canonical_channel_url(base)
    assert write_key == read_key

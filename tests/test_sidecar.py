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
    assert f({"status": "error", "url": "x"}) is True
    assert f({"status": "idle", "url": "unresolved://YouTube/x"}) is True
    assert f({"status": "idle", "url": "has space"}) is True
    assert f(
        {"status": "idle", "url": "https://www.youtube.com/channel/UC/videos"}) is False


def test_delegator_still_works(cb):
    # The App staticmethod must keep delegating to the module function.
    App = cb.MP3DownloaderApp
    assert App._is_unresolved_channel({"status": "needs_resolve", "url": "x"}) is True
    assert App._is_unresolved_channel(
        {"status": "idle", "url": "https://www.youtube.com/channel/UC/videos"}) is False


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

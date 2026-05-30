def test_channel_url_from_id(cb):
    assert cb.channel_url_from_id("UC123") == \
        "https://www.youtube.com/channel/UC123/videos"
    assert cb.channel_url_from_id("") == ""


def test_sidecar_write_then_read(cb, tmp_path):
    folder = tmp_path / "ChannelX"
    folder.mkdir()
    ok = cb.write_channel_sidecar(
        str(folder), channel_id="UCabc", handle="@chanx",
        display_name="Chan X", genre="DnB")
    assert ok is True
    data = cb.read_channel_sidecar(str(folder))
    assert data["channel_id"] == "UCabc"
    assert data["channel_url"] == "https://www.youtube.com/channel/UCabc/videos"


def test_read_sidecar_missing_returns_none(cb, tmp_path):
    assert cb.read_channel_sidecar(str(tmp_path / "nope")) is None


def test_is_unresolved_truth_table(cb):
    App = cb.MP3DownloaderApp
    assert App._is_unresolved_channel({"status": "needs_resolve", "url": "x"}) is True
    assert App._is_unresolved_channel({"status": "error", "url": "x"}) is True
    assert App._is_unresolved_channel({"status": "idle", "url": "unresolved://YouTube/x"}) is True
    assert App._is_unresolved_channel({"status": "idle", "url": "has space"}) is True
    assert App._is_unresolved_channel(
        {"status": "idle", "url": "https://www.youtube.com/channel/UC/videos"}) is False

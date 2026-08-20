"""Main-tab URL normalisation: which YouTube channel tabs get sent to /videos."""
import pytest


@pytest.fixture(scope="module")
def normalize(cb):
    return cb.MP3DownloaderApp._normalize_url


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@chan",
    "https://www.youtube.com/@chan/",
    "https://www.youtube.com/@chan/featured",
    "https://www.youtube.com/@chan/shorts",
    "https://www.youtube.com/@chan/releases",
    "https://www.youtube.com/@chan/Featured/",
])
def test_channel_urls_point_at_videos(normalize, url):
    assert normalize(url) == "https://www.youtube.com/@chan/videos"


@pytest.mark.parametrize("url", ["https://youtube.com/@chan",
                                 "http://www.youtube.com/@chan/featured"])
def test_host_is_preserved_verbatim(normalize, url):
    assert normalize(url) == url.rstrip("/").rsplit("/@", 1)[0] + "/@chan/videos"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@chan/videos",
    "https://www.youtube.com/@chan/streams",
    "https://www.youtube.com/@chan/playlists",
    "https://www.youtube.com/watch?v=abc123",
    "https://www.youtube.com/playlist?list=PLx",
    "https://soundcloud.com/artist/tracks",
    "",
])
def test_everything_else_is_untouched(normalize, url):
    assert normalize(url) == url

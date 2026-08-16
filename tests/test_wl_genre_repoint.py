"""Changing a channel's genre has to carry its out-of-database identity with
it: the link store is keyed by Platform/Genre/DisplayName, and the channel
folder's cratebuilder.json records the genre it was downloaded under."""
import importlib.util
import os
import sys

import pytest

from cratebuilder import links, sidecar


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    try:
        app = m.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")
    app._links_path = os.path.join(str(tmp_path), links.LINKS_FILE_NAME)
    return app


CH = {"display_name": "DnB Portal", "url": "https://youtube.com/channel/UC1",
      "channel_id": "UC1", "platform": "YouTube", "genre": "Old"}


def test_link_store_is_refiled_under_the_new_genre(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])

    app._repoint_channel_genre(CH, "YouTube", "Old", "New")

    assert links.get_link(app._links_path, "YouTube", "New",
                          "DnB Portal") == CH["url"]
    # The stale key must not survive — Fix Link prefills from this store and
    # would otherwise offer the record from a genre the channel has left.
    assert links.get_link(app._links_path, "YouTube", "Old", "DnB Portal") == ""
    app.destroy()


def test_sidecar_genre_is_rewritten_in_place(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    folder = tmp_path / "New" / "DnB Portal"
    folder.mkdir(parents=True)
    sidecar.write_channel_sidecar(str(folder), channel_id="UC1",
                                  display_name="DnB Portal",
                                  platform="YouTube", genre="Old")

    app._repoint_channel_genre(CH, "YouTube", "Old", "New", folder=str(folder))

    meta = sidecar.read_channel_sidecar(str(folder))
    assert meta["genre"] == "New"
    assert meta["channel_id"] == "UC1"
    app.destroy()


def test_repoint_without_a_folder_still_moves_the_link(tmp_path, monkeypatch):
    """A channel with nothing downloaded yet has no sidecar; the link store
    still has to follow the genre."""
    app = _app(tmp_path, monkeypatch)
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])
    app._repoint_channel_genre(CH, "YouTube", "Old", "New", folder=None)
    assert links.get_link(app._links_path, "YouTube", "New",
                          "DnB Portal") == CH["url"]
    app.destroy()


def test_repoint_to_the_same_genre_keeps_the_entry(tmp_path, monkeypatch):
    """A no-op genre 'change' must not delete the key it just wrote."""
    app = _app(tmp_path, monkeypatch)
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])
    app._repoint_channel_genre(CH, "YouTube", "Old", "Old")
    assert links.get_link(app._links_path, "YouTube", "Old",
                          "DnB Portal") == CH["url"]
    app.destroy()


def test_repoint_never_crosses_platforms(tmp_path, monkeypatch):
    """A SoundCloud channel's entries stay under SoundCloud — the genre move
    only ever rewrites keys inside one platform's namespace."""
    app = _app(tmp_path, monkeypatch)
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url="https://yt")
    sc = {**CH, "platform": "SoundCloud", "url": "https://soundcloud.com/x"}
    links.save_link(app._links_path, platform="SoundCloud", genre="Old",
                    display_name="DnB Portal", url=sc["url"])

    app._repoint_channel_genre(sc, "SoundCloud", "Old", "New")

    assert links.get_link(app._links_path, "SoundCloud", "New",
                          "DnB Portal") == sc["url"]
    assert links.get_link(app._links_path, "YouTube", "Old",
                          "DnB Portal") == "https://yt"
    app.destroy()

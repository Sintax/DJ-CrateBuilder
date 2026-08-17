"""Changing a channel's genre has to carry its out-of-database identity with
it: the link store is keyed by Platform/Genre/DisplayName, and the channel
folder's cratebuilder.json records the genre it was downloaded under."""
from cratebuilder import links, sidecar


CH = {"display_name": "DnB Portal", "url": "https://youtube.com/channel/UC1",
      "channel_id": "UC1", "platform": "YouTube", "genre": "Old"}


def test_link_store_is_refiled_under_the_new_genre(app):
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])

    app._repoint_channel_genre(CH, "YouTube", "Old", "New")

    assert links.get_link(app._links_path, "YouTube", "New",
                          "DnB Portal") == CH["url"]
    # The stale key must not survive — Fix Link prefills from this store and
    # would otherwise offer the record from a genre the channel has left.
    assert links.get_link(app._links_path, "YouTube", "Old", "DnB Portal") == ""


def test_sidecar_genre_is_rewritten_in_place(tmp_path, app):
    folder = tmp_path / "New" / "DnB Portal"
    folder.mkdir(parents=True)
    sidecar.write_channel_sidecar(str(folder), channel_id="UC1",
                                  display_name="DnB Portal",
                                  platform="YouTube", genre="Old")

    app._repoint_channel_genre(CH, "YouTube", "Old", "New", folder=str(folder))

    meta = sidecar.read_channel_sidecar(str(folder))
    assert meta["genre"] == "New"
    assert meta["channel_id"] == "UC1"


def test_repoint_without_a_folder_still_moves_the_link(app):
    """A channel with nothing downloaded yet has no sidecar; the link store
    still has to follow the genre."""
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])
    app._repoint_channel_genre(CH, "YouTube", "Old", "New", folder=None)
    assert links.get_link(app._links_path, "YouTube", "New",
                          "DnB Portal") == CH["url"]


def test_repoint_to_the_same_genre_keeps_the_entry(app):
    """A no-op genre 'change' must not delete the key it just wrote."""
    links.save_link(app._links_path, platform="YouTube", genre="Old",
                    display_name="DnB Portal", url=CH["url"])
    app._repoint_channel_genre(CH, "YouTube", "Old", "Old")
    assert links.get_link(app._links_path, "YouTube", "Old",
                          "DnB Portal") == CH["url"]


def test_repoint_never_crosses_platforms(app):
    """A SoundCloud channel's entries stay under SoundCloud — the genre move
    only ever rewrites keys inside one platform's namespace."""
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

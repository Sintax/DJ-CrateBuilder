import json
import os

from cratebuilder import links


def _path(tmp_path):
    return os.path.join(str(tmp_path), links.LINKS_FILE_NAME)


def test_link_key_is_folder_shaped():
    assert links.link_key("SoundCloud", "House", "DJ Foo") == "SoundCloud/House/DJ Foo"
    assert links.link_key(" YouTube ", "  Techno ", " Bar ") == "YouTube/Techno/Bar"


def test_load_links_missing_file_returns_empty(tmp_path):
    assert links.load_links(_path(tmp_path)) == {}


def test_load_links_corrupt_file_returns_empty(tmp_path):
    p = _path(tmp_path)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert links.load_links(p) == {}


def test_save_and_get_roundtrip(tmp_path):
    p = _path(tmp_path)
    assert links.save_link(p, platform="SoundCloud", genre="House",
                           display_name="DJ Foo",
                           url="https://soundcloud.com/djfoo") is True
    assert links.get_link(p, "SoundCloud", "House", "DJ Foo") == \
        "https://soundcloud.com/djfoo"


def test_get_link_unknown_returns_empty(tmp_path):
    p = _path(tmp_path)
    links.save_link(p, platform="SoundCloud", genre="House",
                    display_name="DJ Foo", url="https://soundcloud.com/djfoo")
    assert links.get_link(p, "YouTube", "House", "DJ Foo") == ""


def test_save_link_empty_url_is_noop(tmp_path):
    p = _path(tmp_path)
    assert links.save_link(p, platform="YouTube", genre="X",
                           display_name="Y", url="") is False
    assert not os.path.exists(p)


def test_save_link_updates_existing_key(tmp_path):
    p = _path(tmp_path)
    links.save_link(p, platform="YouTube", genre="X", display_name="Y",
                    url="https://a")
    links.save_link(p, platform="YouTube", genre="X", display_name="Y",
                    url="https://b", channel_id="UC123")
    data = links.load_links(p)
    assert len(data) == 1
    entry = data[links.link_key("YouTube", "X", "Y")]
    assert entry["url"] == "https://b"
    assert entry["channel_id"] == "UC123"


def test_save_link_preserves_unicode(tmp_path):
    p = _path(tmp_path)
    links.save_link(p, platform="SoundCloud", genre="Ambient",
                    display_name="Café Del Mar", url="https://soundcloud.com/cafe")
    with open(p, encoding="utf-8") as f:
        raw = f.read()
    assert "Café Del Mar" in raw
    assert links.get_link(p, "SoundCloud", "Ambient", "Café Del Mar") == \
        "https://soundcloud.com/cafe"


def test_multiple_channels_coexist(tmp_path):
    p = _path(tmp_path)
    links.save_link(p, platform="YouTube", genre="A", display_name="One",
                    url="https://one")
    links.save_link(p, platform="SoundCloud", genre="B", display_name="Two",
                    url="https://two")
    assert links.get_link(p, "YouTube", "A", "One") == "https://one"
    assert links.get_link(p, "SoundCloud", "B", "Two") == "https://two"

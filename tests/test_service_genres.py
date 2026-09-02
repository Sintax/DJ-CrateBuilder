"""genres.create / genres.remove: the Downloads screen's + New genre button and
the channel dialogs' + New / − Remove, with the host doing the folder work.

The rules are the monolith's _add_genre and _remove_selected_genre: a folder
is made under the platform the user picked, on OK; a folder is only ever
removed when it is empty, so downloaded audio cannot be destroyed this way.
"""
import os

import pytest

from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"),
                               log_path=str(tmp_path / "activity.log"))


def test_create_makes_the_folder_under_the_platform_now(service, tmp_path):
    res = service.call("genres.create",
                       {"name": "Deep House", "platform": "YouTube"})

    assert os.path.isdir(tmp_path / "crate" / "YouTube" / "Deep House")
    assert res["genre"] == "Deep House"
    assert res["platform"] == "YouTube"
    assert res["existed"] is False
    assert "Deep House" in res["genres"]


def test_create_sanitises_the_name_as_a_folder(service, tmp_path):
    res = service.call("genres.create",
                       {"name": "  Drum/Bass? ", "platform": "SoundCloud"})

    assert res["genre"] == "Drum_Bass_"
    assert os.path.isdir(tmp_path / "crate" / "SoundCloud" / "Drum_Bass_")


def test_create_accepts_the_pickers_own_spelling(service, tmp_path):
    """The monolith's picker says "Soundcloud"; the crate says SoundCloud."""
    res = service.call("genres.create",
                       {"name": "Techno", "platform": "Soundcloud"})

    assert res["platform"] == "SoundCloud"
    assert os.path.isdir(tmp_path / "crate" / "SoundCloud" / "Techno")


def test_create_reports_an_existing_folder_rather_than_failing(service, tmp_path):
    os.makedirs(tmp_path / "crate" / "YouTube" / "House")

    res = service.call("genres.create", {"name": "House", "platform": "YouTube"})

    assert res["existed"] is True


def test_create_refuses_a_name_that_is_no_folder(service):
    for junk in ("", "   ", None):
        with pytest.raises(CBError, match="usable as a folder"):
            service.call("genres.create", {"name": junk, "platform": "YouTube"})


def test_create_refuses_without_a_platform(service):
    for junk in ("", None, "Choose Platform", "Vimeo"):
        with pytest.raises(CBError, match="Choose a platform"):
            service.call("genres.create", {"name": "House", "platform": junk})


def test_remove_deletes_only_an_empty_folder(service, tmp_path):
    os.makedirs(tmp_path / "crate" / "YouTube" / "Old")

    res = service.call("genres.remove", {"name": "Old", "platform": "YouTube"})

    assert not os.path.exists(tmp_path / "crate" / "YouTube" / "Old")
    assert "Old" not in res["genres"]
    with open(tmp_path / "activity.log", encoding="utf-8") as fh:
        assert "Removed empty genre folder" in fh.read()


def test_remove_refuses_a_folder_that_still_holds_anything(service, tmp_path):
    os.makedirs(tmp_path / "crate" / "YouTube" / "Busy" / "Some Channel")

    with pytest.raises(CBError, match="isn't empty"):
        service.call("genres.remove", {"name": "Busy", "platform": "YouTube"})

    assert os.path.isdir(tmp_path / "crate" / "YouTube" / "Busy" / "Some Channel")


def test_remove_refuses_none_and_a_folder_that_is_not_there(service):
    with pytest.raises(CBError, match="Select a genre"):
        service.call("genres.remove", {"name": "(none)", "platform": "YouTube"})
    with pytest.raises(CBError, match="No YouTube folder named 'Ghost'"):
        service.call("genres.remove", {"name": "Ghost", "platform": "YouTube"})

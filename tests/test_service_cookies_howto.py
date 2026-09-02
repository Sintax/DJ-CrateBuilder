"""cookies.howto: the Browser Cookies card's How-To button, which the tkinter
app answers with CookieHowToWindow. The text is the desktop app's own
COOKIE_HOWTO_TEXTS, read out of its source the way the About FAQ is."""
import pytest

from cratebuilder import remoteauth
from cratebuilder import service as service_mod
from cratebuilder.service import (CBError, CrateBuilderService,
                                  cookie_howto_texts)
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def test_the_texts_are_the_desktop_apps_own():
    texts = cookie_howto_texts()
    assert {"Chrome", "Firefox", "Edge", "Brave", "Opera", "Chromium"} <= set(texts)
    for browser, text in texts.items():
        assert text.startswith(f"Setting Up a Dedicated {browser} Profile"), browser
        assert "Step 1" in text


def test_the_walkthrough_names_the_browser_asked_for(service):
    page = service.call("cookies.howto", {"browser": "Firefox"})
    assert page["browser"] == "Firefox"
    assert page["title"] == "How-To: Setting Up a Dedicated Firefox Profile"
    assert page["text"].startswith("Setting Up a Dedicated Firefox Profile")


def test_a_browser_without_a_page_reads_the_chrome_one(service):
    """CookieHowToWindow's own fallback — Vivaldi is in the Browser list but
    has no walkthrough of its own."""
    page = service.call("cookies.howto", {"browser": "Vivaldi"})
    assert page["browser"] == "Vivaldi"
    assert page["text"] == cookie_howto_texts()["Chrome"]


def test_a_source_without_the_dict_is_a_refusal_not_a_blank_page(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text('ABOUT_CREATED_BY = "Someone"\n', encoding="utf-8")
    assert cookie_howto_texts(str(stub)) == {}

    monkeypatch.setattr(service_mod, "_monolith_path", lambda: str(stub))
    svc = CrateBuilderService(settings=Settings(path=str(tmp_path / "c.json")),
                              db_path=str(tmp_path / "cratebuilder.db"))
    with pytest.raises(CBError):
        svc.cookies_howto("Chrome")


def test_the_parse_is_cached_on_the_files_signature(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text('COOKIE_HOWTO_TEXTS = {"Chrome": "one"}\n', encoding="utf-8")
    assert cookie_howto_texts(str(stub)) == {"Chrome": "one"}
    assert str(stub) in service_mod._COOKIE_HOWTO_CACHE

    stub.write_text('COOKIE_HOWTO_TEXTS = {"Chrome": "two", "Edge": "e"}\n',
                    encoding="utf-8")
    assert cookie_howto_texts(str(stub)) == {"Chrome": "two", "Edge": "e"}


def test_it_is_help_text_so_a_read_only_session_may_ask(service):
    assert "cookies.howto" in remoteauth.READ_METHODS
    assert "cookies.howto" in service._methods()

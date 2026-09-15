"""CrateBuilderService: support.preview / fs.support_send (bug-report bundles)."""

import os

import pytest

from cratebuilder.service import CBError, CrateBuilderService, LOCAL, REMOTE
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    """A service pointed entirely at tmp_path."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(
        settings=settings, db_path=str(tmp_path / "cratebuilder.db"),
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"))


def test_preview_is_scrubbed(service):
    home = os.path.expanduser("~")
    with open(service._debug_log_path, "w", encoding="utf-8") as fh:
        fh.write(f"YDL OPTS | cookiefile {home}/cookies.txt token=abc\n")
    res = service.call("support.preview", {}, transport=REMOTE)
    assert home not in res["debug"]
    assert "token=<redacted>" in res["debug"]
    assert "build" in res["system"]


def test_send_refuses_empty_description(service):
    with pytest.raises(CBError):
        service.call("fs.support_send", {"title": "t", "description": "  "},
                     transport=LOCAL)


def test_send_is_local_only(service):
    with pytest.raises(CBError):
        service.call("fs.support_send", {"title": "t", "description": "x"},
                     transport=REMOTE)


def test_send_writes_bundle_and_opens_issue(service, tmp_path, monkeypatch):
    target = tmp_path / "r.zip"
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: str(target))
    opened = {}

    def fake_open_url(url):
        opened["url"] = url
        return {"opened": True}

    monkeypatch.setattr(service, "open_url", fake_open_url)
    res = service.call("fs.support_send",
                       {"title": "Scan hangs", "description": "It just sits there."},
                       transport=LOCAL)
    assert res["saved"] == str(target) and target.exists()
    assert res["opened"] is True
    assert opened["url"].startswith("https://github.com/Sintax/DJ-CrateBuilder/issues/new?")
    assert "Scan+hangs" in opened["url"]


def test_send_cancel_does_not_open_issue(service, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: None)
    opened = {}

    def fake_open_url(url):
        opened["url"] = url
        return {"opened": True}

    monkeypatch.setattr(service, "open_url", fake_open_url)
    res = service.call("fs.support_send",
                       {"title": "t", "description": "x"}, transport=LOCAL)
    assert res == {"saved": None, "opened": False}
    assert "url" not in opened


def test_preview_scrubs_os_login_name_even_when_home_folder_differs(service, monkeypatch):
    import cratebuilder.service as service_mod

    monkeypatch.setattr(service_mod.getpass, "getuser", lambda: "jsmith.CORP")
    home_basename = os.path.basename(os.path.expanduser("~"))
    assert home_basename != "jsmith.CORP"
    with open(service._log_path, "w", encoding="utf-8") as fh:
        fh.write("user jsmith.CORP started a scan\n")
    res = service.call("support.preview", {}, transport=REMOTE)
    assert "jsmith.CORP" not in res["activity"]
    assert "<USER>" in res["activity"]


def test_send_open_failure_still_reports_the_saved_path(service, tmp_path, monkeypatch):
    target = tmp_path / "r2.zip"
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: str(target))

    def raise_open(url):
        raise CBError("Could not open that link.")

    monkeypatch.setattr(service, "open_url", raise_open)
    res = service.call("fs.support_send",
                       {"title": "t", "description": "x"}, transport=LOCAL)
    assert target.exists()
    assert res == {"saved": str(target), "opened": False}

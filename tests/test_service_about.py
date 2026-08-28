"""cratebuilder.service: the About payload and fs.open_url.

About is read out of the monolith's own source rather than copied, so these
guard the parse (which breaks silently the moment a constant is renamed) and
the local-only link opener's refusals.
"""
import os

import pytest

from cratebuilder import remoteauth, service as service_mod
from cratebuilder.service import (LOCAL, REMOTE, CBError, CrateBuilderService,
                                  about_info)

MONOLITH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "DJ-CrateBuilder_v1.3.py")


@pytest.fixture
def svc(tmp_path):
    from cratebuilder.settings import Settings
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "music"))
    return CrateBuilderService(transport=LOCAL, settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"),
                               log_path=str(tmp_path / "activity.log"),
                               debug_log_path=str(tmp_path / "debug.log"))


# ── the parse ────────────────────────────────────────────────────────────────

def test_about_constants_come_from_the_monolith():
    info = about_info()
    assert info["created_by"] == "Corrupt Sintax"
    assert info["contact_email"] == "CorruptSintax@Gmail.com"
    assert info["description"] == "Vibe-Coded entirely with Claude-AI"
    assert info["github_url"].startswith("https://github.com/")
    assert info["issues_url"].startswith(info["github_url"])


def test_the_faq_is_the_desktop_app_s_own_list():
    """One FAQ, read from `_build_about_tab`. tests/test_faq.py holds the same
    shape rules against the tkinter side, so a drift would fail both."""
    rows = about_info()["faq"]
    assert len(rows) >= 30
    assert all(r["q"].startswith("Q:") for r in rows)
    assert all(r["a"].startswith("A:") for r in rows)
    assert len({r["q"] for r in rows}) == len(rows)


def test_an_unreadable_script_yields_empty_fields_not_an_error(tmp_path):
    missing = str(tmp_path / "nope.py")
    info = about_info(missing)
    assert info["faq"] == []
    assert info["created_by"] == ""


def test_a_file_with_no_faq_assignment_yields_no_rows(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text('ABOUT_CREATED_BY = "Someone"\n', encoding="utf-8")
    info = about_info(str(stub))
    assert info["created_by"] == "Someone"
    assert info["faq"] == []


def test_the_parse_is_cached_per_file_signature(tmp_path):
    """A 13k-line ast.parse per About open would be paid on every repaint."""
    stub = tmp_path / "stub.py"
    stub.write_text('ABOUT_CREATED_BY = "First"\n', encoding="utf-8")
    assert about_info(str(stub))["created_by"] == "First"
    assert str(stub) in service_mod._ABOUT_CACHE
    # A rewrite changes the signature, so the cache does not hide the edit.
    stub.write_text('ABOUT_CREATED_BY = "Second"\nX = 1\n', encoding="utf-8")
    assert about_info(str(stub))["created_by"] == "Second"


# ── the dispatch ─────────────────────────────────────────────────────────────

def test_about_info_dispatches_and_carries_the_build(svc):
    payload = svc.call("about.info")
    assert payload["created_by"] == "Corrupt Sintax"
    assert payload["avatar"] == "assets/about_avatar.png"
    assert payload["note"].startswith("*(For any bugs")
    assert payload["build"] is not None
    assert payload["build_status"] == f"You're on build {payload['build']}."
    assert payload["can_open_urls"] is True


def test_about_is_readable_from_a_remote_session(svc):
    """3n shows About in a remote session precisely so you can read the build
    number off the host — it must not be gated behind the local mount."""
    payload = svc.call("about.info", transport=REMOTE)
    assert payload["created_by"] == "Corrupt Sintax"
    assert payload["can_open_urls"] is False


def test_about_info_is_allowed_in_read_only_mode():
    assert "about.info" in remoteauth.READ_METHODS


# ── fs.open_url ──────────────────────────────────────────────────────────────

def test_open_url_opens_a_web_link_locally(svc, monkeypatch):
    opened = []
    monkeypatch.setattr(service_mod.webbrowser, "open", opened.append)
    assert svc.call("fs.open_url", {"url": "https://github.com/Sintax/DJ-CrateBuilder"})
    assert opened == ["https://github.com/Sintax/DJ-CrateBuilder"]


def test_open_url_opens_a_mailto_link(svc, monkeypatch):
    opened = []
    monkeypatch.setattr(service_mod.webbrowser, "open", opened.append)
    svc.call("fs.open_url", {"url": "mailto:CorruptSintax@Gmail.com"})
    assert opened == ["mailto:CorruptSintax@Gmail.com"]


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/System32/cmd.exe",
    "javascript:alert(1)",
    r"C:\Windows\System32\cmd.exe",
    "",
])
def test_open_url_refuses_anything_that_is_not_a_web_or_mail_link(svc, monkeypatch,
                                                                 url):
    """webbrowser.open falls through to the OS handler, where a file: URL stops
    being navigation and starts being execution."""
    opened = []
    monkeypatch.setattr(service_mod.webbrowser, "open", opened.append)
    with pytest.raises(CBError):
        svc.call("fs.open_url", {"url": url})
    assert opened == []


def test_open_url_is_refused_on_the_remote_transport(svc, monkeypatch):
    opened = []
    monkeypatch.setattr(service_mod.webbrowser, "open", opened.append)
    with pytest.raises(CBError):
        svc.call("fs.open_url", {"url": "https://example.com"},
                 transport=REMOTE)
    assert opened == []

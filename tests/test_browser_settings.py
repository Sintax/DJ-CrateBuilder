# tests/test_browser_settings.py
"""Settings ▸ Browser Integration: the receive-mode binding and the
registry-backed handler toggle."""
import pytest

from cratebuilder import protocolreg
from cratebuilder import ui_strings
from cratebuilder.service import (BROWSER_HANDLER_KEY, RECEIVE_MODE_QUIET,
                                  RECEIVE_MODE_WINDOW, CBError,
                                  CrateBuilderService)
from cratebuilder.settings import Settings


@pytest.fixture
def settings(tmp_path):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(tmp_path / "crate"))
    return s


@pytest.fixture
def service(settings, tmp_path):
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


@pytest.fixture
def fake_registry(monkeypatch):
    """protocolreg over a flag instead of HKCU."""
    state = {"registered": False, "fail": False}
    monkeypatch.setattr(protocolreg, "protocol_is_registered",
                        lambda: state["registered"])

    def _register():
        if state["fail"]:
            return False
        state["registered"] = True
        return True

    def _unregister():
        if state["fail"]:
            return False
        state["registered"] = False
        return True

    monkeypatch.setattr(protocolreg, "register_protocol", _register)
    monkeypatch.setattr(protocolreg, "unregister_protocol", _unregister)
    return state


def test_the_contract_draws_both_rows_in_their_own_section():
    rows = {e["key"]: e for e in ui_strings.SETTINGS_KEYS}
    assert rows["browser_handler"]["section"] == "Browser Integration"
    assert rows["browser_handler"]["type"] == "bool"
    assert rows["browser_handler"]["platform"] == "win32"
    assert rows["browser_receive_mode"]["section"] == "Browser Integration"
    assert rows["browser_receive_mode"]["options"] == [
        "Bring window forward", "Collect quietly"]
    for key in ("settings.browser_handler", "settings.browser_receive_mode",
                "settings.browser_integration", "wl.browser_inbox"):
        assert ui_strings.TOOLTIPS.get(key), key


def test_receive_mode_reads_as_display_form(service, settings):
    assert service.settings_get("browser_receive_mode")["value"] == "Bring window forward"
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    assert service.settings_get("browser_receive_mode")["value"] == "Collect quietly"


def test_receive_mode_writes_stored_form(service, settings):
    service.settings_set("browser_receive_mode", "Collect quietly")
    assert settings.get("browser_receive_mode") == RECEIVE_MODE_QUIET
    service.settings_set("browser_receive_mode", "Bring window forward")
    assert settings.get("browser_receive_mode") == RECEIVE_MODE_WINDOW


def test_receive_mode_rejects_an_unknown_value(service):
    with pytest.raises(CBError):
        service.settings_set("browser_receive_mode", "Teleport")


def test_settings_all_serves_both_keys(service, fake_registry):
    values = service.settings_all()
    assert values["browser_receive_mode"] == "Bring window forward"
    assert values[BROWSER_HANDLER_KEY] is False
    fake_registry["registered"] = True
    assert service.settings_all()[BROWSER_HANDLER_KEY] is True


def test_handler_toggle_registers_and_unregisters(service, fake_registry):
    assert service.settings_set(BROWSER_HANDLER_KEY, True) == {
        "key": BROWSER_HANDLER_KEY, "value": True}
    assert fake_registry["registered"] is True
    assert service.settings_set(BROWSER_HANDLER_KEY, False)["value"] is False
    assert fake_registry["registered"] is False


def test_handler_toggle_refuses_when_the_registry_write_fails(service, fake_registry):
    fake_registry["fail"] = True
    with pytest.raises(CBError):
        service.settings_set(BROWSER_HANDLER_KEY, True)
    assert fake_registry["registered"] is False


def test_handler_toggle_is_local_only(settings, tmp_path, fake_registry):
    remote = CrateBuilderService(transport="remote", settings=settings,
                                 db_path=str(tmp_path / "db.sqlite"))
    with pytest.raises(CBError):
        remote.settings_set(BROWSER_HANDLER_KEY, True)
    assert fake_registry["registered"] is False

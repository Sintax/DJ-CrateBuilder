"""Remote Access parked: the kill switch in cratebuilder/remoteauth.py.

While REMOTE_ACCESS_AVAILABLE is False the "enabled" flag reads as off no
matter what the token store says, the three remote toggles and pairing are
refused with the in-development reason, and both frontends are told so.
"""
import pytest

from cratebuilder import remoteauth, server
from cratebuilder.remoteauth import RemoteState
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


@pytest.fixture
def parked(monkeypatch):
    monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", False)


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def test_enabled_reads_off_even_when_the_file_says_on(tmp_path, parked):
    state = RemoteState(str(tmp_path / "remote.json"))
    state.set_flag("enabled", True)
    assert state.get_flag("enabled") is False
    assert state.config()["enabled"] is False
    # The other two flags are untouched — the user's choice survives the park.
    assert state.get_flag("require_pairing") is True


def test_the_saved_choice_comes_back_when_the_switch_flips(tmp_path, monkeypatch):
    state = RemoteState(str(tmp_path / "remote.json"))
    state.set_flag("enabled", True)
    monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", False)
    assert state.get_flag("enabled") is False
    monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", True)
    assert state.get_flag("enabled") is True


def test_lan_bind_is_refused_while_parked(tmp_path, parked):
    state = RemoteState(str(tmp_path / "remote.json"))
    state.set_flag("enabled", True)
    assert server.bind_host(state, lan=True) is None
    assert server.bind_host(state, lan=False) == server.LOOPBACK


def test_remote_toggles_and_pairing_are_refused(service, parked):
    for key in ("remote_enabled", "remote_require_pairing", "remote_read_only"):
        with pytest.raises(CBError, match="still in development"):
            service.call("settings.set", {"key": key, "value": True})
    with pytest.raises(CBError, match="still in development"):
        service.call("remote.pair_begin", {})


def test_local_settings_still_work(service, parked):
    assert service.call("settings.set", {"key": "notify_errors", "value": False}) == {
        "key": "notify_errors", "value": False}


def test_both_frontends_are_told(service, parked):
    assert service.call("state.snapshot", {})["host"]["remote_available"] is False
    cfg = service.call("remote.config", {})
    assert cfg["available"] is False
    assert cfg["enabled"] is False


def test_the_switch_is_off_in_the_shipped_module():
    src = open(remoteauth.__file__, encoding="utf-8").read()
    assert "\nREMOTE_ACCESS_AVAILABLE = False\n" in src

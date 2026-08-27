"""SETTINGS_BINDINGS: the contract-display <-> stored-value translation layer."""

import sys

import pytest

from cratebuilder import service as cb_service
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


class _FakeReg:
    """Minimal in-memory stand-in for winreg (mirrors tests/test_startup.py)."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1; KEY_SET_VALUE = 2; REG_SZ = 1
    def __init__(self): self.store = {}
    def OpenKey(self, root, path, res=0, access=0): return ("k", path)
    def QueryValueEx(self, key, name):
        if name in self.store: return (self.store[name], self.REG_SZ)
        raise FileNotFoundError(name)
    def SetValueEx(self, key, name, r, t, val): self.store[name] = val
    def DeleteValue(self, key, name): self.store.pop(name, None)
    def CloseKey(self, key): pass


class _BrokenReg(_FakeReg):
    def OpenKey(self, *a, **k):
        raise OSError("access denied")


@pytest.fixture
def settings(tmp_path):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(tmp_path / "crate"))
    return s


@pytest.fixture
def service(settings, tmp_path):
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


# ── bitrate_quality: "192" <-> "192 kbps" ───────────────────────────────────

def test_bitrate_quality_reads_as_display_form(service, settings):
    settings.set("bitrate_quality", "320")
    assert service.settings_get("bitrate_quality")["value"] == "320 kbps"


def test_bitrate_quality_writes_stored_form(service, settings):
    service.settings_set("bitrate_quality", "320 kbps")
    assert settings.get("bitrate_quality") == "320"


# ── auto_dl_interval -> auto_download_interval (renamed key, same values) ──

def test_auto_dl_interval_reads_the_renamed_schema_key(service, settings):
    settings.set("auto_download_interval", "6 hours")
    assert service.settings_get("auto_dl_interval")["value"] == "6 hours"


def test_auto_dl_interval_writes_the_renamed_schema_key(service, settings):
    service.settings_set("auto_dl_interval", "2 days")
    assert settings.get("auto_download_interval") == "2 days"


# ── log_limit -> log_max_mb (int MB, 0 = unlimited) ─────────────────────────

@pytest.mark.parametrize("display,mb", [("1 MB", 1), ("5 MB", 5),
                                         ("10 MB", 10), ("50 MB", 50)])
def test_log_limit_writes_int_mb(service, settings, display, mb):
    service.settings_set("log_limit", display)
    assert settings.get("log_max_mb") == mb


def test_log_limit_unlimited_writes_zero(service, settings):
    service.settings_set("log_limit", "Unlimited")
    assert settings.get("log_max_mb") == 0


def test_log_limit_zero_reads_as_unlimited(service, settings):
    settings.set("log_max_mb", 0)
    assert service.settings_get("log_limit")["value"] == "Unlimited"


def test_log_limit_reads_an_off_grid_stored_value(service, settings):
    # The schema default (2 MB) isn't one of the contract's five choices —
    # the binding must still show it rather than raising or going blank.
    settings.set("log_max_mb", 2)
    assert service.settings_get("log_limit")["value"] == "2 MB"


def test_log_limit_rejects_garbage(service):
    with pytest.raises(CBError):
        service.settings_set("log_limit", "not a size")


# ── sleep_preset: contract name <-> the full stored label ──────────────────

def test_sleep_preset_reads_the_short_name(service, settings):
    settings.set("sleep_preset", "Moderate  (3\u20138 s)")
    assert service.settings_get("sleep_preset")["value"] == "Moderate"


def test_sleep_preset_writes_the_full_stored_label(service, settings):
    service.settings_set("sleep_preset", "Aggressive")
    assert settings.get("sleep_preset") == "Aggressive  (5\u201315 s)"


def test_sleep_preset_rejects_unknown_name(service):
    with pytest.raises(CBError):
        service.settings_set("sleep_preset", "Extreme")


# ── cover_art_mode: 'crop'/'original'/'off' <-> the design's three options ─

@pytest.mark.parametrize("stored,display", [
    ("crop", "On ~ Crop to square"),
    ("original", "On ~ Keep original aspect"),
    ("off", "Off"),
])
def test_cover_art_mode_round_trips(service, settings, stored, display):
    settings.set("cover_art_mode", stored)
    assert service.settings_get("cover_art_mode")["value"] == display
    service.settings_set("cover_art_mode", display)
    assert settings.get("cover_art_mode") == stored


def test_cover_art_mode_rejects_unknown_display(service):
    with pytest.raises(CBError):
        service.settings_set("cover_art_mode", "Sideways")


# ── cookie_method: stored "Browser" <-> contract "Browser Profile" ─────────

def test_cookie_method_reads_browser_profile(service, settings):
    settings.set("cookie_method", "Browser")
    assert service.settings_get("cookie_method")["value"] == "Browser Profile"


def test_cookie_method_writes_bare_browser(service, settings):
    service.settings_set("cookie_method", "Browser Profile")
    assert settings.get("cookie_method") == "Browser"


def test_cookie_method_cookie_file_is_unchanged(service, settings):
    service.settings_set("cookie_method", "Cookie File")
    assert settings.get("cookie_method") == "Cookie File"
    assert service.settings_get("cookie_method")["value"] == "Cookie File"


# ── identity keys still pass straight through ───────────────────────────────

def test_skip_mode_is_an_identity_binding(service, settings):
    service.settings_set("skip_mode", "In Database Only")
    assert settings.get("skip_mode") == "In Database Only"
    assert service.settings_get("skip_mode")["value"] == "In Database Only"


# ── settings_all returns display values ─────────────────────────────────────

def test_settings_all_returns_display_values(service, settings):
    settings.update({
        "bitrate_quality": "256",
        "log_max_mb": 5,
        "sleep_preset": "Light  (1\u20135 s)",
        "cover_art_mode": "original",
        "cookie_method": "Browser",
    })
    values = service.settings_all()
    assert values["bitrate_quality"] == "256 kbps"
    assert values["log_limit"] == "5 MB"
    assert values["sleep_preset"] == "Light"
    assert values["cover_art_mode"] == "On ~ Keep original aspect"
    assert values["cookie_method"] == "Browser Profile"


# ── base_dir validation ──────────────────────────────────────────────────────

def test_base_dir_rejects_a_file_path(service, tmp_path):
    f = tmp_path / "not_a_folder.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(CBError):
        service.settings_set("base_dir", str(f))


def test_base_dir_rejects_a_path_under_a_file(service, tmp_path):
    f = tmp_path / "blocking_file"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(CBError):
        service.settings_set("base_dir", str(f / "subdir"))


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows concept")
def test_base_dir_rejects_a_nonexistent_drive(service):
    with pytest.raises(CBError):
        service.settings_set("base_dir", r"Q:\definitely-not-a-real-drive\crate")


def test_base_dir_rejects_blank(service):
    with pytest.raises(CBError):
        service.settings_set("base_dir", "   ")


def test_base_dir_creates_and_canonicalizes_a_new_folder(service, settings, tmp_path):
    target = tmp_path / "new_crate_root"
    result = service.settings_set("base_dir", str(target))
    assert target.is_dir()
    assert result["value"] == settings.get("base_dir")


# ── run_at_startup: local-transport-only, registry-backed ──────────────────

def test_run_at_startup_refused_on_remote_transport(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    with pytest.raises(CBError):
        remote.settings_set("run_at_startup", True)
    # Refused before ever touching the registry — no fake winreg needed.


def test_run_at_startup_writes_the_registry_on_success(service, settings, monkeypatch):
    fake = _FakeReg()
    monkeypatch.setattr(cb_service.startup, "winreg", fake, raising=False)
    monkeypatch.setattr(cb_service.startup, "_startup_command", lambda: '"C:/app.exe"')
    monkeypatch.setattr(cb_service.sys, "platform", "win32")
    result = service.settings_set("run_at_startup", True)
    assert result["value"] is True
    assert settings.get("run_at_startup") is True
    assert cb_service.startup.startup_is_enabled() is True


def test_run_at_startup_registry_failure_is_not_persisted(service, settings, monkeypatch):
    monkeypatch.setattr(cb_service.startup, "winreg", _BrokenReg(), raising=False)
    monkeypatch.setattr(cb_service.sys, "platform", "win32")
    with pytest.raises(CBError):
        service.settings_set("run_at_startup", True)
    assert settings.get("run_at_startup") is False


def test_run_at_startup_off_windows_just_persists(service, settings, monkeypatch):
    # Off-Windows the registry is never touched — no fake winreg installed,
    # so a stray call here would blow up rather than silently pass.
    monkeypatch.setattr(cb_service.sys, "platform", "linux")
    result = service.settings_set("run_at_startup", True)
    assert result["value"] is True
    assert settings.get("run_at_startup") is True


# ── unknown contract keys still error cleanly ───────────────────────────────

def test_unknown_contract_key_raises_cberror_not_keyerror(service):
    with pytest.raises(CBError):
        service.settings_set("totally_made_up", 1)
    with pytest.raises(CBError):
        service.settings_get("totally_made_up")

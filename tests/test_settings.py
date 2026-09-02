import dataclasses
import json
import os
import threading

import pytest

from cratebuilder import settings as cb_settings
from cratebuilder import util
from cratebuilder.settings import (
    AutomationConfig, CookieConfig, DownloadPolicy, Settings,
)


@pytest.fixture
def cfg_path(tmp_path):
    return str(tmp_path / "cfg.json")


def _read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Schema defaults ──────────────────────────────────────────────────────────

EXPECTED_DEFAULTS = {
    "skip_existing": True,
    "skip_mode": "In Folder Only",
    "limit_enabled": True,
    "limit_minutes": 8,
    "bitrate_quality": "192",
    "bitrate_auto_upgrade": False,
    "no_conversion": False,
    "cover_art_enabled": True,
    "cover_art_mode": "crop",
    "log_max_mb": 2,
    "geo_bypass": True,
    "rotate_ua": True,
    "sleep_enabled": True,
    "sleep_mode": "Auto",
    "sleep_preset": "Light  (1–5 s)",
    "sleep_min": 1,
    "sleep_max": 5,
    "use_cookies": False,
    "cookie_method": "Browser",
    "cookies_browser": "Firefox",
    "cookies_profile": "",
    "cookie_file": "",
    "auto_add_to_watchlist": True,
    "auto_download_interval": "1 day",
    "run_at_startup": False,
    "minimize_to_tray": True,
    "start_minimized": False,
    "watchlist_scan_on_startup": True,
    "watchlist_last_download": 0,
    "update_check_interval": "6 hours",
    "last_update_check": 0.0,
    "url_history": [],
    "dupe_check_enabled": True,
    "notify_scan_found": True,
    "notify_batch_done": True,
    "notify_errors": True,
    "window_geometry": "",
    "window_maximized": False,
    "db_dl_col_widths": None,
    "db_wl_col_widths": None,
    "db_art_col_widths": None,
    "db_dl_col_order": None,
    "db_wl_col_order": None,
    "db_art_col_order": None,
}


def test_every_schema_default(cfg_path, tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    s = Settings(path=cfg_path)
    for key, want in EXPECTED_DEFAULTS.items():
        assert s.get(key) == want, key
    assert s.get("base_dir") == os.path.join(
        str(tmp_path), "Music", "DJ-CrateBuilder")
    assert (set(cb_settings._schema_defaults())
            == set(EXPECTED_DEFAULTS) | {"base_dir"})


def test_defaults_are_not_persisted_until_set(cfg_path):
    s = Settings(path=cfg_path)
    assert not os.path.exists(cfg_path)
    s.set("skip_existing", False)
    assert _read_file(cfg_path) == {"skip_existing": False}


# ── get / set basics ─────────────────────────────────────────────────────────

def test_get_rejects_unknown_key(cfg_path):
    with pytest.raises(KeyError):
        Settings(path=cfg_path).get("mystery")


def test_set_rejects_unknown_key(cfg_path):
    s = Settings(path=cfg_path)
    with pytest.raises(KeyError):
        s.set("mystery", 1)
    assert not os.path.exists(cfg_path)


def test_legacy_only_keys_are_not_schema_keys(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"auto_check_hours": "12 hours",
                   "watchlist_last_check": 123,
                   "watchlist_import_prompted": True}, f)
    s = Settings(path=cfg_path)
    for key in ("auto_check_hours", "watchlist_last_check",
                "watchlist_import_prompted"):
        with pytest.raises(KeyError):
            s.get(key)


def test_set_then_get_roundtrip_and_reload(cfg_path):
    s = Settings(path=cfg_path)
    s.set("limit_minutes", 20)
    assert s.get("limit_minutes") == 20
    assert Settings(path=cfg_path).get("limit_minutes") == 20


def test_get_returns_a_copy(cfg_path):
    s = Settings(path=cfg_path)
    s.get("url_history").append("http://x")
    assert s.get("url_history") == []
    s.set("url_history", ["a"])
    s.get("url_history").append("b")
    assert s.get("url_history") == ["a"]


# ── Legacy migrations ────────────────────────────────────────────────────────

def test_file_rename_probe_reads_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    legacy = tmp_path / ".yt_dj_cratebuilder_config.json"
    legacy.write_text(json.dumps({"limit_minutes": 42}), encoding="utf-8")
    s = Settings()
    assert s.path == str(tmp_path / ".cratebuilder" / "config.json")
    assert s.get("limit_minutes") == 42
    # Copied forward at load, as the old load_config did, so the rename only
    # has to be discovered once. The legacy file is shelved into the folder —
    # kept, but out of the home directory.
    assert _read_file(s.path) == {"limit_minutes": 42}
    s.set("skip_existing", False)
    assert _read_file(s.path) == {"limit_minutes": 42, "skip_existing": False}
    shelved = tmp_path / ".cratebuilder" / "legacy" / "yt_dj_cratebuilder_config.json"
    assert _read_file(str(shelved)) == {"limit_minutes": 42}
    assert not legacy.exists()


def test_file_rename_probe_skipped_for_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    legacy = tmp_path / ".yt_dj_cratebuilder_config.json"
    legacy.write_text(json.dumps({"limit_minutes": 42}), encoding="utf-8")
    s = Settings(path=str(tmp_path / "elsewhere.json"))
    assert s.get("limit_minutes") == 8
    # An explicit path — every test's — leaves the home directory alone.
    assert legacy.exists()
    assert not (tmp_path / ".cratebuilder").exists()


# ── The home directory is tidied into ~/.cratebuilder ────────────────────────

def _home(tmp_path, monkeypatch, **files):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_the_previous_config_moves_into_the_folder_as_config_json(tmp_path, monkeypatch):
    """The whole point: nothing of ours is left in the home directory."""
    home = _home(tmp_path, monkeypatch,
                 **{".dj_cratebuilder_config.json": {"limit_minutes": 17}})
    s = Settings()
    assert s.path == str(home / ".cratebuilder" / "config.json")
    assert s.get("limit_minutes") == 17
    assert _read_file(s.path) == {"limit_minutes": 17}
    assert not (home / ".dj_cratebuilder_config.json").exists()
    assert not (home / ".cratebuilder" / "legacy").exists()


def test_every_older_name_is_shelved_not_deleted(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, **{
        ".dj_cratebuilder_config.json": {"limit_minutes": 17},
        ".yt_dj_cratebuilder_config.json": {"limit_minutes": 42},
        ".audio_downloader_config.json": {"base_dir": "C:/old"},
    })
    moves = util.tidy_home_config()
    assert [os.path.basename(src) for src, _ in moves] == [
        ".dj_cratebuilder_config.json", ".yt_dj_cratebuilder_config.json",
        ".audio_downloader_config.json"]
    legacy = home / ".cratebuilder" / "legacy"
    assert _read_file(str(home / ".cratebuilder" / "config.json")) == {"limit_minutes": 17}
    assert _read_file(str(legacy / "yt_dj_cratebuilder_config.json")) == {"limit_minutes": 42}
    assert _read_file(str(legacy / "audio_downloader_config.json")) == {"base_dir": "C:/old"}
    assert not any(name.startswith(".") and "config" in name
                   for name in os.listdir(home))
    # The oldest name is shelved, never read: it predates every key.
    assert Settings().get("limit_minutes") == 17


def test_a_config_json_already_there_wins_over_a_stray_home_file(tmp_path, monkeypatch):
    """An older build run after the tidy writes a fresh default config into
    the home directory. It must not replace the real one — it is shelved,
    and a second stray gets a counter rather than clobbering the first."""
    home = _home(tmp_path, monkeypatch)
    (home / ".cratebuilder").mkdir()
    (home / ".cratebuilder" / "config.json").write_text(
        json.dumps({"limit_minutes": 17}), encoding="utf-8")
    for n in (1, 2):
        (home / ".dj_cratebuilder_config.json").write_text(
            json.dumps({"limit_minutes": 8, "stray": n}), encoding="utf-8")
        assert Settings().get("limit_minutes") == 17
    legacy = home / ".cratebuilder" / "legacy"
    assert _read_file(str(legacy / "dj_cratebuilder_config.json"))["stray"] == 1
    assert _read_file(str(legacy / "dj_cratebuilder_config-2.json"))["stray"] == 2
    assert not (home / ".dj_cratebuilder_config.json").exists()


def test_a_clean_home_directory_is_left_exactly_as_it_is(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert util.tidy_home_config() == []
    assert not (home / ".cratebuilder").exists()
    # The folder appears with the first write, not the first read.
    s = Settings()
    assert not (home / ".cratebuilder").exists()
    s.set("skip_existing", False)
    assert _read_file(str(home / ".cratebuilder" / "config.json")) == {
        "skip_existing": False}


def test_auto_check_hours_seeds_auto_download_interval(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"auto_check_hours": "12 hours"}, f)
    s = Settings(path=cfg_path)
    assert s.get("auto_download_interval") == "12 hours"
    assert _read_file(cfg_path) == {"auto_check_hours": "12 hours"}
    s.set("skip_existing", True)
    on_disk = _read_file(cfg_path)
    assert on_disk["auto_download_interval"] == "12 hours"
    assert on_disk["auto_check_hours"] == "12 hours"


def test_auto_check_hours_does_not_override_existing_interval(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"auto_check_hours": "12 hours",
                   "auto_download_interval": "1 week"}, f)
    assert Settings(path=cfg_path).get("auto_download_interval") == "1 week"


@pytest.mark.parametrize("old,new", [
    ("In Logs ~ In Folder", "In Database ~ In Folder"),
    ("In Logs Only", "In Database Only"),
])
def test_skip_mode_value_renames(cfg_path, old, new):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"skip_mode": old}, f)
    s = Settings(path=cfg_path)
    assert s.get("skip_mode") == new
    assert _read_file(cfg_path) == {"skip_mode": old}
    s.set("limit_enabled", True)
    assert _read_file(cfg_path)["skip_mode"] == new


def test_cover_art_off_splits_into_flag_plus_mode(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"cover_art_mode": "off"}, f)
    s = Settings(path=cfg_path)
    assert s.get("cover_art_enabled") is False
    assert s.get("cover_art_mode") == "crop"
    assert _read_file(cfg_path) == {"cover_art_mode": "off"}
    s.set("limit_enabled", True)
    on_disk = _read_file(cfg_path)
    assert on_disk["cover_art_enabled"] is False
    assert on_disk["cover_art_mode"] == "crop"


def test_cover_art_off_keeps_explicit_enabled_flag(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"cover_art_mode": "off", "cover_art_enabled": True}, f)
    s = Settings(path=cfg_path)
    assert s.get("cover_art_enabled") is True
    assert s.get("cover_art_mode") == "crop"


def test_cover_art_mode_is_normalised(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"cover_art_mode": "Original"}, f)
    s = Settings(path=cfg_path)
    assert s.get("cover_art_mode") == "original"
    assert s.get("cover_art_enabled") is True


# ── Unknown-key preservation ─────────────────────────────────────────────────

def test_unknown_keys_survive_a_write(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"mystery": {"a": 1}, "watchlist_import_prompted": True,
                   "limit_minutes": 5}, f)
    s = Settings(path=cfg_path)
    s.set("limit_minutes", 9)
    on_disk = _read_file(cfg_path)
    assert on_disk["mystery"] == {"a": 1}
    assert on_disk["watchlist_import_prompted"] is True
    assert on_disk["limit_minutes"] == 9


# ── Atomic write ─────────────────────────────────────────────────────────────

def test_write_goes_through_temp_file_and_replace(cfg_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        with open(src, "r", encoding="utf-8") as f:
            seen.append((json.load(f), src, dst))
        real_replace(src, dst)

    s = Settings(path=cfg_path)
    monkeypatch.setattr(cb_settings.os, "replace", spy)
    s.set("skip_existing", False)
    assert len(seen) == 1
    payload, src, dst = seen[0]
    assert payload == {"skip_existing": False}
    assert src != cfg_path and dst == cfg_path
    assert not os.path.exists(src)


def test_unserializable_value_is_rejected_before_it_reaches_the_store(cfg_path):
    # Committing a value json.dump can't write would make every later write
    # fail too, silently losing all config changes until restart.
    s = Settings(path=cfg_path)
    s.set("limit_minutes", 11)
    with pytest.raises(TypeError):
        s.set("cookie_file", object())
    assert s.get("cookie_file") == ""
    s.set("sleep_min", 4)
    assert _read_file(cfg_path) == {"limit_minutes": 11, "sleep_min": 4}
    assert os.listdir(os.path.dirname(cfg_path)) == [
        os.path.basename(cfg_path)]


def test_write_failure_is_swallowed_and_store_stays_consistent(
        cfg_path, monkeypatch):
    s = Settings(path=cfg_path)
    s.set("limit_minutes", 11)
    monkeypatch.setattr(cb_settings.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("nope")))
    s.set("sleep_min", 4)  # disk write fails, app must not crash
    assert s.get("sleep_min") == 4
    assert _read_file(cfg_path) == {"limit_minutes": 11}
    assert os.listdir(os.path.dirname(cfg_path)) == [
        os.path.basename(cfg_path)]


# ── update() ─────────────────────────────────────────────────────────────────

def test_update_persists_once(cfg_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append(dst)
        real_replace(src, dst)

    s = Settings(path=cfg_path)
    monkeypatch.setattr(cb_settings.os, "replace", spy)
    s.update({"skip_existing": False, "limit_minutes": 3, "sleep_min": 2})
    assert calls == [cfg_path]
    on_disk = _read_file(cfg_path)
    assert on_disk == {"skip_existing": False, "limit_minutes": 3,
                       "sleep_min": 2}


def test_update_rejects_unknown_key_before_applying(cfg_path):
    s = Settings(path=cfg_path)
    with pytest.raises(KeyError):
        s.update({"limit_minutes": 3, "mystery": 1})
    assert s.get("limit_minutes") == 8
    assert not os.path.exists(cfg_path)


# ── Thread safety ────────────────────────────────────────────────────────────

def test_concurrent_sets_lose_no_keys(cfg_path):
    s = Settings(path=cfg_path)
    keys = ["limit_minutes", "sleep_min", "sleep_max", "log_max_mb",
            "skip_existing", "geo_bypass", "rotate_ua", "use_cookies"]

    def worker(key, value):
        for _ in range(20):
            s.set(key, value)

    threads = [threading.Thread(target=worker, args=(k, i))
               for i, k in enumerate(keys)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = _read_file(cfg_path)
    for i, k in enumerate(keys):
        assert on_disk[k] == i, k
        assert s.get(k) == i, k


# ── Snapshots ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls,fields", [
    (CookieConfig, ("use_cookies", "cookie_method", "cookies_browser",
                    "cookies_profile", "cookie_file")),
    (DownloadPolicy, ("skip_existing", "skip_mode", "limit_enabled",
                      "limit_minutes", "bitrate_quality",
                      "bitrate_auto_upgrade", "no_conversion",
                      "sleep_enabled", "sleep_mode", "sleep_preset",
                      "sleep_min", "sleep_max", "geo_bypass", "rotate_ua",
                      "cover_art_enabled", "cover_art_mode")),
    (AutomationConfig, ("auto_download_interval", "watchlist_scan_on_startup",
                        "minimize_to_tray", "start_minimized",
                        "auto_add_to_watchlist", "update_check_interval")),
])
def test_snapshot_dataclasses_frozen_with_exact_fields(cls, fields):
    assert tuple(f.name for f in dataclasses.fields(cls)) == fields
    instance = cls(**{f: None for f in fields})
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, fields[0], "x")


def test_snapshot_builders_reflect_current_values(cfg_path):
    s = Settings(path=cfg_path)
    s.update({"use_cookies": True, "cookies_browser": "Chrome",
              "skip_mode": "In Database Only", "sleep_min": 4,
              "cover_art_enabled": False,
              "auto_download_interval": "1 week"})
    cookies = s.cookie_config()
    assert cookies.use_cookies is True
    assert cookies.cookies_browser == "Chrome"
    assert cookies.cookie_method == "Browser"
    policy = s.download_policy()
    assert policy.skip_mode == "In Database Only"
    assert policy.sleep_min == 4
    assert policy.cover_art_enabled is False
    assert policy.bitrate_quality == "192"
    auto = s.automation_config()
    assert auto.auto_download_interval == "1 week"
    assert auto.minimize_to_tray is True

from cratebuilder import util


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(util, "_config_path", lambda: str(cfg_file))
    util.save_config({"base_dir": "X", "auto_add_to_watchlist": False})
    loaded = util.load_config()
    assert loaded["base_dir"] == "X"
    assert loaded["auto_add_to_watchlist"] is False


def test_watchlist_scan_on_startup_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(util, "_config_path", lambda: str(cfg_file))
    # Missing key falls back to the True default at the call site.
    assert util.load_config().get("watchlist_scan_on_startup", True) is True
    # An explicit False round-trips faithfully.
    util.save_config({"watchlist_scan_on_startup": False})
    assert util.load_config()["watchlist_scan_on_startup"] is False


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    cfg_file = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(util, "_config_path", lambda: str(cfg_file))
    # ensure no legacy file interferes
    monkeypatch.setattr(util.os.path, "expanduser", lambda p: str(tmp_path))
    assert util.load_config() == {}


def test_save_is_indented_json(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(util, "_config_path", lambda: str(cfg_file))
    util.save_config({"a": 1})
    text = cfg_file.read_text(encoding="utf-8")
    assert "\n" in text  # indent=2 produces multi-line output

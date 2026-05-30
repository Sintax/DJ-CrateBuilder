def test_save_then_load_roundtrip(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    cb.save_config({"base_dir": "X", "auto_add_to_watchlist": False})
    loaded = cb.load_config()
    assert loaded["base_dir"] == "X"
    assert loaded["auto_add_to_watchlist"] is False


def test_load_missing_returns_empty(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    # ensure no legacy file interferes
    monkeypatch.setattr(cb.os.path, "expanduser", lambda p: str(tmp_path))
    assert cb.load_config() == {}


def test_save_is_indented_json(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    cb.save_config({"a": 1})
    text = cfg_file.read_text(encoding="utf-8")
    assert "\n" in text  # indent=2 produces multi-line output

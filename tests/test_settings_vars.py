import importlib.util, os, sys, time, pytest

def _app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    try:
        return m.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")

def test_new_settings_defaults(tmp_path, monkeypatch):
    # Isolate from the developer's real ~/.dj_cratebuilder_config.json so this
    # asserts the shipped DEFAULTS, not whatever the user has toggled locally.
    # _config_path() resolves via os.path.expanduser("~"), which honors these.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    app = _app(); app.update()
    assert app._auto_dl_interval.get() == "1 day"
    assert app._run_at_startup.get() is False
    assert app._minimize_to_tray.get() is False
    # Watch List startup scan is on by default (preserves prior behavior).
    assert app._watchlist_scan_on_startup.get() is True
    app.destroy()


def test_legacy_auto_check_interval_carries_over(tmp_path, monkeypatch):
    # A config written by an older build used auto_check_hours for the interval;
    # that must seed the renamed _auto_dl_interval on upgrade.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".dj_cratebuilder_config.json").write_text(
        '{"auto_check_hours": "12 hours", "watchlist_last_check": 1234}',
        encoding="utf-8")
    started = int(time.time())
    app = _app(); app.update()
    assert app._auto_dl_interval.get() == "12 hours"
    # The schedule now counts from app start, NOT a stored anchor: the old
    # watchlist_last_check (1234) is ignored in favor of this launch time.
    assert app._watchlist_last_download >= started
    app.destroy()

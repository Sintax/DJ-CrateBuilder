import importlib.util, os, sys, pytest

def _app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    try:
        return m.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")

def test_new_settings_defaults():
    app = _app(); app.update()
    assert app._auto_check_hours.get() == "24 hours"
    assert app._run_at_startup.get() is False
    assert app._minimize_to_tray.get() is False
    app.destroy()

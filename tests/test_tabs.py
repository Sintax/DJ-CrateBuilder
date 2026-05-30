import importlib.util, os, sys

def _app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    return m.MP3DownloaderApp()

def test_tab_order_is_main_watchlist_settings_about():
    try:
        app = _app()
    except Exception as e:
        import pytest; pytest.skip(f"no display: {e}")
    app.update()
    titles = [app._notebook.tab(i, "text") for i in app._notebook.tabs()]
    joined = " | ".join(titles)
    assert "Main" in titles[0]
    assert "Watch List" in titles[1]
    assert "Settings" in titles[2]
    assert "About" in titles[3]
    app.destroy()

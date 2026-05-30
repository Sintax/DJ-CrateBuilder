def test_tray_module_constructs(monkeypatch):
    from cratebuilder import tray
    calls = []
    t = tray.TrayIcon(schedule=lambda fn: calls.append(fn),
                      on_open=lambda: None, on_scan=lambda: None,
                      on_quit=lambda: None)
    # `available` may be False if pystray/Pillow not installed in CI — both OK.
    assert hasattr(t, "start") and hasattr(t, "notify") and hasattr(t, "stop")

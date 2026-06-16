def test_tray_module_constructs(monkeypatch):
    from cratebuilder import tray
    calls = []
    t = tray.TrayIcon(schedule=lambda fn: calls.append(fn),
                      on_open=lambda: None, on_scan=lambda: None,
                      on_download=lambda: None, on_quit=lambda: None,
                      download_text=lambda *_: "Download All New (3)")
    # `available` may be False if pystray/Pillow not installed in CI — both OK.
    assert hasattr(t, "start") and hasattr(t, "notify") and hasattr(t, "stop")
    assert isinstance(t.available, bool)
    # notify / set_title before start are safe no-ops (no icon yet).
    assert t.notify("hi") is False
    t.set_title("anything")   # must not raise
    # The download label is exposed as a callable for the dynamic menu item.
    assert t._download_text() == "Download All New (3)"

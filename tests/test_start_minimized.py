"""Start App Minimized hides the window before it can ever be seen.

The old flow let the window open normally and hid it on a 1.7-second timer,
so every boot flashed the full UI at the user before dropping to the tray.
The withdraw now happens inside __init__, before a single widget is built —
the window is simply never mapped — and the timer's only surviving job, done
at after(0), is raising the tray icon, which needs the Tk loop running.

quiet=True suppresses the whole start-minimized path (the fixtures rely on
that to get a visible test window), so these tests build their own apps with
quiet=False, stubbing the dependency check and the tray so a production-
faithful init stays offline and icon-free.
"""
import json


def _seed_config(tmp_path, **extra):
    cfg = {"start_minimized": True, **extra}
    (tmp_path / ".dj_cratebuilder_config.json").write_text(
        json.dumps(cfg), encoding="utf-8")


def _quiet_deps(cb_mod, monkeypatch):
    monkeypatch.setattr(cb_mod.MP3DownloaderApp, "_check_deps_async",
                        lambda self: None)


def test_the_window_is_withdrawn_before_init_even_finishes(
        cb_mod, make_app, tmp_path, monkeypatch):
    """The heart of it: no timer, no flash. By the time the constructor
    returns — before the Tk loop has processed one event — the window is
    already withdrawn, so it cannot have spent a moment on screen."""
    _seed_config(tmp_path)
    _quiet_deps(cb_mod, monkeypatch)
    app = make_app(quiet=False)

    assert app.state() == "withdrawn"


def test_the_tray_handoff_fires_on_the_first_loop_turn_not_a_timer(
        cb_mod, make_app, tmp_path, monkeypatch):
    """The 1.7s delay is gone outright: one drain of the event queue is
    enough for _hide_to_tray to have run and asked for the tray icon."""
    _seed_config(tmp_path)
    _quiet_deps(cb_mod, monkeypatch)
    asked = []
    # Truthy stand-in: the tray "exists", so _hide_to_tray withdraws rather
    # than falling back to iconify (whose <Unmap> would schedule a retry and
    # double-count the call).
    monkeypatch.setattr(cb_mod.MP3DownloaderApp, "_ensure_tray",
                        lambda self: asked.append(True) or object())
    app = make_app(quiet=False)

    assert asked == []          # needs the loop, not the constructor
    app.update()
    assert asked == [True]


def test_with_the_option_off_the_window_opens_normally(
        cb_mod, make_app, tmp_path, monkeypatch):
    _seed_config(tmp_path, start_minimized=False)
    _quiet_deps(cb_mod, monkeypatch)
    app = make_app(quiet=False)

    assert app.state() == "normal"
    app.update()
    assert app.state() == "normal"


def test_quiet_builds_still_show_their_window(make_app, tmp_path):
    """The whole GUI lane depends on this: quiet=True suppresses the
    start-minimized path even when the seeded config asks for it."""
    _seed_config(tmp_path)
    app = make_app()            # quiet=True

    assert app.state() == "normal"

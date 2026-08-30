"""web_window.py: the local mount's bridge and its embedded remote thread.

The desktop window's entry point had no test of its own — which mattered most
for the one line that decides whether it keeps its host-only capabilities
(`JsApi.call` passing the LOCAL transport) and for the bind rule its embedded
server thread follows. Neither needs a window: `JsApi` is a plain object and
the bind decision is a pure function.
"""
import inspect
import sys

import pytest

from cratebuilder import tray as cb_tray
from cratebuilder.events import EventBus
from cratebuilder.remoteauth import RemoteState
from cratebuilder.server import ANY_INTERFACE, LOOPBACK, bind_host
from cratebuilder.service import CBError

# pywebview is a declared runtime dependency, but importing the module must not
# be what breaks a headless run of the suite.
web_window = pytest.importorskip("web_window")


class RecordingService:
    """Stands in for CrateBuilderService: records how it was called.

    Carries a real EventBus and the two reads the tray makes of a service —
    `settings.get` and `counts()` — so the tray can be exercised without a
    database, a window or a service.
    """

    def __init__(self, result=None, raises=None, settings=None, pending_new=0):
        self.calls = []
        self._result = result if result is not None else {"ok": "yes"}
        self._raises = raises
        self.settings = dict(settings or {})
        self.pending_new = pending_new
        self.counted = 0             # how often counts() was asked — see fix 10
        self.logged = []
        self.events = EventBus()

    def call(self, method, params=None, transport=None):
        self.calls.append({"method": method, "params": params,
                           "transport": transport})
        if method == "settings.get":
            key = (params or {}).get("key")
            return {"key": key, "value": self.settings.get(key)}
        if self._raises is not None:
            raise self._raises
        return self._result

    def counts(self):
        self.counted += 1
        return {"pending_new": self.pending_new}

    def log_line(self, text):
        self.logged.append(text)


def test_the_window_bridge_always_asks_on_the_local_transport():
    """The one line that decides whether the desktop window keeps update.* and
    fs.* — a bridge that forgot it would silently demote the host's own window
    to a remote client."""
    service = RecordingService()
    api = web_window.JsApi(service)
    assert api.call("state.snapshot", {"a": 1}) == {"ok": True,
                                                    "result": {"ok": "yes"}}
    assert service.calls == [{"method": "state.snapshot", "params": {"a": 1},
                              "transport": "local"}]


def test_the_bridge_returns_a_refusal_envelope_rather_than_raising():
    """The JS facade handles local and remote failures through one code path,
    so a CBError has to come back as {ok: false} — an exception would cross the
    pywebview boundary as an opaque bridge error instead of the user's reason."""
    api = web_window.JsApi(RecordingService(raises=CBError("no such thing")))
    assert api.call("nope") == {"ok": False, "error": "no such thing"}


def test_an_unexpected_error_never_kills_the_bridge():
    api = web_window.JsApi(RecordingService(raises=RuntimeError("boom")))
    answer = api.call("state.snapshot")
    assert answer["ok"] is False
    assert "boom" in answer["error"]


def test_the_embedded_thread_uses_the_same_bind_rule_as_the_entry_point(tmp_path):
    """It bound 0.0.0.0 unconditionally, with the toggle as its only gate and
    no way to run it loopback-only. Both mounts now go through bind_host."""
    state = RemoteState(str(tmp_path / "remote.json"))
    assert bind_host(state, lan=False) == LOOPBACK
    assert bind_host(state, lan=True) is None
    state.set_flag("enabled", True)
    assert bind_host(state, lan=False) == LOOPBACK
    assert bind_host(state, lan=True) == ANY_INTERFACE


def test_a_disabled_host_does_not_print_a_pairing_code(tmp_path, capsys,
                                                       monkeypatch):
    """A code minted while the toggle is off is inert — /pair refuses it — so
    printing one sends the user to type something that cannot work."""
    import web_server

    state = RemoteState(str(tmp_path / "remote.json"))
    assert state.get_flag("enabled") is False

    started = {}
    monkeypatch.setattr(web_server.uvicorn, "Server",
                        lambda config: type("S", (), {"run": lambda s: started.setdefault("ran", True)})())
    monkeypatch.setattr(web_server, "build_service",
                        lambda data_dir: type("Svc", (), {"remote_state": state})())
    monkeypatch.setattr(web_server, "create_app", lambda *a, **k: object())

    web_server.main(["--port", "0", "--pair", "--data-dir", str(tmp_path)])
    printed = capsys.readouterr().out
    assert "Pairing code" not in printed
    assert "remote access is OFF" in printed
    assert state.active_code() is None      # not even minted

    # With the toggle on, --pair prints one as before.
    state.set_flag("enabled", True)
    web_server.main(["--port", "0", "--pair", "--data-dir", str(tmp_path)])
    assert "Pairing code" in capsys.readouterr().out


def test_host_allow_is_persisted_by_the_entry_point(tmp_path, capsys, monkeypatch):
    import web_server

    state = RemoteState(str(tmp_path / "remote.json"))
    monkeypatch.setattr(web_server.uvicorn, "Server",
                        lambda config: type("S", (), {"run": lambda s: None})())
    monkeypatch.setattr(web_server, "build_service",
                        lambda data_dir: type("Svc", (), {"remote_state": state})())
    monkeypatch.setattr(web_server, "create_app", lambda *a, **k: object())

    web_server.main(["--port", "0", "--data-dir", str(tmp_path),
                     "--host-allow", "https://cb.example.com/",
                     "--host-allow", "booth.tailnet.ts.net"])
    assert state.extra_hosts() == ["cb.example.com", "booth.tailnet.ts.net"]
    assert "cb.example.com" in capsys.readouterr().out


def test_the_console_banners_survive_a_cp1252_terminal():
    """A Windows console at the default code page raises UnicodeEncodeError on
    print(), which takes the process down before it ever listens — which is
    exactly what the "remote access is off" note did. The pretty typography
    belongs in the messages that travel as JSON, not in console copy."""
    import web_server

    for name in ("LAN_REFUSED", "DISABLED_NOTE"):
        getattr(web_server, name).encode("cp1252")


def test_the_embedded_thread_refuses_lan_without_consent(tmp_path, capsys):
    """--lan with the toggle off starts nothing at all — it must not quietly
    fall back to loopback and leave the user thinking the LAN mount is up."""
    class Svc:
        remote_state = RemoteState(str(tmp_path / "remote.json"))

    assert web_window.start_remote_mount(Svc(), port=0, lan=True) is None
    assert "Allow remote control" in capsys.readouterr().err


def test_the_local_bundle_is_served_revalidated(tmp_path, monkeypatch):
    """pywebview means to send no-store and doesn't.

    Its static route sets the header on `bottle.response`, then returns
    `bottle.static_file(...)` — an HTTPResponse whose own headers replace the
    ones set there. The bundle reaches the window carrying only Last-Modified
    and ETag, WebView2 applies heuristic freshness, and an edited web/ file is
    invisible until that window expires. Measured against the real window:
    cache-control absent, and all three assets served with transferSize 0.
    """
    bottle = pytest.importorskip("bottle")
    # Registers the current function for restoration, so the patch this test
    # applies cannot leak into the rest of the suite.
    monkeypatch.setattr(bottle, "static_file", bottle.static_file)
    (tmp_path / "app.css").write_text(".x{}", encoding="utf-8")

    web_window.serve_bundle_revalidated()
    response = bottle.static_file("app.css", root=str(tmp_path))

    assert "no-cache" in response.headers.get("Cache-Control", "")


def test_serving_the_bundle_revalidated_twice_does_not_stack_wrappers(monkeypatch):
    """A second application must be a no-op rather than another layer of
    wrapping around the same function."""
    bottle = pytest.importorskip("bottle")
    monkeypatch.setattr(bottle, "static_file", bottle.static_file)

    first = web_window.serve_bundle_revalidated()
    second = web_window.serve_bundle_revalidated()

    assert first is second is bottle.static_file


# ── entry-point duties (frozen awareness) ────────────────────────────────────
# The four things main() must do before/around building the window, mirroring
# DJ-CrateBuilder_v2.0.py's __main__ block. Each is factored into a seam that
# takes no window and no real singleton port, so none of this spawns one.

def test_run_scan_worker_if_requested_is_a_noop_without_the_flag():
    assert web_window.run_scan_worker_if_requested(["prog"]) is None


def test_run_scan_worker_if_requested_answers_the_protocol_and_exits(monkeypatch):
    """Must never reach the singleton lock or a window — this only proves the
    dispatch: the flag is detected and scanproc.worker_main() decides the
    exit code, before anything else in main() runs."""
    import cratebuilder.scanproc as scanproc

    calls = []
    monkeypatch.setattr(scanproc, "worker_main", lambda: calls.append(1) or 0)
    with pytest.raises(SystemExit) as info:
        web_window.run_scan_worker_if_requested(["prog", "--scan-worker"])
    assert info.value.code == 0
    assert calls == [1]


def test_acquire_or_hand_off_returns_the_lock_on_success(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(web_window, "acquire_single_instance",
                        lambda port: sentinel)
    assert web_window.acquire_or_hand_off(port=0) is sentinel


def test_acquire_or_hand_off_hands_off_and_exits_when_already_running(monkeypatch):
    asked = []
    monkeypatch.setattr(web_window, "acquire_single_instance", lambda port: None)
    monkeypatch.setattr(web_window, "request_show", asked.append)
    with pytest.raises(SystemExit) as info:
        web_window.acquire_or_hand_off(port=49737)
    assert info.value.code == 0
    assert asked == [49737]


def test_restore_window_shows_before_it_restores():
    """The order is the whole fix, not a style choice.

    A window hidden to the tray is Visible=False with WindowState still
    Minimized, and winforms re-applies that stored show-state when Show()
    runs — so a WindowState written while the form is invisible is thrown
    away. restore() before show() leaves the window visible and still
    minimized: a taskbar button and nothing on screen, which is what clicking
    the tray icon did. Measured against a real pywebview window, not inferred.
    """
    calls = []

    class Window:
        def show(self):
            calls.append("show")

        def restore(self):
            calls.append("restore")

    web_window.restore_window(Window())
    assert calls == ["show", "restore"]


@pytest.mark.parametrize("throwing", ["show", "restore"])
def test_restore_window_guards_each_call_separately(throwing):
    """Each call is the whole fix for a different state — show() for a window
    hidden in the tray, restore() for one only minimized when a second launch
    asks — so a throw in either must not skip the other. And nothing may
    reach the caller: this runs on the single-instance listener's thread and
    on pystray's menu thread, where a raise is unhandled."""
    calls = []

    class Window:
        def show(self):
            calls.append("show")
            if throwing == "show":
                raise RuntimeError("window already destroyed")

        def restore(self):
            calls.append("restore")
            if throwing == "restore":
                raise RuntimeError("window already destroyed")

    web_window.restore_window(Window())    # must not raise

    assert calls == ["show", "restore"]


def test_prepare_runtime_workspace_purges_and_chdirs(tmp_path, monkeypatch):
    purged = []
    monkeypatch.setattr(web_window.ucore, "default_workspace",
                        lambda: str(tmp_path / "workspace"))
    monkeypatch.setattr(web_window.ucore, "purge_dir", purged.append)
    monkeypatch.setattr(web_window.util, "runtime_data_dir",
                        lambda: str(tmp_path))
    chdired = []
    monkeypatch.setattr(web_window.os, "chdir", chdired.append)

    web_window.prepare_runtime_workspace()

    assert purged == [str(tmp_path / "workspace")]
    assert chdired == [str(tmp_path)]


def test_prepare_runtime_workspace_swallows_an_unchdirable_directory(monkeypatch):
    """A Run-key startup launch begins in System32; if chdir can't land on
    the real runtime dir either, the launch must still proceed."""
    monkeypatch.setattr(web_window.ucore, "default_workspace", lambda: "")
    monkeypatch.setattr(web_window.ucore, "purge_dir", lambda p: None)
    monkeypatch.setattr(web_window.util, "runtime_data_dir", lambda: "unreachable")

    def _raise(path):
        raise OSError("no such directory")

    monkeypatch.setattr(web_window.os, "chdir", _raise)

    web_window.prepare_runtime_workspace()    # must not raise


# ── the system tray ──────────────────────────────────────────────────────────
# WindowTray over fakes: no pystray icon is ever raised, no window is ever
# created, and every menu callback runs inline instead of on its own thread.

class FakeWindow:
    """The three pywebview calls the tray makes, plus the JS it evaluates."""

    def __init__(self):
        self.actions = []
        self.js = []

    def hide(self):
        self.actions.append("hide")

    def show(self):
        self.actions.append("show")

    def minimize(self):
        self.actions.append("minimize")

    def restore(self):
        self.actions.append("restore")

    def destroy(self):
        self.actions.append("destroy")

    def evaluate_js(self, script):
        self.js.append(script)


class FakeTray:
    """A cratebuilder.tray.TrayIcon stand-in that never touches pystray.

    It takes **menu, so nothing here would notice a renamed TrayIcon
    parameter — test_the_menu_arguments_fit_the_real_trayicon is what binds
    the two.
    """

    def __init__(self, available=True, started=True, **menu):
        self.menu = menu
        self.available = available
        self._started = started
        self.titles = []
        self.notifications = []
        self.stopped = False

    def start(self):
        return self._started

    def set_title(self, text):
        self.titles.append(text)

    def notify(self, message, title="DJ-CrateBuilder"):
        self.notifications.append((message, title))

    def stop(self):
        self.stopped = True


def tray_factory(available=True, started=True, made=None, raises=None):
    """A `tray_factory` for WindowTray that builds FakeTrays (or throws)."""
    def factory(**menu):
        if raises is not None:
            raise raises
        icon = FakeTray(available=available, started=started, **menu)
        if made is not None:
            made.append(icon)
        return icon
    return factory


@pytest.fixture
def make_tray():
    """make_tray(...) -> (tray, window, service, icons). Stops every tray it
    builds, so no tooltip Timer outlives its test."""
    built = []

    def build(available=True, started=True, settings=None, pending_new=0,
              raises=None, factory_raises=None):
        service = RecordingService(settings=settings, pending_new=pending_new,
                                   raises=raises)
        window = FakeWindow()
        icons = []
        factory = tray_factory(available=available, started=started,
                               made=icons, raises=factory_raises)

        tray = web_window.WindowTray(window, service, tray_factory=factory,
                                     spawn=lambda fn: fn())
        built.append(tray)
        return tray, window, service, icons

    yield build
    for tray in built:
        tray.stop()


# ── minimize → tray ──────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="tray is win32-only")
def test_minimize_hides_to_the_tray_when_the_setting_is_on(make_tray):
    tray, window, _, icons = make_tray(settings={"minimize_to_tray": True})

    tray.on_minimized()
    tray.on_minimized()

    assert window.actions == ["hide", "hide"]
    assert len(icons) == 1          # lazy, and raised only once


def test_minimize_leaves_the_window_alone_when_the_setting_is_off(make_tray):
    tray, window, _, icons = make_tray(settings={"minimize_to_tray": False})

    tray.on_minimized()

    assert window.actions == []
    assert icons == []              # no icon raised for a minimise we ignore


def test_minimize_never_hides_off_win32(make_tray, monkeypatch):
    """The monolith's second condition: the tray is a Windows affordance and
    Settings only offers the option there."""
    tray, window, _, _ = make_tray(settings={"minimize_to_tray": True})
    monkeypatch.setattr(web_window.sys, "platform", "linux")

    tray.on_minimized()

    assert window.actions == []


@pytest.mark.skipif(sys.platform != "win32", reason="tray is win32-only")
def test_the_minimize_setting_is_read_on_every_event(make_tray):
    """Ticking 'Minimize to System Tray' has to take effect on the next
    minimise, not at the next launch — the monolith reads the BooleanVar its
    checkbox writes, so a cached read here would be a regression."""
    tray, window, service, _ = make_tray(settings={"minimize_to_tray": False})

    tray.on_minimized()
    service.settings["minimize_to_tray"] = True
    tray.on_minimized()

    assert window.actions == ["hide"]
    assert [c["params"]["key"] for c in service.calls] == ["minimize_to_tray"] * 2


@pytest.mark.skipif(sys.platform != "win32", reason="tray is win32-only")
@pytest.mark.parametrize("kwargs", [{"available": False}, {"started": False},
                                    {"factory_raises": RuntimeError("no backend")}])
def test_an_unavailable_tray_leaves_the_window_minimized(make_tray, kwargs):
    """The window is not hidden, and nothing else is done to it either.

    _hide_to_tray falls back to iconify(); re-issuing the minimize here would
    be a second WindowState write, which pywebview answers with another
    `minimized` event on another thread — and with no icon to break it, every
    one of those would try again. Tk generates no <Unmap> for an already
    iconic window, which is the only reason the monolith can share one path.

    The third case is a factory that RAISES rather than reporting itself
    unavailable: the lazy pystray import, or Icon construction on a broken
    backend. A traceback on this thread reaches nobody.
    """
    tray, window, _, _ = make_tray(settings={"minimize_to_tray": True},
                                   **kwargs)

    tray.on_minimized()            # must not raise

    assert window.actions == []


# ── the menu ─────────────────────────────────────────────────────────────────

def test_open_restores_the_window(make_tray):
    tray, window, _, _ = make_tray()

    tray.open()

    assert window.actions == ["show", "restore"]


def test_scan_now_shows_the_watch_list_then_scans(make_tray):
    """_tray_scan_now: focus the app, select the Watch List, then scan."""
    tray, window, service, _ = make_tray()

    tray.scan_now()

    assert window.actions == ["show", "restore"]
    assert window.js == ['location.hash = "watchlist"']
    assert service.calls == [{"method": "watchlist.scan_all", "params": None,
                              "transport": "local"}]


def test_download_all_new_shows_the_downloads_screen_then_downloads(make_tray):
    """_tray_download_all_new selects the Main tab, where the batch progress
    lives; Downloads is that screen here."""
    tray, window, service, _ = make_tray()

    tray.download_all_new()

    assert window.actions == ["show", "restore"]
    assert window.js == ['location.hash = "downloads"']
    assert service.calls == [{"method": "watchlist.download_all_new",
                              "params": None, "transport": "local"}]


def test_a_refused_menu_action_is_logged_and_notified_never_raised(make_tray):
    """'No new tracks pending' is the answer to the click, not a crash — and
    an exception here would surface inside pystray's menu thread.

    Logged as well as notified, the order _notify_tray uses: the balloon
    alone would leave a Scan Now refused from the tray with nothing in
    activity.log to explain why nothing happened."""
    tray, _, service, icons = make_tray(settings={"minimize_to_tray": True},
                                        raises=CBError("No new tracks pending."))
    tray.hide()                                     # raises the icon

    tray.download_all_new()                         # must not raise

    assert icons[0].notifications == [("No new tracks pending.",
                                       "DJ-CrateBuilder")]
    assert service.logged == ["🔔 Tray: No new tracks pending."]


def test_quit_stops_the_icon_and_closes_the_window(make_tray):
    tray, window, _, icons = make_tray(settings={"minimize_to_tray": True})
    tray.hide()

    tray.quit()

    assert icons[0].stopped is True
    assert window.actions == ["hide", "destroy"]


def test_the_menu_is_wired_to_the_trays_own_actions(make_tray):
    """TrayIcon owns the order (Open / Scan Now / Download All New / Quit);
    this is the half WindowTray owns — which callback each item runs."""
    tray, _, _, icons = make_tray(settings={"minimize_to_tray": True})
    tray.hide()
    menu = icons[0].menu

    assert menu["on_open"] == tray.open
    assert menu["on_scan"] == tray.scan_now
    assert menu["on_download"] == tray.download_all_new
    assert menu["on_quit"] == tray.quit
    assert menu["download_text"]() == "Download All New (0)"


def test_the_download_label_tracks_the_pending_count(make_tray):
    """The label mirrors the Watch List's 'Download All New (N)' button: one
    read to seed it, then off the event bus — never a poll, which would query
    the database on a timer."""
    tray, _, service, _ = make_tray(pending_new=3)
    assert tray.download_label() == "Download All New (3)"

    service.events.emit("state.patch", {"counts": {"pending_new": 7}})

    assert tray.download_label() == "Download All New (7)"
    assert service.counted == 1                 # seeded once, never re-read


def test_the_pending_count_is_not_read_until_the_label_is(make_tray):
    """counts() pulls library_stats() AND genres(), which scandirs the crate
    root and every platform folder under it. Building the tray happens in
    main() ahead of webview.start(), and this label does not exist until an
    icon has been raised — so the read waits for the first read of the label."""
    tray, _, service, _ = make_tray(pending_new=2)

    assert service.counted == 0

    assert tray.download_label() == "Download All New (2)"
    assert service.counted == 1


def test_an_event_before_the_first_label_read_wins(make_tray):
    """The bus is fresher than any seed read: a count that arrived already
    must not be overwritten by a database read done later."""
    tray, _, service, _ = make_tray(pending_new=2)

    service.events.emit("state.patch", {"counts": {"pending_new": 8}})

    assert tray.download_label() == "Download All New (8)"
    assert service.counted == 0


def test_stopping_the_tray_lets_go_of_the_event_bus(make_tray):
    tray, _, service, _ = make_tray(pending_new=1)

    tray.stop()
    service.events.emit("state.patch", {"counts": {"pending_new": 9}})

    assert tray.download_label() == "Download All New (1)"


# ── the hover tooltip ────────────────────────────────────────────────────────

def test_the_tooltip_is_idle_when_nothing_is_running(make_tray):
    tray, _, _, _ = make_tray()

    assert tray.summary() == "DJ-CrateBuilder\nIdle"


def test_the_tooltip_reports_progress_and_the_watch_list(make_tray):
    tray, _, service, _ = make_tray()
    service.events.emit("progress.current", {"title": "Track One",
                                             "job": "watchlist"})
    service.events.emit("progress.overall", {"done": 2, "total": 5,
                                             "percent": 40, "job": "watchlist"})
    service.events.emit("watchlist.card", {"id": 1, "status": "scanning"})

    summary = tray.summary()

    assert summary.splitlines() == ["DJ-CrateBuilder", "▶ Track One",
                                    "Overall: 2/5  40%",
                                    "👁 Watch List: scanning 1…"]


def test_a_finished_job_ends_the_progress_lines(make_tray):
    tray, _, service, _ = make_tray()
    service.events.emit("progress.current", {"title": "Track One",
                                             "job": "batch"})
    service.events.emit("job.finished", {"job": "batch", "ok": True})

    assert tray.summary() == "DJ-CrateBuilder\nIdle"


def test_the_tooltip_is_capped_at_127_characters(make_tray):
    """A Windows tray tooltip is capped at 127 chars; past that the backend
    rejects or truncates it."""
    tray, _, service, _ = make_tray()
    service.events.emit("progress.current", {"title": "A" * 400})
    service.events.emit("progress.overall", {"done": 12, "total": 240,
                                             "percent": 5,
                                             "eta_text": "about 9 min left"})
    for row in range(4):
        service.events.emit("watchlist.card", {"id": row, "status": "scanning"})

    summary = tray.summary()

    assert len(summary) == 127
    assert summary.startswith("DJ-CrateBuilder\n▶ " + "A" * 54 + "…")


# ── start minimized ──────────────────────────────────────────────────────────

def test_start_minimized_hands_the_hidden_window_to_the_tray(make_tray):
    """The window is created hidden so it never flashes; this is the second
    half — the handoff, once the event loop is up."""
    tray, window, _, icons = make_tray()

    assert tray.start_minimized() is True
    assert window.actions == []                     # never shown at all
    assert len(icons) == 1


@pytest.mark.parametrize("kwargs", [{"available": False}, {"started": False},
                                    {"factory_raises": RuntimeError("no backend")}])
def test_start_minimized_shows_the_window_when_the_tray_cannot_start(make_tray,
                                                                     kwargs):
    """Better a minimized window than a hidden one with no icon to click.

    Including when raising the icon THREW: start_minimized() runs on
    webview.start()'s own thread, where a traceback reaches nobody and would
    leave a hidden window with no taskbar button and no tray icon — the one
    state this method exists to prevent."""
    tray, window, _, _ = make_tray(**kwargs)

    assert tray.start_minimized() is False           # must not raise
    assert window.actions == ["show", "minimize"]


def test_the_menu_arguments_fit_the_real_trayicon(make_tray):
    """FakeTray takes **menu, so a renamed TrayIcon parameter would leave
    every test above green while the real _ensure() raised TypeError. This
    binds them: what _ensure() passes must bind to TrayIcon.__init__."""
    tray, _, _, icons = make_tray(settings={"minimize_to_tray": True})
    tray.hide()

    # bind() raises TypeError on an unknown, missing or renamed parameter.
    inspect.signature(cb_tray.TrayIcon.__init__).bind(object(), **icons[0].menu)


# ── main(): the wiring itself ────────────────────────────────────────────────
# Every line below was deletable with the suite still green, which is how the
# tray and the startup scan came to be missing from the web port in the first
# place. No window is created and no event loop is started: webview's two
# entry points are recorded, and the callback webview.start() is handed is
# invoked by hand.

class FakeEvent:
    """pywebview's Event: `+=` appends a handler."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class MainWindow(FakeWindow):
    """A created window, plus the two events main() subscribes to."""

    def __init__(self):
        super().__init__()
        self.events = type("Events", (), {})()
        self.events.closing = FakeEvent()
        self.events.minimized = FakeEvent()


class MainService(RecordingService):
    """RecordingService plus the surface main() itself uses."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.timers = 0
        self.startup_scans = 0
        self.on_update_restart = None
        self.remote_state = type("Remote", (),
                                 {"get_flag": lambda self, name: False})()

    def start_update_timer(self):
        self.timers += 1

    def start_startup_scan(self):
        self.startup_scans += 1

    def close(self):
        pass


def run_main(monkeypatch, settings=None, available=True):
    """main() with every real edge stubbed. Returns what it wired up."""
    service = MainService(settings=settings)
    window = MainWindow()
    created = {}
    started = []
    icons = []

    monkeypatch.setattr(web_window, "CrateBuilderService",
                        lambda transport=None: service)
    monkeypatch.setattr(web_window, "acquire_or_hand_off", lambda: object())
    monkeypatch.setattr(web_window, "prepare_runtime_workspace", lambda: None)
    monkeypatch.setattr(web_window, "serve_bundle_revalidated", lambda: None)
    monkeypatch.setattr(web_window, "listen_for_show_requests",
                        lambda lock, on_show: None)
    monkeypatch.setattr(web_window.sys, "argv", ["web_window.py"])
    monkeypatch.setitem(web_window.webview.settings, "ALLOW_DOWNLOADS",
                        web_window.webview.settings.get("ALLOW_DOWNLOADS"))
    # The tray is reached through the lazy import inside _ensure(), so this is
    # what keeps a real pystray icon off the test machine's taskbar.
    monkeypatch.setattr(cb_tray, "TrayIcon",
                        tray_factory(available=available, made=icons))

    def create_window(title, url, **kwargs):
        created.update(kwargs)
        return window

    monkeypatch.setattr(web_window.webview, "create_window", create_window)
    monkeypatch.setattr(web_window.webview, "start",
                        lambda func, **kwargs: started.append(func))

    web_window.main()
    return service, window, created, started[0], icons


def stop_the_wired_tray(window):
    """Run the WindowTray.stop main() subscribed, so no Timer outlives the
    test — and prove the handler is the tray's while we are here."""
    for handler in window.events.closing.handlers:
        if getattr(handler, "__func__", None) is web_window.WindowTray.stop:
            handler()
            return True
    return False


def test_main_wires_the_tray_to_the_windows_events(monkeypatch):
    service, window, created, started, _ = run_main(monkeypatch)

    assert service.timers == 1
    assert [getattr(h, "__func__", None)
            for h in window.events.minimized.handlers] == [
        web_window.WindowTray.on_minimized]
    assert service.close in window.events.closing.handlers
    assert stop_the_wired_tray(window) is True      # closing += tray.stop
    assert created["hidden"] is False               # the setting is off


def test_main_arms_the_startup_scan_only_once_the_loop_is_up(monkeypatch):
    """Armed before create_window, the 2.2 s settle would be spent while
    WebView2 was still starting and the cards a scan repaints would have no
    subscriber yet. The monolith measures its after(2200, …) from a built UI."""
    service, window, _, started, _ = run_main(monkeypatch)

    assert service.startup_scans == 0

    started()                                   # what webview.start() calls

    assert service.startup_scans == 1
    stop_the_wired_tray(window)


@pytest.mark.parametrize("wanted", [True, False])
def test_main_hides_the_window_at_creation_only_when_asked(monkeypatch, wanted):
    """hidden=True at creation is the half that keeps the window from ever
    flashing; the tray handoff is the other."""
    _, window, created, started, icons = run_main(
        monkeypatch, settings={"start_minimized": wanted})

    assert created["hidden"] is wanted

    started()

    assert (len(icons) == 1) is wanted          # handed to the tray, or not
    assert window.actions == []                 # and never shown either way
    stop_the_wired_tray(window)

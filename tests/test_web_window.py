"""web_window.py: the local mount's bridge and its embedded remote thread.

The desktop window's entry point had no test of its own — which mattered most
for the one line that decides whether it keeps its host-only capabilities
(`JsApi.call` passing the LOCAL transport) and for the bind rule its embedded
server thread follows. Neither needs a window: `JsApi` is a plain object and
the bind decision is a pure function.
"""
import pytest

from cratebuilder.remoteauth import RemoteState
from cratebuilder.server import ANY_INTERFACE, LOOPBACK, bind_host
from cratebuilder.service import CBError

# pywebview is a declared runtime dependency, but importing the module must not
# be what breaks a headless run of the suite.
web_window = pytest.importorskip("web_window")


class RecordingService:
    """Stands in for CrateBuilderService: records how it was called."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result if result is not None else {"ok": "yes"}
        self._raises = raises

    def call(self, method, params=None, transport=None):
        self.calls.append({"method": method, "params": params,
                           "transport": transport})
        if self._raises is not None:
            raise self._raises
        return self._result


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


def test_restore_window_calls_restore_then_show():
    calls = []

    class Window:
        def restore(self):
            calls.append("restore")

        def show(self):
            calls.append("show")

    web_window.restore_window(Window())
    assert calls == ["restore", "show"]


def test_restore_window_swallows_a_race_with_the_window_closing():
    """pywebview marshals restore()/show() from the listener's own thread; a
    window that closed a moment before must not take that thread down."""
    class Window:
        def restore(self):
            raise RuntimeError("window already destroyed")

        def show(self):
            raise AssertionError("never reached")

    web_window.restore_window(Window())    # must not raise


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

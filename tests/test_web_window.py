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

"""Local window for the web frontend: pywebview over the web/ bundle.

Runs the same bundle a remote browser gets, bound to the local transport — so
`update.*` and `fs.*` are reachable here and nowhere else. Start it with
`python web_window.py`; the tkinter app is unaffected and can run beside it.

When remote access is switched on in Settings ▸ Remote Access, the same
process also serves the remote mount on a background thread — one
`CrateBuilderService`, one job registry, one event bus, two transports. It is
never started otherwise, and nothing here ever switches the setting on.
"""

import json
import os
import sys
import threading

import webview

from cratebuilder.service import LOCAL, CBError, CrateBuilderService

REMOTE_PORT = 8770

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WINDOW_TITLE = "DJ-CrateBuilder"
# Sized in device pixels, which are not CSS pixels: on a 125%-scaled display a
# 1240px window is only ~995 CSS px of viewport, below the 1100px the layouts
# are designed against. Ask for enough that a scaled display still clears it.
WINDOW_SIZE = (1560, 980)
MIN_SIZE = (1280, 820)


class JsApi:
    """The local transport: one method, mirroring the WebSocket RPC exactly.

    Errors come back as an envelope rather than an exception so the JS facade
    handles local and remote failures through one code path.
    """

    def __init__(self, service):
        self._service = service

    def call(self, method, params=None):
        try:
            return {"ok": True,
                    "result": self._service.call(method, params,
                                                 transport=LOCAL)}
        except CBError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:                     # never kill the bridge
            return {"ok": False, "error": f"Unexpected host error: {exc}"}


def start_push_bridge(window, service):
    """Subscribe the window to service.events; unsubscribe when it closes.

    This is what replaces tkinter's after()-polling for the web frontend:
    a worker thread emits, and the event reaches JS via evaluate_js.
    """
    def push(event_type, payload):
        try:
            window.evaluate_js(
                f"window.cbApi && cbApi._push({json.dumps(event_type)}, "
                f"{json.dumps(payload)})")
        except Exception:
            pass                   # window closed mid-push; keep the emitter alive

    unsubscribe = service.events.subscribe(push)
    window.events.closing += lambda: unsubscribe()


def start_remote_mount(service, port=REMOTE_PORT):
    """Serve the remote transport from this process, on a daemon thread.

    Only ever called when the user has turned remote access on, which is also
    what makes binding off-loopback legitimate: the toggle IS the consent. The
    thread is a daemon so closing the window still ends the process.
    """
    import uvicorn                    # deferred: the window works without it

    from cratebuilder.server import create_app

    state = service.remote_state
    app = create_app(service, state)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True,
                              name="cratebuilder-remote")
    thread.start()
    print(f"Remote mount listening on http://0.0.0.0:{port}/ "
          f"({state.device_count()} paired device(s))")
    return server


def main():
    index = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(index):
        sys.exit(f"web bundle missing: {index}")

    screen = ""
    if "--screen" in sys.argv:
        pos = sys.argv.index("--screen") + 1
        if pos < len(sys.argv):
            screen = "#" + sys.argv[pos]

    service = CrateBuilderService(transport=LOCAL)
    if service.remote_state.get_flag("enabled"):
        try:
            start_remote_mount(service)
        except Exception as exc:                     # never block the window
            print(f"Remote mount could not start: {exc}", file=sys.stderr)
    webview.settings["ALLOW_DOWNLOADS"] = True       # Export CSV, log downloads
    window = webview.create_window(
        WINDOW_TITLE,
        index + screen,
        js_api=JsApi(service),
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
    )
    # private_mode=False keeps localStorage across restarts, so the database
    # viewer's column widths and order can live client-side.
    webview.start(lambda: start_push_bridge(window, service), private_mode=False)


if __name__ == "__main__":
    main()

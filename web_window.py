"""Local window for the web frontend: pywebview over the web/ bundle.

Runs the same bundle a remote browser gets, bound to the local transport — so
`update.*` and `fs.*` are reachable here and nowhere else. Start it with
`python web_window.py`; the tkinter app is unaffected and can run beside it.
"""

import os
import sys

import webview

from cratebuilder.service import CBError, CrateBuilderService

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
            return {"ok": True, "result": self._service.call(method, params)}
        except CBError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:                     # never kill the bridge
            return {"ok": False, "error": f"Unexpected host error: {exc}"}


def main():
    index = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(index):
        sys.exit(f"web bundle missing: {index}")

    screen = ""
    if "--screen" in sys.argv:
        pos = sys.argv.index("--screen") + 1
        if pos < len(sys.argv):
            screen = "#" + sys.argv[pos]

    service = CrateBuilderService(transport="local")
    webview.settings["ALLOW_DOWNLOADS"] = True       # Export CSV, log downloads
    webview.create_window(
        WINDOW_TITLE,
        index + screen,
        js_api=JsApi(service),
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
    )
    # private_mode=False keeps localStorage across restarts, so the database
    # viewer's column widths and order can live client-side.
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()

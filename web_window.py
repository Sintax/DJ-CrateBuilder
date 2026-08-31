"""Local window for the web frontend: pywebview over the web/ bundle.

Runs the same bundle a remote browser gets, bound to the local transport — so
`update.*` and `fs.*` are reachable here and nowhere else. Start it with
`python web_window.py`; the tkinter app is unaffected and can run beside it.

When remote access is switched on in Settings ▸ Remote Access, the same
process also serves the remote mount on a background thread — one
`CrateBuilderService`, one job registry, one event bus, two transports. It is
never started otherwise, and nothing here ever switches the setting on. That
thread binds 127.0.0.1 unless `--lan` is passed as well, exactly as
`web_server.py` does.

The entry block below carries the same frozen-exe duties as
DJ-CrateBuilder_v2.0.py's own `__main__`: answering a `--scan-worker`
relaunch before anything else runs, the single-instance lock (sharing
SINGLE_INSTANCE_PORT with the tkinter app — both own the same database), the
leftover update-workspace purge, and the startup `chdir`.
"""

import json
import os
import sys
import threading

import bottle
import webview

from cratebuilder import updater_core as ucore
from cratebuilder import util
from cratebuilder.service import (JOB_FINISHED, LOCAL, CBError,
                                  CrateBuilderService, app_icon_path)
from cratebuilder.singleton import (SINGLE_INSTANCE_PORT, acquire_single_instance,
                                    listen_for_show_requests, request_show)

REMOTE_PORT = 8770

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WINDOW_TITLE = "DJ-CrateBuilder"
# Sized in device pixels, which are not CSS pixels: on a 125%-scaled display a
# 1240px window is only ~995 CSS px of viewport, below the 1100px the layouts
# are designed against. Ask for enough that a scaled display still clears it.
WINDOW_SIZE = (1560, 980)
MIN_SIZE = (1280, 820)

# How often a moved window's placement is written out, in seconds — the
# monolith's _PLACEMENT_FLUSH_MS in the unit threading.Timer takes. The value
# itself is captured as it moves, so this only bounds what a hard kill can
# lose; closing the window flushes immediately.
PLACEMENT_FLUSH_INTERVAL = 60.0

# Windows caps a tray tooltip at 127 characters; keep it well under.
TRAY_TOOLTIP_LIMIT = 127
# The monolith's after(2000, _tray_title_tick) cadence, in seconds.
TRAY_TITLE_INTERVAL = 2.0
# How long a track title may run before the tooltip elides it, as _tray_summary
# truncates it.
TRAY_TITLE_MAX = 55


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


def setting_is_on(service, key):
    """One live read of a boolean setting, through the service's own surface.

    Live on purpose: the tray's rules are read per event, not cached at
    startup, so a checkbox toggled in Settings takes effect on the next
    minimise — the monolith reads the BooleanVar its checkbox writes, which
    has the same property for free. A setting that can't be read is off:
    that leaves the window on screen rather than hidden with no icon.
    """
    try:
        return bool(service.call("settings.get", {"key": key},
                                 transport=LOCAL)["value"])
    except Exception:
        return False


def run_detached(fn):
    """Run *fn* on a short-lived daemon thread — WindowTray's `schedule`.

    Nothing in a tray callback needs a UI thread the way Tk demands one, but
    it still must not run on pystray's own menu thread: a service call that
    takes a moment to answer would freeze the menu until it did.
    """
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


class WindowTray:
    """The system tray icon for the pywebview window.

    The web port of DJ-CrateBuilder_v2.0.py's tray section: the same lazy
    creation on first hide, the same four menu items in the same order, the
    same hover tooltip, and the same two conditions on the minimize button.
    `cratebuilder.tray.TrayIcon` is reused unchanged. Two things depart from
    the monolith, both because there is no Tk main thread here:

    * TrayIcon marshals every menu callback through `schedule` because Tk
      demands its own thread. pywebview marshals window calls itself and
      `service.call` is thread-safe, so nothing needs a UI thread — but a
      callback still must not run on pystray's own menu thread, where a call
      that takes a moment to answer would freeze the menu. Each gets a
      short-lived daemon thread instead of the monolith's `after(0, fn)`.
    * The tooltip is assembled from event-bus state instead of being read off
      Tk labels, and re-armed on a threading.Timer instead of `after(2000)`.
      The service is never polled for it: `counts()` reads the database, and
      a hover tooltip is not a reason to query it twice a second.
    """

    def __init__(self, window, service, tray_factory=None, spawn=run_detached):
        self._window = window
        self._service = service
        self._tray_factory = tray_factory
        self._spawn = spawn
        self._icon = None
        self._timer = None
        self._stopped = False
        # Tk serialised _ensure_tray for the monolith by construction; here
        # pywebview delivers each window event on a thread of its own, so two
        # minimises in quick succession could otherwise raise two icons.
        self._lock = threading.Lock()
        self._pending_new = None     # read on first use — see download_label
        self._statuses = {}          # watchlist row id → its last known status
        self._current = None         # last progress.current payload
        self._overall = None         # last progress.overall payload
        self._unsubscribe = service.events.subscribe(self._on_event)

    # ── menu actions ─────────────────────────────────────────────────────────
    def open(self):
        """Tray 'Open': bring the window back from the tray."""
        restore_window(self._window)

    def scan_now(self):
        """Tray 'Scan Now': focus the app, show the Watch List, then scan —
        the monolith's _tray_scan_now, whose tab select is a hash route here."""
        self.open()
        self._navigate("watchlist")
        self._call("watchlist.scan_all")

    def download_all_new(self):
        """Tray 'Download All New': focus the app, show Downloads — the web
        equivalent of the monolith's Main tab, where the batch progress lives
        — then run the same action as the Watch List button."""
        self.open()
        self._navigate("downloads")
        self._call("watchlist.download_all_new")

    def quit(self):
        """Tray 'Quit': drop the icon, then close the window, which is what
        ends webview.start() and the process with it.

        No close confirmation, unlike the monolith's _tray_close: the web
        window's own X button doesn't confirm either.
        """
        self.stop()
        try:
            self._window.destroy()
        except Exception:
            pass

    def _navigate(self, screen):
        """Route the frontend to *screen*. The nav is hash-based (web/app.js),
        so setting the hash is what selecting a tab was."""
        try:
            self._window.evaluate_js(f"location.hash = {json.dumps(screen)}")
        except Exception:
            pass

    def _call(self, method):
        """One tray-initiated service call, on the local transport.

        A refusal — nothing pending, a job already running — is an answer to
        the menu click, not a failure: it is notified and dropped. Nothing may
        be raised back into pystray's menu thread.
        """
        try:
            self._service.call(method, transport=LOCAL)
        except CBError as exc:
            self._notify(str(exc))
        except Exception:
            pass

    def _notify(self, message):
        """Say a refused action out loud, the monolith's _notify_tray order:
        the activity log always, then the tray balloon if an icon is up. The
        balloon alone would leave a Scan Now refused from the tray with
        nothing in activity.log to explain the silence."""
        try:
            self._service.log_line(f"🔔 Tray: {message}")
        except Exception:
            pass
        icon = self._icon
        if icon is not None:
            icon.notify(message, WINDOW_TITLE)

    # ── window lifecycle ─────────────────────────────────────────────────────
    def on_minimized(self):
        """Minimize button → hide to the system tray when that option is on.

        Both conditions are the monolith's _on_minimize: the setting, read
        live so toggling it in Settings takes effect immediately, and win32 —
        the tray is a Windows affordance and Settings only offers the option
        there. Unlike the monolith this needs no settle delay: pywebview
        raises `minimized` for the window itself, not for every child widget
        being unmapped.
        """
        if sys.platform != "win32":
            return
        if not setting_is_on(self._service, "minimize_to_tray"):
            return
        self.hide()

    def hide(self):
        """Hide to the tray, keeping the app running.

        An unavailable tray means the window simply stays minimized — this is
        only ever reached from the minimize button, so it already is. That is
        where this parts company with _hide_to_tray, whose fallback calls
        iconify(): Tk generates no <Unmap> for an already-iconic window, so
        the monolith cannot feed itself. Here, a second WindowState write is
        answered by pywebview with another `minimized` event on another
        thread, and with no icon to break the cycle every one of them would
        try again. start_minimized() keeps the show-and-minimize fallback,
        because there the window really is hidden and needs it.
        """
        if self._ensure() is None:
            return
        self._window_call("hide")

    def start_minimized(self):
        """Hand a window created hidden straight to the tray.

        Two-part, exactly as the monolith: the window is created `hidden=True`
        so it is never mapped at all (it withdraws before a single widget is
        built, so the UI can't flash), and this half — the tray handoff, which
        needs the event loop running — is the after(0) at the end of its
        __init__. Returns whether the tray took it.

        If the tray can't start, the window is shown minimized instead: a
        hidden window with no icon has no way back. That is _hide_to_tray's
        fallback, one step earlier — and the one path where minimizing is
        right, since the window is hidden rather than already minimized (see
        hide()).
        """
        if self._ensure() is not None:
            return True
        self._window_call("show")
        self._window_call("minimize")
        return False

    def stop(self):
        """Stop the icon, its tooltip timer and the event subscription.

        The monolith stops its tray in _quit_app; the subscription is this
        port's own, and left behind it would keep pushing events into a dead
        window's tray state.
        """
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            icon, self._icon = self._icon, None
        if timer is not None:
            timer.cancel()
        if unsubscribe is not None:
            unsubscribe()
        if icon is not None:
            icon.stop()

    def _window_call(self, name):
        """pywebview marshals hide/show/minimize from any thread, but a window
        already destroyed must not take the caller's thread down with it."""
        try:
            getattr(self._window, name)()
        except Exception:
            pass

    # ── the icon ─────────────────────────────────────────────────────────────
    def _ensure(self):
        """Create and start the icon on first hide (lazy), as _ensure_tray
        does. None means the tray is unavailable and the caller must fall
        back — pystray missing, no image, a backend that refused to run, or
        anything that went wrong on the way.

        Nothing may escape: tray.py catches only ImportError around pystray,
        pystray's own Icon construction can throw on a broken backend, and
        every caller is on a thread nobody is watching — a window event's, or
        webview.start()'s. A traceback there reaches no one and leaves a
        start_minimized launch with a hidden window, no taskbar button and no
        icon. Being the thing that guarantees a way back is this method's
        whole job, so a failure is a None, never a raise.
        """
        try:
            with self._lock:
                if self._icon is not None or self._stopped:
                    return self._icon
                factory = self._tray_factory
                if factory is None:
                    from cratebuilder.tray import TrayIcon   # lazy: pystray
                    factory = TrayIcon
                icon = factory(schedule=self._spawn,
                               on_open=self.open,
                               on_scan=self.scan_now,
                               on_download=self.download_all_new,
                               on_quit=self.quit,
                               download_text=lambda *_: self.download_label(),
                               image=self._load_image())
                if not icon.available or not icon.start():
                    return None
                self._icon = icon
        except Exception:
            return None
        self._tick()
        # Re-read rather than hand back the local: a stop() landing in the gap
        # has already stopped this icon, and the caller must not hide the
        # window behind a dead one.
        with self._lock:
            return self._icon

    @staticmethod
    def _load_image():
        """The real app icon as a PIL image, or None to let TrayIcon draw its
        placeholder (the monolith's _load_tray_image)."""
        path = app_icon_path()
        if not path:
            return None
        try:
            from PIL import Image
            return Image.open(path)
        except Exception:
            return None

    def download_label(self):
        """The live 'Download All New (N)' menu label, mirroring the Watch
        List button — TrayIcon reads it through a callable for exactly that.

        The count is seeded HERE, on first read, not at construction: this
        label does not exist until an icon has been raised, while counts()
        reads the database and scandirs the crate root and every platform
        folder under it — which at construction is work done in main(), ahead
        of webview.start(), delaying first paint for a menu nobody has opened.
        Everything else about the icon is lazy; so is this. An event that
        arrives first wins, since it is fresher than any read here.
        """
        if self._pending_new is None:
            self._pending_new = self._read_pending_new()
        return f"Download All New ({self._pending_new})"

    def _read_pending_new(self):
        """The pending-new total, or 0 when it can't be read."""
        try:
            return int(self._service.counts().get("pending_new") or 0)
        except Exception:
            return 0

    def _on_event(self, type, payload):
        """Keep the label and the tooltip current from the same bus the push
        bridge feeds the frontend from."""
        payload = payload or {}
        if type == "state.patch":
            counts = payload.get("counts") or {}
            if "pending_new" in counts:
                self._pending_new = int(counts.get("pending_new") or 0)
        elif type == "watchlist.card":
            self._statuses[payload.get("id")] = payload.get("status")
        elif type == "progress.current":
            self._current = payload
        elif type == "progress.overall":
            self._overall = payload
        elif type == JOB_FINISHED:
            job = payload.get("job")
            if self._current is not None and self._current.get("job") in (job, None):
                self._current = None
            if self._overall is not None and self._overall.get("job") in (job, None):
                self._overall = None

    def summary(self):
        """The multi-line hover tooltip, from the live progress and Watch List
        state — the event-bus reading of _tray_summary.

        The monolith's 'Queue: N left' line is left out: the queue panel is
        rendered client-side from queue.row events and its remaining count has
        no event-bus equivalent, while the overall line already carries the
        same work as done/total.
        """
        lines = [WINDOW_TITLE]
        if self._current:
            title = str(self._current.get("title") or "").strip() or "working…"
            if len(title) > TRAY_TITLE_MAX:
                title = title[:TRAY_TITLE_MAX - 1] + "…"
            lines.append(f"▶ {title}")
        overall = self._overall or {}
        parts = []
        if overall.get("total"):
            parts.append(f"{overall.get('done') or 0}/{overall['total']}")
        if overall.get("percent") is not None:
            parts.append(f"{overall['percent']}%")
        if str(overall.get("eta_text") or "").strip():
            parts.append(overall["eta_text"].strip())
        if parts:
            lines.append("Overall: " + "  ".join(parts))
        scanning = sum(1 for s in self._statuses.values() if s == "scanning")
        if scanning:
            lines.append(f"👁 Watch List: scanning {scanning}…")
        elif any(s == "downloading" for s in self._statuses.values()):
            lines.append("👁 Watch List: downloading new tracks…")
        if len(lines) == 1:
            lines.append("Idle")
        return "\n".join(lines)[:TRAY_TOOLTIP_LIMIT]

    def _tick(self):
        """Refresh the hover tooltip and re-arm, while the icon lives."""
        icon = self._icon
        if icon is None or self._stopped:
            return
        icon.set_title(self.summary())
        timer = threading.Timer(TRAY_TITLE_INTERVAL, self._tick)
        timer.daemon = True
        with self._lock:
            # stop() reads and cancels _timer under this lock, so arming
            # outside it would let a stop() in the gap leave a live Timer
            # nothing ever cancels.
            if self._stopped:
                return
            self._timer = timer
        timer.start()


def serve_bundle_revalidated():
    """Make pywebview's bundle server send the Cache-Control it means to.

    pywebview serves web/ over its own bottle server — on a fixed port, because
    private_mode is off — and its static route sets no-store on
    `bottle.response`. It then returns `bottle.static_file(...)`, an
    HTTPResponse whose own headers replace the ones set there, so the bundle
    arrives carrying nothing but Last-Modified and ETag. A cache is then free
    to guess how long that stays fresh, and WebView2 guesses hours: an updated
    web/ file goes unseen behind the copy the window already has, which is how
    a nightly could ship new Python against a user's old screens.

    no-cache rather than no-store: the browser still stores the bundle and
    still gets a 304, it just has to ask first. Returns the installed function
    so a second call is a no-op instead of another layer of wrapping.
    """
    if getattr(bottle.static_file, "_cb_revalidated", False):
        return bottle.static_file

    original = bottle.static_file

    def static_file(*args, **kwargs):
        response = original(*args, **kwargs)
        response.set_header("Cache-Control", "no-cache")
        return response

    static_file._cb_revalidated = True
    bottle.static_file = static_file
    return static_file


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


def host_allow_args(argv):
    """Every `--host-allow NAME` in *argv*, in order.

    Hand-parsed rather than argparse'd because this entry point has no parser —
    it forwards `--screen` positionally and is launched by the app, not typed.
    """
    names = []
    for index, token in enumerate(argv):
        if token == "--host-allow" and index + 1 < len(argv):
            names.append(argv[index + 1])
        elif token.startswith("--host-allow="):
            names.append(token.split("=", 1)[1])
    return names


def start_remote_mount(service, port=REMOTE_PORT, lan=False, host_allow=None):
    """Serve the remote transport from this process, on a daemon thread.

    Binds through `server.bind_host`, the same rule `web_server.py` uses:
    loopback unless `--lan` is given AND remote access is switched on. The
    toggle alone is consent, not intent — a desktop window is not a reason to
    put a control surface on the LAN without being asked. Returns None when
    `--lan` was asked for and the toggle says no.

    *host_allow* names this host should also answer to — the public name of a
    proxy or tunnel, or a Tailscale MagicDNS name. Merged into the store, so
    naming one once configures it for good.

    The thread is a daemon so closing the window still ends the process.
    """
    import uvicorn                    # deferred: the window works without it

    from cratebuilder.server import bind_host, create_app, uvicorn_kwargs

    state = service.remote_state
    if host_allow:
        state.add_extra_hosts(host_allow)
    host = bind_host(state, lan=lan)
    if host is None:
        print("Remote mount: --lan needs 'Allow remote control over the "
              "internet' switched on first.", file=sys.stderr)
        return None
    app = create_app(service, state)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                           log_level="warning",
                                           **uvicorn_kwargs()))
    thread = threading.Thread(target=server.run, daemon=True,
                              name="cratebuilder-remote")
    thread.start()
    print(f"Remote mount listening on http://{host}:{port}/ "
          f"({state.device_count()} paired device(s))")
    return server


def run_scan_worker_if_requested(argv=None):
    """Detect `--scan-worker` and, if present, answer the scanproc protocol
    and exit — before anything else runs.

    Handled ahead of everything in the entry block, mirroring the monolith:
    a scan worker (the frozen exe relaunched by cratebuilder.scanproc for
    an isolated channel listing) must never create a window, bind the
    single-instance port, or arm the update timer. The scanproc import is
    deferred to keep that cost off a launch that isn't a worker at all.
    Returns without exiting when the flag is absent, so main() can proceed
    as an ordinary launch.
    """
    if "--scan-worker" not in (sys.argv if argv is None else argv):
        return
    from cratebuilder import scanproc as cb_scanproc
    sys.exit(cb_scanproc.worker_main())


def acquire_or_hand_off(port=SINGLE_INSTANCE_PORT):
    """Claim the single-instance lock, or hand off to the instance that
    already holds it and exit.

    Shares SINGLE_INSTANCE_PORT with the tkinter app deliberately — both
    own the same database and must never run together. Returns the bound
    lock socket on success; the caller must keep a reference to it for the
    whole process lifetime, or it is garbage-collected and the lock silently
    released.
    """
    lock = acquire_single_instance(port)
    if lock is None:
        request_show(port)
        sys.exit(0)
    return lock


def restore_window(window):
    """Bring *window* back from the tray/taskbar on a second launch's
    request. pywebview marshals show()/restore() safely from a thread that
    isn't the UI thread — this is called from the single-instance listener's
    own thread, and from the tray menu's — but a race with the window already
    closing must not take that thread down with it.

    show() must come first. hide() leaves the form Visible=False with its
    WindowState still Minimized, and a WindowState written while the form is
    invisible does not stick — winforms re-applies the stored minimized
    show-state when Show() runs. restore() before show() therefore lands on a
    visible window that is still minimized: a taskbar button and no window,
    which is what clicking the tray icon looked like. Showing first and
    un-minimizing after ends Normal and focused from every state the window
    can be in, and raises only `restored` — never `minimized`, so the tray's
    own minimize handler cannot bounce the window straight back.

    Guarded separately rather than sharing one `try`: each call is the whole
    fix for a different state — show() for a window hidden in the tray,
    restore() for one merely minimized when a second launch asks — so a throw
    in either must not skip the other."""
    for call in (window.show, window.restore):
        try:
            call()
        except Exception:
            pass


def screen_rects():
    """The monitors a window may be placed on, as (x, y, w, h) work areas —
    in the units pywebview's own window geometry speaks.

    The web port of DJ-CrateBuilder_v2.0.py's screen_work_areas(), with
    pywebview's screen list standing in for EnumDisplayMonitors, plus one
    conversion the monolith never needed. pywebview reports a SCREEN in
    physical pixels but a WINDOW in device-independent ones: this machine's
    125% primary measures 2048x1152 as a screen, while a window filling it
    reports a width of 1638. Comparing a remembered window against an
    unconverted screen would read every window on a scaled display as hanging
    off the right-hand edge, and "fit" it by dragging it back each launch. So
    each screen is divided by its OWN scale — which is also what keeps a
    second monitor running at a different scale in the right place.

    Work areas rather than full bounds, for the monolith's reason: a window
    placed under the taskbar has a title bar the user cannot grab. Primary
    first — on Windows it is the monitor whose bounds start at the origin — so
    fit_window_geometry's "centre it on screens[0]" fallback lands on the
    display the user actually looks at. Returns [] when the layout cannot be
    read, which leaves the caller its own default placement.
    """
    try:
        screens = list(webview.screens)
    except Exception:
        return []
    found = []
    for screen in screens:
        try:
            scale = float(getattr(screen, "scale", 1) or 1)
            frame = getattr(screen, "frame", None)
            # .frame is the work area, but only the platforms that have one
            # populate it with a rectangle — fall back to the full bounds.
            try:
                rect = (frame.X, frame.Y, frame.Width, frame.Height)
            except AttributeError:
                rect = (screen.x, screen.y, screen.width, screen.height)
            x, y, width, height = (int(value / scale) for value in rect)
            if width <= 0 or height <= 0:
                continue
            found.append(((screen.x, screen.y) != (0, 0),
                          (x, y, width, height)))
        except Exception:
            continue
    found.sort(key=lambda item: item[0])       # False (primary) sorts first
    return [rect for _, rect in found]


def window_placement_kwargs(service, min_size=MIN_SIZE):
    """`create_window` kwargs that reopen the window where it last was.

    Empty when there is nothing remembered, or nothing trustworthy to restore
    it onto — which leaves pywebview the centred WINDOW_SIZE default, the
    monolith's "leave the centred default already set above".

    A remembered geometry is never obeyed blindly. util.fit_window_geometry
    checks it against the monitors attached RIGHT NOW, so a window saved on a
    second screen does not reopen off the edge of a laptop that has since been
    undocked — visible to the window manager and unreachable with the mouse.
    Going through the same helper the tkinter app uses is the point: the rules
    for what a remembered window may do are one piece of tested logic, not two.
    """
    try:
        remembered, maximized = service.window_placement()
    except Exception:
        return {}
    kwargs = {}
    try:
        fitted = util.fit_window_geometry(remembered, screen_rects(),
                                          min_size=min_size)
        parsed = util.parse_window_geometry(fitted) if fitted else None
    except Exception:
        parsed = None
    if parsed:
        width, height, x, y = parsed
        kwargs.update(width=width, height=height, x=x, y=y)
    if maximized:
        kwargs["maximized"] = True
    return kwargs


class WindowPlacement:
    """Carries the window's size and position between sessions.

    The web port of the monolith's window-placement section, with the same
    three-part shape — capture as the window moves, flush on a slow timer,
    flush again on the way out — and the same rule about which states may be
    captured at all. A maximized window reports the size of the screen it
    fills; a minimized or tray-hidden one reports where it last was. Writing
    either back would mean the window never reopens at the size the user
    actually chose, so only the normal state updates the geometry and being
    maximized is remembered as its own flag.

    pywebview supplies what tkinter's single <Configure> did not: `moved` and
    `resized` carry the new value, and `maximized`/`restored`/`minimized` say
    which state the window is in — so the state is never asked for, which
    matters because these arrive on threads that are not the UI thread and
    could not ask.
    """

    def __init__(self, window, service, interval=PLACEMENT_FLUSH_INTERVAL):
        self._window = window
        self._service = service
        self._interval = interval
        self._lock = threading.Lock()
        self._timer = None
        self._stopped = False
        self._state = "normal"
        self._size = None
        self._pos = None
        self._dirty = False
        try:
            _, self._maximized = service.window_placement()
        except Exception:
            self._maximized = False

    def start(self):
        """Seed from the live window, then subscribe and arm the flush timer.

        Seeded explicitly rather than waiting for the first event: `moved` and
        `resized` each carry only half of a geometry, so a session where the
        user resized but never moved the window would otherwise have nothing
        to write. Reading the window costs one call and settles both halves.

        A window does not always open where it was asked to. fit_window_geometry
        corrects a placement that no longer fits the monitors attached now, and
        pywebview converts a coordinate to physical pixels and back with int(),
        which can drop a pixel on a scaled display. So where the window ACTUALLY
        opened is written back when it differs from what was remembered —
        otherwise the stored value stays one the window never had, and every
        later launch re-derives the same correction from it. The monolith
        converges the same way, through the <Configure> its own restore fires.
        The conversion loses at most a pixel or two before it settles: the fixed
        points are the coordinates that survive the round trip exactly.
        """
        remembered = None
        try:
            remembered, _ = self._service.window_placement()
        except Exception:
            pass
        try:
            self._size = (int(self._window.width), int(self._window.height))
            self._pos = (int(self._window.x), int(self._window.y))
        except Exception:
            pass
        if self._geometry() not in (None, remembered):
            self._dirty = True
        self._window.events.moved += self.on_moved
        self._window.events.resized += self.on_resized
        self._window.events.maximized += self.on_maximized
        self._window.events.restored += self.on_restored
        self._window.events.minimized += self.on_minimized
        self._arm()

    # ── the window telling us where it is ────────────────────────────────────
    def on_moved(self, x, y):
        self._record(pos=(int(x), int(y)))

    def on_resized(self, width, height):
        self._record(size=(int(width), int(height)))

    def on_maximized(self):
        with self._lock:
            self._state = "maximized"
            if not self._maximized:
                self._maximized = True
                self._dirty = True

    def on_minimized(self):
        with self._lock:
            self._state = "minimized"

    def on_restored(self):
        """Back to a normal window — from maximized, or from the tray.

        Un-minimizing a window that WAS maximized does not reach this:
        winforms only raises `restored` when the new state is Normal, and that
        window goes back to Maximized, which raises `maximized` instead. So
        clearing the flag here cannot lose a maximized window's state.
        """
        with self._lock:
            self._state = "normal"
            if self._maximized:
                self._maximized = False
                self._dirty = True

    def _record(self, pos=None, size=None):
        """Capture a placement, if this is a state whose geometry means
        anything. Does no I/O — see PLACEMENT_FLUSH_INTERVAL."""
        with self._lock:
            if self._stopped or self._state != "normal":
                return
            if pos is not None and pos != self._pos:
                self._pos, self._dirty = pos, True
            if size is not None and size != self._size:
                self._size, self._dirty = size, True

    # ── writing it out ───────────────────────────────────────────────────────
    def _geometry(self):
        """The captured placement as a geometry string, or None while only
        half of one is known."""
        if self._size is None or self._pos is None:
            return None
        return util.format_window_geometry(self._size[0], self._size[1],
                                           self._pos[0], self._pos[1])

    def flush(self):
        """Write the captured placement out, if it has moved since the last
        write. True when something was actually stored.

        `_dirty` is cleared only on a successful write, so a failed one is
        retried by the next tick rather than being silently dropped."""
        with self._lock:
            if not self._dirty:
                return False
            geometry, maximized = self._geometry(), self._maximized
        if geometry is None:
            return False
        if not self._service.save_window_placement(geometry, maximized):
            return False
        with self._lock:
            self._dirty = False
        return True

    def _arm(self):
        with self._lock:
            if self._stopped:
                return
            self._timer = threading.Timer(self._interval, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _tick(self):
        self.flush()
        self._arm()

    def stop(self):
        """Last flush, then stand down — the window is closing.

        Wired ahead of `service.close` on the closing event, which pywebview
        runs synchronously in the order handlers were added.
        """
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        self.flush()


def prepare_runtime_workspace():
    """Once-per-launch startup housekeeping, done before the window opens.

    Purges any leftover update workspace from a prior update (the apply
    path deliberately leaves it for the updater; see docs/specs/
    2026-08-29-webui-local-updater-design.md), then normalises the working
    directory — a Run-key startup launch begins in C:\\Windows\\System32,
    which poisons CPython's last-resort temp-dir fallback.
    """
    ucore.purge_dir(ucore.default_workspace())
    try:
        os.chdir(util.runtime_data_dir())
    except OSError:
        pass


def main():
    index = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(index):
        sys.exit(f"web bundle missing: {index}")

    lock = acquire_or_hand_off()
    prepare_runtime_workspace()

    screen = ""
    if "--screen" in sys.argv:
        pos = sys.argv.index("--screen") + 1
        if pos < len(sys.argv):
            screen = "#" + sys.argv[pos]

    service = CrateBuilderService(transport=LOCAL)
    # Explicit, like start_remote_mount() below — a constructed service does
    # not arm its own auto-check timer (cratebuilder/service.py's __init__).
    service.start_update_timer()
    if service.remote_state.get_flag("enabled"):
        try:
            start_remote_mount(service, lan="--lan" in sys.argv,
                               host_allow=host_allow_args(sys.argv))
        except Exception as exc:                     # never block the window
            print(f"Remote mount could not start: {exc}", file=sys.stderr)
    webview.settings["ALLOW_DOWNLOADS"] = True       # Export CSV, log downloads
    serve_bundle_revalidated()
    # Created hidden when the user asked to start in the tray, so the window is
    # never mapped at all — the monolith withdraws before it builds a widget
    # for the same reason. The handoff to the tray is the second half, once the
    # event loop is up; see WindowTray.start_minimized.
    start_minimized = setting_is_on(service, "start_minimized")
    # Reopened where the last session left it, so the window is sized once
    # rather than drawn at the default and then moved. The defaults here are
    # the fallback they always were: window_placement_kwargs is empty on a
    # first run, and on any launch where the remembered place no longer fits
    # the monitors that are actually attached.
    frame = {"width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1],
             "min_size": MIN_SIZE, "hidden": start_minimized}
    frame.update(window_placement_kwargs(service))
    window = webview.create_window(
        WINDOW_TITLE,
        index + screen,
        js_api=JsApi(service),
        **frame,
    )
    tray = WindowTray(window, service)
    placement = WindowPlacement(window, service)
    # The update.apply worker calls this from its own (non-UI) thread once the
    # updater process has been handed off. window.destroy() is safe to call
    # off the main thread — pywebview marshals it — and makes webview.start()
    # return, which exits the process (updater.exe waits up to 30s on this
    # PID before swapping files).
    service.on_update_restart = window.destroy
    # Ahead of service.close: pywebview runs `closing` handlers synchronously,
    # in the order they were added, and this one still has a write to make.
    window.events.closing += placement.stop
    window.events.closing += service.close
    window.events.closing += tray.stop
    window.events.minimized += tray.on_minimized
    listen_for_show_requests(lock, lambda: restore_window(window))

    def started():
        start_push_bridge(window, service)
        # After the window exists, because it seeds itself by reading the
        # window's own size and position (see WindowPlacement.start).
        placement.start()
        # Explicit, like start_update_timer() above — and armed only once the
        # bridge is subscribed, so the cards the scan repaints have somewhere
        # to arrive. Armed before create_window, its 2.2 s settle would be
        # spent while WebView2 was still starting; the monolith measures its
        # after(2200, …) from a UI that is already built.
        service.start_startup_scan()
        if start_minimized:
            tray.start_minimized()

    # private_mode=False keeps localStorage across restarts, so the database
    # viewer's column widths and order can live client-side.
    webview.start(started, private_mode=False)


if __name__ == "__main__":
    run_scan_worker_if_requested()
    main()

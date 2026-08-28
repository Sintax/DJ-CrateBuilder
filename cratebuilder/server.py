"""FastAPI mount for the web bundle: RPC, the event socket, pairing and logs.

The remote half of the two-transport design (HANDOFF §2): the same `web/`
bundle the pywebview window loads, served over HTTP, driving the SAME
`CrateBuilderService` object the window drives. Nothing here duplicates an
action — every call lands in `service.call(..., transport="remote")`, which is
where `update.*` and `fs.*` are already refused server-side.

No tkinter, by the package's rule. TLS is out of scope on purpose (HANDOFF
§8.2): put this behind Caddy or a Cloudflare Tunnel; plain HTTP is LAN-only.
A proxied deployment must name its public hostname — `--host-allow <name>` on
either entry point — or the DNS-rebinding defence below will refuse the
browser's Host and Origin, which are the proxy's name and not this machine's.
"""

import asyncio
import ipaddress
import logging
import os
import re
import socket
import threading

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cratebuilder import remoteauth
from cratebuilder.remoteauth import (CLOSE_DISABLED, DISABLED_REASON,
                                     ControlHeld, PairingRefused, RemoteState,
                                     UNPAIRED_REASON)
from cratebuilder.service import REMOTE, CBError, CrateBuilderService

LOOPBACK = "127.0.0.1"
ANY_INTERFACE = "0.0.0.0"

WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

TOKEN_HEADER = "X-CB-Token"

# How many event frames may back up for one socket before the oldest are
# dropped. A phone on a slow link must not be able to grow the host's memory
# for the length of a batch — progress is already coalesced upstream, and a
# stale frame is worth less than the newest one.
EVENT_QUEUE_MAX = 512

# WebSocket close code for "you are not paired". In the application range, so
# a browser can tell it apart from a network drop and show the pairing screen
# instead of retrying forever. Also what a revoked device's socket is closed
# with, since "your token is gone" is the same fact either way.
WS_UNAUTHORIZED = 4401
# "The host is up, but remote access is switched off." Distinct from 4401
# because the client's token is still good — it must keep it and keep retrying,
# not drop it and demand a fresh pairing code.
WS_DISABLED = 4403
# "This page's origin is not one I answer to." Also distinct from 4401, and for
# the same reason: the token is fine, the DEPLOYMENT is wrong, and a client that
# read this as revocation would throw away a good token and send the user to a
# pairing screen that cannot fix it. Mirrors the HTTP 421 the Host check
# returns, which is where the number comes from.
WS_BAD_ORIGIN = 4421


def bind_host(remote_state, lan=False):
    """The interface a mount may listen on.

    One rule, used by BOTH entry points (`web_server.py` and the desktop
    window's embedded thread): loopback unless the caller asked for the LAN
    *and* the user has already switched remote access on. Neither the flag on
    its own nor the flag off with `--lan` gets past this — the flag is consent,
    `--lan` is intent, and going off-loopback needs both.
    """
    if not lan:
        return LOOPBACK
    return ANY_INTERFACE if remote_state.get_flag("enabled") else None


# ── Host / Origin (DNS-rebinding defence) ────────────────────────────────────
# There is no CORS middleware, so a cross-origin page cannot read /rpc. What it
# CAN do is resolve a name it controls to 127.0.0.1 and become same-origin with
# a loopback-bound host — at which point /pair is reachable with no preflight.
# A rebinding attack needs a DOMAIN NAME, so the defence is to accept only an
# address literal, localhost, this machine's own name, and any name the user has
# explicitly added with `--host-allow` (RemoteState.extra_hosts).
#
# That last clause is what keeps the documented deployment working: behind Caddy
# or a Cloudflare Tunnel the browser sends the PUBLIC name, and a Tailscale
# MagicDNS name is how a booth machine is actually reached. Naming it is the
# user's decision, which is exactly the difference between it and an
# attacker-chosen domain.

_ALLOWED_HOST_NAMES = {"localhost", "localhost.localdomain"}
BAD_HOST_REASON = (
    "That host name is not one this server answers to. Reach it by address or "
    "by the host machine's own name — or start the host with "
    "--host-allow <name> to add the name you are using.")
BAD_ORIGIN_REASON = (
    "That page's origin is not one this host answers to. If you are reaching "
    "it through a proxy or a tunnel, start the host with --host-allow <name> "
    "naming the public address.")


def _local_names():
    names = set(_ALLOWED_HOST_NAMES)
    try:
        own = socket.gethostname().lower()
        names.add(own)
        names.add(f"{own}.local")
        names.add(own.split(".")[0])
    except OSError:
        pass
    return names


def host_is_allowed(host_header, allowed_names=None):
    """True when a Host header names this machine rather than someone's domain."""
    raw = remoteauth.normalise_host(host_header)
    if not raw:
        return False            # HTTP/1.1 requires it; a browser always sends it
    try:
        ipaddress.ip_address(raw)
        return True             # an address literal cannot be rebound
    except ValueError:
        pass
    return raw in (allowed_names if allowed_names is not None else _local_names())


def origin_is_allowed(origin, host_header, allowed_names=None):
    """True when a WebSocket upgrade comes from somewhere this host answers to.

    Browsers always send Origin on an upgrade and — unlike `fetch` — the
    WebSocket API is NOT subject to the same-origin policy, so without this a
    page on any site could open the event socket. A non-browser client (a
    script, a test harness) sends no Origin and is not what this defends
    against; it still needs a valid token.

    Same-origin is the ordinary case. The second clause is for a proxy that
    rewrites Host to the upstream: the browser then sends its real Origin (the
    public name) against a Host of 127.0.0.1, and the two cannot match however
    correct the deployment is — so the configured names are consulted as well.
    An address literal is NOT blanket-accepted here, unlike in a Host header: a
    page served from an attacker's own IP would otherwise pass.
    """
    if not origin:
        return True
    netloc = str(origin).split("://", 1)[-1].strip().lower().rstrip("/")
    if not netloc:
        return False
    if netloc == (host_header or "").strip().lower():
        return True
    name = remoteauth.normalise_host(origin)
    return bool(name) and name in (allowed_names if allowed_names is not None
                                   else _local_names())


class RpcBody(BaseModel):
    method: str
    params: dict | None = None


class PairBody(BaseModel):
    code: str | None = None
    device_name: str | None = None


# ── token redaction in the access log ────────────────────────────────────────
# The WebSocket handshake has to carry its token in the query string — the
# browser WebSocket API cannot set a request header — and uvicorn's access log
# prints the full path with its query, so an ordinary `--log-level info` run
# would write every device's long-lived token to the console and to whatever is
# capturing it. "Store the hash, never the token" has to hold for the logs too.

_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"']+")
_REDACT_LOGGERS = ("uvicorn.access", "uvicorn.error")


def _redact(text):
    return _TOKEN_QUERY_RE.sub(r"\1<redacted>", text)


class TokenRedactingFilter(logging.Filter):
    """Replaces any `token=…` in a log record with `token=<redacted>`."""

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a
                                for a in record.args)
        return True


def install_log_redaction():
    """Attach the filter to uvicorn's loggers, once.

    Called from create_app rather than left to the entry point: a token in a
    log file is a security property, not a configuration preference, and it
    must not depend on whoever hosts the app remembering to ask for it.
    """
    for name in _REDACT_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())


def uvicorn_kwargs():
    """The uvicorn settings this app's security depends on.

    `proxy_headers=False` is not a preference. `_client_key` — the pairing rate
    limiter's only key — reads `request.client.host`, and uvicorn defaults to
    `proxy_headers=True` with `forwarded_allow_ips` resolving to 127.0.0.1: it
    then replaces the client address with whatever the caller puts in
    `X-Forwarded-For` for every request that arrives from loopback. That is the
    documented deployment (behind Caddy or a Cloudflare Tunnel, where every
    request comes from loopback and the header is attacker-supplied), and it
    turns "5 attempts per 5 minutes per address" into "5 per attacker-chosen
    string" — which makes a 6-digit code brute-forceable inside its own TTL.

    A deployment that genuinely terminates at a proxy it trusts has to opt in
    explicitly, with a `forwarded_allow_ips` naming that proxy. The shipped
    default cannot be header-spoofable.
    """
    return {"proxy_headers": False, "forwarded_allow_ips": []}


def _client_key(request):
    """The address a pairing attempt is rate-limited against.

    The real socket peer — see `uvicorn_kwargs` for what keeps it that way.
    """
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def create_app(service, remote_state, web_dir=WEB_DIR, allowed_hosts=None):
    """The ASGI app for one host process.

    *service* is the shared `CrateBuilderService`; *remote_state* the shared
    `RemoteState`. Both are handed in rather than built here so the desktop
    window and this server are provably the same core — one job registry, one
    event bus, one token store.

    *allowed_hosts* REPLACES the built-in allow-list (address literals,
    localhost and this machine's own name); pass a set only to test the policy
    itself. The ordinary way to add a name is `RemoteState.extra_hosts`, which
    `--host-allow` writes and which is merged in below — read per request, so
    adding a name does not need a restart.
    """
    install_log_redaction()
    app = FastAPI(title="DJ-CrateBuilder", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    app.state.remote = remote_state

    def allowed_names():
        base = set(allowed_hosts) if allowed_hosts is not None else _local_names()
        return base | set(remote_state.extra_hosts())

    app.state.allowed_names = allowed_names

    @app.middleware("http")
    async def check_host(request: Request, call_next):
        if not host_is_allowed(request.headers.get("host"), allowed_names()):
            return JSONResponse({"detail": BAD_HOST_REASON}, status_code=421)
        return await call_next(request)

    def require_enabled():
        """Refuse every remote route while the host says remote access is off.

        Checked per request, not once at startup: "Allow remote control" reads
        as a live control and is the only thing standing between the user and
        an exposed host, so switching it off has to shut clients out of a mount
        that is already listening — whatever interface it happens to be on.
        """
        if not remote_state.get_flag("enabled"):
            raise HTTPException(status_code=403, detail=DISABLED_REASON)

    def device_for(request):
        """The paired device behind this request, or a 401.

        One message for every failure mode. A 401 that distinguished "no
        header" from "unknown token" would confirm to a prober which of its
        guesses had reached a real device row.
        """
        require_enabled()
        token = request.headers.get(TOKEN_HEADER) or ""
        device = remote_state.authenticate(token)
        if device is None:
            raise HTTPException(status_code=401, detail=UNPAIRED_REASON)
        return device

    def refused(message):
        return {"ok": False, "error": message}

    # ── remote log-watch bookkeeping ─────────────────────────────────────────
    # `logs.watch {on:true}` starts a ref-counted tail thread in the service,
    # and the only thing that decrements it is the client's own {on:false} —
    # which a closed tab or a phone that drops off Wi-Fi never sends, so the
    # 1 Hz poll thread and its bus traffic would outlive every viewer. The
    # event socket is the one thing that knows the viewer is gone, so the
    # counts are mirrored per device here and unwound when that device's LAST
    # socket closes. Local (pywebview) callers are untouched: their window
    # closing is the process ending.
    log_watches = {}                    # device id -> {log name: count}
    log_watches_lock = threading.Lock()

    def _note_log_watch(device_id, name, on):
        with log_watches_lock:
            held = log_watches.setdefault(device_id, {})
            if on:
                held[name] = held.get(name, 0) + 1
            else:
                left = held.get(name, 0) - 1
                if left > 0:
                    held[name] = left
                else:
                    held.pop(name, None)
            if not held:
                log_watches.pop(device_id, None)

    def _release_log_watches(device_id):
        """Hand back every watch this device still holds. Called once its last
        socket has gone, so nothing it opened can outlive it."""
        with log_watches_lock:
            held = log_watches.pop(device_id, None) or {}
        for name, count in held.items():
            for _ in range(count):
                try:
                    service.call("logs.watch", {"name": name, "on": False},
                                 transport=REMOTE)
                except Exception:
                    break       # the log is gone or already unwatched

    # ── pairing (the only routes reachable unpaired) ─────────────────────────

    @app.get("/pair/info")
    def pair_info():
        """What the pairing screen needs before it has a token. Deliberately
        one boolean — not whether a code is live, not how many devices are
        paired, neither of which an unpaired caller has any business knowing."""
        require_enabled()
        return {"require_pairing": remote_state.get_flag("require_pairing")}

    @app.post("/pair")
    def pair(body: PairBody, request: Request):
        require_enabled()
        try:
            claimed = remote_state.claim(body.code, body.device_name,
                                         client=_client_key(request))
        except PairingRefused as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        device = claimed["device"]
        # The host window's Remote Access card is showing the code that just
        # got used — tell it, rather than leaving it claiming nobody is paired
        # until something else happens to re-render it. A count and nothing
        # else: this goes out on the shared bus, so every remote socket sees
        # it, and the roster is the local window's to know. The card re-reads
        # `remote.config` on this signal anyway.
        service.emit("remote.devices", {"count": remote_state.device_count()})
        session = remote_state.session(device["id"], device["name"])
        return {"token": claimed["token"], "device": device,
                "session": session}

    # ── RPC ──────────────────────────────────────────────────────────────────

    @app.post("/rpc")
    def rpc(body: RpcBody, request: Request):
        """One contract method, in the same `{ok, result|error}` envelope the
        pywebview bridge returns — so web/api.js unwraps both the same way.

        A sync handler on purpose: Starlette runs it in its threadpool, which
        is where a service call that blocks on SQLite or yt-dlp belongs, and it
        is what makes the per-call transport (a thread-local) exact.
        """
        device = device_for(request)
        method = body.method or ""
        params = body.params or {}

        if method == remoteauth.SESSION_METHOD:
            return {"ok": True,
                    "result": remote_state.session(device["id"], device["name"])}
        if method in remoteauth.CONTROL_METHODS:
            return _control_call(method, device)

        allowed, reason = remote_state.method_allowed(method, device["id"])
        if not allowed:
            return refused(reason)
        remote_state.touch_control(device["id"])
        try:
            result = service.call(method, params, transport=REMOTE)
            if method == "logs.watch":
                _note_log_watch(device["id"], params.get("name"),
                                bool(params.get("on")))
            return {"ok": True, "result": result}
        except CBError as exc:
            return refused(str(exc))
        except Exception as exc:                      # never kill the mount
            return refused(f"Unexpected host error: {exc}")

    def _control_call(method, device):
        """The single-writer lock. Answered here rather than in the service:
        which device is asking is a fact of the connection, and the service
        deliberately knows nothing about connections."""
        if method == "remote.release_control":
            remote_state.release_control(device["id"])
            holder = remote_state.control_holder()
            service.emit("control.holder", holder or {})
            return {"ok": True,
                    "result": remote_state.session(device["id"], device["name"])}
        allowed, reason = remote_state.method_allowed(method, device["id"])
        if not allowed:
            return refused(reason)
        try:
            remote_state.claim_control(device["id"], device["name"])
        except ControlHeld as exc:
            return refused(str(exc))
        service.emit("control.holder", remote_state.control_holder() or {})
        return {"ok": True,
                "result": remote_state.session(device["id"], device["name"])}

    # ── log download (Task 5's remote path) ──────────────────────────────────

    @app.get("/logs/{name}")
    def download_log(name: str, request: Request):
        """The remote replacement for "System Viewer": the host's text editor
        cannot be opened from a browser, so the file is streamed instead.

        *name* never becomes a path here — the service maps "activity"/"debug"
        to the two files it owns and refuses anything else, so there is no
        caller-supplied path for a traversal to live in.
        """
        device = device_for(request)
        allowed, reason = remote_state.method_allowed("logs.download", device["id"])
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        try:
            path = service.call("logs.download", {"name": name},
                                transport=REMOTE)["path"]
        except CBError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not os.path.isfile(path):
            raise HTTPException(status_code=404,
                                detail="That log has not been written yet.")
        return FileResponse(path, media_type="text/plain; charset=utf-8",
                            filename=os.path.basename(path))

    # ── events ───────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def events(websocket: WebSocket):
        """One socket per client, carrying every host push as {type, payload}.

        Authentication happens BEFORE the subscription: a socket that cannot
        prove it is paired is closed having received nothing, so an unpaired
        listener can never see a track title, a channel name or a path.
        """
        enabled = remote_state.get_flag("enabled")
        # The Host allow-list is HTTP middleware, which a WebSocket scope never
        # reaches — this is the socket's half of the same defence, and it is
        # measured against the same set of names.
        origin_ok = origin_is_allowed(websocket.headers.get("origin"),
                                      websocket.headers.get("host"),
                                      allowed_names())
        token = websocket.query_params.get("token") or ""
        device = remote_state.authenticate(token) if enabled else None
        await websocket.accept()
        if not enabled:
            await websocket.close(code=WS_DISABLED)
            return
        if not origin_ok:
            # NOT 4401: the token is good and the deployment is wrong. A client
            # that read this as revocation would throw a valid token away.
            await websocket.close(code=WS_BAD_ORIGIN)
            return
        if device is None:
            await websocket.close(code=WS_UNAUTHORIZED)
            return

        loop = asyncio.get_running_loop()
        outbox = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        cut_code = None

        def _put(frame):
            """Enqueue on the loop thread, dropping the oldest when full."""
            if outbox.full():
                try:
                    outbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                outbox.put_nowait(frame)
            except asyncio.QueueFull:
                pass

        def deliver(event_type, payload):
            """Called from whatever worker thread emitted — hop to the loop."""
            try:
                loop.call_soon_threadsafe(
                    _put, {"type": event_type, "payload": payload})
            except RuntimeError:
                pass                # loop already closing; drop the frame

        def cut_socket(reason):
            """This device's access was withdrawn — revoked, or the master
            switch turned off. Called from whatever thread withdrew it, so it
            hops to the loop and ends the sender with the sentinel — the same
            clean stop a disconnect uses, never a mid-send cancellation.

            The two reasons close with different codes because they are
            different facts about the client's token: revoked means throw it
            away, disabled means keep it and keep retrying."""
            code = (WS_DISABLED if reason == CLOSE_DISABLED
                    else WS_UNAUTHORIZED)

            def stop():
                nonlocal cut_code
                cut_code = code
                _put(None)
            try:
                loop.call_soon_threadsafe(stop)
            except RuntimeError:
                pass

        unsubscribe = service.events.subscribe(deliver)
        conn_key = remote_state.register_connection(device["id"], cut_socket)
        reader = asyncio.create_task(_watch_for_disconnect(websocket, outbox))
        try:
            await websocket.send_json({
                "type": "host.status",
                "payload": {"online": True, "transport": "remote",
                            "session": remote_state.session(device["id"],
                                                            device["name"])}})
            # One sending coroutine, ended by a sentinel rather than by
            # cancellation: a send cancelled halfway leaves the connection in a
            # state neither side can finish tidily.
            while True:
                frame = await outbox.get()
                if frame is None:
                    break
                await websocket.send_json(frame)
            if cut_code is not None:
                # Cut mid-stream. An application close code rather than a plain
                # close, so the browser can tell revocation (drop the token,
                # show the pairing screen) from the master switch going down
                # (keep the token, keep retrying) instead of reconnecting with
                # a dead token every three seconds.
                await websocket.close(code=cut_code)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            unsubscribe()
            # Ref-counted: a reload opens the new socket before the old one
            # closes and a second tab is a second socket, so only the device's
            # LAST socket going releases the lock. The idle window in
            # claim_control covers the sockets that die badly.
            if remote_state.unregister_connection(device["id"], conn_key):
                service.emit("control.holder",
                             remote_state.control_holder() or {})
            # Same ref-count, one layer up: only the device's last socket
            # going means the viewer is really gone, and only then may the
            # log tails it opened be handed back (F6).
            if not remote_state.is_connected(device["id"]):
                _release_log_watches(device["id"])

    async def _watch_for_disconnect(websocket, outbox):
        """Nothing is expected from the client — this exists so a disconnect is
        noticed even while no event is being sent, and it ends the sender by
        putting the sentinel rather than by cancelling it."""
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            pass
        finally:
            if outbox.full():
                try:
                    outbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                outbox.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ── static bundle ────────────────────────────────────────────────────────
    # Mounted LAST so the routes above win. Unauthenticated by design: the
    # bundle IS the pairing screen, and it carries no host data — every byte
    # that means anything arrives through /rpc, which does require a token.

    if os.path.isdir(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def build(settings=None, db_path=None, remote_path=None, **kwargs):
    """A service + remote state + app for a process that hosts only the server.

    Convenience for `web_server.py` and for tests; a process that also opens
    the desktop window builds the service itself and calls `create_app`.
    """
    state = RemoteState(remote_path) if remote_path else None
    service = CrateBuilderService(transport=REMOTE, settings=settings,
                                  db_path=db_path, remote_state=state,
                                  **kwargs)
    return service, service.remote_state, create_app(service,
                                                     service.remote_state)

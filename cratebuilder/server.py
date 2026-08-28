"""FastAPI mount for the web bundle: RPC, the event socket, pairing and logs.

The remote half of the two-transport design (HANDOFF §2): the same `web/`
bundle the pywebview window loads, served over HTTP, driving the SAME
`CrateBuilderService` object the window drives. Nothing here duplicates an
action — every call lands in `service.call(..., transport="remote")`, which is
where `update.*` and `fs.*` are already refused server-side.

No tkinter, by the package's rule. TLS is out of scope on purpose (HANDOFF
§8.2): put this behind Caddy or a Cloudflare Tunnel; plain HTTP is LAN-only.
"""

import asyncio
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cratebuilder import remoteauth
from cratebuilder.remoteauth import (ControlHeld, PairingRefused, RemoteState,
                                     UNPAIRED_REASON)
from cratebuilder.service import REMOTE, CBError, CrateBuilderService

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
# instead of retrying forever.
WS_UNAUTHORIZED = 4401


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


def _client_key(request):
    """The address a pairing attempt is rate-limited against."""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def create_app(service, remote_state, web_dir=WEB_DIR):
    """The ASGI app for one host process.

    *service* is the shared `CrateBuilderService`; *remote_state* the shared
    `RemoteState`. Both are handed in rather than built here so the desktop
    window and this server are provably the same core — one job registry, one
    event bus, one token store.
    """
    install_log_redaction()
    app = FastAPI(title="DJ-CrateBuilder", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    app.state.remote = remote_state

    def device_for(request):
        """The paired device behind this request, or a 401.

        One message for every failure mode. A 401 that distinguished "no
        header" from "unknown token" would confirm to a prober which of its
        guesses had reached a real device row.
        """
        token = request.headers.get(TOKEN_HEADER) or ""
        device = remote_state.authenticate(token)
        if device is None:
            raise HTTPException(status_code=401, detail=UNPAIRED_REASON)
        return device

    def refused(message):
        return {"ok": False, "error": message}

    # ── pairing (the only routes reachable unpaired) ─────────────────────────

    @app.get("/pair/info")
    def pair_info():
        """What the pairing screen needs before it has a token. Deliberately
        one boolean — not whether a code is live, not how many devices are
        paired, neither of which an unpaired caller has any business knowing."""
        return {"require_pairing": remote_state.get_flag("require_pairing")}

    @app.post("/pair")
    def pair(body: PairBody, request: Request):
        try:
            claimed = remote_state.claim(body.code, body.device_name,
                                         client=_client_key(request))
        except PairingRefused as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        device = claimed["device"]
        # The host window's Remote Access card is showing the code that just
        # got used — tell it, rather than leaving it claiming nobody is paired
        # until something else happens to re-render it. Carries no token and no
        # code: only the same public rows `remote.devices` already returns.
        service.emit("remote.devices", {"devices": remote_state.devices()})
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
            return {"ok": True,
                    "result": service.call(method, params, transport=REMOTE)}
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
        token = websocket.query_params.get("token") or ""
        device = remote_state.authenticate(token)
        await websocket.accept()
        if device is None:
            await websocket.close(code=WS_UNAUTHORIZED)
            return

        loop = asyncio.get_running_loop()
        outbox = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)

        def deliver(event_type, payload):
            """Called from whatever worker thread emitted — hop to the loop."""
            def put():
                if outbox.full():
                    try:
                        outbox.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                outbox.put_nowait({"type": event_type, "payload": payload})
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass                # loop already closing; drop the frame

        unsubscribe = service.events.subscribe(deliver)
        remote_state.mark_connected(device["id"], True)
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
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            unsubscribe()
            # A clean disconnect frees the control lock immediately; the idle
            # window in claim_control covers the sockets that die badly.
            remote_state.mark_connected(device["id"], False)
            service.emit("control.holder", remote_state.control_holder() or {})

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

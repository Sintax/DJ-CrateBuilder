"""Scan worker: run one Watch List channel listing in a subprocess.

A yt-dlp flat-extraction is megabytes of JSON and regex — pure-Python work
that holds the GIL — so running it on a thread starves the thread painting
the window no matter how few run at once. A child process has its own
interpreter, so the UI stays at full speed while a scan runs.

Both halves of the boundary live in this one module: the parent side
(list_channel_isolated), the child side (worker_main, reached via
`python -m cratebuilder.scanproc` from source or `--scan-worker` on the
frozen exe), and the JSON codec they speak — shared, so the two sides
cannot drift apart. The child answers the listing through a real
YdlSession, keeping it the single read-only yt-dlp boundary; the typed
errors it raises are carried across the pipe and re-raised as the same
types, so callers cannot tell which side of the fence the session ran on.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict

from cratebuilder.settings import CookieConfig
from cratebuilder.ydl import (
    YdlError, YdlOffline, YdlPermanent, YdlUnclassified)


class ScanCancelled(Exception):
    """The caller asked to stop while the child was still listing. The child
    has been killed; nothing about the channel has been learned."""


class ScanWorkerError(RuntimeError):
    """The worker itself failed — could not run, crashed, timed out, or
    answered gibberish. Says nothing about the channel, so callers must
    treat it as transient, exactly like any other mid-scan breakage."""


# How long one listing may take before the child is presumed hung. A big
# channel over a slow link takes minutes; nothing legitimate takes fifteen.
WORKER_TIMEOUT = 900.0

# How often the waiting parent wakes to check for cancellation.
_POLL_SECONDS = 0.25

# Typed ydl error <-> wire kind. Every YdlError subclass must appear here or
# the child's verdict would arrive downgraded to a worker crash.
_KIND_BY_TYPE = {
    YdlOffline: "offline",
    YdlPermanent: "permanent",
    YdlUnclassified: "unclassified",
}
_TYPE_BY_KIND = {kind: type_ for type_, kind in _KIND_BY_TYPE.items()}

_CRASH_KIND = "crash"


# ── the wire codec, shared by both sides ─────────────────────────────────────
def encode_request(url, cookies=None, ignore_no_formats=False):
    """One listing request as a JSON line: the target URL plus the auth
    snapshot the child's YdlSession should carry."""
    return json.dumps({
        "url": url,
        "cookies": asdict(cookies) if cookies is not None else None,
        "ignore_no_formats": bool(ignore_no_formats),
    })


def decode_request(text):
    """Read a request back into (url, CookieConfig|None, ignore_no_formats).
    Raises on anything that is not a complete request — a half-written or
    foreign stdin must fail loudly, not scan the wrong thing."""
    req = json.loads(text)
    url = req["url"]
    if not isinstance(url, str) or not url:
        raise ValueError("request carries no url")
    cookies = req.get("cookies")
    return (url,
            CookieConfig(**cookies) if cookies is not None else None,
            bool(req.get("ignore_no_formats")))


def encode_result(entries):
    """A successful listing. default=str so one exotic value deep in a
    yt-dlp entry degrades to its text instead of killing the whole scan."""
    return json.dumps({"ok": True, "entries": entries}, default=str)


def encode_error(exc):
    """Any failure as JSON: a typed YdlError keeps its kind, message, intent
    and target; anything else is a crash of the worker itself."""
    if isinstance(exc, YdlError):
        return json.dumps({"ok": False,
                           "kind": _KIND_BY_TYPE.get(type(exc), _CRASH_KIND),
                           "message": exc.message,
                           "intent": exc.intent,
                           "target": exc.target})
    return json.dumps({"ok": False, "kind": _CRASH_KIND,
                       "message": f"{type(exc).__name__}: {exc}"})


def decode_result(text, returncode=0, stderr_tail=""):
    """Turn the child's stdout back into entries, or raise what it raised.

    A typed error comes back as the same YdlError subclass the child's
    session raised, so wl_scan_verdict_for keeps judging by type. Anything
    unreadable — empty output, garbage, an unknown kind — is a
    ScanWorkerError carrying the exit code and the tail of stderr, which is
    where the real traceback went."""
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("not an object")
    except ValueError:
        detail = f" — {stderr_tail.strip()}" if stderr_tail.strip() else ""
        raise ScanWorkerError(
            f"scan worker returned no readable answer "
            f"(exit code {returncode}){detail}")
    if result.get("ok"):
        entries = result.get("entries")
        return entries if isinstance(entries, list) else []
    kind = result.get("kind")
    message = result.get("message") or "scan worker failed"
    error_type = _TYPE_BY_KIND.get(kind)
    if error_type is None:
        raise ScanWorkerError(message)
    raise error_type(message, intent=result.get("intent"),
                     target=result.get("target"))


# ── the child ────────────────────────────────────────────────────────────────
def worker_main(stdin=None, stdout=None, session_factory=None):
    """Child entry: read one request, answer one listing, exit.

    The result pipe is claimed before any work runs and sys.stdout is
    repointed at stderr, so nothing yt-dlp or a cookie extractor prints can
    land inside the JSON the parent is parsing. Returns the exit code:
    0 when a decodable answer was written (a typed error is an answer),
    1 when the worker itself broke, 2 for an unreadable request."""
    out = stdout
    if out is None:
        out = sys.stdout
        sys.stdout = sys.stderr
    try:
        text = (stdin if stdin is not None else sys.stdin).read()
        try:
            url, cookies, ignore_no_formats = decode_request(text)
        except Exception as exc:
            out.write(encode_error(exc))
            return 2
        if session_factory is None:
            from cratebuilder.ydl import YdlSession
            session_factory = lambda c: YdlSession(
                cookies=c, debug=lambda line: print(line, file=sys.stderr))
        try:
            entries = session_factory(cookies).list_channel(
                url, ignore_no_formats=ignore_no_formats)
            out.write(encode_result(entries))
            return 0
        except YdlError as exc:
            out.write(encode_error(exc))
            return 0
        except Exception as exc:
            out.write(encode_error(exc))
            return 1
    finally:
        try:
            out.flush()
        except Exception:
            pass


# ── the parent ───────────────────────────────────────────────────────────────
def worker_command():
    """The (argv, cwd) that starts a worker on this install.

    Frozen, sys.executable is the app's own exe, which recognises
    --scan-worker before it touches Tk or the single-instance lock. From
    source it is the Python interpreter, pointed at this module with the
    repo root as cwd so the package resolves wherever the app was launched
    from."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--scan-worker"], None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-m", "cratebuilder.scanproc"], root


def list_channel_isolated(url, cookies=None, ignore_no_formats=False,
                          should_cancel=None, debug=None,
                          timeout=WORKER_TIMEOUT, command=None):
    """YdlSession.list_channel, answered by a subprocess.

    Same contract as the in-process intent — returns the raw yt-dlp entry
    dicts, raises the same typed errors — plus two things a thread cannot
    offer: the GIL stays free, and *should_cancel* (polled four times a
    second) kills the listing mid-flight instead of waiting it out, raising
    ScanCancelled. A worker that cannot answer at all raises
    ScanWorkerError; OSError from the spawn itself propagates, so a caller
    can fall back to listing in-process.

    *command* overrides the worker argv (tests drive stub children with it).
    """
    argv, cwd = (command, None) if command is not None else worker_command()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", creationflags=creationflags)
    try:
        try:
            proc.stdin.write(encode_request(url, cookies, ignore_no_formats))
            proc.stdin.close()
        except OSError:
            pass    # child died before reading; communicate() has the story
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if should_cancel is not None and should_cancel():
                    _kill(proc)
                    raise ScanCancelled(url)
                if time.monotonic() >= deadline:
                    _kill(proc)
                    raise ScanWorkerError(
                        f"scan worker timed out after {int(timeout)}s")
    except BaseException:
        _kill(proc)
        raise
    _forward_stderr(err, debug)
    return decode_result(out, returncode=proc.returncode,
                         stderr_tail=err[-300:] if err else "")


def _kill(proc):
    """Stop a child for good, reaping it so no zombie outlives the scan."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _forward_stderr(err, debug):
    """Relay the child's stderr — its session's redacted opts line, yt-dlp
    warnings, any traceback — into the parent's debug sink."""
    if not debug or not err:
        return
    for line in err.strip().splitlines():
        if line.strip():
            debug(f"SCAN WORKER | {line.rstrip()}")


if __name__ == "__main__":
    sys.exit(worker_main())

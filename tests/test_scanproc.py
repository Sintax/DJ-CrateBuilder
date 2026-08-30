"""The scan worker: the wire codec, the child, and the parent driving real
subprocesses — plus the app-side wiring that hands a scan to it.
"""
import io
import os
import subprocess
import sys
import time

import pytest

from cratebuilder import scanproc
from cratebuilder.settings import CookieConfig
from cratebuilder.ydl import (
    YdlError, YdlOffline, YdlPermanent, YdlUnclassified)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COOKIES = CookieConfig(use_cookies=True, cookie_method="Browser",
                       cookies_browser="Firefox", cookies_profile="work",
                       cookie_file="")

ENTRIES = [{"id": "v1", "title": "Track One", "duration": 300},
           {"id": "v2", "title": "Track Two", "duration": 200}]


# ── the wire codec ───────────────────────────────────────────────────────────
def test_a_request_round_trips_its_cookie_snapshot():
    url, cookies, ignore = scanproc.decode_request(
        scanproc.encode_request("https://yt/c", COOKIES, True))
    assert (url, cookies, ignore) == ("https://yt/c", COOKIES, True)


def test_a_request_with_no_cookies_round_trips_none():
    _url, cookies, ignore = scanproc.decode_request(
        scanproc.encode_request("https://yt/c"))
    assert cookies is None and ignore is False


@pytest.mark.parametrize("text", ["", "{}", '{"url": ""}', "not json"])
def test_an_incomplete_request_refuses_to_decode(text):
    with pytest.raises(Exception):
        scanproc.decode_request(text)


def test_a_listing_round_trips_its_entries():
    assert scanproc.decode_result(scanproc.encode_result(ENTRIES)) == ENTRIES


def test_an_exotic_entry_value_degrades_to_text_instead_of_crashing():
    """default=str: one non-JSON value deep in a yt-dlp entry must not cost
    the whole channel its scan."""
    entries = [{"id": "v1", "odd": {1, 2}}]
    decoded = scanproc.decode_result(scanproc.encode_result(entries))
    assert decoded[0]["id"] == "v1"
    assert isinstance(decoded[0]["odd"], str)


@pytest.mark.parametrize("error_type", [YdlOffline, YdlPermanent,
                                        YdlUnclassified])
def test_a_typed_error_crosses_the_pipe_as_the_same_type(error_type):
    """wl_scan_verdict_for judges by exception type, so the child's verdict
    must arrive as exactly the class the child's session raised."""
    encoded = scanproc.encode_error(
        error_type("boom", intent="list_channel", target="https://yt/c"))
    with pytest.raises(error_type) as info:
        scanproc.decode_result(encoded)
    assert not type(info.value) is YdlError
    assert info.value.message == "boom"
    assert info.value.intent == "list_channel"
    assert info.value.target == "https://yt/c"


def test_every_ydl_error_type_has_a_wire_kind():
    """A YdlError subclass missing from the map would cross the pipe as a
    worker crash — silently downgrading a real verdict to transient."""
    for error_type in (YdlOffline, YdlPermanent, YdlUnclassified):
        assert error_type in scanproc._KIND_BY_TYPE


def test_a_worker_crash_decodes_to_a_worker_error():
    with pytest.raises(scanproc.ScanWorkerError, match="ValueError: broke"):
        scanproc.decode_result(scanproc.encode_error(ValueError("broke")))


def test_unreadable_output_names_the_exit_code_and_stderr():
    with pytest.raises(scanproc.ScanWorkerError) as info:
        scanproc.decode_result("Traceback (most...", returncode=3,
                               stderr_tail="MemoryError: boom")
    assert "exit code 3" in str(info.value)
    assert "MemoryError: boom" in str(info.value)


# ── the child, in-process ────────────────────────────────────────────────────
def _run_worker(request_text, session_factory):
    out = io.StringIO()
    code = scanproc.worker_main(stdin=io.StringIO(request_text), stdout=out,
                                session_factory=session_factory)
    return code, out.getvalue()


class _Session:
    def __init__(self, entries=None, error=None):
        self.calls = []
        self._entries, self._error = entries, error

    def list_channel(self, url, ignore_no_formats=False):
        self.calls.append((url, ignore_no_formats))
        if self._error is not None:
            raise self._error
        return list(self._entries)


def test_the_worker_answers_a_listing_with_the_cookies_it_was_sent():
    seen = {}

    def factory(cookies):
        seen["cookies"] = cookies
        return _Session(entries=ENTRIES)

    code, out = _run_worker(
        scanproc.encode_request("https://yt/c", COOKIES), factory)
    assert code == 0
    assert scanproc.decode_result(out) == ENTRIES
    assert seen["cookies"] == COOKIES


def test_the_worker_passes_ignore_no_formats_through():
    session = _Session(entries=[])
    _run_worker(scanproc.encode_request("https://yt/c", None, True),
                lambda c: session)
    assert session.calls == [("https://yt/c", True)]


def test_a_typed_failure_is_an_answer_not_a_crash():
    """Exit 0: the worker did its job — the channel's verdict IS the result."""
    code, out = _run_worker(
        scanproc.encode_request("https://yt/c"),
        lambda c: _Session(error=YdlPermanent("gone", intent="list_channel",
                                              target="https://yt/c")))
    assert code == 0
    with pytest.raises(YdlPermanent, match="gone"):
        scanproc.decode_result(out)


def test_a_worker_crash_exits_nonzero_but_still_answers():
    code, out = _run_worker(scanproc.encode_request("https://yt/c"),
                            lambda c: _Session(error=ValueError("broke")))
    assert code == 1
    with pytest.raises(scanproc.ScanWorkerError):
        scanproc.decode_result(out)


def test_an_unreadable_request_exits_2():
    code, out = _run_worker("garbage", lambda c: _Session(entries=[]))
    assert code == 2
    with pytest.raises(scanproc.ScanWorkerError):
        scanproc.decode_result(out)


# ── the parent, against real child processes ─────────────────────────────────
def _stub_worker_command(body):
    """A real child speaking the real protocol: runs worker_main with a fake
    session defined by *body* (python source for a `factory(cookies)`)."""
    script = (f"import sys; sys.path.insert(0, {REPO_ROOT!r})\n"
              f"from cratebuilder import scanproc\n"
              f"from cratebuilder.ydl import *\n"
              f"{body}\n"
              f"sys.exit(scanproc.worker_main(session_factory=factory))")
    return [sys.executable, "-c", script]


def test_a_listing_survives_the_real_pipes():
    entries = scanproc.list_channel_isolated(
        "https://yt/c", cookies=COOKIES,
        command=_stub_worker_command(
            "class S:\n"
            "    def list_channel(self, url, ignore_no_formats=False):\n"
            "        return [{'id': 'v1', 'title': 'Track One'}]\n"
            "factory = lambda c: S()"))
    assert entries == [{"id": "v1", "title": "Track One"}]


def test_a_typed_error_survives_the_real_pipes():
    with pytest.raises(YdlPermanent, match="gone for real"):
        scanproc.list_channel_isolated(
            "https://yt/c",
            command=_stub_worker_command(
                "class S:\n"
                "    def list_channel(self, url, ignore_no_formats=False):\n"
                "        raise YdlPermanent('gone for real',\n"
                "                           intent='list_channel')\n"
                "factory = lambda c: S()"))


def test_cancelling_kills_the_child_instead_of_waiting_it_out():
    """The capability a thread never had: a cancelled scan stops now."""
    started = time.monotonic()
    with pytest.raises(scanproc.ScanCancelled):
        scanproc.list_channel_isolated(
            "https://yt/c", should_cancel=lambda: True,
            command=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert time.monotonic() - started < 20


def test_a_hung_worker_times_out_as_a_worker_error():
    started = time.monotonic()
    with pytest.raises(scanproc.ScanWorkerError, match="timed out"):
        scanproc.list_channel_isolated(
            "https://yt/c", timeout=1.0,
            command=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert time.monotonic() - started < 20


def test_a_dead_child_reports_its_exit_code_and_forwards_stderr():
    forwarded = []
    with pytest.raises(scanproc.ScanWorkerError) as info:
        scanproc.list_channel_isolated(
            "https://yt/c", debug=forwarded.append,
            command=[sys.executable, "-c",
                     "import sys; sys.stderr.write('boom trace'); "
                     "sys.exit(3)"])
    assert "exit code 3" in str(info.value)
    assert "boom trace" in str(info.value)
    assert any("boom trace" in line for line in forwarded)


def test_worker_command_from_source_points_at_this_module():
    argv, cwd = scanproc.worker_command()
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "cratebuilder.scanproc"]
    assert cwd == REPO_ROOT


def test_worker_command_frozen_relaunches_the_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\app\DJ-CrateBuilder.exe")
    argv, cwd = scanproc.worker_command()
    assert argv == [r"C:\app\DJ-CrateBuilder.exe", "--scan-worker"]
    assert cwd is None


def test_python_dash_m_reaches_the_worker():
    """The from-source entry, for real: -m must resolve and hand stdin to
    worker_main (a garbage request answered as exit 2 proves both)."""
    proc = subprocess.run(
        [sys.executable, "-m", "cratebuilder.scanproc"], cwd=REPO_ROOT,
        input="garbage", capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    with pytest.raises(scanproc.ScanWorkerError):
        scanproc.decode_result(proc.stdout)


def test_the_frozen_flag_reaches_the_worker_before_tk_or_the_lock():
    """`DJ-CrateBuilder --scan-worker` must answer the protocol and exit —
    never open a window, and never touch the single-instance port (a worker
    that did would poke the running app and exit with no answer). Driven
    from source the same way the frozen exe runs it."""
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "DJ-CrateBuilder_v2.0.py"),
         "--scan-worker"], cwd=REPO_ROOT,
        input="garbage", capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    with pytest.raises(scanproc.ScanWorkerError):
        scanproc.decode_result(proc.stdout)


# ── the app-side wiring ──────────────────────────────────────────────────────
def test_the_scan_hands_the_worker_the_live_cookie_snapshot(app, monkeypatch):
    app._use_cookies.set(True)
    app._cookie_method.set("Browser")
    app._cookies_browser.set("Firefox")
    seen = {}

    def fake(url, cookies=None, should_cancel=None, debug=None, **kw):
        seen.update(url=url, cookies=cookies, should_cancel=should_cancel)
        return []

    monkeypatch.setattr(scanproc, "list_channel_isolated", fake)
    app._scan_list_channel("https://yt/c", cid=7)
    assert seen["url"] == "https://yt/c"
    assert seen["cookies"].use_cookies is True
    assert seen["cookies"].cookies_browser == "Firefox"


def test_the_scan_cancel_predicate_sees_both_cancel_channels(app,
                                                             monkeypatch):
    """Cancel All and the per-card ✕ must both kill the child mid-listing."""
    seen = {}
    monkeypatch.setattr(
        scanproc, "list_channel_isolated",
        lambda url, should_cancel=None, **kw:
            seen.update(should_cancel=should_cancel) or [])
    app._scan_list_channel("https://yt/c", cid=7)
    cancel = seen["should_cancel"]
    assert cancel() is False
    app._wl_cancel_cids.add(7)
    assert cancel() is True
    app._wl_cancel_cids.discard(7)
    app._cancel_flag.set()
    assert cancel() is True
    app._cancel_flag.clear()


def test_a_worker_that_cannot_start_falls_back_in_process(app, monkeypatch):
    """One laggy scan beats a Watch List that cannot scan at all."""
    def refuse(*a, **kw):
        raise OSError("blocked by policy")

    class _Session:
        def list_channel(self, url):
            return ENTRIES

    monkeypatch.setattr(scanproc, "list_channel_isolated", refuse)
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _Session())
    assert app._scan_list_channel("https://yt/c", cid=7) == ENTRIES


def test_a_cancelled_listing_leaves_the_card_idle(app, monkeypatch):
    """ScanCancelled from a killed child must land exactly where the flag
    checks land: status idle, nothing about the channel changed."""
    cid = app._db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCkill/videos",
        display_name="Killed Mid Scan", platform="YouTube", genre="DnB")
    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))

    def cancelled(url, cid_):
        raise scanproc.ScanCancelled(url)

    monkeypatch.setattr(app, "_scan_list_channel", cancelled)
    app._watchlist_scan_channel(cid)
    row = app._db.get_watchlist_channel(cid)
    assert row["status"] == "idle"
    assert app._wl_scan_active == 0

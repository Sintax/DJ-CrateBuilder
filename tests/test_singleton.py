"""Tests for the single-instance loopback lock."""
import os
import socket
import sys
import threading
import time

import pytest

from cratebuilder import singleton
from cratebuilder.singleton import (
    acquire_single_instance, request_show, listen_for_show_requests,
    SINGLE_INSTANCE_PORT)


def _free_port():
    """Grab an ephemeral port, then release it so the test can re-bind it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_first_acquire_succeeds():
    port = _free_port()
    sock = acquire_single_instance(port)
    try:
        assert sock is not None
    finally:
        if sock:
            sock.close()


def test_second_acquire_on_same_port_returns_none():
    port = _free_port()
    first = acquire_single_instance(port)
    try:
        assert first is not None
        assert acquire_single_instance(port) is None   # lock already held
    finally:
        if first:
            first.close()


def test_lock_releases_when_socket_closed():
    port = _free_port()
    first = acquire_single_instance(port)
    assert first is not None
    first.close()                                       # simulate process exit
    second = acquire_single_instance(port)              # must reclaim the port
    try:
        assert second is not None
    finally:
        if second:
            second.close()


def test_default_port_is_in_private_range():
    assert 49152 <= SINGLE_INSTANCE_PORT <= 65535


def test_request_show_triggers_listener_callback():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        event = threading.Event()
        listen_for_show_requests(holder, event.set)

        request_show(port)

        assert event.wait(timeout=2), "listener callback was never invoked"
    finally:
        holder.close()


def test_request_show_is_a_noop_when_nothing_is_listening():
    port = _free_port()
    # No instance holds the port — must not raise.
    request_show(port, timeout=0.2)


def test_listener_stops_when_socket_closed():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    listen_for_show_requests(holder, lambda: None)
    holder.close()
    time.sleep(0.2)
    # The listener thread's accept() loop should have exited cleanly; a
    # fresh acquire on the same port must succeed once it's released.
    second = acquire_single_instance(port)
    try:
        assert second is not None
    finally:
        if second:
            second.close()


from cratebuilder.singleton import forward_add, listen_for_requests


def test_forward_add_delivers_uri_to_on_add():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        got = []
        event = threading.Event()
        listen_for_requests(holder, on_show=event.set,
                            on_add=lambda u: (got.append(u), event.set()))
        uri = "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa"
        forward_add(port, uri)
        assert event.wait(timeout=2)
        assert got == [uri]
    finally:
        holder.close()


def test_show_still_dispatches_to_on_show_not_on_add():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()
        added = []
        listen_for_requests(holder, on_show=shown.set, on_add=added.append)
        request_show(port)
        assert shown.wait(timeout=2)
        assert added == []
    finally:
        holder.close()


def test_add_without_on_add_handler_is_ignored_not_fatal():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()
        listen_for_requests(holder, on_show=shown.set)   # no on_add
        forward_add(port, "djcrate://add?v=1")
        request_show(port)                               # listener must survive
        assert shown.wait(timeout=2)
    finally:
        holder.close()


def test_oversized_line_is_capped_and_does_not_kill_listener():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        got = []
        event = threading.Event()

        def on_add(u):
            got.append(u)
            if u == "djcrate://ok":
                event.set()

        listen_for_requests(holder, on_show=lambda: None, on_add=on_add)
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.sendall(b"add " + b"x" * 20000 + b"\n")
        forward_add(port, "djcrate://ok")
        assert event.wait(timeout=2)
        assert len(got[0]) <= 8192
        assert got[-1] == "djcrate://ok"
    finally:
        holder.close()


def test_a_throwing_on_add_does_not_kill_the_listener():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()

        def boom(_uri):
            raise RuntimeError("handler bug")

        listen_for_requests(holder, on_show=shown.set, on_add=boom)
        forward_add(port, "djcrate://add?v=1")
        request_show(port)
        assert shown.wait(timeout=2)
    finally:
        holder.close()


def test_forward_add_is_a_noop_when_nothing_is_listening():
    port = _free_port()
    forward_add(port, "djcrate://add?v=1", timeout=0.2)   # must not raise


def test_a_connection_that_cannot_be_timed_out_is_just_an_empty_line():
    """settimeout raises on a socket the peer has already torn down. It used
    to sit outside the try, so that exception escaped the listener's while
    loop and killed the listener for the rest of the run — one dropped
    connection and djcrate:// stopped working until the app restarted."""
    class Dead:
        def settimeout(self, _secs):
            raise OSError("socket is closed")

        def recv(self, _n):        # pragma: no cover - must never be reached
            raise AssertionError("recv on a socket that cannot be timed out")

    assert singleton._read_line(Dead()) == ""


# ── Handing the foreground to the running instance ──────────────────────────

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="the owner lookup is Windows-only")


@windows_only
def test_listener_pid_finds_the_process_holding_the_lock():
    """A second launch needs the running instance's PID to hand it the
    foreground, and the lock port is the one thing both processes share."""
    port = _free_port()
    holder = acquire_single_instance(port)
    try:
        assert singleton.listener_pid(port) == os.getpid()
    finally:
        holder.close()


def test_listener_pid_is_none_when_nothing_holds_the_port():
    assert singleton.listener_pid(_free_port()) is None


def test_grant_foreground_hands_it_to_the_lock_holder(monkeypatch):
    granted = []
    monkeypatch.setattr(singleton, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(singleton, "_allow_set_foreground",
                        lambda pid: granted.append(pid) or True)
    assert singleton.grant_foreground(49737) is True
    assert granted == [4242]


def test_grant_foreground_does_nothing_without_a_holder(monkeypatch):
    granted = []
    monkeypatch.setattr(singleton, "listener_pid", lambda port: None)
    monkeypatch.setattr(singleton, "_allow_set_foreground", granted.append)
    assert singleton.grant_foreground(49737) is False
    assert granted == []


def test_grant_foreground_never_raises(monkeypatch):
    """Best-effort like request_show: a lookup that blows up must not stop
    the second launch handing its link over and exiting."""
    def boom(_port):
        raise OSError("table unavailable")
    monkeypatch.setattr(singleton, "listener_pid", boom)
    assert singleton.grant_foreground(49737) is False

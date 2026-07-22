"""Tests for the single-instance loopback lock."""
import socket
import threading
import time

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

"""Tests for the single-instance loopback lock."""
import socket

from cratebuilder.singleton import acquire_single_instance, SINGLE_INSTANCE_PORT


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

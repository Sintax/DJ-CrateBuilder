"""Single-instance lock via a loopback socket bind (Tk-free, stdlib only).

Binding 127.0.0.1:<port> succeeds for exactly one process at a time. The OS
holds the port for that process's lifetime and frees it the instant the process
ends — cleanly or by crash — so no stale lock is ever left behind. A second
process that tries to bind the same port gets OSError, which is our "already
running" signal.
"""
import socket

# Fixed, obscure loopback port in the private range (49152-65535). Not
# configurable by design (YAGNI) — see the design doc's trade-off note.
SINGLE_INSTANCE_PORT = 49737


def acquire_single_instance(port):
    """Try to claim the single-instance lock by binding a loopback socket.

    Returns the bound socket on success. The CALLER MUST keep a reference to it
    for the whole process lifetime — if it is garbage-collected the socket
    closes and the lock is silently released. Returns None when the port is
    already bound (another instance holds the lock) or binding otherwise fails.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: we WANT a second bind to fail. Loopback only — never
        # exposed to the network, and no listen() is needed to hold the port.
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock

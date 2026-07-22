"""Single-instance lock via a loopback socket bind (Tk-free, stdlib only).

Binding 127.0.0.1:<port> succeeds for exactly one process at a time. The OS
holds the port for that process's lifetime and frees it the instant the process
ends — cleanly or by crash — so no stale lock is ever left behind. A second
process that tries to bind the same port gets OSError, which is our "already
running" signal. The lock-holder also listens on the socket so a second
launch can ask it to restore its window instead of just exiting silently.
"""
import socket
import threading

# Fixed, obscure loopback port in the private range (49152-65535). Not
# configurable by design (YAGNI) — see the design doc's trade-off note.
SINGLE_INSTANCE_PORT = 49737


def acquire_single_instance(port):
    """Try to claim the single-instance lock by binding+listening on a
    loopback socket.

    Returns the bound socket on success. The CALLER MUST keep a reference to it
    for the whole process lifetime — if it is garbage-collected the socket
    closes and the lock is silently released. Returns None when the port is
    already bound (another instance holds the lock) or binding otherwise fails.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: we WANT a second bind to fail. Loopback only — never
        # exposed to the network.
        sock.bind(("127.0.0.1", port))
        sock.listen(5)
    except OSError:
        sock.close()
        return None
    return sock


def request_show(port, timeout=0.5):
    """Ask an already-running instance holding *port* to restore its window.

    Best-effort: called by a second launch that lost the single-instance
    race. If the connect fails (instance is shutting down, firewall, etc.)
    this just does nothing — the second launch still exits without starting
    a duplicate app.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"show")
    except OSError:
        pass


def listen_for_show_requests(sock, on_show):
    """Run on a daemon thread, calling on_show() for every connection
    accepted on *sock*. *on_show* must marshal back to the UI thread itself
    (this thread is not the Tk main thread). Returns once *sock* is closed.
    """
    def _loop():
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                conn.recv(16)
            except OSError:
                pass
            finally:
                conn.close()
            on_show()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t

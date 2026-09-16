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


def forward_add(port, uri, timeout=0.5):
    """Relay a djcrate:// URI to the already-running instance holding *port*.

    Best-effort like request_show: called by a second launch that lost the
    bind race while carrying a protocol-handler argument. Wire format per the
    extension repo's docs/specs/djcrate-uri-v1.md §2: 'add <uri>\\n', UTF-8.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"add " + uri.encode("utf-8") + b"\n")
    except OSError:
        pass


def _read_line(conn, cap=8192):
    """Read one newline-terminated line (or until EOF/cap) from *conn*.

    Replaces the old fixed conn.recv(16): the 'add' verb carries a URI that
    doesn't fit in 16 bytes. The cap stops a hostile local writer growing the
    buffer without bound; the timeout stops a silent connection parking the
    listener thread forever.
    """
    chunks, total = [], 0
    conn.settimeout(1.0)
    try:
        while total < cap:
            data = conn.recv(1024)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if b"\n" in data:
                break
    except OSError:
        pass
    return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def listen_for_requests(sock, on_show, on_add=None):
    """Run on a daemon thread, dispatching one verb per connection accepted on
    *sock*: 'show' (or a bare/legacy connection) calls on_show(); 'add <uri>'
    calls on_add(uri) when a handler was given. Unknown verbs are ignored, and
    a callback that raises is swallowed — nothing a browser sends may stop
    the next connection being served. Callbacks run on this thread, not a UI
    thread. Returns once *sock* is closed.
    """
    def _loop():
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                line = _read_line(conn)
            finally:
                conn.close()
            try:
                if line.startswith("add "):
                    if on_add is not None:
                        on_add(line[4:].strip())
                elif line == "show" or not line:
                    on_show()
                # anything else: unknown verb, ignore (contract §4)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def listen_for_show_requests(sock, on_show):
    """Back-compat alias from the two-verb widening; new callers should use
    listen_for_requests."""
    return listen_for_requests(sock, on_show)

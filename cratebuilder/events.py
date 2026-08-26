"""In-process event bus and a coalescer for high-rate progress events."""

import threading
import time

DEFAULT_COALESCED_TYPES = ("progress.current", "progress.overall")
DEFAULT_INTERVAL = 0.25

_MISSING = object()


class EventBus:
    """Thread-safe publish/subscribe: subscribers never break each other."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self, fn):
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe():
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)
        return unsubscribe

    def emit(self, type, payload):
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(type, payload)
            except Exception:
                pass


class Coalescer:
    """Rate-limits chosen event types before forwarding them to an EventBus.

    A coalesced type is forwarded immediately if at least `interval` seconds
    have passed since the last forward of that type; otherwise the latest
    payload is stashed and a one-shot timer flushes it if nothing else
    arrives to flush it first. Other types pass straight through.
    """

    def __init__(self, bus, coalesced_types=DEFAULT_COALESCED_TYPES,
                 interval=DEFAULT_INTERVAL, now=None):
        self._bus = bus
        self._coalesced_types = set(coalesced_types)
        self._interval = interval
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._last_sent = {}
        self._pending = {}
        self._timers = {}

    def emit(self, type, payload):
        if type not in self._coalesced_types:
            self._bus.emit(type, payload)
            return
        send_now = False
        with self._lock:
            now = self._now()
            last = self._last_sent.get(type)
            if last is None or now - last >= self._interval:
                self._last_sent[type] = now
                self._pending.pop(type, None)
                send_now = True
            else:
                self._pending[type] = payload
                self._schedule(type)
        if send_now:
            self._bus.emit(type, payload)

    def flush(self):
        """Force any pending payloads out now — call before terminal events."""
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
            now = self._now()
            for type in pending:
                self._last_sent[type] = now
                timer = self._timers.pop(type, None)
                if timer is not None:
                    timer.cancel()
        for type, payload in pending.items():
            self._bus.emit(type, payload)

    def _schedule(self, type):
        if type in self._timers:
            return          # already scheduled — will pick up the latest payload
        timer = threading.Timer(self._interval, self._on_timer, args=(type,))
        timer.daemon = True
        self._timers[type] = timer
        timer.start()

    def _on_timer(self, type):
        with self._lock:
            self._timers.pop(type, None)
            payload = self._pending.pop(type, _MISSING)
            if payload is not _MISSING:
                self._last_sent[type] = self._now()
        if payload is not _MISSING:
            self._bus.emit(type, payload)
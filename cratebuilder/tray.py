"""System-tray icon wrapper (Windows). Tk-free: all UI actions are marshalled
back to the main thread through the `schedule` callback passed in."""
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None
    Image = None


def _default_image():
    """A simple 64x64 icon drawn at runtime (no asset file needed)."""
    img = Image.new("RGB", (64, 64), (20, 20, 20))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 54, 54), fill=(220, 30, 40))
    d.ellipse((26, 26, 38, 38), fill=(20, 20, 20))
    return img


class TrayIcon:
    """Owns a pystray.Icon. on_open/on_scan/on_download/on_quit are zero-arg
    callables that must be safe to call (they will be wrapped via `schedule`).
    download_text is an optional callable returning the live label for the
    Download-All-New menu item (so it mirrors the Watch List button)."""

    def __init__(self, schedule, on_open, on_scan, on_download, on_quit,
                 download_text=None, image=None):
        self._schedule = schedule
        self._icon = None
        self._thread = None
        self._image = image or (_default_image() if Image else None)
        self._on_open = on_open
        self._on_scan = on_scan
        self._on_download = on_download
        self._on_quit = on_quit
        self._download_text = download_text or (lambda *_: "Download All New")

    @property
    def available(self):
        return pystray is not None and self._image is not None

    def start(self):
        """Create and run the tray icon on a daemon thread.

        Returns True if started, False if unavailable or already running.
        Not thread-safe: call from a single thread (the app calls it only
        from the Tk main thread).
        """
        if not self.available or self._icon is not None:
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda *_: self._schedule(self._on_open),
                             default=True),
            pystray.MenuItem("Scan Now", lambda *_: self._schedule(self._on_scan)),
            # Text is a callable so the label tracks the live pending-new count,
            # mirroring the Watch List 'Download All New (N)' button.
            pystray.MenuItem(self._download_text,
                             lambda *_: self._schedule(self._on_download)),
            pystray.MenuItem("Quit", lambda *_: self._schedule(self._on_quit)),
        )
        self._icon = pystray.Icon("DJ-CrateBuilder", self._image,
                                  "DJ-CrateBuilder", menu)
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        return True

    def set_title(self, text):
        """Update the hover tooltip shown over the tray icon. No-op if the
        icon isn't running or the backend rejects the update."""
        if self._icon is not None:
            try:
                self._icon.title = text
            except Exception:
                pass

    def notify(self, message, title="DJ-CrateBuilder"):
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
                return True
            except Exception:
                return False
        return False

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

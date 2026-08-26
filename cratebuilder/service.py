"""Transport-agnostic action surface shared by the local window and remote clients."""

import itertools
import os
import re
import sys
import threading

from cratebuilder import ui_strings, util
from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase
from cratebuilder.events import Coalescer, EventBus
from cratebuilder.settings import Settings

MAIN_SCRIPT = "DJ-CrateBuilder_v1.3.py"
DB_NAME = "cratebuilder.db"

LOCAL = "local"
REMOTE = "remote"

# Method prefixes the remote transport must refuse server-side. The design's
# rule is that a browser elsewhere can never replace the binary it is talking
# to, and that only the host may see the host's filesystem — so this is checked
# here, not left to a client to respect.
LOCAL_ONLY = ("update.", "fs.")

_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.M)
_BUILD_RE = re.compile(r"^APP_BUILD\s*=\s*(\d+)", re.M)


class CBError(Exception):
    """A failure with a message meant to be shown to the user as-is."""


def repo_root():
    """The directory holding the v1.3 script — the app's runtime data dir.

    Resolved from this file rather than sys.argv[0]: the web frontend is
    launched by its own entry point, which would otherwise point
    runtime_data_dir at the wrong folder and open a second, empty database.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    return util.runtime_data_dir(os.path.join(repo_root(), MAIN_SCRIPT))


def version_info(script_path=None):
    """APP_VERSION / APP_BUILD read from the monolith as text.

    Parsed rather than imported: importing the v1.3 script builds a Tk window,
    which would drag the whole service into the gui test lane for two constants.
    """
    path = script_path or os.path.join(repo_root(), MAIN_SCRIPT)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return {"version": None, "build": None}
    version = _VERSION_RE.search(source)
    build = _BUILD_RE.search(source)
    return {"version": version.group(1) if version else None,
            "build": int(build.group(1)) if build else None}


class CrateBuilderService:
    """Every UI action, transport-agnostic.

    Raises CBError with a user-facing message; never returns a tkinter widget
    or a Tk variable. One instance per host process — the batch it owns is the
    web frontend's queue, kept here rather than in a widget so both transports
    see the same list.
    """

    def __init__(self, transport=LOCAL, settings=None, db_path=None):
        if transport not in (LOCAL, REMOTE):
            raise ValueError(f"unknown transport: {transport}")
        self.transport = transport
        self._settings = settings or Settings()
        self._db_path = db_path or os.path.join(app_dir(), DB_NAME)
        self._lock = threading.Lock()
        self._batch = []
        self._ids = itertools.count(1)
        self.events = EventBus()
        self._emit = Coalescer(self.events)
        self._jobs = {}

    # ── events / jobs ─────────────────────────────────────────────────────────

    def emit(self, type, payload):
        self._emit.emit(type, payload)

    def _job_running(self, category):
        with self._lock:
            return category in self._jobs

    def _start_job(self, category, target, *args):
        """Run `target` on a daemon thread; refuse a second job per category."""
        with self._lock:
            if category in self._jobs:
                raise CBError(f"A {category} job is already running.")
            job_id = next(self._ids)
            self._jobs[category] = job_id

        def run():
            try:
                target(*args)
            finally:
                with self._lock:
                    self._jobs.pop(category, None)

        threading.Thread(target=run, daemon=True).start()
        return job_id

    # ── dispatch ──────────────────────────────────────────────────────────────

    def call(self, method, params=None):
        """Route one contract method name to its handler.

        The single entry point both transports use, so the local pywebview
        bridge and a future WebSocket RPC cannot drift in what they accept.
        """
        if self.transport == REMOTE and method.startswith(LOCAL_ONLY):
            raise CBError("That action is only available in the app window on "
                          "the host machine.")
        handler = self._methods().get(method)
        if handler is None:
            raise CBError(f"Unknown action: {method}")
        return handler(dict(params or {}))

    def _methods(self):
        return {
            "state.snapshot": lambda p: self.snapshot(),
            "ui_strings": lambda p: self.ui_strings(),
            "settings.get": lambda p: self.settings_get(p.get("key")),
            "settings.set": lambda p: self.settings_set(p.get("key"),
                                                        p.get("value")),
            "batch.list": lambda p: self.batch_list(),
            "batch.add": lambda p: self.batch_add(p.get("url"), p.get("genre"),
                                                  p.get("platform")),
            "batch.remove": lambda p: self.batch_remove(p.get("id")),
            "batch.move": lambda p: self.batch_move(p.get("id"),
                                                    p.get("delta", 0)),
            "batch.clear": lambda p: self.batch_clear(),
            "batch.skip": lambda p: self.batch_skip(p.get("id")),
            "watchlist.list": lambda p: self.watchlist_list(),
            "db.groups": lambda p: self.db_groups(),
            "fs.pick_folder": lambda p: self.pick_folder(),
        }

    def _unavailable(self, what):
        raise CBError(f"{what} is not wired up yet in the web frontend.")

    # ── state ─────────────────────────────────────────────────────────────────

    def snapshot(self):
        """Everything the shell needs on connect; all else arrives as deltas."""
        library = self.library_stats()
        return {
            "app": {"name": "DJ-CrateBuilder", **version_info()},
            "host": {"transport": self.transport, "online": True,
                     "app_dir": app_dir()},
            "counts": {
                "downloads": library["downloads"],
                "watchlist": library["watchlist"],
                "pending_new": library["pending_new"],
                "genres": len(self.genres()),
            },
            "library": library,
            "batch": self.batch_list(),
            "watchlist": self.watchlist_list(),
            "running": {"batch": self._job_running("batch"),
                        "watchlist": self._job_running("watchlist"),
                        "maintenance": self._job_running("maintenance")},
            "settings": self.settings_all(),
            "settings_path": self._settings.path,
            "platform": sys.platform,
            "genres": self.genres(),
            "capabilities": {
                "update": self.transport == LOCAL,
                "filesystem": self.transport == LOCAL,
            },
        }

    def ui_strings(self):
        """The shared tooltip registry, so no string is duplicated in JS."""
        return {"tooltips": ui_strings.TOOLTIPS,
                "settings_keys": ui_strings.SETTINGS_KEYS}

    # ── library / database ────────────────────────────────────────────────────

    def _db(self):
        if not os.path.isfile(self._db_path):
            return None
        return DownloadsDatabase(self._db_path)

    def library_stats(self):
        """Counts for the Overview, or zeros when no database exists yet.

        Never creates the database: opening DownloadsDatabase runs the schema
        migrations, so probing a missing file would write one into the user's
        install just because the frontend was opened.
        """
        db = self._db()
        if db is None:
            return {"available": False, "downloads": 0, "watchlist": 0,
                    "pending_new": 0, "path": self._db_path}
        channels = db.get_all_watchlist_channels()
        return {
            "available": True,
            "downloads": db.get_download_count(),
            "watchlist": len(channels),
            "pending_new": sum(int(c["pending_new_count"] or 0) for c in channels),
            "path": self._db_path,
        }

    def watchlist_list(self):
        """Watch List channels, with the row shape the frontend renders.

        The DB column names are normalised here rather than in JS so no screen
        has to know the schema — and so a column rename is one edit, not a hunt
        through the bundle.
        """
        db = self._db()
        if db is None:
            return []
        out = []
        for row in db.get_all_watchlist_channels():
            row = dict(row)
            row["name"] = row.get("display_name") or row.get("url") or "Channel"
            row["new_count"] = int(row.get("pending_new_count") or 0)
            row["downloaded"] = int(row.get("total_downloaded") or 0)
            row["last_scan"] = row.get("last_scanned_timestamp")
            # Only YouTube entries can be resolved to a canonical channel id;
            # SoundCloud has no equivalent, so a null there is not a fault.
            row["unresolved"] = (str(row.get("platform") or "").lower() == "youtube"
                                 and not row.get("channel_id"))
            out.append(row)
        return out

    def db_groups(self):
        """Group skeleton for the database viewer — counts only, no rows."""
        db = self._db()
        if db is None:
            return {"available": False, "groups": []}
        return {"available": True, "total": db.get_download_count(),
                "groups": []}

    # ── settings ──────────────────────────────────────────────────────────────

    def settings_all(self):
        """Every schema key the design's Settings screen renders."""
        out = {}
        for entry in ui_strings.SETTINGS_KEYS:
            key = entry.get("key")
            try:
                out[key] = self._settings.get(key)
            except KeyError:
                continue        # contract lists remote-only keys the app lacks
        return out

    def settings_get(self, key):
        if not key:
            raise CBError("No setting was named.")
        try:
            return {"key": key, "value": self._settings.get(key)}
        except KeyError:
            raise CBError(f"Unknown setting: {key}")

    def settings_set(self, key, value):
        """Set one key and echo the stored value back, mirroring autosave."""
        if not key:
            raise CBError("No setting was named.")
        try:
            self._settings.set(key, value)
        except KeyError:
            raise CBError(f"Unknown setting: {key}")
        except TypeError as exc:
            raise CBError(str(exc))
        return {"key": key, "value": self._settings.get(key)}

    # ── batch queue ───────────────────────────────────────────────────────────

    def batch_list(self):
        with self._lock:
            return [dict(row) for row in self._batch]

    def batch_add(self, url, genre=None, platform=None):
        url = (url or "").strip()
        if not url:
            raise CBError("Paste a YouTube or SoundCloud link first.")
        row = {
            "id": next(self._ids),
            "url": url,
            "genre": genre or CrateLayout.NO_GENRE_VALUE,
            "platform": platform or "",
            "state": "queued",
        }
        with self._lock:
            self._batch.append(row)
        return dict(row)

    def batch_remove(self, row_id):
        with self._lock:
            before = len(self._batch)
            self._batch = [r for r in self._batch if r["id"] != row_id]
            if len(self._batch) == before:
                raise CBError("That queue row is no longer in the batch.")
        return self.batch_list()

    def batch_move(self, row_id, delta):
        """Move one row up or down; a move past either end is a no-op."""
        with self._lock:
            index = next((i for i, r in enumerate(self._batch)
                          if r["id"] == row_id), None)
            if index is None:
                raise CBError("That queue row is no longer in the batch.")
            target = index + int(delta or 0)
            if 0 <= target < len(self._batch):
                self._batch.insert(target, self._batch.pop(index))
        return self.batch_list()

    def batch_clear(self):
        with self._lock:
            self._batch = []
        return self.batch_list()

    def batch_skip(self, row_id):
        """Mark a row skipped, or un-mark it — the Main tab's per-row toggle."""
        with self._lock:
            for row in self._batch:
                if row["id"] == row_id:
                    row["state"] = ("queued" if row["state"] == "skipped"
                                    else "skipped")
                    return dict(row)
        raise CBError("That queue row is no longer in the batch.")

    # ── crate ─────────────────────────────────────────────────────────────────

    def genres(self):
        """Genre folder names present under the crate root, de-duplicated."""
        base = self._settings.get("base_dir")
        found = set()
        try:
            platforms = [e for e in os.scandir(base) if e.is_dir()]
        except OSError:
            return []
        for platform in platforms:
            try:
                for entry in os.scandir(platform.path):
                    if entry.is_dir():
                        found.add(CrateLayout.genre_value(entry.name))
            except OSError:
                continue
        return sorted(found)

    # ── host-only ─────────────────────────────────────────────────────────────

    def pick_folder(self):
        """Native folder picker; the remote transport never reaches this."""
        if self.transport != LOCAL:
            raise CBError("Folder browsing only works in the app window on the "
                          "host machine.")
        try:
            import webview
        except ImportError:
            raise CBError("The window toolkit is unavailable.")
        window = webview.active_window()
        if window is None:
            raise CBError("No window is open to attach the picker to.")
        picked = window.create_file_dialog(webview.FOLDER_DIALOG)
        return {"path": picked[0] if picked else None}

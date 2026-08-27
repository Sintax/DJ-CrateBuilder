"""Transport-agnostic action surface shared by the local window and remote clients."""

import itertools
import os
import re
import sys
import threading

from cratebuilder import activitylog, startup, ui_strings, util
from cratebuilder.batchrun import BatchRunner
from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase
from cratebuilder.events import Coalescer, EventBus
from cratebuilder.settings import Settings

MAIN_SCRIPT = "DJ-CrateBuilder_v1.3.py"
DB_NAME = "cratebuilder.db"
ACTIVITY_LOG = "activity.log"

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


# ── settings bindings ───────────────────────────────────────────────────────
# The contract (cratebuilder.ui_strings.SETTINGS_KEYS) and the config schema
# (cratebuilder.settings.Settings) were named independently, so a handful of
# keys don't line up: a different schema key name, or the same key holding a
# display string on one side and a bare stored value on the other. Each entry
# below is (schema_key, to_display, from_display); every schema key the
# contract doesn't list here is read/written as-is (see _identity).

def _identity(value):
    return value


def _bitrate_to_display(value):
    text = str(value)
    return text if text.endswith("kbps") else f"{text} kbps"


def _bitrate_from_display(value):
    return str(value).split()[0]


def _log_limit_to_display(value):
    try:
        mb = int(value)
    except (TypeError, ValueError):
        mb = 0
    return "Unlimited" if not mb else f"{mb} MB"


def _log_limit_from_display(value):
    text = str(value).strip()
    if text.lower() == "unlimited":
        return 0
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        raise ValueError(f"Not a valid log size limit: {value!r}")


# Keyed by the exact label cratebuilder.util.THROTTLE_PRESETS stores in the
# config file; the contract only shows the preset name, not the (min, max)
# seconds range baked into the stored label.
_SLEEP_PRESET_TO_DISPLAY = {stored: stored.split()[0]
                            for stored in util.THROTTLE_PRESETS}
_SLEEP_PRESET_FROM_DISPLAY = {display: stored
                              for stored, display in _SLEEP_PRESET_TO_DISPLAY.items()}


def _sleep_preset_to_display(value):
    return _SLEEP_PRESET_TO_DISPLAY.get(value, value)


def _sleep_preset_from_display(value):
    try:
        return _SLEEP_PRESET_FROM_DISPLAY[value]
    except KeyError:
        raise ValueError(f"Unknown throttle preset: {value!r}")


# cover_art_mode stores one of cratebuilder.artwork.COVER_ART_MODES ('crop',
# 'original', 'off'); the design's Formatting dropdown spells all three out,
# independently of the separate cover_art_enabled checkbox (screen 3j has
# both controls — see UI-design/CrateBuilder Remote v3.dc.html #3j).
_COVER_ART_MODE_TO_DISPLAY = {
    "crop": "On ~ Crop to square",
    "original": "On ~ Keep original aspect",
    "off": "Off",
}
_COVER_ART_MODE_FROM_DISPLAY = {display: stored
                                for stored, display in _COVER_ART_MODE_TO_DISPLAY.items()}


def _cover_art_mode_to_display(value):
    return _COVER_ART_MODE_TO_DISPLAY.get(value, value)


def _cover_art_mode_from_display(value):
    try:
        return _COVER_ART_MODE_FROM_DISPLAY[value]
    except KeyError:
        raise ValueError(f"Unknown cover art formatting: {value!r}")


# cookie_method stores "Browser" (see DJ-CrateBuilder_v1.3.py's
# _cookie_method_combo), but the contract's option reads "Browser Profile".
_COOKIE_METHOD_TO_DISPLAY = {"Browser": "Browser Profile", "Cookie File": "Cookie File"}
_COOKIE_METHOD_FROM_DISPLAY = {display: stored
                               for stored, display in _COOKIE_METHOD_TO_DISPLAY.items()}


def _cookie_method_to_display(value):
    return _COOKIE_METHOD_TO_DISPLAY.get(value, value)


def _cookie_method_from_display(value):
    try:
        return _COOKIE_METHOD_FROM_DISPLAY[value]
    except KeyError:
        raise ValueError(f"Unknown cookie method: {value!r}")


SETTINGS_BINDINGS = {
    "bitrate_quality": ("bitrate_quality", _bitrate_to_display, _bitrate_from_display),
    "auto_dl_interval": ("auto_download_interval", _identity, _identity),
    "log_limit": ("log_max_mb", _log_limit_to_display, _log_limit_from_display),
    "sleep_preset": ("sleep_preset", _sleep_preset_to_display, _sleep_preset_from_display),
    "cover_art_mode": ("cover_art_mode", _cover_art_mode_to_display, _cover_art_mode_from_display),
    "cookie_method": ("cookie_method", _cookie_method_to_display, _cookie_method_from_display),
}


def _binding(key):
    """(schema_key, to_display, from_display) for a contract key — identity
    for every key SETTINGS_BINDINGS doesn't call out."""
    return SETTINGS_BINDINGS.get(key, (key, _identity, _identity))


def _validate_base_dir(path):
    """Canonicalize a save-directory path, creating it if needed.

    Rejects a blank path, a path that already exists as a file, and a path
    that cannot be created (e.g. a drive that doesn't exist) — the same
    guarantees DJ-CrateBuilder_v1.3.py's _save_settings gets from
    filedialog.askdirectory plus os.makedirs, which a typed path bypasses.
    """
    raw = (path or "").strip()
    if not raw:
        raise CBError("Enter a save directory.")
    canonical = os.path.abspath(os.path.expanduser(raw))
    if os.path.isfile(canonical):
        raise CBError(f"{canonical} is a file, not a folder.")
    try:
        os.makedirs(canonical, exist_ok=True)
    except OSError as exc:
        raise CBError(f"That folder could not be used: {exc.strerror or exc}")
    return canonical


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

    def __init__(self, transport=LOCAL, settings=None, db_path=None,
                 log_path=None):
        if transport not in (LOCAL, REMOTE):
            raise ValueError(f"unknown transport: {transport}")
        self.transport = transport
        self._settings = settings or Settings()
        self._db_path = db_path or os.path.join(app_dir(), DB_NAME)
        self._log_path = log_path or os.path.join(app_dir(), ACTIVITY_LOG)
        self._lock = threading.Lock()
        self._batch = []
        self._ids = itertools.count(1)
        self.events = EventBus()
        self._emit = Coalescer(self.events)
        self._jobs = {}
        self._batch_runner = None

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
            "download.start": lambda p: self.download_start(),
            "download.pause": lambda p: self.download_pause(),
            "download.resume": lambda p: self.download_resume(),
            "download.cancel": lambda p: self.download_cancel(),
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
            "counts": self.counts(library),
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

    def counts(self, library=None):
        """The four headline numbers the shell's counters show."""
        library = library if library is not None else self.library_stats()
        return {"downloads": library["downloads"],
                "watchlist": library["watchlist"],
                "pending_new": library["pending_new"],
                "genres": len(self.genres())}

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
        """Every schema key the design's Settings screen renders, translated
        to the display form SETTINGS_BINDINGS says the contract expects."""
        out = {}
        for entry in ui_strings.SETTINGS_KEYS:
            key = entry.get("key")
            schema_key, to_display, _ = _binding(key)
            try:
                out[key] = to_display(self._settings.get(schema_key))
            except KeyError:
                continue        # contract lists remote-only keys the app lacks
        return out

    def settings_get(self, key):
        if not key:
            raise CBError("No setting was named.")
        schema_key, to_display, _ = _binding(key)
        try:
            return {"key": key, "value": to_display(self._settings.get(schema_key))}
        except KeyError:
            raise CBError(f"Unknown setting: {key}")

    def settings_set(self, key, value):
        """Set one key and echo the stored value back, mirroring autosave.

        *value* arrives in the contract's display form; SETTINGS_BINDINGS
        translates it to what Settings actually stores before writing.
        """
        if not key:
            raise CBError("No setting was named.")
        schema_key, to_display, from_display = _binding(key)
        if schema_key == "run_at_startup":
            return self._set_run_at_startup(value, to_display)
        try:
            stored = from_display(value)
        except (TypeError, ValueError) as exc:
            raise CBError(str(exc))
        if schema_key == "base_dir":
            stored = _validate_base_dir(stored)
        try:
            self._settings.set(schema_key, stored)
        except KeyError:
            raise CBError(f"Unknown setting: {key}")
        except TypeError as exc:
            raise CBError(str(exc))
        return {"key": key, "value": to_display(self._settings.get(schema_key))}

    def _set_run_at_startup(self, value, to_display):
        """Toggle the Windows Run-at-login registry entry, then persist.

        Local transport only — it edits the host's own registry, which a
        remote browser has no business reaching. Mirrors
        _on_run_at_startup_toggle in DJ-CrateBuilder_v1.3.py: the registry
        write is the source of truth, and a failed write is never persisted.
        """
        if self.transport != LOCAL:
            raise CBError("Run App on Startup can only be changed from the "
                          "app window on the host machine.")
        enabled = bool(value)
        if sys.platform == "win32" and not startup.set_startup(enabled):
            raise CBError("Could not update the Windows startup entry.")
        try:
            self._settings.set("run_at_startup", enabled)
        except KeyError:
            raise CBError("Unknown setting: run_at_startup")
        return {"key": "run_at_startup",
                "value": to_display(self._settings.get("run_at_startup"))}

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
        self._require_idle_queue()
        with self._lock:
            before = len(self._batch)
            self._batch = [r for r in self._batch if r["id"] != row_id]
            if len(self._batch) == before:
                raise CBError("That queue row is no longer in the batch.")
        return self.batch_list()

    def batch_move(self, row_id, delta):
        """Move one row up or down; a move past either end is a no-op."""
        self._require_idle_queue()
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
        self._require_idle_queue()
        with self._lock:
            self._batch = []
        return self.batch_list()

    def batch_skip(self, row_id):
        """Mark a row skipped, or un-mark it — the Main tab's per-row toggle.

        While a batch runs the toggle only goes one way, and it goes through to
        the runner: a row that is downloading right now is interrupted on the
        spot, matching the tkinter Skip button.
        """
        running = self._batch_runner if self._job_running("batch") else None
        with self._lock:
            for row in self._batch:
                if row["id"] == row_id:
                    row["state"] = ("skipped" if running is not None else
                                    "queued" if row["state"] == "skipped"
                                    else "skipped")
                    found = dict(row)
                    break
            else:
                raise CBError("That queue row is no longer in the batch.")
        if running is not None:
            running.skip_row(row_id)
        return found

    def _require_idle_queue(self):
        """The queue is locked while a batch runs — only skip and add work,
        mirroring the tkinter Main tab."""
        if self._job_running("batch"):
            raise CBError("The queue is locked while a download is running. "
                          "Cancel it first, or skip the row instead.")

    # ── downloads ─────────────────────────────────────────────────────────────

    def download_start(self):
        """Run the current queue on the batch job thread.

        The runner is handed the LIVE queue list, not a copy: a row added
        mid-batch has to be picked up, which is what the tkinter Watch List's
        append-to-running does. Nothing can shrink the list underneath it —
        remove/move/clear are refused for the duration.
        """
        with self._lock:
            rows = self._batch
            if not any(r.get("state") != "skipped" for r in rows):
                raise CBError("Add a link to the queue before starting a "
                              "download.")
        runner = BatchRunner(
            self._settings, self._db_for_write(), self.emit,
            log_line=self.log_line, counts=self.counts,
            flush=self._emit.flush)
        previous = self._batch_runner
        self._batch_runner = runner
        try:
            job_id = self._start_job("batch", runner.run, rows)
        except CBError:
            self._batch_runner = previous
            raise
        return {"job_id": job_id}

    def download_pause(self):
        self._running_batch().pause()
        return {"paused": True}

    def download_resume(self):
        self._running_batch().resume()
        return {"paused": False}

    def download_cancel(self):
        self._running_batch().cancel()
        return {"cancelled": True}

    def _running_batch(self):
        runner = self._batch_runner
        if runner is None or not self._job_running("batch"):
            raise CBError("No download is running.")
        return runner

    def _db_for_write(self):
        """The database a download writes its rows into — created on demand,
        unlike the read-only probes, which never bring one into existence."""
        return DownloadsDatabase(self._db_path)

    def log_line(self, text):
        """Append one line to activity.log in the app dir, timestamped exactly
        as the tkinter app's logger writes it."""
        return activitylog.append(self._log_path, text)

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

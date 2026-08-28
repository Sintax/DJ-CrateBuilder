"""Transport-agnostic action surface shared by the local window and remote clients."""

import base64
import csv
import io
import itertools
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

from cratebuilder import activitylog, rebuild, startup, ui_strings, util
from cratebuilder.artwork import DEFAULT_COVER_ART_MODE, extract_cover
from cratebuilder.batchresolve import platform_dir
from cratebuilder.batchrun import BatchRunner
from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase
from cratebuilder.events import Coalescer, EventBus
from cratebuilder.links import LINKS_FILE_NAME
from cratebuilder.settings import Settings
from cratebuilder.sidecar import is_unresolved_channel

MAIN_SCRIPT = "DJ-CrateBuilder_v1.3.py"
DB_NAME = "cratebuilder.db"
ACTIVITY_LOG = "activity.log"
DEBUG_LOG = "debug.log"

LOCAL = "local"
REMOTE = "remote"

# The job registry's category for every Watch List run. One at a time, so a
# scan and a download can never fight over the same channel folder.
WATCHLIST_JOB = "watchlist"

# The one event that means "this job category is free again". Emitted by
# _start_job AFTER the slot is released, which is what separates it from the
# runs' own terminal events (`batch.finished`, the closing DONE scan line) —
# those are emitted from inside the run, while the slot is still held. A
# frontend refreshes its state on this; the others are display only.
JOB_FINISHED = "job.finished"

# Method prefixes the remote transport must refuse server-side. The design's
# rule is that a browser elsewhere can never replace the binary it is talking
# to, and that only the host may see the host's filesystem — so this is checked
# here, not left to a client to respect.
LOCAL_ONLY = ("update.", "fs.")

# logs.download only ever hands back a path (see CrateBuilderService.logs_download)
# — never touches the host filesystem itself — so it's safe on the remote
# transport too; Task 11's /logs/<name> route is what makes that path useful
# to a browser that isn't the host.
DEFAULT_LOG_WINDOW = 2000
MAX_LOG_SEARCH_MATCHES = 500
DEFAULT_LOG_WATCH_INTERVAL = 1.0

# db.query page size — the Downloads/Artwork tabs' "load more" step. A 20k-row
# library must never arrive in one payload; this is what keeps that true.
DEFAULT_DB_PAGE_SIZE = 200
# The ceiling db.query enforces on any caller's limit, so the "never one
# payload" property is the server's, not a convention the client happens to
# follow — a browser on the remote transport is not code this host controls.
MAX_DB_PAGE_SIZE = 5000
# db.export_csv fetches the whole filtered set in one pass rather than paging
# it — comfortably above any real library size while still bounding worst-case
# memory, and avoids the LIMIT 0 footgun (SQLite reads that as "zero rows",
# not "no limit").
EXPORT_ROW_CAP = 1_000_000
# db.artwork_preview holds the bytes, their base64 form, the data URL and the
# JSON envelope live at once, so an uncapped read is a multiple of the file's
# size in host memory. Far above any real cover art.
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
# fs.reveal mode="open" hands the path to the OS default handler, which on
# Windows means "execute" for .exe/.bat/.lnk and friends. Only the media the
# Database viewer actually previews may be opened that way — derived from the
# library's own audio set so a Rebuild-DB run can never index a row that
# "Open File" then refuses, plus the sidecar types artwork.raw_thumbnail writes.
OPENABLE_EXTENSIONS = frozenset(rebuild.AUDIO_EXTS) | {
    ".jpg", ".jpeg", ".png", ".webp",
}

_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.M)
_BUILD_RE = re.compile(r"^APP_BUILD\s*=\s*(\d+)", re.M)


class CBError(Exception):
    """A failure with a message meant to be shown to the user as-is."""


# ── settings bindings ───────────────────────────────────────────────────────
# The contract (cratebuilder.ui_strings.SETTINGS_KEYS) and the config schema
# (cratebuilder.settings.Settings) were named independently, so a handful of
# keys don't line up: a different schema key name, or the same key holding a
# display string on one side and a bare stored value on the other. Each entry
# below is a (get, set) pair — get(settings) -> display value, set(settings,
# display) -> persists it — built around the whole Settings object rather
# than a bare value so a binding CAN be cross-key (see cover_art_mode).
# Every key the contract doesn't list here is read/written as-is via
# _simple_binding's identity defaults.

def _identity(value):
    return value


def _simple_binding(schema_key, to_display=_identity, from_display=_identity):
    """A binding whose display value is a pure function of one schema key.

    Covers every case except a translation that has to look at, or change, a
    second key. Add a cross-key binding as its own hand-written (get, set)
    pair — see _cover_art_mode_get/_cover_art_mode_set — never by stretching
    this helper or special-casing it in settings_get/settings_set.
    """
    def get(settings):
        return to_display(settings.get(schema_key))

    def set_(settings, display):
        settings.set(schema_key, from_display(display))

    return get, set_


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


# cover_art_mode is cross-key, not a simple translation. The design's
# Formatting dropdown spells out three options — "On ~ Crop to square",
# "On ~ Keep original aspect", "Off" — but the app's real data model only
# ever lets cover_art_mode itself hold 'crop'/'original'; "no cover art" is
# carried entirely by the separate cover_art_enabled flag. That split is
# deliberate and load-bearing: cratebuilder.settings._migrate() runs on
# EVERY Settings() construction (not just a legacy-named file) and
# unconditionally rewrites a stored cover_art_mode == "off" back to
# DEFAULT_COVER_ART_MODE, on the theory that "off" is a pre-checkbox legacy
# value, not a live user choice — see _migrate's docstring and
# DJ-CrateBuilder_v1.3.py's own _COVER_ART_FORMAT_MODES, which excludes
# "off" from its dropdown for the same reason. Writing the literal "off"
# here would look fine in-session and silently revert on the next load, so
# this binding writes cover_art_enabled instead and never writes "off".
_COVER_ART_MODE_TO_DISPLAY = {
    "crop": "On ~ Crop to square",
    "original": "On ~ Keep original aspect",
}
_COVER_ART_MODE_FROM_DISPLAY = {display: stored
                                for stored, display in _COVER_ART_MODE_TO_DISPLAY.items()}


def _cover_art_mode_get(settings):
    """"Off" whenever cover art is disabled, regardless of what the
    formatting key happens to hold — cover_art_enabled is the single source
    of truth for "off", matching DJ-CrateBuilder_v1.3.py's
    _cover_art_mode_value()."""
    if not settings.get("cover_art_enabled"):
        return "Off"
    mode = settings.get("cover_art_mode")
    return _COVER_ART_MODE_TO_DISPLAY.get(
        mode, _COVER_ART_MODE_TO_DISPLAY[DEFAULT_COVER_ART_MODE])


def _cover_art_mode_set(settings, display):
    """"Off" clears cover_art_enabled and leaves cover_art_mode alone — never
    writes "off" there. Any other choice turns cover art back on and writes
    the formatting key. Both keys are written in one update() so a crash
    between the two writes can't leave them disagreeing."""
    if display == "Off":
        settings.set("cover_art_enabled", False)
        return
    try:
        mode = _COVER_ART_MODE_FROM_DISPLAY[display]
    except KeyError:
        raise ValueError(f"Unknown cover art formatting: {display!r}")
    settings.update({"cover_art_enabled": True, "cover_art_mode": mode})


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
    "bitrate_quality": _simple_binding("bitrate_quality", _bitrate_to_display, _bitrate_from_display),
    "auto_dl_interval": _simple_binding("auto_download_interval"),
    "log_limit": _simple_binding("log_max_mb", _log_limit_to_display, _log_limit_from_display),
    "sleep_preset": _simple_binding("sleep_preset", _sleep_preset_to_display, _sleep_preset_from_display),
    "cover_art_mode": (_cover_art_mode_get, _cover_art_mode_set),
    "cookie_method": _simple_binding("cookie_method", _cookie_method_to_display, _cookie_method_from_display),
}


def _binding(key):
    """(get, set) for a contract key — get(settings) -> display value,
    set(settings, display) -> persists it. Identity (the schema key of the
    same name, untranslated) for every key SETTINGS_BINDINGS doesn't call
    out."""
    return SETTINGS_BINDINGS.get(key, _simple_binding(key))


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


# ── log files ────────────────────────────────────────────────────────────────
# Split on raw bytes, not decoded text: errors="replace" can change how many
# characters a line decodes to, but never how many bytes it occupies, and byte
# offsets are the currency logs.tail/logs.search/log.append all speak in.

def _iter_log_lines(data):
    """Yield (start_offset, raw_line_bytes, bytes_consumed) for each line in
    *data* — bytes_consumed includes the trailing newline where one exists.
    A trailing \\r is stripped from the returned line (activitylog.append and
    the monolith's file handlers both open in text mode, which writes CRLF on
    Windows) but still counts toward bytes_consumed, so offsets stay exact
    either way."""
    start = 0
    total = len(data)
    while start < total:
        nl = data.find(b"\n", start)
        if nl == -1:
            end = total
            consumed = end - start
        else:
            end = nl
            consumed = (end - start) + 1
        raw = data[start:end]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        yield start, raw, consumed
        start += consumed


def _read_log_bytes(path):
    """Whole-file bytes, or None if the log doesn't exist yet or can't be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _channel_id(params):
    """The watchlist row id one call names. The contract spells it
    "channel_id"; "id" is accepted too, since that is what the row itself
    calls the same number."""
    value = params.get("channel_id")
    return params.get("id") if value is None else value


def watchlist_card(row, **extra):
    """One Watch List channel in the row shape the frontend renders.

    The DB column names are normalised here rather than in JS so no screen has
    to know the schema — and so a column rename is one edit, not a hunt through
    the bundle. *extra* carries the fields only a live run can know (a channel's
    download progress), which is why this is one function rather than two: the
    card a scan pushes and the card the snapshot lists are the same card.
    """
    row = dict(row)
    row["name"] = row.get("display_name") or row.get("url") or "Channel"
    row["new_count"] = int(row.get("pending_new_count") or 0)
    row["downloaded"] = int(row.get("total_downloaded") or 0)
    row["last_scan"] = row.get("last_scanned_timestamp")
    # Only YouTube entries can be resolved to a canonical channel id;
    # SoundCloud has no equivalent, so a null there is not a fault.
    row["unresolved"] = (str(row.get("platform") or "").lower() == "youtube"
                         and not row.get("channel_id"))
    row.update(extra)
    return row


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
                 log_path=None, debug_log_path=None,
                 log_watch_interval=DEFAULT_LOG_WATCH_INTERVAL):
        if transport not in (LOCAL, REMOTE):
            raise ValueError(f"unknown transport: {transport}")
        self.transport = transport
        self._settings = settings or Settings()
        self._db_path = db_path or os.path.join(app_dir(), DB_NAME)
        self._log_path = log_path or os.path.join(app_dir(), ACTIVITY_LOG)
        self._debug_log_path = debug_log_path or os.path.join(app_dir(), DEBUG_LOG)
        self._lock = threading.Lock()
        self._batch = []
        self._ids = itertools.count(1)
        self.events = EventBus()
        self._emit = Coalescer(self.events)
        self._jobs = {}
        self._batch_runner = None
        self._watchlist_ops = None
        # The durable channel-link store lives beside the database, so a test
        # pointing db_path at a tmp dir never writes the developer's real one.
        self._links_path = os.path.join(
            os.path.dirname(self._db_path) or app_dir(), LINKS_FILE_NAME)
        self._log_watch_interval = log_watch_interval
        self._log_watchers = {}
        self.reset_stale_watchlist_rows()

    # ── events / jobs ─────────────────────────────────────────────────────────

    def emit(self, type, payload):
        self._emit.emit(type, payload)

    def _job_running(self, category):
        with self._lock:
            return category in self._jobs

    def _start_job(self, category, target, *args):
        """Run `target` on a daemon thread; refuse a second job per category.

        The run's own terminal events (`batch.finished`, the closing `DONE`
        scan line) are emitted from inside *target*, while this category is
        still in `self._jobs` — so a client that reacts to one by asking for a
        snapshot can be told the job is still running and re-arm a run that has
        already ended. `job.finished` is emitted after the slot is released
        precisely so that answer cannot come back stale: it, not the display
        events, is what a frontend resyncs on.
        """
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
                self.emit(JOB_FINISHED, {"job": category})

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
            "watchlist.scan": lambda p: self.watchlist_scan(_channel_id(p)),
            "watchlist.scan_all": lambda p: self.watchlist_scan_all(),
            "watchlist.download_new":
                lambda p: self.watchlist_download_new(_channel_id(p)),
            "watchlist.download_all_new":
                lambda p: self.watchlist_download_all_new(),
            "watchlist.force_download":
                lambda p: self.watchlist_force_download(_channel_id(p)),
            "watchlist.cancel": lambda p: self.watchlist_cancel(_channel_id(p)),
            "watchlist.cancel_all": lambda p: self.watchlist_cancel_all(),
            "watchlist.add": lambda p: self.watchlist_add(p.get("url"),
                                                          p.get("genre")),
            "watchlist.edit": lambda p: self.watchlist_edit(
                _channel_id(p), p.get("url"), p.get("genre")),
            "watchlist.remove":
                lambda p: self.watchlist_remove(_channel_id(p)),
            "watchlist.details":
                lambda p: self.watchlist_details(_channel_id(p)),
            "watchlist.forget_unavailable":
                lambda p: self.watchlist_forget_unavailable(_channel_id(p)),
            "watchlist.resolve_candidates":
                lambda p: self.watchlist_resolve_candidates(_channel_id(p)),
            "watchlist.resolve_apply": lambda p: self.watchlist_resolve_apply(
                _channel_id(p), p.get("resolved_url"),
                p.get("resolved_channel_id")),
            "db.groups": lambda p: self.db_groups(p.get("preset"),
                                                   p.get("filters") or {}),
            "db.query": lambda p: self.db_query(
                p.get("table"), p.get("filters") or {}, p.get("sort") or {},
                p.get("offset", 0), p.get("limit", DEFAULT_DB_PAGE_SIZE),
                want_total=p.get("want_total", True)),
            "db.export_csv": lambda p: self.db_export_csv(
                p.get("table"), p.get("filters") or {}, p.get("sort") or {}),
            "db.artwork_preview": lambda p: self.db_artwork_preview(p.get("id")),
            "fs.pick_folder": lambda p: self.pick_folder(),
            "fs.reveal": lambda p: self.fs_reveal(p.get("path"),
                                                  p.get("mode", "folder")),
            "logs.tail": lambda p: self.logs_tail(
                p.get("name"), p.get("offset"), p.get("limit", DEFAULT_LOG_WINDOW),
                bool(p.get("before"))),
            "logs.search": lambda p: self.logs_search(
                p.get("name"), p.get("query"), bool(p.get("regex"))),
            "logs.download": lambda p: self.logs_download(p.get("name")),
            "logs.watch": lambda p: self.logs_watch(p.get("name"), p.get("on")),
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
                        "watchlist": self._job_running(WATCHLIST_JOB),
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
        """Watch List channels, each as the card watchlist_card builds."""
        db = self._db()
        if db is None:
            return []
        return [watchlist_card(row) for row in db.get_all_watchlist_channels()]

    # ── watch list ────────────────────────────────────────────────────────────
    # Dispatch only: every one of these is a thin wrapper over
    # cratebuilder.watchrun, which owns the orchestration. What lives here is
    # what the transport layer owns — turning a params dict into a row id,
    # deciding whether a call starts a job or joins one, and refusing a call
    # that has nothing to act on.

    @property
    def _watchlist(self):
        """The Watch List operations, built on first use.

        Imported here rather than at module scope because watchrun imports
        CBError and watchlist_card from this module; deferring keeps the
        dependency one-way and the import cycle impossible."""
        if self._watchlist_ops is None:
            from cratebuilder.watchrun import WatchlistOps
            self._watchlist_ops = WatchlistOps(
                self._settings, self._db_for_write, self.emit,
                links_path=self._links_path, log_line=self.log_line,
                counts=self.counts, flush=self._emit.flush)
        return self._watchlist_ops

    def reset_stale_watchlist_rows(self):
        """Clear 'scanning'/'downloading' left behind by a killed frontend.

        Runs once per service, at construction: those statuses only mean
        anything while a live thread owns the row, and no thread survives the
        process. Without this the web frontend has no stale-status recovery at
        all — the tkinter app calls the same helper at ITS startup, but a user
        who only ever opens the web UI would never reach it, and Task 9's
        downloading card greys out every control except a Cancel for a job that
        is not running. Never creates the database: a service pointed at a path
        with no file yet has no rows to clear.
        """
        db = self._db()
        if db is None:
            return 0
        try:
            return db.reset_stale_watchlist_scans()
        except Exception:
            return 0

    def _watchlist_rows(self):
        db = self._db()
        return db.get_all_watchlist_channels() if db is not None else []

    def _watchlist_row(self, channel_id):
        """The watchlist row one call names, or a CBError naming the miss."""
        try:
            wanted = int(channel_id)
        except (TypeError, ValueError):
            raise CBError("No channel was named.")
        for row in self._watchlist_rows():
            if row.get("id") == wanted:
                return row
        raise CBError("That channel is no longer in the Watch List.")

    def watchlist_scan(self, channel_id):
        """Scan one channel for new uploads."""
        row = self._watchlist_row(channel_id)
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_scan,
                                          [row["id"]])}

    def watchlist_scan_all(self):
        """Scan every watched channel, in list order."""
        ids = [row["id"] for row in self._watchlist_rows()]
        if not ids:
            raise CBError("No channels to scan.")
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_scan, ids)}

    def watchlist_download_new(self, channel_id):
        """Download one channel's pending new tracks.

        Pressed while a Watch List download is already running, the channel
        joins that run's queue rather than being refused — the tkinter Watch
        List's append-to-running, which is what the design's Download New
        tooltip promises."""
        row = self._watchlist_row(channel_id)
        if self._job_running(WATCHLIST_JOB):
            position = self._watchlist.enqueue(row["id"])
            if position is not None:
                return {"queued_position": position}
            # No download run to join — a scan owns the job. Falling through
            # lets _start_job give the "already running" answer rather than
            # inventing a second one here.
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_download,
                                          [row["id"]])}

    def watchlist_download_all_new(self):
        """Download every channel's pending new tracks."""
        ids = [row["id"] for row in self._watchlist_rows()
               if int(row.get("pending_new_count") or 0) > 0]
        if not ids:
            raise CBError("No new tracks pending across any channels. Try "
                          "Scan All first.")
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_download, ids)}

    def watchlist_force_download(self, channel_id):
        """Re-process one channel's whole catalogue, skipping nothing."""
        row = self._watchlist_row(channel_id)
        if is_unresolved_channel(row):
            raise CBError("This channel's link isn't resolved yet. Use Fix "
                          "Link on the card first, then Force Download.")
        job_id = self._start_job(WATCHLIST_JOB, self._watchlist.run_download,
                                 [row["id"]], True)
        return {"job_id": job_id}

    def watchlist_cancel(self, channel_id):
        """Stop the scan or download running on one channel."""
        row = self._watchlist_row(channel_id)
        return self._watchlist.cancel(row["id"])

    def watchlist_cancel_all(self):
        """Stop every in-progress Watch List scan and download."""
        return self._watchlist.cancel_all()

    def watchlist_add(self, url, genre=None):
        return self._watchlist.add(url, genre)

    def watchlist_edit(self, channel_id, url=None, genre=None):
        """Change a channel's link and/or genre.

        A genre change is refused while EITHER download category is running: it
        moves the channel folder on disk, and a TrackDownloader mid-flight —
        from a Main-tab batch or a Watch List run — holds a save_dir and an
        expected_path computed from the old location, so the move would strand
        it writing into a folder the database no longer describes. A link or
        plain-field edit touches no files and stays allowed."""
        row = self._watchlist_row(channel_id)
        picked = (genre or "").strip()
        if picked and picked != (row.get("genre") or CrateLayout.NO_GENRE_VALUE):
            if self._job_running("batch") or self._job_running(WATCHLIST_JOB):
                raise CBError("A download is running. The channel's folder "
                              "can't be moved to another genre until it "
                              "finishes — cancel it first, or change the link "
                              "only.")
        return self._watchlist.edit(row["id"], url=url, genre=genre)

    def watchlist_remove(self, channel_id):
        row = self._watchlist_row(channel_id)
        return self._watchlist.remove(row["id"])

    def watchlist_details(self, channel_id):
        """One channel's folder, movable-track count and unavailable-track
        count — the Edit dialog's lazy reads, in a single round trip."""
        row = self._watchlist_row(channel_id)
        return self._watchlist.details(row["id"])

    def watchlist_forget_unavailable(self, channel_id):
        row = self._watchlist_row(channel_id)
        return self._watchlist.forget_unavailable(row["id"])

    def watchlist_resolve_candidates(self, channel_id):
        row = self._watchlist_row(channel_id)
        return self._watchlist.resolve_candidates(row["id"])

    def watchlist_resolve_apply(self, channel_id, resolved_url=None,
                                resolved_channel_id=None):
        row = self._watchlist_row(channel_id)
        return self._watchlist.resolve_apply(
            row["id"], resolved_url=resolved_url,
            channel_id=resolved_channel_id)

    def db_groups(self, preset, filters):
        """One level of the Downloads tab's group tree — counts only, no
        rows. *filters* pins the levels already drilled into (see
        DownloadsDatabase.group_downloads)."""
        db = self._db()
        if db is None:
            return {"available": False, "groups": []}
        preset = preset or next(iter(DownloadsDatabase.GROUP_PRESETS))
        if DownloadsDatabase.GROUP_PRESETS.get(preset) is None:
            raise CBError(f"Unknown group-by preset: {preset!r}")
        try:
            groups = db.group_downloads(preset, filters)
        except ValueError as exc:
            raise CBError(str(exc))
        return {"available": True, "groups": groups}

    # ── database viewer: contract-id row mapping ────────────────────────────
    # cratebuilder.db's helpers speak raw DB column names (channel_name,
    # upload_date, download_timestamp, ...) by design (see task-6-report.md)
    # — this is the one place that translates them to the contract's UI ids
    # (channel, upload, downloaded, ...), including fields no db.py method
    # can produce on its own: the Watch List folder path (needs Settings'
    # base_dir + CrateLayout) and cleanup eligibility, and the Artwork tab's
    # derived on_disk fact (needs a live filesystem check the raw row can't
    # carry). db.py stays pure SQL; this stays the one seam that knows both
    # vocabularies.

    _DL_SORT_MAP = {
        "title": "title", "channel": "channel_name", "genre": "genre",
        "platform": "platform", "upload": "upload_date",
        "downloaded": "download_timestamp", "bitrate": "bitrate",
    }
    _ART_SORT_MAP = {
        "title": "title", "channel": "channel_name", "platform": "platform",
        "embedded": "artwork_embedded", "sidecar": "artwork_path",
        "thumb_url": "thumbnail_url",
    }
    _WL_SORT_KEYS = {
        "channel": lambda r: (r.get("channel") or "").lower(),
        "link": lambda r: (r.get("link") or "").lower(),
        "folder": lambda r: (r.get("folder") or "").lower(),
        "platform": lambda r: (r.get("platform") or "").lower(),
        "genre": lambda r: (r.get("genre") or "").lower(),
        "last_scan": lambda r: int(r.get("last_scan") or 0),
        "pending": lambda r: int(r.get("pending") or 0),
        "total": lambda r: int(r.get("total") or 0),
        "status": lambda r: (r.get("status") or "").lower(),
    }

    _EXPORT_COLUMNS = {
        "downloads": [("title", "Title"), ("channel", "Channel"),
                      ("genre", "Genre"), ("platform", "Platform"),
                      ("upload", "Upload date"), ("downloaded", "Downloaded"),
                      ("bitrate", "Bitrate"), ("file_path", "File path")],
        "watchlist": [("channel", "Channel"), ("link", "URL Link"),
                      ("folder", "Folder"), ("platform", "Platform"),
                      ("genre", "Genre"), ("pending", "Pending new"),
                      ("total", "Total dl'd"), ("status", "Status")],
        "artwork": [("title", "Track"), ("channel", "Channel"),
                    ("platform", "Platform"), ("embedded", "Embedded"),
                    ("sidecar", "Sidecar"), ("thumb_url", "Thumbnail URL"),
                    ("file_path", "File path")],
    }

    @staticmethod
    def _map_download_row(row):
        ts = row.get("download_timestamp")
        downloaded = ""
        if ts:
            try:
                downloaded = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                downloaded = ""
        return {
            "id": row.get("id"),
            "title": row.get("title") or "",
            "channel": row.get("channel_name") or "",
            "genre": (row.get("genre") or "").strip() or "(none)",
            "platform": (row.get("platform") or "").strip() or "(unknown)",
            "upload": row.get("upload_date") or "",
            "downloaded": downloaded,
            "downloaded_ts": ts,
            "bitrate": row.get("bitrate") or "",
            "file_path": row.get("file_path") or "",
            "channel_url": row.get("channel_url") or "",
        }

    @staticmethod
    def _map_artwork_row(row, derive_on_disk=True):
        path = (row.get("artwork_path") or "").strip()
        # The CSV export has no On Disk column, so statting every exported
        # row would be a full-library filesystem scan for a value nothing
        # reads.
        on_disk = (os.path.isfile(path) if path else None) if derive_on_disk else None
        return {
            "id": row.get("id"),
            "title": row.get("title") or "(untitled)",
            "channel": row.get("channel_name") or "",
            "platform": row.get("platform") or "",
            "embedded": bool(row.get("artwork_embedded")),
            "sidecar": os.path.basename(path) if path else "",
            "sidecar_path": path,
            "on_disk": on_disk,
            "thumb_url": row.get("thumbnail_url") or "",
            "file_path": row.get("file_path") or "",
        }

    def _channel_folder(self, platform, genre, display_name):
        """The channel's crate folder — pure naming via CrateLayout, same
        path a download would write to, or "" for an unrecognised platform
        or a base_dir CrateLayout can't build a path from."""
        if platform not in ("YouTube", "SoundCloud"):
            return ""
        try:
            base = self._settings.get("base_dir")
            return CrateLayout.channel_dir(
                platform_dir(base, platform), genre, display_name)
        except Exception:
            return ""

    def _wl_cleanup_eligibility(self, row, folder):
        """(eligible, reason) for one Watch List row's Folders Cleanup
        checkbox — port of the monolith's _wl_cleanup_eligibility, plus a
        web-only "downloading" case the design adds (a channel mid-download
        can't safely be cleaned; the monolith's dialog never faces this
        since its own scan runs sequentially). Reason text comes from the
        ui_strings registry where a key exists, and verbatim from the
        monolith's own strings where it doesn't — never paraphrased."""
        if (row.get("status") or "").lower() == "downloading":
            return False, ui_strings.tooltip("db.cleanup_ineligible_downloading")
        if is_unresolved_channel(row) or row.get("status") in ("needs_resolve", "error"):
            return False, ui_strings.tooltip("db.cleanup_ineligible_unresolved")
        if not folder or not os.path.isdir(folder):
            return False, "Folder missing — no downloads to clean."
        try:
            has_mp3 = any(f.lower().endswith(".mp3") for f in os.listdir(folder))
        except OSError:
            has_mp3 = False
        if not has_mp3:
            return False, "Folder empty — nothing to clean."
        return True, ""

    # The monolith's UNRESOLVED_URL_PREFIX (DJ-CrateBuilder_v1.3.py) and
    # cratebuilder.db's private _UNRESOLVED_URL_PREFIX — a duplicate literal,
    # not an import, same "monolith depends on cratebuilder, never the
    # reverse" reasoning db.py already applies to its own copy. Needed here,
    # not db.query_watchlist_rows(): that helper already blanks a sentinel
    # url to "" before returning, which would hide the signal
    # is_unresolved_channel needs from _wl_cleanup_eligibility — so this
    # table reads db.get_all_watchlist_channels() (raw rows) and does its
    # own blanking after eligibility has already been judged.
    _UNRESOLVED_URL_PREFIX = "unresolved://"

    def _map_watchlist_row(self, row):
        platform = (row.get("platform") or "").strip()
        folder = self._channel_folder(platform, row.get("genre"),
                                      row.get("display_name"))
        eligible, reason = self._wl_cleanup_eligibility(row, folder)
        raw_url = row.get("url") or ""
        unresolved_link = raw_url.startswith(self._UNRESOLVED_URL_PREFIX)
        return {
            "id": row.get("id"),
            "channel": row.get("display_name") or raw_url or "Channel",
            "link": "" if unresolved_link else raw_url,
            "link_unresolved": unresolved_link,
            "folder": folder,
            "platform": platform,
            "genre": row.get("genre") or "",
            "last_scan": row.get("last_scanned_timestamp"),
            "pending": int(row.get("pending_new_count") or 0),
            "total": int(row.get("total_downloaded") or 0),
            "status": row.get("status") or "idle",
            "eligible": eligible,
            "ineligible_reason": reason,
        }

    @classmethod
    def _sort_column(cls, sort, mapping, default):
        """The raw DB column one contract sort id names.

        An unmapped id is refused rather than quietly falling back to the
        default: a header that renders its own sort arrow while the query
        ordered by something else is a lie the user can't see through."""
        col = sort.get("col")
        if col in (None, ""):
            return default
        mapped = mapping.get(col)
        if mapped is None:
            raise CBError(f"That column can't be sorted: {col}")
        return mapped

    def _query_rows(self, table, filters, sort, offset, limit,
                    want_total=True, derive_on_disk=True):
        """(rows, total) for one Database-viewer table, rows mapped to the
        contract's UI column ids. Shared by db_query (one page) and
        db_export_csv (the whole filtered set, offset 0 / a very high
        limit) so the two can never disagree about what a filter means.

        want_total=False skips the count entirely (total comes back as
        None) — "Sidecar missing on disk" has to stat every candidate to
        count them, so a "load more" that already knows the total from its
        first page must not pay for it again on every page turn."""
        db = self._db()
        if db is None:
            return [], (0 if want_total else None)
        filters = dict(filters or {})
        sort = sort or {}
        offset = max(0, int(offset or 0))
        limit = max(0, int(limit or 0))

        if table == "downloads":
            col = self._sort_column(sort, self._DL_SORT_MAP, "download_timestamp")
            descending = bool(sort.get("desc", True))
            try:
                total = db.count_downloads(filters) if want_total else None
                rows = db.query_downloads(filters, order_by=col,
                                          descending=descending,
                                          limit=limit, offset=offset)
            except ValueError as exc:
                raise CBError(str(exc))
            return [self._map_download_row(r) for r in rows], total

        if table == "artwork":
            filter_name = filters.get("filter_name") or DownloadsDatabase.ARTWORK_FILTERS[0]
            search = filters.get("search")
            col = self._sort_column(sort, self._ART_SORT_MAP, "title")
            descending = bool(sort.get("desc", False))
            try:
                total = (db.count_artwork_rows(filter_name, search=search)
                         if want_total else None)
                rows = db.query_artwork_rows(filter_name, search=search,
                                             order_by=col, descending=descending,
                                             limit=limit, offset=offset)
            except ValueError as exc:
                raise CBError(str(exc))
            return [self._map_artwork_row(r, derive_on_disk) for r in rows], total

        if table == "watchlist":
            rows = [self._map_watchlist_row(r)
                   for r in db.get_all_watchlist_channels()]
            search = (filters.get("search") or "").strip().lower()
            if search:
                rows = [r for r in rows if search in
                        f"{r['channel']} {r['link']} {r['folder']}".lower()]
            key = self._sort_column(sort, self._WL_SORT_KEYS, None)
            if key:
                rows.sort(key=key, reverse=bool(sort.get("desc", False)))
            total = len(rows)
            if limit:
                rows = rows[offset:offset + limit]
            elif offset:
                rows = rows[offset:]
            return rows, (total if want_total else None)

        raise CBError(f"Unknown table: {table!r}")

    def db_query(self, table, filters, sort, offset, limit, want_total=True):
        """One page of {table}'s rows for the Database viewer.

        *limit* is clamped to MAX_DB_PAGE_SIZE here, so a 20k-row library
        arriving in one payload is impossible for any caller, not just for
        the client this repo happens to ship."""
        limit = min(max(1, int(limit or DEFAULT_DB_PAGE_SIZE)), MAX_DB_PAGE_SIZE)
        rows, total = self._query_rows(table, filters, sort, offset, limit,
                                       want_total=bool(want_total))
        return {"rows": rows, "total": total}

    # A leading =, +, -, @, tab or CR makes Excel and LibreOffice read a cell
    # as a formula. Track titles are third-party text straight off
    # YouTube/SoundCloud, so a channel can name an upload =cmd|'/c calc'!A1
    # and have it fire when the user opens their own export.
    _CSV_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

    @classmethod
    def _csv_cell(cls, key, value):
        if key == "embedded":
            return "Yes" if value else "No"
        if value is None:
            return ""
        if isinstance(value, str) and value[:1] in cls._CSV_FORMULA_LEADERS:
            return "'" + value
        return value

    def db_export_csv(self, table, filters, sort):
        """The whole current filtered set for *table* as CSV text.

        Returned inline only — nothing is written to disk. The browser
        builds its download Blob from "csv" (the same shape logs.download's
        local path already uses), and a host-side file would otherwise
        accumulate one full copy of the library per export with nothing to
        delete it."""
        columns = self._EXPORT_COLUMNS.get(table)
        if columns is None:
            raise CBError(f"Unknown export table: {table!r}")
        rows, _total = self._query_rows(table, filters, sort, 0, EXPORT_ROW_CAP,
                                        want_total=False, derive_on_disk=False)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([label for _key, label in columns])
        for row in rows:
            writer.writerow([self._csv_cell(key, row.get(key))
                             for key, _label in columns])
        return {"filename": f"cratebuilder_{table}.csv",
                "rows": len(rows), "csv": buf.getvalue()}

    def db_artwork_preview(self, row_id):
        """{data_url} (base64) for one downloads row's artwork — never sent
        as part of a row payload.

        Keyed by row id, never by a path: the sidecar and MP3 paths are read
        off the row itself, so no client-supplied path ever reaches open()
        (HANDOFF §8.4). Prefers the sidecar file; falls back to the bytes
        embedded in the MP3 so a track whose sidecar was deleted still
        previews, matching the monolith's _art_show_preview. {data_url:
        None} when neither source has anything to show — the caller renders
        the empty-state text.

        Both paths come straight off the row this method reads itself, so
        there is no client-supplied path to gate — fs.reveal's containment
        check has nothing to add here and is not used. What actually stops
        a non-image being served is the MIME label — read from the bytes
        rather than assumed, so anything that isn't a PNG/JPEG/WebP is
        refused instead of shipped as image/jpeg with the file's contents
        base64'd inside it.

        The two branches bound their read differently: the sidecar stops at
        the syscall (MAX_PREVIEW_BYTES + 1), while extract_cover materialises
        the whole APIC frame before its size is checked. That path is a
        DB-recorded MP3 the library wrote, never a caller's choice, so the
        worst case is a legitimately huge embedded cover briefly costing
        host memory."""
        db = self._db()
        row = db.get_download(row_id) if db is not None else None
        if row is None:
            return {"data_url": None}
        path = (row.get("artwork_path") or "").strip()
        file_path = (row.get("file_path") or "").strip()
        data = None
        note = ""
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    data = fh.read(MAX_PREVIEW_BYTES + 1)
            except OSError:
                data = None
            if data and len(data) > MAX_PREVIEW_BYTES:
                return {"data_url": None,
                        "note": "Image too large to preview"}
            if data:
                note = os.path.basename(path)
        if not data and file_path:
            data = extract_cover(file_path)
            if data and len(data) > MAX_PREVIEW_BYTES:
                return {"data_url": None,
                        "note": "Image too large to preview"}
            if data:
                note = ("embedded artwork (no sidecar on disk)" if path
                        else "embedded artwork")
        if not data:
            return {"data_url": None}
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            return {"data_url": None, "note": "That file is not an image"}
        width = height = None
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.width, im.height
        except Exception:
            pass
        encoded = base64.b64encode(data).decode("ascii")
        return {"data_url": f"data:{mime};base64,{encoded}", "note": note,
                "width": width, "height": height, "size": len(data)}

    # ── settings ──────────────────────────────────────────────────────────────

    def settings_all(self):
        """Every schema key the design's Settings screen renders, translated
        to the display form SETTINGS_BINDINGS says the contract expects."""
        out = {}
        for entry in ui_strings.SETTINGS_KEYS:
            key = entry.get("key")
            get, _ = _binding(key)
            try:
                out[key] = get(self._settings)
            except KeyError:
                continue        # contract lists remote-only keys the app lacks
        return out

    def settings_get(self, key):
        if not key:
            raise CBError("No setting was named.")
        get, _ = _binding(key)
        try:
            return {"key": key, "value": get(self._settings)}
        except KeyError:
            raise CBError(f"Unknown setting: {key}")

    def settings_set(self, key, value):
        """Set one key and echo the stored value back, mirroring autosave.

        *value* arrives in the contract's display form; the binding's set()
        translates it to what Settings actually stores before writing.
        """
        if not key:
            raise CBError("No setting was named.")
        get, set_ = _binding(key)
        if key == "run_at_startup":
            return self._set_run_at_startup(value, get)
        if key == "base_dir":
            value = _validate_base_dir(value)
        try:
            set_(self._settings, value)
        except KeyError:
            raise CBError(f"Unknown setting: {key}")
        except (TypeError, ValueError) as exc:
            raise CBError(str(exc))
        return {"key": key, "value": get(self._settings)}

    def _set_run_at_startup(self, value, get):
        """Toggle the Windows Run-at-login registry entry, then persist.

        Local transport only — it edits the host's own registry, which a
        remote browser has no business reaching. Stricter than
        _on_run_at_startup_toggle in DJ-CrateBuilder_v1.3.py, which only
        refuses to persist a failed *enable* (a failed disable is still
        saved as False there regardless of the registry's actual state):
        here, any set_startup() failure — enable or disable — is refused
        and nothing is persisted.
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
        return {"key": "run_at_startup", "value": get(self._settings)}

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

    # ── logs ──────────────────────────────────────────────────────────────────
    # Both viewers (Activity/Debug) share this surface, keyed by the short name
    # the frontend uses ("activity"/"debug") rather than the filename, so a
    # renamed log file is one edit here instead of a hunt through web/app.js.

    def _log_path_for(self, name):
        if name == "activity":
            return self._log_path
        if name == "debug":
            return self._debug_log_path
        raise CBError(f"Unknown log: {name!r}")

    def logs_tail(self, name, offset=None, limit=DEFAULT_LOG_WINDOW, before=False):
        """A window of decoded lines from the log, anchored by byte offset.

        offset=None means "the last `limit` lines" (tail-anchored); otherwise
        the window starts at that byte and reads forward — unless `before` is
        set, in which case it reads backward: the last `limit` lines that end
        at or before that byte. That's how the viewer lazy-loads earlier
        content after a search jump has landed the window mid-file (`before`
        is ignored when `offset` is None, since tail-anchored already reads
        from the end backward). A falsy `limit` means no cap — used by the
        download path to pull the whole file in one call. `offset` in the
        response is the byte position right after the last line returned, so
        passing it back continues forward from there (or, with `before=True`,
        continues further backward from `start`); `start` is the window's own
        first byte, for a caller (jumping to a search hit, say) that needs to
        tell whether a given offset already falls inside what's loaded.
        `total_lines` is the whole file's line count — free to report, since
        every call already walks the whole file to keep byte offsets exact.
        Never raises for a missing file — that's simply an empty log."""
        path = self._log_path_for(name)
        limit = int(limit) if limit else None
        data = _read_log_bytes(path)
        if data is None:
            return {"lines": [], "offset": 0, "start": 0, "size": 0,
                    "total_lines": 0, "path": path}
        size = len(data)
        all_lines = list(_iter_log_lines(data))
        if offset is None:
            chosen = all_lines[-limit:] if limit else all_lines
        elif before:
            end_byte = max(0, min(int(offset), size))
            chosen = [line for line in all_lines if line[0] + line[2] <= end_byte]
            if limit:
                chosen = chosen[-limit:]
        else:
            start_byte = max(0, min(int(offset), size))
            chosen = [line for line in all_lines if line[0] >= start_byte]
            if limit:
                chosen = chosen[:limit]
        lines = [raw.decode("utf-8", errors="replace") for (_s, raw, _n) in chosen]
        if chosen:
            start = chosen[0][0]
            end = chosen[-1][0] + chosen[-1][2]
        else:
            start = end = size if offset is None else max(0, min(int(offset), size))
        return {"lines": lines, "offset": end, "start": start, "size": size,
                "total_lines": len(all_lines), "path": path}

    def logs_search(self, name, query, regex=False):
        """Every matching line in the whole file, server-side — not just the
        window a client currently has loaded. `matches` is capped for payload
        size; `total` is always the true count."""
        path = self._log_path_for(name)
        query = (query or "")
        if not query:
            return {"matches": [], "total": 0}
        data = _read_log_bytes(path)
        if data is None:
            return {"matches": [], "total": 0}
        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as exc:
                raise CBError(f"Not a valid search pattern: {exc}")
            test = pattern.search
        else:
            needle = query.lower()
            test = lambda text: needle in text.lower()  # noqa: E731
        matches = []
        total = 0
        for line_no, (start, raw, _n) in enumerate(_iter_log_lines(data), start=1):
            text = raw.decode("utf-8", errors="replace")
            if test(text):
                total += 1
                if len(matches) < MAX_LOG_SEARCH_MATCHES:
                    matches.append({"offset": start, "line_no": line_no})
        return {"matches": matches, "total": total}

    def logs_download(self, name):
        """The log's absolute path. Read-only and side-effect free, so it's
        fine on the remote transport too — Task 11's /logs/<name> route is
        what turns this into an actual download for a browser that isn't the
        host; for now the caller gets the path back either way."""
        return {"path": self._log_path_for(name)}

    def logs_watch(self, name, on):
        """Start or stop the background tail for one log, ref-counted so a
        second client opening the same viewer doesn't stop the first one's
        watch when it later closes. The poll loop is a plain daemon thread,
        not a job-registry job — nothing about it competes with a batch or
        maintenance run, and it must be able to stop the moment nobody is
        watching, not wait for a whole run to finish."""
        path = self._log_path_for(name)
        on = bool(on)
        with self._lock:
            watcher = self._log_watchers.get(name)
            if on:
                if watcher is None:
                    watcher = {"count": 0, "stop": threading.Event(), "thread": None}
                    self._log_watchers[name] = watcher
                watcher["count"] += 1
                if watcher["thread"] is None or not watcher["thread"].is_alive():
                    watcher["stop"].clear()
                    thread = threading.Thread(
                        target=self._watch_log, args=(name, path, watcher["stop"]),
                        daemon=True)
                    watcher["thread"] = thread
                    thread.start()
            elif watcher is not None:
                watcher["count"] = max(0, watcher["count"] - 1)
                if watcher["count"] == 0:
                    watcher["stop"].set()
        return {"watching": on}

    def _watch_log(self, name, path, stop_event):
        """Poll *path*'s size on a daemon thread; emit log.append for what
        grew since the last poll. A shrink (the log's own size-cap trimming,
        or the file being deleted and recreated) just resyncs the baseline —
        better to miss one delta across a trim than to emit bytes that no
        longer mean what they used to."""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        while not stop_event.wait(self._log_watch_interval):
            try:
                new_size = os.path.getsize(path)
            except OSError:
                continue
            if new_size > size:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(size)
                        chunk = handle.read(new_size - size)
                except OSError:
                    continue
                lines = [raw.decode("utf-8", errors="replace")
                         for (_s, raw, _n) in _iter_log_lines(chunk)]
                if lines:
                    self.emit("log.append", {"name": name, "lines": lines,
                                             "offset": new_size})
                size = new_size
            elif new_size < size:
                size = new_size

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

    def _fs_path_is_contained(self, path):
        """True when *path* is somewhere the viewer is allowed to point the
        OS at: inside the current crate folder, or a path the library
        itself recorded.

        The second clause is not slack — base_dir is a setting the user can
        change, and every row written before that change names a folder
        outside today's crate root. Refusing those would break "Open File"
        on the user's own history; accepting anything at all would let page
        content name a path the app has never seen."""
        try:
            target = os.path.realpath(path)
        except OSError:
            return False
        root = (self._settings.get("base_dir") or "").strip()
        if root:
            try:
                root = os.path.realpath(root)
                if os.path.commonpath([root, target]) == root:
                    return True
            except (OSError, ValueError):
                pass        # different drives, or an unresolvable base_dir
        db = self._db()
        return db is not None and db.download_path_is_recorded(path)

    def fs_reveal(self, path, mode="folder"):
        """Open *path* with its OS default app (mode="open") or select it
        in its containing folder (mode="folder", the default) — the
        Database viewer's Open File / Open Containing Folder context-menu
        actions.

        Remote never reaches this: the LOCAL_ONLY prefix check in call()
        already refuses every "fs." method server-side before dispatch, and
        this repeats the check the way pick_folder does, rather than
        trusting that alone. Local is not a blank cheque either — mode
        "open" is ShellExecute, so an .exe/.bat/.lnk there would be "run
        this program" rather than "show me this track": only the audio and
        image types the viewer itself displays are openable, and both modes
        require the path to be one the library owns.

        The extension gate reads the RESOLVED name, so a symlink or junction
        called cover.jpg cannot smuggle a payload.exe past it — the check and
        the containment test then agree on which file they are talking
        about."""
        if self.transport != LOCAL:
            raise CBError("Opening files only works in the app window on the "
                          "host machine.")
        path = (path or "").strip()
        if not path:
            raise CBError("No path is recorded for this row.")
        try:
            target = os.path.realpath(path)
        except OSError:
            raise CBError("That path is outside the crate folder.")
        if mode == "open" and os.path.splitext(target)[1].lower() not in OPENABLE_EXTENSIONS:
            raise CBError("Only the app's own audio and image files can be "
                          "opened from here.")
        if not self._fs_path_is_contained(path):
            raise CBError("That path is outside the crate folder.")
        try:
            if mode == "open":
                if not os.path.exists(path):
                    raise CBError(f"This file is no longer on disk:\n{path}")
                self._os_open(path)
            else:
                if sys.platform == "win32" and os.path.exists(path):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    folder = path if os.path.isdir(path) else os.path.dirname(path)
                    while folder and not os.path.isdir(folder):
                        parent = os.path.dirname(folder)
                        if parent == folder:
                            break
                        folder = parent
                    if not folder or not os.path.isdir(folder):
                        raise CBError(f"Could not find a folder for:\n{path}")
                    self._os_open(folder)
        except OSError as exc:
            raise CBError(f"Could not open that location: {exc}")
        return {"opened": True}

    @staticmethod
    def _os_open(target):
        """Open *target* (a file or folder) with the OS default handler."""
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])

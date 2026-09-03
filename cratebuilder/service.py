"""Transport-agnostic action surface shared by the local window and remote clients."""

import ast
import base64
import csv
import io
import itertools
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

from cratebuilder import (activitylog, debuglog, rebuild, startup, ui_strings,
                          util, ydl)
from cratebuilder import updater_core as ucore
from cratebuilder.artwork import DEFAULT_COVER_ART_MODE, extract_cover
from cratebuilder.batchresolve import PLATFORM_SUBDIR, platform_dir
from cratebuilder.batchrun import BatchRunner
from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase
from cratebuilder.events import Coalescer, EventBus
from cratebuilder.links import LINKS_FILE_NAME
from cratebuilder.remoteauth import REMOTE_FILE_NAME, RemoteState
from cratebuilder.settings import Settings
from cratebuilder.sidecar import is_unresolved_channel

MAIN_SCRIPT = "DJ-CrateBuilder_v2.0.py"
DB_NAME = "cratebuilder.db"
ACTIVITY_LOG = "activity.log"
DEBUG_LOG = "debug.log"

LOCAL = "local"
REMOTE = "remote"

# The job registry's category for every Watch List run. One at a time, so a
# scan and a download can never fight over the same channel folder.
WATCHLIST_JOB = "watchlist"

# Cold-boot guard for the startup scan. When the app auto-launches at Windows
# login the network is often a few seconds behind, and a scan run while offline
# just fails every channel for nothing. So the startup scan waits for
# connectivity first: probe every _NET_DELAY seconds, up to _NET_TRIES times,
# then give up quietly — a manual scan still runs normally.
WATCHLIST_STARTUP_NET_TRIES = 18      # ≈ 90 s window at the delay below
WATCHLIST_STARTUP_NET_DELAY = 5.0     # seconds between connectivity probes
WATCHLIST_STARTUP_DELAY = 2.2         # the monolith's after(2200, …) settle

# The job registry's category for every database maintenance run — rebuild,
# de-dup, tag repair and artwork backfill share one slot, so two of them can
# never be writing the downloads table at the same time.
MAINTENANCE_JOB = "maintenance"

# The job registry's category for an in-app self-update. Its slot excludes,
# and is excluded by, batch/watchlist/maintenance in both directions — an
# update swaps every file under the app and restarts it, which a download or
# maintenance run mid-flight would not survive; see _require_idle_for_update
# and the UPDATE_JOB checks in _require_idle_for_download/_require_idle_library.
UPDATE_JOB = "update"

# The one event that means "this job category is free again". Emitted by
# _start_job AFTER the slot is released, which is what separates it from the
# runs' own terminal events (`batch.finished`, the closing DONE scan line) —
# those are emitted from inside the run, while the slot is still held. A
# frontend refreshes its state on this; the others are display only.
#
# It also carries HOW the run ended — `ok` and `error` — because that is the
# only place a crash can be reported from. A run that raises has no terminal
# event of its own, so without these a frontend settles a dead job as a
# success (which is exactly what it used to do).
JOB_FINISHED = "job.finished"

# Its mirror: a job category has just been CLAIMED. Emitted by _start_job with
# the slot already taken, so a frontend resyncing on it cannot be answered with
# a snapshot that says nothing is running.
#
# It exists because a run does not have to be started by the frontend watching
# it. The launch scan starts itself, the tray's Scan Now starts one with no page
# involved, and on the remote transport one browser starts runs a second browser
# has to render. Every one of those used to leave the other client's controls
# reading idle — offering a Scan that the host would refuse and a Cancel that
# was closed — until something else happened to trigger a refresh.
JOB_STARTED = "job.started"

# What a crashed job is called in the error notification _start_job publishes.
# Maintenance passes its own per-task title instead, since "Rebuild Database
# from Files" is what the user pressed, not "Database maintenance".
JOB_TITLES = {
    "batch": "Downloads",
    WATCHLIST_JOB: "Watch List",
    MAINTENANCE_JOB: "Database maintenance",
    UPDATE_JOB: "Update",
}

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

# The three Remote Access toggles the contract lists as settings keys but the
# app's own config schema has no room for: they govern who may reach this
# control surface, so they live in cratebuilder_remote.json beside the tokens
# they gate rather than in the user's ordinary settings file. Mapped here so
# settings.get/set still speak the contract's names.
REMOTE_SETTINGS_KEYS = {
    "remote_enabled": "enabled",
    "remote_require_pairing": "require_pairing",
    "remote_read_only": "read_only",
}

# base_dir is the trust root fs.reveal's containment check is measured against
# (see _fs_path_is_contained), so a remote client able to move it could point
# the boundary at anywhere on the host and then walk through it. The picker is
# already LOCAL_ONLY; this closes the typed-path half.
REMOTE_BASE_DIR_REFUSAL = (
    "The save directory is the boundary that keeps a remote session inside "
    "your crate folder, so it can only be changed from the app window on the "
    "host machine.")

REMOTE_SETTING_REFUSAL = (
    "Remote access settings can only be changed from the app window on the "
    "host machine.")

# The three cross-job refusals, spelled once. Each is raised from two places —
# the synchronous pre-flight a clicker sees, and the guard `_start_job` runs
# under the job lock — and the two must say exactly the same thing.
MAINTENANCE_BLOCKS_DOWNLOAD = (
    "A database maintenance job is running. It rewrites the downloads table, "
    "so a download started now could be lost — wait for it to finish, or "
    "cancel it from its progress window.")

RETAG_BLOCKS_DOWNLOAD = (
    "A Watch List genre change is rewriting a channel's genre tags right now. "
    "A download landing new files in that folder would tag the same files at "
    "the same moment — wait for it to finish, then try again.")

RETAG_BLOCKS_TAG_SWEEP = (
    "A Watch List genre change is rewriting that channel's genre tags right "
    "now. Two tag sweeps must never write the same file at once — wait for it "
    "to finish, then try again.")

DOWNLOAD_BLOCKS_LIBRARY = (
    "A download is running. This rewrites the downloads table, so it has to "
    "wait — cancel the run or let it finish, then try again.")

RETAG_BLOCKS_GENRE_MOVE = (
    "A Watch List genre change is still rewriting a channel's genre tags. A "
    "second genre move would put two tag writers on the same files — wait for "
    "it to finish, then try again.")

# The update job's exclusion, spelled once for each direction — see UPDATE_JOB.
UPDATE_BLOCKS_DOWNLOAD = (
    "An update is installing. The app will restart to finish it, so a "
    "download has to wait — try again once the update completes.")

UPDATE_BLOCKS_LIBRARY = (
    "An update is installing. The app will restart to finish it, so "
    "database maintenance has to wait — try again once the update "
    "completes.")

UPDATE_NEEDS_IDLE_JOBS = (
    "A download, scan, or database maintenance job is running. Installing "
    "an update restarts the app, which would cut that off mid-way — wait "
    "for it to finish, or cancel it, then try again.")

UPDATE_BLOCKS_SCAN = (
    "An update is installing. The app will restart to finish it, so a "
    "Watch List scan has to wait — try again once the update completes.")

# The settings the tkinter app freezes for the length of a run
# (`_set_download_lock`, DJ-CrateBuilder_v2.0.py:9994), mapped from the widgets
# it disables to the contract keys that drive them:
#
#   _skip_existing_cb → skip_existing        _skip_mode_combo  → skip_mode
#   _bitrate_combo    → bitrate_quality      _bitrate_upgrade_cb → bitrate_auto_upgrade
#   _no_conv_cb       → no_conversion        _cover_art_combo  → cover_art_mode
#   _limit_enable_cb / _limit_slider / the ± buttons
#                     → limit_enabled, limit_minutes
#   _settings_dir_entry / _settings_browse_btn → base_dir
#
# The freeze is what makes the per-track re-read of the policy safe: both
# frontends re-read every setting for every track, so anything writable
# mid-run lands on the very next track of a run the user believes is pinned —
# and a base_dir change scatters one batch across two crate roots. The three
# DB-maintenance buttons in the same widget list are already refused server
# side by `_require_idle_library`; `_update_btn` has no web equivalent.
# `cover_art_enabled` is deliberately absent, because the monolith leaves its
# checkbox live — this ports the lock, it does not widen it.
DOWNLOAD_LOCKED_SETTINGS = {
    "base_dir": "the save directory",
    "skip_existing": "the skip-existing option",
    "skip_mode": "the skip mode",
    "bitrate_quality": "the output quality",
    "bitrate_auto_upgrade": "the bitrate auto-upgrade",
    "no_conversion": "the no-conversion option",
    "cover_art_mode": "the cover-art formatting",
    "limit_enabled": "the length limiter",
    "limit_minutes": "the length limit",
}

_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.M)
_BUILD_RE = re.compile(r"^APP_BUILD\s*=\s*(\d+)", re.M)

# The auto-check dropdown's choices, read verbatim from the monolith's own
# UPDATE_CHECK_OPTIONS (DJ-CrateBuilder_v2.0.py:474) rather than reparsed from
# source: it is a plain list literal assigned once, with no per-release drift
# to guard against the way APP_BUILD has. There is no "Off" state — the
# monolith's list never offers one for this dropdown (unlike the download
# auto-run interval, which does).
UPDATE_CHECK_OPTIONS = ["1 hour", "3 hours", "6 hours", "12 hours", "1 day"]

# The two manifest URL constants, read out of the monolith the same way
# version_info() reads APP_VERSION/APP_BUILD — one copy, no drift. Unlike
# those two, each is built from adjacent string literals rather than one
# short literal, so this walks the assignment via ast.literal_eval instead of
# a line-anchored regex.
_MANIFEST_URL_NAMES = ("UPDATE_MANIFEST_URL", "UPDATE_MANIFEST_URL_LINUX")

# Keyed by path -> (stat signature, {name: url}), exactly like _ABOUT_CACHE
# below — a 13k-line ast.parse on every check/apply/hourly timer fire is the
# same avoidable cost about_info() already solved once.
_MANIFEST_URL_CACHE = {}


def _manifest_urls(script_path=None):
    """{'UPDATE_MANIFEST_URL': ..., 'UPDATE_MANIFEST_URL_LINUX': ...} read
    from the monolith's own module-level assignments, or {} if the file
    can't be read or parsed."""
    path = script_path or _monolith_path()
    try:
        stamp = os.stat(path)
        signature = (stamp.st_mtime_ns, stamp.st_size)
    except OSError:
        signature = None
    cached = _MANIFEST_URL_CACHE.get(path)
    if signature is not None and cached and cached[0] == signature:
        return dict(cached[1])

    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return {}
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", None)
            if name in _MANIFEST_URL_NAMES:
                try:
                    found[name] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    if signature is not None:
        _MANIFEST_URL_CACHE[path] = (signature, dict(found))
    return found

# The About screen's own copy. The avatar is the bundle's, at the 44x44 the
# tkinter label uses; the note is the monolith's bug/suggestion line, which
# lives inside a widget call there and so has no constant to read.
ABOUT_AVATAR = "assets/about_avatar.png"
ABOUT_NOTE = ("*(For any bugs encountered or suggestions you'd like to make, "
              "submit them using the Submit Issues/Suggestions button.)")
# Which monolith module constant fills which About field.
ABOUT_CONSTANTS = {
    "created_by": "ABOUT_CREATED_BY",
    "contact_email": "ABOUT_CONTACT_EMAIL",
    "description": "ABOUT_DESCRIPTION",
    "github_url": "GITHUB_URL",
    "issues_url": "GITHUB_ISSUES_URL",
}
# The About tab's FAQ is a local list inside this function, not a constant.
ABOUT_FAQ_OWNER = "_build_about_tab"
ABOUT_FAQ_NAME = "faq"

# What fs.open_url will hand to the host's browser. mailto is the author's
# contact link; nothing else is a scheme a page of ours has any reason to open,
# and file:/ javascript: on os.startfile is how a link becomes an execution.
OPENABLE_URL_SCHEMES = ("http", "https", "mailto")

# about_info parses a 13k-line file, so the result is kept until the file
# changes underneath it. Keyed by path -> (stat signature, payload).
_ABOUT_CACHE = {}
_COOKIE_HOWTO_CACHE = {}


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
# DJ-CrateBuilder_v2.0.py's own _COVER_ART_FORMAT_MODES, which excludes
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
    of truth for "off", matching DJ-CrateBuilder_v2.0.py's
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


# cookie_method stores "Browser" (see DJ-CrateBuilder_v2.0.py's
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


def _genre_platform(value):
    """The crate's platform key for a genre folder, from either spelling the
    monolith's New Genre picker uses ("Soundcloud" included)."""
    wanted = str(value or "").strip().lower()
    for key in PLATFORM_SUBDIR:
        if key.lower() == wanted:
            return key
    raise CBError("Choose a platform for the genre folder.")


def _validate_base_dir(path):
    """Canonicalize a save-directory path, creating it if needed.

    Rejects a blank path, a path that already exists as a file, and a path
    that cannot be created (e.g. a drive that doesn't exist) — the same
    guarantees DJ-CrateBuilder_v2.0.py's _save_settings gets from
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
    Frozen, this resolves inside PyInstaller's `_internal/` — fine for
    finding this file, but never the right root for user data or the
    bundled monolith; see app_dir() and _monolith_path().
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    """Where the DB, both logs, the link store and the remote-access store
    live.

    From source, that's beside the monolith script, as always. Frozen,
    it's the install root next to DJ-CrateBuilder.exe — where every
    existing 1.3 install already keeps cratebuilder.db — never repo_root(),
    which resolves inside `_internal/` under PyInstaller and would orphan
    that data on upgrade. util.runtime_data_dir's not-writable fallback
    still applies either way.
    """
    script = (sys.executable if getattr(sys, "frozen", False)
             else os.path.join(repo_root(), MAIN_SCRIPT))
    return util.runtime_data_dir(script)


def _monolith_path():
    """Where the monolith's source text lives right now.

    From source, the repo checkout; frozen, PyInstaller's --add-data root
    (sys._MEIPASS), which is where the build places a bundled copy of the
    script. version_info, about_info and _manifest_urls all read the
    monolith as plain text rather than importing it — this is the one
    place that decides which copy they see, so the three can't drift.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, MAIN_SCRIPT)
    return os.path.join(repo_root(), MAIN_SCRIPT)


def bundled_ffmpeg_dir():
    """The bundled FFmpeg's directory when frozen, else None.

    The service-side twin of DJ-CrateBuilder_v2.0.py's bundled_ffmpeg_dir():
    PyInstaller ships ffmpeg.exe/ffprobe.exe beside the app's own exe, so
    pointing yt-dlp straight at that folder works regardless of how the app
    is installed. From source, None lets yt-dlp find FFmpeg on PATH, as
    documented.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(sys.executable)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for cand in (exe_dir, getattr(sys, "_MEIPASS", None)):
        if cand and os.path.isfile(os.path.join(cand, name)):
            return cand
    return None


def app_icon_path():
    """The bundled app icon (icon.ico), or None when it isn't there.

    The service-side twin of DJ-CrateBuilder_v2.0.py's app_icon_path(), for
    the same reason bundled_ffmpeg_dir() has one: the web entry point cannot
    import the monolith to borrow it — that builds a Tk window — and its tray
    icon wants the real artwork rather than TrayIcon's runtime-drawn
    placeholder. Same resolution order as the monolith's: beside the frozen
    executable, then PyInstaller's data root, then beside the script from
    source. The Linux .deb ships the icon as a hicolor PNG instead of beside
    the script, which is the last candidate.
    """
    if getattr(sys, "frozen", False):
        cands = (os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", None))
    else:
        cands = (repo_root(),)
    for cand in cands:
        if cand:
            path = os.path.join(cand, "icon.ico")
            if os.path.isfile(path):
                return path
    png = "/usr/share/icons/hicolor/256x256/apps/dj-cratebuilder.png"
    if os.path.isfile(png):
        return png
    return None


def version_info(script_path=None):
    """APP_VERSION / APP_BUILD read from the monolith as text.

    Parsed rather than imported: importing the v1.3 script builds a Tk window,
    which would drag the whole service into the gui test lane for two constants.
    """
    path = script_path or _monolith_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return {"version": None, "build": None}
    version = _VERSION_RE.search(source)
    build = _BUILD_RE.search(source)
    return {"version": version.group(1) if version else None,
            "build": int(build.group(1)) if build else None}


def about_info(script_path=None):
    """The About screen's content, read out of the monolith as source text.

    Parsed with `ast` for the reason version_info is parsed at all: importing
    the v1.3 script builds a Tk window. The author fields and the two GitHub
    URLs are module constants; the FAQ is a local list inside
    `_build_about_tab`, so there is nothing importable even if that were free —
    and thirty questions copied into a second file drift within a release.

    A file that cannot be read or parsed yields empty fields rather than an
    error: the About screen still renders, minus the parts it could not find.
    """
    path = script_path or _monolith_path()
    try:
        stamp = os.stat(path)
        signature = (stamp.st_mtime_ns, stamp.st_size)
    except OSError:
        signature = None
    cached = _ABOUT_CACHE.get(path)
    if signature is not None and cached and cached[0] == signature:
        return _about_copy(cached[1])

    info = {name: "" for name in ABOUT_CONSTANTS}
    info["faq"] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return info

    wanted = {const: field for field, const in ABOUT_CONSTANTS.items()}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            field = wanted.get(getattr(target, "id", None))
            if field and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                info[field] = node.value.value
    info["faq"] = _about_faq(tree)
    if signature is not None:
        _ABOUT_CACHE[path] = (signature, _about_copy(info))
    return info


def _about_copy(info):
    """A caller-owned copy. `dict()` alone would share the `faq` LIST with the
    module-level cache, so one caller appending to it would hand every later
    session an extra question."""
    out = dict(info)
    out["faq"] = [dict(row) for row in info.get("faq") or ()]
    return out


def cookie_howto_texts(script_path=None):
    """COOKIE_HOWTO_TEXTS — the per-browser profile walkthroughs — read out of
    the monolith as source text, for the reason about_info reads the FAQ that
    way: one copy, and importing the script would build a Tk window. Returns
    {browser: text}, or {} when the file cannot be read or holds no such dict.
    Cached on the file's (mtime, size) signature like _ABOUT_CACHE."""
    path = script_path or _monolith_path()
    try:
        stamp = os.stat(path)
        signature = (stamp.st_mtime_ns, stamp.st_size)
    except OSError:
        return {}
    cached = _COOKIE_HOWTO_CACHE.get(path)
    if cached and cached[0] == signature:
        return dict(cached[1])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return {}
    texts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) \
                or not isinstance(node.value, ast.Dict):
            continue
        if not any(getattr(target, "id", None) == "COOKIE_HOWTO_TEXTS"
                   for target in node.targets):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)):
                texts[key.value] = value.value
    _COOKIE_HOWTO_CACHE[path] = (signature, dict(texts))
    return texts


def _about_faq(tree):
    """The [(question, answer), ...] literal `_build_about_tab` builds its
    accordion from, as the {q, a} rows the web About screen renders."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == ABOUT_FAQ_OWNER):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Assign):
                continue
            names = [getattr(t, "id", None) for t in inner.targets]
            if ABOUT_FAQ_NAME not in names:
                continue
            try:
                pairs = ast.literal_eval(inner.value)
            except (ValueError, TypeError):
                return []
            return [{"q": str(item[0]), "a": str(item[1])}
                    for item in pairs
                    if isinstance(item, (list, tuple)) and len(item) >= 2]
    return []


class CrateBuilderService:
    """Every UI action, transport-agnostic.

    Raises CBError with a user-facing message; never returns a tkinter widget
    or a Tk variable. One instance per host process — the batch it owns is the
    web frontend's queue, kept here rather than in a widget so both transports
    see the same list.
    """

    def __init__(self, transport=LOCAL, settings=None, db_path=None,
                 log_path=None, debug_log_path=None,
                 log_watch_interval=DEFAULT_LOG_WATCH_INTERVAL,
                 remote_state=None):
        if transport not in (LOCAL, REMOTE):
            raise ValueError(f"unknown transport: {transport}")
        # The transport is a property of the CALL, not of the object: one
        # process hosts both mounts (the desktop window and the server thread)
        # over one service core, so `call(..., transport=REMOTE)` sets it for
        # the duration of that call and this is only the fallback for callers
        # that don't say. Thread-local, because the two mounts run on different
        # threads — the server's routes are sync handlers, which Starlette runs
        # in its threadpool, one request per thread.
        self._default_transport = transport
        self._call_transport = threading.local()
        self._settings = settings or Settings()
        self._db_path = db_path or os.path.join(app_dir(), DB_NAME)
        self._log_path = log_path or os.path.join(app_dir(), ACTIVITY_LOG)
        self._debug_log_path = debug_log_path or os.path.join(app_dir(), DEBUG_LOG)
        # debug.log is WRITTEN here, not merely read by the log viewer. The
        # database and every TrackDownloader this service builds are handed this
        # logger, which is the whole of what the tkinter app puts in that file.
        self._dbg = debuglog.build_debug_logger(
            self._debug_log_path, max_bytes=self._log_max_bytes())
        debuglog.session_banner(self._dbg,
                                version=version_info().get("version"))
        self._lock = threading.Lock()
        self._batch = []
        self._ids = itertools.count(1)
        self.events = EventBus()
        self._emit = Coalescer(self.events)
        self._jobs = {}
        self._batch_runner = None
        self._watchlist_ops = None
        self._maintenance_ops = None
        self._cleanup_ops = None
        # How many Watch List genre-move retag threads are writing tags right
        # now. Guarded by self._lock, alongside the job registry it excludes.
        self._retags = 0
        # The durable channel-link store lives beside the database, so a test
        # pointing db_path at a tmp dir never writes the developer's real one.
        self._links_path = os.path.join(
            os.path.dirname(self._db_path) or app_dir(), LINKS_FILE_NAME)
        # Same reasoning for the remote-access store: a test pointing db_path
        # at a tmp dir must never read or write the developer's real device
        # tokens. Injectable so the window and the server thread share one.
        self._remote_path = os.path.join(
            os.path.dirname(self._db_path) or app_dir(), REMOTE_FILE_NAME)
        self._remote_state = remote_state
        self._log_watch_interval = log_watch_interval
        self._log_watchers = {}
        # Invoked from the update.apply worker thread once the updater process
        # has been handed off, so the local window can exit cleanly. None on
        # a service nothing has wired it into (every test, the remote-only
        # entry points) — the worker treats that as "nothing to call".
        self.on_update_restart = None
        self._update_timer = None
        self._next_update_check_ts = None
        # The auto-download scheduler, armed by start_auto_download_timer().
        # The anchor is set when it is armed, never read from
        # `watchlist_last_download` — see that method for why.
        self._auto_dl_thread = None
        self._auto_dl_anchor = 0
        self._auto_dl_next_ts = None
        # Whether that None has ever been ANNOUNCED, which is not the same as
        # its being the current value: an interval already set to 'Off' at
        # launch publishes None, and without this the de-duplication would
        # read it as unchanged and say nothing at all.
        self._auto_dl_announced = False
        self._auto_dl_wake = threading.Event()
        self._closed = False
        self.reset_stale_watchlist_rows()
        # The auto-check timer is armed explicitly via start_update_timer(),
        # not as a constructor side effect — the same reason
        # start_remote_mount() is a separate call rather than something the
        # constructor does on its own. A service built for one snapshot or
        # one test (every test, most tooling) must not leak a daemon Timer
        # just from being constructed; the local window calls
        # start_update_timer() itself, right after building the service.

    def _log_max_bytes(self):
        """The byte cap the user's Log Size Limit puts on each log file;
        0 means Unlimited, exactly as the tkinter app reads it."""
        try:
            return int(self._settings.get("log_max_mb") or 0) * 1024 * 1024
        except (TypeError, ValueError):
            return 0

    # ── events / jobs ─────────────────────────────────────────────────────────

    def emit(self, type, payload):
        if type == "notification" and self._notification_muted(payload):
            return
        self._emit.emit(type, payload)

    def _notification_muted(self, payload):
        """Settings ▸ Remote Access's three notification toggles, applied at
        the one place every notification passes — so the bell, the toasts and
        every paired device all hear the same thing, or nothing.

        `kind` names the two announcements with a toggle of their own: the
        Watch List scan's closing summary ("scan_found") and the batch's
        ("batch_done"). "Notify on errors" covers every error-level
        notification, whichever run raised it. Everything else — an update,
        a maintenance summary — is always delivered.
        """
        payload = payload or {}
        kind = payload.get("kind")
        if kind == "scan_found":
            return not self._settings.get("notify_scan_found")
        if kind == "batch_done":
            return not self._settings.get("notify_batch_done")
        if payload.get("level") == "error":
            return not self._settings.get("notify_errors")
        return False

    def _job_running(self, category):
        with self._lock:
            return category in self._jobs

    def _start_job(self, category, target, *args, title=None, guard=None):
        """Run `target` on a daemon thread; refuse a second job per category.

        The run's own terminal events (`batch.finished`, the closing `DONE`
        scan line) are emitted from inside *target*, while this category is
        still in `self._jobs` — so a client that reacts to one by asking for a
        snapshot can be told the job is still running and re-arm a run that has
        already ended. `job.finished` is emitted after the slot is released
        precisely so that answer cannot come back stale: it, not the display
        events, is what a frontend resyncs on.

        A run that RAISES is caught here rather than being left to take its
        worker thread down silently. It has no terminal event of its own, so
        this is the only place its failure can be reported: an `error`-level
        `notification` carrying the message, and `job.finished` stamped
        `ok=False` so a frontend renders a failure instead of settling the
        dialog as a green success. Both are emitted after the slot is
        released, for the same reason `job.finished` always was.

        *guard* is a callable run WHILE THIS HOLDS `self._lock`, immediately
        after the slot is confirmed free and before it is taken; it refuses the
        start by raising CBError. It exists so a check that has to be atomic
        with claiming the slot — "no Watch List retag thread is writing tags
        right now" — cannot be raced by one starting in between. It must never
        re-acquire `self._lock`.
        """
        with self._lock:
            if category in self._jobs:
                raise CBError(f"A {category} job is already running.")
            if guard is not None:
                guard()
            job_id = next(self._ids)
            self._jobs[category] = job_id

        # Outside the lock, but with the slot already held — see JOB_STARTED.
        self.emit(JOB_STARTED, {"job": category, "job_id": job_id})

        def run():
            error = None
            try:
                target(*args)
            except Exception as exc:
                # A CBError's message was written to be read by the user;
                # anything else needs its type to be identifiable at all.
                message = str(exc).strip()
                error = (message if isinstance(exc, CBError)
                         else f"{type(exc).__name__}: {message}".rstrip(": "))
            finally:
                with self._lock:
                    self._jobs.pop(category, None)
                if error is not None:
                    self.emit("notification", {
                        "level": "error",
                        "title": title or JOB_TITLES.get(category, category),
                        "body": error,
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "job": category,
                    })
                self.emit(JOB_FINISHED, {"job": category,
                                         "ok": error is None,
                                         "error": error})

        threading.Thread(target=run, daemon=True).start()
        return job_id

    # ── tag-write exclusion ───────────────────────────────────────────────────
    # mutagen's ID3.save() rewrites an MP3 in place, shifting the audio when the
    # tag grows — so two threads saving the same file can truncate it. Only two
    # things in the app write tags in bulk: the Watch List's genre-move retag
    # and the db.repair_tags sweep. The monolith makes them exclusive with one
    # `_tag_repair_active` flag it can only read from the Tk thread; here they
    # are on different threads, so the claim and the check share the job lock —
    # a retag can only start while no maintenance job holds the slot, and
    # db.repair_tags can only take the slot while no retag is in flight.

    def claim_tag_writes(self):
        """Register a Watch List retag about to start. False means refuse.

        Refused while a maintenance job holds the slot, and refused while
        ANOTHER retag is still sweeping: two genre moves on one channel in
        quick succession (A→B, then B→C before the first sweep ends) would
        otherwise put two `genrefix.repair_track` writers on the same MP3s,
        which is the truncation this whole mechanism exists to prevent. The
        pre-flight in `watchlist_edit` is what the user normally sees; this is
        the half that cannot be raced, because the check and the claim share
        one hold of the lock.

        Also refused while an update is installing: a retag moves the
        channel folder on disk and rewrites its MP3s' ID3 frames in place —
        exactly the write an update's file-swap-and-restart must not land
        on top of. See UPDATE_JOB.
        """
        with self._lock:
            if (UPDATE_JOB in self._jobs or MAINTENANCE_JOB in self._jobs
                    or self._retags):
                return False
            self._retags += 1
            return True

    def release_tag_writes(self):
        with self._lock:
            self._retags = max(0, self._retags - 1)

    # Every refusal below reads `self._jobs` / `self._retags` WITHOUT taking
    # `self._lock`, because each is called from two places: once on the calling
    # thread as the synchronous pre-flight (so a refusal is the answer to the
    # user's click rather than a crash notification a moment later), and once
    # as `_start_job`'s `guard`, which runs with the lock already held and is
    # the call that actually decides. Taking the lock here would deadlock the
    # second; a torn read in the first is harmless, since the guard re-asks.

    def _refuse_while_retagging(self):
        """`_start_job`'s guard for db.repair_tags."""
        if self._retags:
            raise CBError(RETAG_BLOCKS_TAG_SWEEP)

    def _require_idle_for_download(self):
        """Refuse a download while a maintenance job or a retag is live.

        The mirror of `_require_idle_library`, which refuses the three
        table-rewriting maintenance jobs while a download runs. Without the
        maintenance half the exclusion only holds in one direction: a download
        started mid-rebuild writes rows that `clear_all_downloads()` then
        deletes, or lands in the window between the clear and the backfill and
        is silently dropped. The monolith leaves its Start button live during a
        rebuild and has exactly that hole; this closes it rather than porting
        it.

        The retag half is the same argument one layer down: a download landing
        new files into a folder a genre-move sweep is still walking tags them
        (`batchrun._settle` → `TrackDownloader.tag`) while `genrefix` is saving
        the same file. `watchlist_edit` already refuses the move while a
        download runs; this is the reverse order.

        The update half is the same argument one layer up: an update swaps
        every file under the app and restarts it, which a download mid-flight
        would not survive — see UPDATE_JOB.
        """
        if UPDATE_JOB in self._jobs:
            raise CBError(UPDATE_BLOCKS_DOWNLOAD)
        if MAINTENANCE_JOB in self._jobs:
            raise CBError(MAINTENANCE_BLOCKS_DOWNLOAD)
        if self._retags:
            raise CBError(RETAG_BLOCKS_DOWNLOAD)

    def _require_idle_for_scan(self):
        """Refuse a Watch List scan while an update is installing.

        A scan claims WATCHLIST_JOB, the same category a download uses, but
        that only excludes another scan/download — a different category
        (UPDATE_JOB) is not refused by `_start_job` on its own. An update
        restarts the app mid-run, which a scan subprocess mid-listing would
        not survive any better than a download would."""
        if UPDATE_JOB in self._jobs:
            raise CBError(UPDATE_BLOCKS_SCAN)

    # ── dispatch ──────────────────────────────────────────────────────────────

    @property
    def transport(self):
        """Which mount the call being served came in on.

        Read all over this class (capabilities, the host-only refusals) and
        answered per call, so the same service object tells the desktop window
        it may reveal a folder and tells the server thread it may not.
        """
        return getattr(self._call_transport, "value", None) or self._default_transport

    @property
    def remote_state(self):
        """The device-token / pairing store, built on first use.

        Lazy for the same reason the database is: constructing one reads (and
        can create) a file, which a service built only to answer a snapshot
        should not do.
        """
        if self._remote_state is None:
            self._remote_state = RemoteState(self._remote_path)
        return self._remote_state

    def call(self, method, params=None, transport=None):
        """Route one contract method name to its handler.

        The single entry point both transports use, so the local pywebview
        bridge and the WebSocket/HTTP RPC cannot drift in what they accept.
        *transport* names the mount this one call arrived on and holds for the
        duration of the call; omitted, the constructor's value stands.
        """
        if transport is not None and transport not in (LOCAL, REMOTE):
            raise ValueError(f"unknown transport: {transport}")
        previous = getattr(self._call_transport, "value", None)
        self._call_transport.value = transport or previous
        try:
            if self.transport == REMOTE and method.startswith(LOCAL_ONLY):
                raise CBError("That action is only available in the app window "
                              "on the host machine.")
            handler = self._methods().get(method)
            if handler is None:
                raise CBError(f"Unknown action: {method}")
            return handler(dict(params or {}))
        finally:
            self._call_transport.value = previous

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
            "db.maintenance_preview":
                lambda p: self.maintenance_preview(p.get("task")),
            "db.rebuild": lambda p: self.maintenance_start("db.rebuild"),
            "db.dedupe": lambda p: self.maintenance_start("db.dedupe"),
            "db.repair_tags": lambda p: self.maintenance_start("db.repair_tags"),
            "db.fetch_artwork":
                lambda p: self.maintenance_start("db.fetch_artwork"),
            "db.maintenance_cancel": lambda p: self.maintenance_cancel(),
            "db.maintenance_skip": lambda p: self.maintenance_skip(),
            "db.cleanup_start":
                lambda p: self.cleanup_start(p.get("channel_ids")),
            "db.cleanup_decide": lambda p: self.cleanup_decide(
                p.get("action"), p.get("paths")),
            "db.cleanup_cancel": lambda p: self.cleanup_cancel(),
            "db.cleanup_pending": lambda p: self.cleanup_pending(),
            "about.info": lambda p: self.about(),
            "cookies.howto": lambda p: self.cookies_howto(p.get("browser")),
            "genres.create": lambda p: self.genres_create(p.get("name"),
                                                          p.get("platform")),
            "genres.remove": lambda p: self.genres_remove(p.get("name"),
                                                          p.get("platform")),
            "fs.pick_folder": lambda p: self.pick_folder(),
            "fs.reveal": lambda p: self.fs_reveal(p.get("path"),
                                                  p.get("mode", "folder")),
            "fs.open_url": lambda p: self.open_url(p.get("url")),
            "logs.tail": lambda p: self.logs_tail(
                p.get("name"), p.get("offset"), p.get("limit", DEFAULT_LOG_WINDOW),
                bool(p.get("before"))),
            "logs.search": lambda p: self.logs_search(
                p.get("name"), p.get("query"), bool(p.get("regex"))),
            "logs.download": lambda p: self.logs_download(p.get("name")),
            "logs.watch": lambda p: self.logs_watch(p.get("name"), p.get("on")),
            "remote.config": lambda p: self.remote_config(),
            "remote.devices": lambda p: self.remote_devices(),
            "remote.pair_begin": lambda p: self.remote_pair_begin(),
            "remote.pair_cancel": lambda p: self.remote_pair_cancel(),
            "remote.revoke": lambda p: self.remote_revoke(
                p.get("device_id") or p.get("token_hash")),
            "update.check": lambda p: self.update_check(),
            "update.apply": lambda p: self.update_apply(),
            "update.status": lambda p: self.update_status(),
            "update.set_interval":
                lambda p: self.update_set_interval(p.get("value")),
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
                        "maintenance": self._job_running(MAINTENANCE_JOB),
                        # Which of the four, so a frontend that reloaded mid
                        # run can reopen the right progress dialog. Read off
                        # the ops object only if one has ever been built —
                        # asking for it would construct one for nothing.
                        "maintenance_task": self._maintenance_task_name()},
            "settings": self.settings_all(),
            "settings_path": self._settings.path,
            # When the scheduler next fires, so a page that just loaded shows
            # the same line the automation.next_run event keeps current.
            "next_auto_download": self.next_auto_download(),
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

    def about(self):
        """Everything the About screen renders, including the build number.

        Readable on both transports: the design shows About in a remote
        session precisely so you can tell whether the host is current. Only
        the updater controls beside it are local-session-only, and those are
        gated by the `update.` prefix in LOCAL_ONLY, not here.
        """
        info = about_info()
        version = version_info()
        return {
            **info,
            **version,
            "app_name": "DJ-CrateBuilder",
            "avatar": ABOUT_AVATAR,
            "note": ABOUT_NOTE,
            "build_status": (f"You're on build {version['build']}."
                             if version.get("build") is not None else ""),
            "can_open_urls": self.transport == LOCAL,
        }

    def cookies_howto(self, browser):
        """The dedicated-profile walkthrough for *browser* — what the tkinter
        CookieHowToWindow shows, with its fallback: a browser without a page of
        its own (Vivaldi) reads the Chrome one. Readable on both transports;
        it is help text."""
        texts = cookie_howto_texts()
        name = str(browser or "").strip() or "Chrome"
        text = texts.get(name) or texts.get("Chrome")
        if not text:
            raise CBError("The walkthrough could not be read from the app's "
                          "own source.")
        return {"browser": name,
                "title": f"How-To: Setting Up a Dedicated {name} Profile",
                "text": text}

    # ── library / database ────────────────────────────────────────────────────

    def _db(self):
        if not os.path.isfile(self._db_path):
            return None
        return DownloadsDatabase(self._db_path, debug_logger=self._dbg)

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
                counts=self.counts, flush=self._emit.flush,
                ffmpeg_dir=bundled_ffmpeg_dir(), debug=self._dbg,
                claim_tag_writes=self.claim_tag_writes,
                release_tag_writes=self.release_tag_writes)
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
        self._require_idle_for_scan()
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_scan,
                                          [row["id"]],
                                          guard=self._require_idle_for_scan)}

    def watchlist_scan_all(self):
        """Scan every watched channel, in list order."""
        ids = [row["id"] for row in self._watchlist_rows()]
        if not ids:
            raise CBError("No channels to scan.")
        self._require_idle_for_scan()
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_scan, ids,
                                          guard=self._require_idle_for_scan)}

    def watchlist_download_new(self, channel_id):
        """Download one channel's pending new tracks.

        Pressed while a Watch List download is already running, the channel
        joins that run's queue rather than being refused — the tkinter Watch
        List's append-to-running, which is what the design's Download New
        tooltip promises."""
        row = self._watchlist_row(channel_id)
        self._require_idle_for_download()
        if self._job_running(WATCHLIST_JOB):
            position = self._watchlist.enqueue(row["id"])
            if position is not None:
                return {"queued_position": position}
            # No download run to join — a scan owns the job. Falling through
            # lets _start_job give the "already running" answer rather than
            # inventing a second one here.
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_download,
                                          [row["id"]],
                                          guard=self._require_idle_for_download)}

    def watchlist_download_all_new(self):
        """Download every channel's pending new tracks.

        Re-anchors the auto-download schedule, the monolith's "Download All
        New stamps the anchor": whoever pressed it — the user, the tray, or
        the scheduler itself — the next scheduled run is a full interval from
        this download rather than from the one before it.
        """
        ids = [row["id"] for row in self._watchlist_rows()
               if int(row.get("pending_new_count") or 0) > 0]
        if not ids:
            raise CBError("No new tracks pending across any channels. Try "
                          "Scan All first.")
        self._require_idle_for_download()
        self._reanchor_auto_download()
        return {"job_id": self._start_job(WATCHLIST_JOB,
                                          self._watchlist.run_download, ids,
                                          guard=self._require_idle_for_download)}

    def watchlist_force_download(self, channel_id):
        """Re-process one channel's whole catalogue, skipping nothing."""
        row = self._watchlist_row(channel_id)
        if is_unresolved_channel(row):
            raise CBError("This channel's link isn't resolved yet. Use Fix "
                          "Link on the card first, then Force Download.")
        self._require_idle_for_download()
        job_id = self._start_job(WATCHLIST_JOB, self._watchlist.run_download,
                                 [row["id"]], True,
                                 guard=self._require_idle_for_download)
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
        plain-field edit touches no files and stays allowed.

        It is refused while a MAINTENANCE job runs for a second reason: the
        move ends by rewriting the moved folder's genre tags, and a
        db.repair_tags sweep may already be saving those same MP3s. Two
        mutagen writes to one file is a truncated track, not a lost update —
        so the two are made exclusive here and, for the reverse order, at
        `claim_tag_writes`.

        And it is refused while ANOTHER genre move's retag is still sweeping,
        for the third time the same argument: A→B followed by B→C before the
        first sweep ends is two `genrefix` writers on one channel's files.
        `claim_tag_writes` refuses that too, atomically; this is the half that
        tells the user why instead of silently skipping their tags."""
        row = self._watchlist_row(channel_id)
        picked = (genre or "").strip()
        if picked and picked != (row.get("genre") or CrateLayout.NO_GENRE_VALUE):
            if self._retags:
                raise CBError(RETAG_BLOCKS_GENRE_MOVE)
            if self._job_running("batch") or self._job_running(WATCHLIST_JOB):
                raise CBError("A download is running. The channel's folder "
                              "can't be moved to another genre until it "
                              "finishes — cancel it first, or change the link "
                              "only.")
            if self._job_running(MAINTENANCE_JOB):
                raise CBError("A database maintenance job is running. A genre "
                              "move rewrites the channel's genre tags, which "
                              "must not happen while another sweep is writing "
                              "to the same files — wait for it to finish, or "
                              "change the link only.")
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

    # The monolith's UNRESOLVED_URL_PREFIX (DJ-CrateBuilder_v2.0.py) and
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

    # ── database maintenance ──────────────────────────────────────────────────
    # Dispatch only: cratebuilder.maintenance owns every sequence. What lives
    # here is what the transport layer owns — refusing a run that would fight
    # a download for the downloads table, and handing the run to the job
    # registry so the frontend hears `job.finished` when the slot is free.

    @property
    def _maintenance(self):
        """The maintenance operations, built on first use.

        Imported here rather than at module scope for the same reason
        watchrun is: maintenance imports CBError and MAINTENANCE_JOB from this
        module, and deferring keeps the dependency one-way."""
        if self._maintenance_ops is None:
            from cratebuilder.maintenance import MaintenanceOps
            self._maintenance_ops = MaintenanceOps(
                self._settings, self._db_for_write, self.emit,
                log_line=self.log_line, counts=self.counts,
                flush=self._emit.flush, ffmpeg_dir=bundled_ffmpeg_dir())
        return self._maintenance_ops

    # Rebuild, de-dup and the artwork backfill all write the downloads table
    # the moment a download is also writing it, so the tkinter app disables
    # their three buttons for the length of a run (_set_download_lock, which
    # covers a Watch List batch too) — and the rebuild's clear/backfill would
    # silently swallow a concurrent backfill's writes besides. Repair Track
    # Tags is deliberately absent from that list: it only rewrites tags inside
    # files and never touches a row, so the monolith leaves it available mid
    # download and so does this.
    _MAINTENANCE_NEEDS_IDLE = ("db.rebuild", "db.dedupe", "db.fetch_artwork",
                               "db.cleanup")

    def _maintenance_task_name(self):
        """Which maintenance-slot job is running: one of the four, or Folders
        Cleanup. Read off the ops objects only if one has ever been built —
        asking for either would construct it for nothing."""
        if self._cleanup_ops is not None and self._cleanup_ops.running:
            return self._cleanup_ops.task
        if self._maintenance_ops is not None:
            return self._maintenance_ops.task
        return None

    def _require_idle_library(self, task):
        """Unlocked like the other refusals — see the note above
        `_refuse_while_retagging`. Serves both `maintenance_preview` (where
        there is no slot to claim) and `_maintenance_guard`.

        The UPDATE_JOB check applies to EVERY maintenance task, including
        db.repair_tags — which is otherwise deliberately excluded from
        `_MAINTENANCE_NEEDS_IDLE` (it only rewrites tags inside files, not a
        download-vs-rebuild collision on the downloads table). An update
        still can't tolerate it: a repair sweep saves ID3 frames in place
        with mutagen the same way a retag does, and an update's
        restart-mid-write is exactly the corruption that write must never
        race. So this check runs BEFORE the _MAINTENANCE_NEEDS_IDLE early
        return, not after it.
        """
        if UPDATE_JOB in self._jobs:
            raise CBError(UPDATE_BLOCKS_LIBRARY)
        if task not in self._MAINTENANCE_NEEDS_IDLE:
            return
        if "batch" in self._jobs or WATCHLIST_JOB in self._jobs:
            raise CBError(DOWNLOAD_BLOCKS_LIBRARY)

    def _maintenance_guard(self, task):
        """`_start_job`'s guard for a maintenance run: both of the task's
        cross-category checks, made atomic with claiming the slot."""
        self._require_idle_library(task)
        if task == "db.repair_tags":
            self._refuse_while_retagging()

    def maintenance_preview(self, task):
        """The counts a confirm modal quotes, or the reason there is nothing
        to confirm. Never starts anything."""
        self._require_idle_library(task)
        return self._maintenance.preview(task)

    def maintenance_start(self, task):
        """Run one maintenance job on the maintenance job thread.

        The preflight runs HERE, on the calling thread, so a refusal reaches
        the user as the answer to their click; `_start_job`'s worker reports a
        CBError raised a moment later as a crash instead.

        Every one of those checks is repeated as `_start_job`'s guard, and
        that repetition is the point: the preview between them is a DB and
        filesystem pass, so a download starting while it runs would pass its
        own idle check (a different job category) and both would hold a slot.
        The pre-flight is for the message; the guard is what makes it true."""
        runners = {
            "db.rebuild": self._maintenance.run_rebuild,
            "db.dedupe": self._maintenance.run_dedupe,
            "db.repair_tags": self._maintenance.run_repair_tags,
            "db.fetch_artwork": self._maintenance.run_fetch_artwork,
        }
        runner = runners.get(task)
        if runner is None:
            raise CBError(f"Unknown maintenance job: {task!r}")
        self._require_idle_library(task)
        if task == "db.repair_tags":
            self._refuse_while_retagging()
        self._maintenance.preview(task)
        job_id = self._start_job(MAINTENANCE_JOB, runner,
                                 title=self._maintenance.title_for(task),
                                 guard=lambda: self._maintenance_guard(task))
        return {"job_id": job_id, "task": task}

    def maintenance_cancel(self):
        """Stop the running job after the item in flight. Safe to call when
        nothing is running — cancelling never destroys anything, so a click
        that races the job's own ending must not answer with an error."""
        return self._maintenance.cancel()

    def maintenance_skip(self):
        """Abandon the track the artwork backfill is fetching right now."""
        return self._maintenance.skip()

    # ── Folders Cleanup ───────────────────────────────────────────────────────

    @property
    def _cleanup(self):
        """Folders Cleanup, built on first use — deferred like maintenance,
        since cleanuprun imports from this module."""
        if self._cleanup_ops is None:
            from cratebuilder.cleanuprun import CleanupOps
            self._cleanup_ops = CleanupOps(
                self._settings, self._db_for_write, self.emit,
                log_line=self.log_line, debug=self._dbg)
        return self._cleanup_ops

    def cleanup_start(self, channel_ids):
        """Run Folders Cleanup over *channel_ids* on the maintenance slot.

        The eligibility the Database viewer's checkboxes already show is
        judged again here, from the raw rows, so a client cannot tick a
        channel the viewer greyed out — the monolith's dialog never faces
        that, its ticks being its own.
        """
        from cratebuilder.cleanuprun import TASK, TITLE
        cids = []
        for value in channel_ids or []:
            try:
                cids.append(int(value))
            except (TypeError, ValueError):
                raise CBError(f"Not a channel id: {value!r}")
        if not cids:
            raise CBError("Tick at least one channel first.")
        self._require_idle_library(TASK)
        db = self._db()
        if db is None:
            raise CBError("There is no database yet, so there is nothing "
                          "to clean.")
        for cid in cids:
            row = db.get_watchlist_channel(cid)
            if row is None:
                raise CBError(f"Channel {cid} is no longer in the Watch List.")
            folder = self._channel_folder((row.get("platform") or "").strip(),
                                          row.get("genre"),
                                          row.get("display_name"))
            eligible, reason = self._wl_cleanup_eligibility(row, folder)
            if not eligible:
                name = (row.get("display_name") or row.get("url")
                        or f"channel {cid}")
                raise CBError(f"{name}: {reason}")
        job_id = self._start_job(MAINTENANCE_JOB, self._cleanup.run, cids,
                                 title=TITLE,
                                 guard=lambda: self._maintenance_guard(TASK))
        return {"job_id": job_id, "task": TASK, "channels": len(cids)}

    def cleanup_decide(self, action, paths):
        """Answer the channel review the run is waiting on."""
        return self._cleanup.decide(action, paths)

    def cleanup_cancel(self):
        """Stop after the channel in flight; confirmed deletions stay."""
        return self._cleanup.cancel()

    def cleanup_pending(self):
        """The review awaiting an answer, for a page that reloaded mid-run.
        Never builds the ops object: nothing pending is the answer then."""
        pending = (self._cleanup_ops.pending_review()
                   if self._cleanup_ops is not None else None)
        return {"review": pending}

    # ── settings ──────────────────────────────────────────────────────────────

    def settings_all(self):
        """Every schema key the design's Settings screen renders, translated
        to the display form SETTINGS_BINDINGS says the contract expects."""
        out = {}
        for entry in ui_strings.SETTINGS_KEYS:
            key = entry.get("key")
            flag = REMOTE_SETTINGS_KEYS.get(key)
            if flag is not None:
                out[key] = self.remote_state.get_flag(flag)
                continue
            get, _ = _binding(key)
            try:
                out[key] = get(self._settings)
            except KeyError:
                continue        # contract lists remote-only keys the app lacks
        return out

    def settings_get(self, key):
        if not key:
            raise CBError("No setting was named.")
        flag = REMOTE_SETTINGS_KEYS.get(key)
        if flag is not None:
            return {"key": key, "value": self.remote_state.get_flag(flag)}
        get, _ = _binding(key)
        try:
            return {"key": key, "value": get(self._settings)}
        except KeyError:
            raise CBError(f"Unknown setting: {key}")

    def settings_set(self, key, value):
        """Set one key and echo the stored value back, mirroring autosave.

        *value* arrives in the contract's display form; the binding's set()
        translates it to what Settings actually stores before writing.

        The download-policy keys are frozen for the length of a run — see
        `_refuse_frozen_setting`.
        """
        if not key:
            raise CBError("No setting was named.")
        self._refuse_frozen_setting(key)
        flag = REMOTE_SETTINGS_KEYS.get(key)
        if flag is not None:
            if self.transport != LOCAL:
                raise CBError(REMOTE_SETTING_REFUSAL)
            stored = self.remote_state.set_flag(flag, bool(value))
            if flag == "enabled" and not stored:
                # set_flag has just closed every live socket and dropped the
                # control lock with them; the host's own Remote Access card is
                # still drawing whoever held it.
                self.emit("control.holder", {})
            return {"key": key, "value": stored}
        get, set_ = _binding(key)
        if key == "run_at_startup":
            return self._set_run_at_startup(value, get)
        if key == "base_dir":
            if self.transport != LOCAL:
                raise CBError(REMOTE_BASE_DIR_REFUSAL)
            value = _validate_base_dir(value)
        try:
            set_(self._settings, value)
        except KeyError:
            raise CBError(f"Unknown setting: {key}")
        except (TypeError, ValueError) as exc:
            raise CBError(str(exc))
        if key == "log_limit":
            # The cap has to reach the handler already holding debug.log open,
            # or the new limit only takes effect after a restart — the tkinter
            # app's _autosave_log_limit re-caps and trims on the spot.
            debuglog.set_max_bytes(self._dbg, self._log_max_bytes())
        if key == "auto_dl_interval":
            # The monolith reschedules from the same write that saves it
            # (_autosave_automation_settings). Without this the new interval
            # is stored and displayed but the armed timer keeps the old one
            # until the app restarts — this whole feature's failure mode.
            self._auto_dl_wake.set()
        return {"key": key, "value": get(self._settings)}

    def _refuse_frozen_setting(self, key):
        """The server-side half of the tkinter app's `_set_download_lock`.

        Both frontends re-read the download policy for EVERY track — that is
        deliberate, and it is only safe because the monolith disables the
        widgets behind those keys for the length of a run. The web stack ported
        the re-read and not the freeze, so a save-directory change mid-batch
        scattered one run across two crate roots and a bitrate change landed on
        the very next track. Refused rather than queued: a setting the user
        cannot see take effect is worse than one they are told to wait for.

        Only the keys `DOWNLOAD_LOCKED_SETTINGS` names, and only while a
        download job holds a slot — everything else stays settable mid-run,
        exactly as it does in the tkinter window.
        """
        what = DOWNLOAD_LOCKED_SETTINGS.get(key)
        if what is None:
            return
        if self._job_running("batch") or self._job_running(WATCHLIST_JOB):
            raise CBError(
                f"A download is running, so {what} is frozen until it "
                f"finishes. Every track re-reads these settings, so a change "
                f"now would land part-way through the run — cancel the "
                f"download, or wait for it to finish.")

    def _set_run_at_startup(self, value, get):
        """Toggle the Windows Run-at-login registry entry, then persist.

        Local transport only — it edits the host's own registry, which a
        remote browser has no business reaching. Stricter than
        _on_run_at_startup_toggle in DJ-CrateBuilder_v2.0.py, which only
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

    # ── remote access (design 3j's Remote Access card) ────────────────────────
    # The card is live on the LOCAL mount and read-only on a remote one: every
    # write here refuses off-host, because a browser that has been let in must
    # not be able to widen the door it came through — turn off the pairing
    # requirement, or drop the other devices that could take control back.
    # `remote.claim_control` is deliberately NOT here: which device is asking
    # is a fact of the connection, so the server answers that one itself.

    def _require_local_remote_admin(self):
        if self.transport != LOCAL:
            raise CBError(REMOTE_SETTING_REFUSAL)

    def remote_config(self):
        """The Remote Access card's whole state in one call.

        Two things are for the local window only. The live pairing code,
        obviously — a remote client handed that could pair a second device.
        And the paired-device ROSTER: who else the user has let in is not a
        fact about the caller's own connection, and a read-only session has no
        business learning the names and dates of every other device. Remote
        gets the count, which is all its (read-only-with-reason) card renders.
        """
        state = self.remote_state
        out = dict(state.config())
        out["control"] = state.control_holder()
        out["local"] = self.transport == LOCAL
        out["device_count"] = state.device_count()
        if self.transport == LOCAL:
            out["devices"] = state.devices()
            out["pairing"] = state.active_code()
        else:
            out["devices"] = []
        return out

    def remote_devices(self):
        if self.transport != LOCAL:
            return {"devices": [], "device_count": self.remote_state.device_count()}
        return {"devices": self.remote_state.devices(),
                "device_count": self.remote_state.device_count()}

    def remote_pair_begin(self):
        """Mint the 6-digit code the desktop window shows. Local only."""
        self._require_local_remote_admin()
        return self.remote_state.begin_pairing()

    def remote_pair_cancel(self):
        self._require_local_remote_admin()
        self.remote_state.cancel_pairing()
        return {"pairing": None}

    def remote_revoke(self, target):
        """Drop one paired device, or every one of them for "all"."""
        self._require_local_remote_admin()
        wanted = str(target or "").strip()
        if not wanted:
            raise CBError("No device was named.")
        removed = self.remote_state.revoke(wanted)
        if removed:
            self.emit("control.holder", self.remote_state.control_holder() or {})
        return {"removed": removed, "devices": self.remote_state.devices()}

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
        self._require_idle_for_download()
        runner = BatchRunner(
            self._settings, self._db_for_write(), self.emit,
            log_line=self.log_line, counts=self.counts,
            flush=self._emit.flush, ffmpeg_dir=bundled_ffmpeg_dir(),
            debug=self._dbg)
        previous = self._batch_runner
        self._batch_runner = runner
        try:
            job_id = self._start_job("batch", runner.run, rows,
                                     guard=self._require_idle_for_download)
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
        return DownloadsDatabase(self._db_path, debug_logger=self._dbg)

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

    def genres_create(self, name, platform):
        """Create <base>/<Platform>/<Genre> now — the monolith's _add_genre
        without its dialog. A folder that already exists is reported, not
        refused, exactly as that dialog's "Already Exists" box does."""
        platform = _genre_platform(platform)
        safe = util.safe_filename(name, strip=True)
        if not safe:
            raise CBError("That name isn't usable as a folder.")
        target = os.path.join(
            platform_dir(self._settings.get("base_dir"), platform), safe)
        existed = os.path.isdir(target)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise CBError(f"Unable to create the genre folder: {exc}\n\n"
                          f"Path: {target}")
        return {"genre": safe, "platform": platform, "existed": existed,
                "genres": self.genres()}

    def genres_remove(self, name, platform):
        """Delete a genre folder — only an empty one, the monolith's
        _remove_selected_genre rule: "(none)", a genre with no folder on disk
        and any folder still holding channel folders are all refused, so this
        can never destroy downloaded audio."""
        platform = _genre_platform(platform)
        picked = str(name or "").strip()
        if not picked or picked == CrateLayout.NO_GENRE_VALUE:
            raise CBError("Select a genre to remove first.")
        gdir = os.path.join(
            platform_dir(self._settings.get("base_dir"), platform), picked)
        if not os.path.isdir(gdir):
            raise CBError(f"No {platform} folder named '{picked}' exists.")
        if os.listdir(gdir):
            raise CBError(f"'{picked}' isn't empty — a genre folder can only "
                          f"be removed once every channel folder inside it "
                          f"has been moved out or deleted.")
        try:
            os.rmdir(gdir)
        except OSError as exc:
            raise CBError(f"Couldn't delete the folder: {exc}")
        self.log_line(f"Removed empty genre folder: {gdir}")
        return {"genre": picked, "platform": platform,
                "genres": self.genres()}

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
        about.

        A DIRECTORY is its own case in both modes, and the reason is that
        neither of the file cases means anything for one: `explorer /select,`
        on a folder highlights it inside its PARENT rather than opening it,
        and the extension allow-list has nothing to match, so "open" would
        refuse every folder outright. Either mode on a directory therefore
        means "open this folder" — which is what the Watch List's and the
        Database viewer's Open Folder actions are asking for."""
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
        is_dir = os.path.isdir(target)
        if (mode == "open" and not is_dir
                and os.path.splitext(target)[1].lower() not in OPENABLE_EXTENSIONS):
            raise CBError("Only the app's own audio and image files can be "
                          "opened from here.")
        if not self._fs_path_is_contained(path):
            raise CBError("That path is outside the crate folder.")
        try:
            if is_dir:
                self._os_open(path)
            elif mode == "open":
                if not os.path.exists(path):
                    raise CBError(f"This file is no longer on disk:\n{path}")
                self._os_open(path)
            else:
                if sys.platform == "win32" and os.path.exists(path):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    # Never a directory — that case returned above.
                    folder = os.path.dirname(path)
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

    def open_url(self, url):
        """Open *url* in the host's own browser — the About screen's GitHub,
        Submit Issues and mailto links, exactly as `_build_about_tab` does.

        Remote never reaches this (the LOCAL_ONLY "fs." prefix refuses it
        before dispatch, and this repeats the check the way fs_reveal does);
        a remote session is handed the URL to copy instead. The scheme
        allow-list is not ceremony: webbrowser.open falls back to the OS
        handler, where a file: or javascript: URL stops being navigation.
        """
        if self.transport != LOCAL:
            raise CBError("Links open in the app window on the host machine. "
                          "Copy the address instead.")
        # Anything that is not a string is not a link; refusing it as a CBError
        # keeps every rejection on this method the same shape.
        url = url.strip() if isinstance(url, str) else ""
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in OPENABLE_URL_SCHEMES:
            raise CBError("Only web and mail links can be opened from here.")
        try:
            webbrowser.open(url)
        except Exception as exc:
            raise CBError(f"Could not open that link: {exc}")
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

    # ── self-update (local session only — LOCAL_ONLY's "update." prefix) ───────
    # cratebuilder.updater_core owns every moving part (manifest fetch,
    # checksum, extract, the swap-process handoff); what lives here is what the
    # transport layer owns — refusing a run the way the tkinter About tab does,
    # trusting nothing the client says about which build to install, and
    # keeping an update's file-swap exclusive with every other job category
    # that writes into the install or the downloads table.

    def _update_manifest_url(self):
        urls = _manifest_urls()
        key = "UPDATE_MANIFEST_URL_LINUX" if ucore.is_linux() else "UPDATE_MANIFEST_URL"
        return urls.get(key)

    def update_check(self):
        """Fetch the manifest and report what it says — never raises for an
        unreachable host or an invalid manifest; that is itself the result
        the About screen and the auto-check timer both act on.

        `last_update_check` is persisted here on every call, matching the
        monolith's `_check_updates_worker` (manual or automatic, a check is a
        check). An unparseable/missing monolith source (no `_update_manifest_url()`
        to fetch) reports the same "unreachable" shape rather than raising —
        this method's whole contract is that it never throws.
        """
        url = self._update_manifest_url()
        manifest = ucore.fetch_manifest(url) if url else None
        try:
            self._settings.set("last_update_check", time.time())
        except Exception:
            pass
        current = version_info()["build"]
        result = {
            "reachable": manifest is not None,
            "valid": False,
            "available": False,
            "current_build": current,
            "latest_build": None,
            "notes": None,
            "can_self_update": ucore.can_self_update(),
        }
        if manifest is None:
            return result
        ok, _reason = ucore.validate_manifest(manifest)
        result["valid"] = ok
        if not ok:
            return result
        # latest_build is reported whenever the manifest is reachable and
        # valid, current build or not — the About screen's status line wants
        # to say what build is live even when it isn't newer. notes stays
        # available-only: there is nothing to show notes FOR otherwise.
        result["latest_build"] = int(manifest["build"])
        result["available"] = ucore.is_update_available(manifest, current)
        if result["available"]:
            result["notes"] = str(manifest.get("notes", "")).strip() or None
        return result

    def _require_idle_for_update(self):
        """`update_apply`'s pre-flight AND `_start_job`'s guard: refuse to
        start unless batch, watchlist and maintenance are ALL idle and no
        genre-move retag is sweeping. An update swaps every file under the
        app and restarts it — anything still writing when that happens is
        the failure mode the monolith's own `_launch_updater_and_quit` polls
        around; refusing to start is simpler and just as safe."""
        if ("batch" in self._jobs or WATCHLIST_JOB in self._jobs
                or MAINTENANCE_JOB in self._jobs or self._retags):
            raise CBError(UPDATE_NEEDS_IDLE_JOBS)

    def update_apply(self):
        """Download, verify, stage, and hand off to the separate updater
        process. Takes NO trusted parameters from the client — the manifest
        is fetched here, and the build/url/sha256 the worker acts on come
        from that fetch alone.
        """
        url = self._update_manifest_url()
        if not url:
            raise CBError("Couldn't determine the update server address — "
                          "the app's own source could not be read.")
        manifest = ucore.fetch_manifest(url)
        try:
            self._settings.set("last_update_check", time.time())
        except Exception:
            pass
        ok, _reason = ucore.validate_manifest(manifest) if manifest else (False, "")
        if not ok:
            raise CBError("Couldn't reach the update server, or the update "
                          "information looks invalid right now. Try Check "
                          "for updates again in a moment.")
        current = version_info()["build"]
        if not ucore.is_update_available(manifest, current):
            raise CBError(f"You're already on the latest build ({current}).")
        build = int(manifest["build"])

        # Linux: the pkexec/apt install flow is deliberately not ported here,
        # regardless of whether this install could otherwise self-update —
        # Linux users update by installing the newer .deb.
        if ucore.is_linux():
            raise CBError(
                "The in-app updater isn't available for this Linux install. "
                "Download the latest .deb and install it manually: "
                "https://github.com/Sintax/DJ-CrateBuilder/releases/tag/"
                "linux-v2.0")
        if not ucore.can_self_update():
            raise CBError(
                f"Build {build} is available, but you're running from "
                "source.\n\nUpdate with git (pull the latest) instead of "
                "the in-app updater.")
        self._require_idle_for_update()

        dl_url = manifest["url"]
        sha256 = manifest["sha256"]
        notes = str(manifest.get("notes", "")).strip() or None

        def worker():
            ws = ucore.default_workspace()
            ucore.purge_dir(ws)
            os.makedirs(ws, exist_ok=True)
            # This try only covers the part where a failure means "nothing
            # usable happened yet" — download, verify, stage, and the Popen
            # call itself. The moment Popen returns successfully, the staged
            # payload belongs to the separate updater process, which is
            # already waiting on this PID to exit: purging `ws` past that
            # point would delete the very files it's about to copy, even
            # though the update itself succeeded. Nothing after the handoff
            # boundary runs inside this try.
            try:
                zip_path = os.path.join(ws, f"build-{build}.zip")

                def progress(done, total):
                    self.emit("update.progress", {
                        "phase": "download",
                        "pct": int(done * 100 / total) if total else None,
                        "done_mb": done // 1048576,
                        "total_mb": (total // 1048576) if total else None,
                    })

                ucore.download(dl_url, zip_path, progress_cb=progress)

                self.emit("update.progress", {"phase": "verify"})
                if not ucore.verify_sha256(zip_path, sha256):
                    raise CBError(
                        "checksum mismatch — the download may be corrupt")

                self.emit("update.progress", {"phase": "stage"})
                staged = os.path.join(ws, "staged")
                ucore.purge_dir(staged)
                ucore.extract_zip(zip_path, staged)

                # update.progress is coalesced (cratebuilder/events.py); flush
                # its last pending frame so it can never arrive after the
                # events that supersede it — the same reason MaintenanceOps
                # flushes before its own terminal notification.
                self._emit.flush()
                self.emit("update.restarting", {"build": build})
                app_dir = ucore.install_dir()
                cmd = ucore.launch_updater_command(
                    os.getpid(), staged, app_dir, sys.executable,
                    os.path.join(ws, "backup"), os.path.join(ws, "update.log"))
                flags = 0
                if os.name == "nt":
                    flags = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
                subprocess.Popen(cmd, close_fds=True, creationflags=flags,
                                 cwd=app_dir)
            except Exception:
                # Flush first so the last progress frame lands before the
                # error notification/job.finished that supersede it, then
                # purge — never leave a half-downloaded/half-staged workspace
                # behind. Only reachable while nothing has been handed off
                # yet.
                self._emit.flush()
                ucore.purge_dir(ws)
                raise

            # The handoff succeeded: the update itself is done from this
            # process's point of view, so job.finished must say ok=True even
            # if the restart callback below misbehaves — the app is about to
            # exit either way, and there is no user-facing failure to report.
            if self.on_update_restart is not None:
                try:
                    self.on_update_restart()
                except Exception:
                    pass

        job_id = self._start_job(UPDATE_JOB, worker, title="Update",
                                 guard=self._require_idle_for_update)
        return {"job_id": job_id, "build": build, "notes": notes}

    def update_status(self):
        """Everything the About screen's Updates card renders besides the
        result of the last check: the interval dropdown's options and current
        value, when the next silent check will fire, and whether this build
        can self-update at all."""
        return {
            "interval": self._settings.get("update_check_interval"),
            "options": UPDATE_CHECK_OPTIONS,
            "last_check": self._settings.get("last_update_check"),
            "next_check": self._next_update_check_ts,
            "can_self_update": ucore.can_self_update(),
            "running": self._job_running(UPDATE_JOB),
        }

    def update_set_interval(self, value):
        if value not in UPDATE_CHECK_OPTIONS:
            raise CBError(f"Unknown auto-check interval: {value!r}")
        self._settings.set("update_check_interval", value)
        self._arm_update_timer()
        return self.update_status()

    def start_update_timer(self):
        """Arm the LOCAL-only auto-check timer. Explicit rather than a
        constructor side effect — the counterpart of `start_remote_mount()`
        being its own call rather than something `__init__` does. Callers
        that only need one snapshot (nearly every test, most tooling) never
        pay for a background Timer just from building a service; the local
        window calls this once, right after constructing its own.

        No-ops on REMOTE and once `close()` has run — see `_arm_update_timer`.
        """
        self._arm_update_timer()

    def window_placement(self):
        """Where the last session left the window, as (geometry, maximized).

        A plain method rather than an RPC, like `start_update_timer` — this is
        the local window asking about its own frame, not a setting the Settings
        screen renders. Keeping it off the `settings.*` surface is also what
        stops a remote browser reading or writing the host's window placement,
        which is none of its business.
        """
        return (self._settings.get("window_geometry"),
                bool(self._settings.get("window_maximized")))

    def save_window_placement(self, geometry, maximized):
        """Remember where the window is, for the next launch.

        Writes through Settings in one update, the monolith's
        _save_window_placement — never a `settings.set` per key, which would
        rewrite the whole config file twice and put window placement through
        the frozen-setting checks that guard a running download's policy.

        Never raises: losing a window position is not a reason to take down
        the caller, which is a window event handler or the close path.
        """
        try:
            self._settings.update({"window_geometry": geometry,
                                   "window_maximized": bool(maximized)})
            return True
        except Exception:
            return False

    def populate_watchlist_from_folders(self):
        """Fill an empty Watch List from the crate folders already on disk —
        the monolith's first-run step, after(1200, _watchlist_populate_from_
        folders), which the web port never inherited: a reinstall that lost
        cratebuilder.db (the v1.3 uninstaller wiped the install folder) left
        every channel folder behind with no way back into the Watch List.

        The window and the headless server each call this once at launch,
        ahead of start_startup_scan — which arms nothing for an empty list,
        so the rows have to be there first. Synchronous and in-process, like
        the window-placement calls: it reads folders and writes rows, nothing
        more, so no transport rule applies. A list with rows is left alone
        without the writer ever being built, and a machine with no crate and
        no database gets neither. Never raises — a launch step that failed
        would otherwise take the window down for a Watch List that was empty
        anyway. Returns how many rows were added.
        """
        try:
            db = self._db()
            if db is not None and db.get_all_watchlist_channels():
                return 0
            return self._watchlist.populate_from_folders()
        except Exception:
            return 0

    def start_startup_scan(self):
        """Arm the launch scan of every watched channel, so the cards show
        current new-track counts without the user pressing anything.

        Explicit rather than a constructor side effect, for the same reason
        `start_update_timer()` is: a service built for one snapshot or one
        test must not spawn a background thread just from being constructed.
        The local window calls this once, right after building its service.

        LOCAL only, the same rule `_arm_update_timer` follows — a headless
        remote mount must never make the host start scanning channels on its
        own; only the machine whose window just opened owes that scan.

        Returns the thread it started, or None when nothing was armed: the
        setting is off, the Watch List is empty (the monolith's
        get_all_watchlist_channels gate), or this is a remote mount.
        """
        if self._default_transport != LOCAL:
            return None
        if not self._settings.get("watchlist_scan_on_startup"):
            return None
        if not self._watchlist_rows():
            return None
        thread = threading.Thread(target=self._startup_scan_wait, daemon=True,
                                  name="cratebuilder-startup-scan")
        thread.start()
        return thread

    def _startup_scan_wait(self):
        """Settle, wait for the network, then scan every watched channel.

        The monolith arms this as `after(2200, _watchlist_startup_scan)` and
        polls for connectivity on a worker thread, marshalling the scan back
        to the UI thread. There is no UI thread here, so the settle delay and
        the connectivity poll share this one thread and the scan starts from
        it directly.

        The busy re-check the monolith does before scanning is in two halves
        here. `watchlist_scan_all()` is one: it raises CBError when the Watch
        List slot is taken (or the list has since emptied), which is a
        startup scan giving way rather than a failure. The batch check above
        it is the other — the monolith's `self._downloading` — because a
        plain download holds no Watch List slot and nothing would otherwise
        refuse a Scan All landing on top of one. Both matter: minutes can
        pass in the network wait, and the user is not idle in them. Nothing
        else may escape either — this thread must never take itself down
        noisily or leave the window waiting on it.

        `_closed` is read under the lock `close()` sets it under, before the
        settle and again before scanning, so a window closed mid-wait ends
        the thread instead of scanning into a half-torn-down service.
        """
        try:
            if self._is_closed():
                return
            time.sleep(WATCHLIST_STARTUP_DELAY)
            if not self._wait_for_network():
                return
            if self._is_closed():
                return
            if self._job_running("batch"):
                return
            self.log_line("🚀 Startup check: scanning all channels…")
            try:
                self.watchlist_scan_all()
            except CBError:
                pass                # a manual run took the slot; give way
        except Exception:
            pass

    def _wait_for_network(self):
        """Poll for connectivity within the cold-boot budget; True once the
        network answers.

        False means give up — either the budget ran out, which is said once
        in the activity log and never retried (a scheduled or manual scan
        still runs normally), or the service closed while this waited, which
        says nothing at all.
        """
        for attempt in range(WATCHLIST_STARTUP_NET_TRIES):
            if self._is_closed():
                return False
            if ydl.network_is_reachable():
                return True
            if attempt == 0:
                self.log_line("🌐 Waiting for the network before the "
                              "startup scan…")
            time.sleep(WATCHLIST_STARTUP_NET_DELAY)
        self.log_line("Startup scan skipped — no network detected. Channels "
                      "keep their links; scan once you're back online.")
        return False

    # ── the auto-download scheduler ───────────────────────────────────────────
    # The web port of the monolith's automation section: _reschedule_auto_
    # download, _auto_download_tick and _auto_download_after_scan. Its three
    # decisions are already pure logic in cratebuilder/util.py — when the next
    # run is due, what a settle poll should do, and how the next run reads —
    # and none of them had a caller here, so nothing has auto-downloaded on a
    # schedule since v2.0 shipped.
    #
    # Tk's after() chain becomes one daemon thread waiting on an Event: the
    # wait is how the timer is armed, and setting the event is how a changed
    # interval or a manual Download All New makes it recompute without being
    # torn down.

    def start_auto_download_timer(self):
        """Arm the periodic scan-all-then-download-new run.

        Explicit rather than a constructor side effect, and LOCAL only, for
        the reasons `start_update_timer` and `start_startup_scan` are: a
        service built for one snapshot or one test must not spawn a thread,
        and a headless remote mount must never make the host start downloading
        on its own. Returns the thread, or None when nothing was armed.

        The schedule counts from THIS launch, never from the stored
        `watchlist_last_download`, which is the monolith's rule and the reason
        it holds: an app opened after a week away would otherwise be instantly
        overdue and start downloading while the user was still looking at it.
        """
        if self._default_transport != LOCAL:
            return None
        with self._lock:
            if self._closed or self._auto_dl_thread is not None:
                return None
            self._auto_dl_anchor = int(time.time())
            thread = threading.Thread(target=self._auto_download_loop,
                                      daemon=True,
                                      name="cratebuilder-auto-download")
            self._auto_dl_thread = thread
        thread.start()
        return thread

    def _reanchor_auto_download(self):
        """Count the interval from now, and wake the loop to re-arm.

        The monolith's "Download All New stamps the anchor": a download that
        just happened — scheduled or pressed by hand — puts the next scheduled
        one a full interval away rather than minutes later. Safe to call when
        no scheduler is running; it is then only a stored number.
        """
        with self._lock:
            self._auto_dl_anchor = int(time.time())
        self._auto_dl_wake.set()

    def _auto_download_interval_seconds(self):
        """The dropdown's interval in seconds, or None for 'Off'/unreadable."""
        try:
            return util.interval_label_to_seconds(
                self._settings.get("auto_download_interval"))
        except Exception:
            return None

    def _publish_next_auto_download(self, ts):
        """Announce when the next scheduled run is due, once per change.

        The label is built here rather than in the frontend so the wording is
        the monolith's own next_run_label and there is one implementation of
        it. None means the schedule is off.
        """
        with self._lock:
            if self._auto_dl_announced and ts == self._auto_dl_next_ts:
                return
            self._auto_dl_next_ts = ts
            self._auto_dl_announced = True
        self.emit("automation.next_run", self.next_auto_download())

    def next_auto_download(self):
        """The next scheduled run as {ts, text} — the snapshot's copy of what
        `automation.next_run` pushes."""
        ts = self._auto_dl_next_ts
        return {"ts": ts, "text": util.next_run_label(ts)}

    def _auto_download_loop(self):
        """Wait out the interval, run once, repeat. Never raises.

        The wait IS the timer, so waking early is how a re-arm happens: a
        changed interval, a manual Download All New re-anchoring, or close().
        The event is cleared before the interval and anchor are read, so a set
        landing in the gap is answered by reading the value it was announcing
        rather than being missed.
        """
        try:
            while not self._is_closed():
                self._auto_dl_wake.clear()
                due = util.next_run_delay_ms(
                    self._auto_download_interval_seconds(),
                    self._auto_dl_anchor, time.time())
                if due is None:                     # 'Off' — no timer at all
                    self._publish_next_auto_download(None)
                    self._auto_dl_wake.wait()
                    continue
                delay_ms, next_ts = due
                self._publish_next_auto_download(next_ts)
                if self._auto_dl_wake.wait(delay_ms / 1000.0):
                    continue                        # re-armed; decide again
                self._auto_download_run()
        except Exception:
            pass

    def _auto_download_run(self):
        """One scheduled run: scan every channel, wait for it, download what
        it found. Advances the anchor on every path that reaches the end, so a
        run that found nothing still waits a full interval before trying again.
        """
        if not self._wait_until_idle():
            return
        if self._is_closed():
            return
        self.log_line("⏰ Scheduled auto-download starting…")
        try:
            self.watchlist_scan_all()
        except CBError:
            # Nothing to scan, or the slot went in the gap. Either way this
            # cycle is spent; the next is a full interval out.
            self._reanchor_auto_download()
            return
        if not self._wait_for_scan_to_settle():
            return
        rows = self._watchlist_rows()
        pending = [r for r in rows if int(r.get("pending_new_count") or 0) > 0]
        total_new = sum(int(r.get("pending_new_count") or 0) for r in pending)
        if not total_new:
            self.log_line("⏰ Auto-download complete — no new tracks.")
            self._reanchor_auto_download()
            return
        try:
            self.watchlist_download_all_new()
        except CBError:
            self._reanchor_auto_download()
            return
        # Again, though the download just did it on its way in: this cycle is
        # over either way, and a run whose end depends on a collaborator to
        # close its own schedule fires twice the moment that stops being true.
        self._reanchor_auto_download()
        self.emit("notification", {
            "level": "info",
            "title": "Watch List",
            "body": f"{total_new} new track"
                    f"{'' if total_new == 1 else 's'} downloading across "
                    f"{len(pending)} channel"
                    f"{'' if len(pending) == 1 else 's'}",
            "at": datetime.now().isoformat(timespec="seconds"),
            "job": WATCHLIST_JOB,
        })

    def _wait_until_idle(self):
        """Hold until nothing else is running. False means give up on this
        cycle — closed, or re-armed while waiting.

        A scheduled run never interrupts the user's own work: the monolith
        re-arms its tick every BUSY_RETRY_MS while a manual scan or download
        is in flight, and this is the same wait without the timer chain. Both
        job categories count — a batch owns the same yt-dlp session a scan
        would need.
        """
        while True:
            if self._is_closed():
                return False
            if not (self._job_running("batch")
                    or self._job_running(WATCHLIST_JOB)):
                return True
            if self._auto_dl_wake.wait(util.BUSY_RETRY_MS / 1000.0):
                return False

    def _wait_for_scan_to_settle(self):
        """Poll until the scan releases the Watch List slot. False means give
        up on this cycle.

        `scan_settle_verdict` owns the rule, including the cap that stops a
        wedged scan being polled forever — and, when that cap is reached, the
        anchor is still advanced so the next cycle is a full interval away
        rather than an immediate retry of the same stuck scan.
        """
        polls = 0
        while True:
            verdict = util.scan_settle_verdict(
                1 if self._job_running(WATCHLIST_JOB) else 0, polls)
            if verdict == "proceed":
                return True
            if verdict == "give_up":
                self.log_line("⏰ Auto-download gave up waiting for scans "
                              "to finish.")
                self._reanchor_auto_download()
                return False
            polls += 1
            if self._auto_dl_wake.wait(util.SCAN_SETTLE_POLL_MS / 1000.0):
                return False
            if self._is_closed():
                return False

    def _is_closed(self):
        """The shutdown flag, read under the lock `close()` sets it under."""
        with self._lock:
            return self._closed

    def _arm_update_timer(self):
        """(Re)arm the silent auto-check timer from the current interval.

        LOCAL only — a remote browser must never make this host poll GitHub
        on its own behalf, matching update.*'s LOCAL_ONLY gate. Per ADR 0001
        this reads the real wall clock rather than an injected one; a test
        wanting a fast fire sets a short interval, not a fake clock.

        Never called by `__init__` — see `start_update_timer`. Guarded by
        `self._closed` under the same lock so a fire already in flight when
        `close()` runs (it clears `_update_timer` before re-arming, outside
        the lock) can't resurrect a timer past that close.
        """
        if self._default_transport != LOCAL:
            return
        with self._lock:
            if self._closed:
                return
            if self._update_timer is not None:
                self._update_timer.cancel()
            secs = util.interval_label_to_seconds(
                self._settings.get("update_check_interval"))
            if not secs:
                self._update_timer = None
                self._next_update_check_ts = None
                return
            timer = threading.Timer(secs, self._update_timer_fire)
            timer.daemon = True
            self._update_timer = timer
            self._next_update_check_ts = time.time() + secs
            timer.start()

    def _update_timer_fire(self):
        """One scheduled silent check, then re-arm for the next interval.

        Skipped (but still re-armed) while any job is running — installing
        mid-scan is how updates fail, and the next tick offers it again once
        things are quiet. Never downloads on its own: a newer build is only
        announced, exactly like the monolith's automatic check with nobody
        watching the window.
        """
        with self._lock:
            self._update_timer = None
            busy = bool(self._jobs)
        if not busy:
            try:
                result = self.update_check()
            except Exception:
                result = None
            if result and result["reachable"] and result["valid"] and result["available"]:
                self.emit("update.available", {
                    "build": result["latest_build"],
                    "current_build": result["current_build"],
                    "notes": result["notes"],
                    "can_self_update": result["can_self_update"],
                })
                self.emit("notification", {
                    "level": "info",
                    "title": "Update available",
                    "body": (f"Build {result['latest_build']} is available "
                            f"— you're on {result['current_build']}. Open "
                            "About to install."),
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "job": UPDATE_JOB,
                })
        self._arm_update_timer()

    def close(self):
        """Release the background resources this service holds — chiefly the
        auto-check timer, so a service built for one test or one process
        shutdown never leaves a daemon Timer armed past its use.

        `_closed` is set under the same lock `_arm_update_timer` checks, so a
        fire already past its own lock hold when this runs still can't
        re-arm afterwards — see `_arm_update_timer`."""
        with self._lock:
            self._closed = True
            if self._update_timer is not None:
                self._update_timer.cancel()
                self._update_timer = None
        # Its thread is parked on this; setting it is what lets the wait end
        # and the loop see `_closed` rather than sleeping out a whole interval.
        self._auto_dl_wake.set()

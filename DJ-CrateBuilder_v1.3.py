import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import os
import sys
import subprocess
import re
import io
import json
import random
import logging
import time
import webbrowser
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date

from cratebuilder.util import (
    load_config, save_config, today_yyyymmdd,
    days_ago_yyyymmdd, subtract_days_from_yyyymmdd,
    format_yyyymmdd_readable, format_timestamp_relative,
    interval_label_to_seconds,
    normalize_track_key, scan_folder_newest_mp3, safe_filename, push_mru,
    detect_platform, redact_ydl_opts, build_cookie_opts,
    derive_collection_name, find_matching_watchlist_row,
    soundcloud_profile_handle, merge_soundcloud_candidates,
    runtime_data_dir,
)
from cratebuilder.sidecar import (
    channel_url_from_id, channel_id_from_url,
    read_channel_sidecar, write_channel_sidecar, is_unresolved_channel,
    watch_fetch_url, classify_scan_entries,
)
from cratebuilder.db import DownloadsDatabase
from cratebuilder.cleanup import (
    is_scan_trustworthy, classify_local_files, partition_trash)
from cratebuilder import startup as cb_startup
from cratebuilder import updater_core as ucore
from cratebuilder.tagging import write_track_tags, read_source_url
from cratebuilder import artwork as cb_artwork
from cratebuilder.singleton import acquire_single_instance, SINGLE_INSTANCE_PORT

# ══════════════════════════════════════════════════════════════════════════════
# Version & About — edit these values to update the app info
# ══════════════════════════════════════════════════════════════════════════════
APP_NAME    = "DJ-CrateBuilder"
APP_VERSION = "1.3"
# Nightly build number. The display version stays pinned at APP_VERSION; only
# this integer increments for small in-place updates. Bump it for every build
# you publish to the nightly channel. Publish with: python scripts/release.py
APP_BUILD   = 26

ABOUT_CREATED_BY  = "CorruptSintax@Gmail.com"
ABOUT_DESCRIPTION = "Vibe-Coded entirely with Claude-AI"
GITHUB_URL        = "https://github.com/Sintax/DJ-CrateBuilder"
GITHUB_ISSUES_URL = "https://github.com/Sintax/DJ-CrateBuilder/issues/new"
# Raw manifest for the in-app updater. Lives on a dedicated `nightly` branch so
# `main` and the tagged v1.3 release are never touched by a nightly push.
UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Sintax/DJ-CrateBuilder/"
    "nightly/update.json"
)
# Linux .deb update manifest, published as an asset on the linux-v1.3 release
# by .github/workflows/build-deb.yml. Same schema as update.json.
UPDATE_MANIFEST_URL_LINUX = (
    "https://github.com/Sintax/DJ-CrateBuilder/releases/download/"
    "linux-v1.3/update-linux.json"
)
# About-tab updater button labels. The button doubles as the install trigger:
# it reads "Check for updates" normally and flips to "Update Now" once a newer
# build has been detected.
UPDATE_BTN_CHECK  = "  ⟳  Check for updates  "
UPDATE_BTN_UPDATE = "  ⟳  Update Now  "

# Full version string shown to the user, e.g. "1.3.1".
APP_VERSION_FULL = f"{APP_VERSION}.{APP_BUILD}"

# ── Add or remove lines below to customize the About tab content. ──────────
# ── Each tuple is  ("Label", "Value")  and will display as a row. ──────────
ABOUT_FIELDS = [
    ("Application",  f"{APP_NAME}  v{APP_VERSION_FULL}"),
    ("Created by",   ABOUT_CREATED_BY),
    ("Built with",   ABOUT_DESCRIPTION),
]
# ══════════════════════════════════════════════════════════════════════════════

# ── Cover art ─────────────────────────────────────────────────────────────────
# The Settings combobox shows a friendly label; the config file stores the bare
# mode string that cratebuilder.artwork understands. These two dicts are the
# only place the two vocabularies meet.
_COVER_ART_LABELS = {
    "crop":     "Crop to square (recommended)",
    "original": "Keep original aspect (16:9)",
    "off":      "Off — no cover art",
}
_COVER_ART_MODES_BY_LABEL = {v: k for k, v in _COVER_ART_LABELS.items()}

# ── Dependency check ──────────────────────────────────────────────────────────
def check_dependencies():
    missing = []
    try:
        import yt_dlp
    except ImportError:
        missing.append("yt-dlp")
    return missing

# ── Bundled FFmpeg location ─────────────────────────────────────────────────
def bundled_ffmpeg_dir():
    """Return the directory of the bundled ffmpeg.exe, or None to fall back to PATH.

    In the packaged (PyInstaller) build, ffmpeg.exe/ffprobe.exe are shipped next
    to the application executable. Pointing yt-dlp straight at that folder means
    the installer no longer has to add anything to PATH, so it works regardless
    of how/where the app is installed. When running from source, return None and
    let yt-dlp discover FFmpeg on PATH as documented.
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
    """Return the absolute path to the bundled app icon (icon.ico), or None.

    Mirrors bundled_ffmpeg_dir(): in the packaged (PyInstaller) build the icon
    is shipped beside the executable (and/or in the _MEIPASS temp dir); from
    source it sits next to this script. Used to give the tray icon and the Tk
    window the real app icon instead of a runtime-drawn placeholder.
    """
    if getattr(sys, "frozen", False):
        cands = (os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", None))
    else:
        cands = (os.path.dirname(os.path.abspath(__file__)),)
    for cand in cands:
        if cand:
            p = os.path.join(cand, "icon.ico")
            if os.path.isfile(p):
                return p
    # Linux .deb ships the icon as a hicolor PNG rather than beside the script.
    png = "/usr/share/icons/hicolor/256x256/apps/dj-cratebuilder.png"
    if os.path.isfile(png):
        return png
    return None


def _wheel_delta(event):
    """Normalise a mouse-wheel event to a Windows-style delta (±120 per notch).

    Windows/macOS deliver <MouseWheel> carrying event.delta. Linux/X11 has no
    <MouseWheel>: the wheel arrives as <Button-4> (up) / <Button-5> (down) with
    event.delta == 0. Every wheel handler routes through this so a single delta
    formula works on all platforms; pair it with _bind_wheel() for the events.
    """
    num = getattr(event, "num", None)
    if num == 4:
        return 120
    if num == 5:
        return -120
    return event.delta


def _bind_wheel(widget, handler, add=None):
    """Bind *handler* to every mouse-wheel event across platforms.

    Binds <MouseWheel> (Windows/macOS) plus <Button-4>/<Button-5> (Linux/X11)
    so wheel scrolling works everywhere. Handlers must read the notch via
    _wheel_delta(event), never event.delta directly.
    """
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(seq, handler, add=add)

# ── Color palette ─────────────────────────────────────────────────────────────
BG        = "#0f0f0f"
SURFACE   = "#1a1a1a"
SURFACE2  = "#242424"
BORDER    = "#2e2e2e"
TEXT      = "#f0f0f0"
TEXT_DIM  = "#888888"
TEXT_MED  = "#bbbbbb"
SUCCESS   = "#22c55e"
MAROON    = "#800000"   # Overall-progress bar fill
SKIP_COL  = "#6b7280"
LINK_COL  = "#60a5fa"   # light blue for clickable links

# Database-viewer grid: hairline dividers + zebra striping over the near-black
# tree field (#0a0a0a), so each row/column reads as its own box.
DB_FIELD  = "#0a0a0a"   # tree field background
DB_GRID   = "#3a3a3a"   # light-grey hairline dividers / outlines
DB_STRIPE = "#161616"   # alternate-row tint (subtle lift over DB_FIELD)

# Platform accent colours
YT_RED    = "#ff3b3b"
YT_DARK   = "#cc2222"
SC_ORANGE = "#ff5500"
SC_DARK   = "#cc4400"

# Watch List accent
WL_BLUE      = "#60a5fa"   # light blue accent (icons, status, hover)
WL_BLUE_DARK = "#2563eb"   # darker blue fill for buttons (carries white text)
# Dark maroon for the global Cancel button while it's idle (nothing to cancel),
# so the control reads as "armed but inactive" rather than a plain grey button.
WL_CANCEL_IDLE = "#5e1414"

# ── Platform config ───────────────────────────────────────────────────────────
PLATFORMS = {
    "YouTube": {
        "accent":      YT_RED,
        "accent_dark": YT_DARK,
        "icon":        "▶",
        "label":       "YouTube → MP3",
        "sub":         "Single video  •  channel URL  •  playlist",
        "placeholder": "https://www.youtube.com/@ChannelName   or   a single video URL",
        "url_pattern": r"(youtube\.com|youtu\.be)",
        "bad_url_msg": "That doesn't look like a YouTube URL.",
        "fetch_label": "Fetching channel / playlist info…",
        "item_word":   "video",
        "subdir":      "YouTube",
        "url_builder": lambda entry: (
            entry.get("url") or
            entry.get("webpage_url") or
            f"https://www.youtube.com/watch?v={entry.get('id','')}"
        ),
    },
    "SoundCloud": {
        "accent":      SC_ORANGE,
        "accent_dark": SC_DARK,
        "icon":        "◈",
        "label":       "SoundCloud → MP3",
        "sub":         "Single track  •  artist profile  •  set / playlist",
        "placeholder": "https://soundcloud.com/artist-name   or   a single track URL",
        "url_pattern": r"soundcloud\.com",
        "bad_url_msg": "That doesn't look like a SoundCloud URL.",
        "fetch_label": "Fetching profile / set info…",
        "item_word":   "track",
        "subdir":      "SoundCloud",
        "url_builder": lambda entry: (
            entry.get("url") or
            entry.get("webpage_url") or
            entry.get("id", "")
        ),
    },
}

# ── Queue states ──────────────────────────────────────────────────────────────
ST_PENDING = "pending"
ST_ACTIVE  = "active"
ST_DONE    = "done"
ST_SKIPPED = "skipped"
ST_ERROR   = "error"

STATE_ICON = {
    ST_PENDING: ("○", TEXT_DIM),
    ST_ACTIVE:  ("◉", None),       # accent colour filled in dynamically
    ST_DONE:    ("✓", SUCCESS),
    ST_SKIPPED: ("⊘", SKIP_COL),
    ST_ERROR:   ("✗", YT_RED),
}

# ── User-Agent pool (one is chosen per batch session) ────────────────────────
USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# ── Throttle presets (Auto mode ranges) ──────────────────────────────────────
THROTTLE_PRESETS = {
    "Light  (1–5 s)":      (1, 5),
    "Moderate  (3–8 s)":   (3, 8),
    "Aggressive  (5–15 s)": (5, 15),
}

# ── Watch List: how many days to subtract from the detected cutoff to cushion
# ── against approximate_date imprecision. If the latest download was on the
# ── 10th, we scan for anything uploaded after the 5th. Any overlap is caught
# ── by skip-existing, so over-scanning is safe but under-scanning is not.
WATCHLIST_CUTOFF_BUFFER_DAYS = 5

# ── Maximum number of channel scans allowed to run concurrently during
# ── "Scan All". Caps how hard we hit YouTube at once so large watch lists
# ── don't pile up dozens of simultaneous yt-dlp requests and time out.
WATCHLIST_MAX_CONCURRENT_SCANS = 3

# ── Cold-boot guard for the startup scan. When the app auto-launches at Windows
# ── login the network is often a few seconds behind; scanning while offline
# ── fails every channel and (per is_unresolved_channel) makes resolved cards
# ── look like they "need a channel ID". So the startup scan waits for
# ── connectivity first: probe every _DELAY seconds, up to _TRIES times, then
# ── give up quietly (scheduled / manual scans still run normally).
WATCHLIST_STARTUP_NET_TRIES = 18      # ≈ 90 s window at the delay below
WATCHLIST_STARTUP_NET_DELAY = 5.0     # seconds between connectivity probes

# ── "Since" date preset options used in the Add Channel dialog ───────────────
SINCE_DATE_OPTIONS = [
    "Today  (only future uploads)",
    "Last 30 days",
    "Last 90 days",
    "Last 6 months",
    "Last 1 year",
    "Custom date…",
    "Scan my music folder",
]

# ── Config persistence ────────────────────────────────────────────────────────
# _config_path, load_config, save_config moved to cratebuilder.util (imported above)

DEFAULT_BASE = os.path.join(os.path.expanduser("~"), "Music", "DJ-CrateBuilder")


# ══════════════════════════════════════════════════════════════════════════════
# Date utilities (YYYYMMDD string format used throughout yt-dlp + this app)
# ══════════════════════════════════════════════════════════════════════════════
# date/interval helpers moved to cratebuilder.util (imported above)


# Auto-download interval choices for the Settings combobox. Each non-"Off" label
# is '<integer> <unit>' (hours/days/week), parsed by interval_label_to_seconds.
AUTO_DOWNLOAD_OPTIONS = ["Off", "6 hours", "12 hours", "1 day", "2 days",
                         "3 days", "1 week"]

# How often the app silently re-checks GitHub for a newer nightly build. Shown
# in the About-tab dropdown; each label parses via interval_label_to_seconds.
UPDATE_CHECK_OPTIONS = ["1 hour", "3 hours", "6 hours", "12 hours", "1 day"]

# Sentinel URL prefix stored for Watch List channels whose canonical YouTube
# /channel/UC… URL isn't known yet (e.g. imported from a folder name). Such
# channels must be resolved via "Fix Link" before they can be scanned.
UNRESOLVED_URL_PREFIX = "unresolved://"


# interval_label_to_seconds moved to cratebuilder.util (imported above)


# ═════════════════════════════════════════════════════════════════════════════
# Head-trimming file handler — keeps a log file under a byte cap by dropping the
# OLDEST lines from the top, so the file always retains the most recent activity.
# A plain RotatingFileHandler would spill into .1/.2 backups instead of trimming
# the live file in place, which isn't what we want here.
# ═════════════════════════════════════════════════════════════════════════════
class _HeadTrimFileHandler(logging.FileHandler):
    """FileHandler that caps the log at *max_bytes* by removing the oldest lines
    from the top once the file grows past the cap. ``max_bytes <= 0`` means
    unlimited (never trims). After a trim the file is left at ~90% of the cap so
    we don't rewrite on every subsequent line."""

    def __init__(self, filename, max_bytes=0, encoding=None):
        self.max_bytes = max_bytes
        super().__init__(filename, encoding=encoding)
        self.maybe_trim()   # trim a pre-existing oversized file on open

    def emit(self, record):
        super().emit(record)
        try:
            if self.max_bytes > 0 and self.stream is not None \
                    and self.stream.tell() >= self.max_bytes:
                self._trim()
        except Exception:
            pass   # logging must never raise into the app

    def maybe_trim(self):
        """Trim now if the file already exceeds the cap (e.g. on open or after
        the cap is lowered at runtime). Safe no-op when unlimited or small."""
        try:
            if self.max_bytes > 0 and os.path.exists(self.baseFilename) \
                    and os.path.getsize(self.baseFilename) > self.max_bytes:
                self._trim()
        except Exception:
            pass

    def _trim(self):
        # logging handlers use a reentrant lock, so acquiring here is safe even
        # when called from inside emit() (which already holds it).
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            target = max(1, int(self.max_bytes * 0.9))
            with open(self.baseFilename, "rb") as f:
                data = f.read()
            if len(data) > target:
                data = data[-target:]
                # Drop the partial first line so the file starts on a boundary.
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1:]
                with open(self.baseFilename, "wb") as f:
                    f.write(data)
            self.stream = self._open()
        finally:
            self.release()


# ═════════════════════════════════════════════════════════════════════════════
# Tooltip — dark-themed hover tooltip for any tkinter widget.
# Usage:     Tooltip(widget, "Short explanation of what this does.")
# ═════════════════════════════════════════════════════════════════════════════
class Tooltip:
    def __init__(self, widget, text, delay=500, wraplength=280):
        self.widget     = widget
        self.text       = text
        self.delay      = delay
        self.wraplength = wraplength
        self._tip       = None
        self._after_id  = None
        widget.bind("<Enter>",       self._on_enter, add="+")
        widget.bind("<Leave>",       self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _e=None):
        self._schedule()

    def _on_leave(self, _e=None):
        self._unschedule()
        self._hide()

    def _schedule(self):
        self._unschedule()
        if self.text:
            self._after_id = self.widget.after(self.delay, self._show)

    def _unschedule(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _widget_monitor_bounds(self):
        """Return (left, top, right, bottom) of the monitor's work area for
        the monitor the widget currently lives on. On Windows this uses
        MonitorFromWindow + GetMonitorInfoW so a tooltip on monitor 2 stays
        on monitor 2 (winfo_screenwidth() only knows the primary monitor).
        Non-Windows platforms fall back to Tk's virtual root, which spans
        every monitor — won't follow the widget perfectly, but at least
        won't yank the tooltip back to monitor 1. Returns None if anything
        goes wrong; the caller treats that as 'skip clamping'."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                class _MI(ctypes.Structure):
                    _fields_ = [("cbSize",    wintypes.DWORD),
                                ("rcMonitor", wintypes.RECT),
                                ("rcWork",    wintypes.RECT),
                                ("dwFlags",   wintypes.DWORD)]

                MONITOR_DEFAULTTONEAREST = 2
                user32 = ctypes.windll.user32
                # Toplevel HWND is the reliable handle on Windows; child
                # widgets in some Tk builds don't get their own HWND.
                hwnd = self.widget.winfo_toplevel().winfo_id()
                hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                if not hmon:
                    return None
                mi = _MI(); mi.cbSize = ctypes.sizeof(_MI)
                if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    return None
                r = mi.rcWork  # excludes taskbar
                return (r.left, r.top, r.right, r.bottom)
            # Non-Windows: virtual root covers every monitor combined.
            vrx = self.widget.winfo_vrootx() or 0
            vry = self.widget.winfo_vrooty() or 0
            return (vrx, vry,
                    vrx + self.widget.winfo_vrootwidth(),
                    vry + self.widget.winfo_vrootheight())
        except Exception:
            return None

    def _show(self):
        if self._tip or not self.text:
            return
        try:
            self._tip = tk.Toplevel(self.widget)
            self._tip.wm_overrideredirect(True)
            self._tip.configure(bg="#000000")
            tk.Label(
                self._tip, text=self.text,
                font=("Segoe UI", 9),
                bg="#1a1a1a", fg="#e5e5e5",
                padx=9, pady=6,
                relief="solid", bd=1,
                wraplength=self.wraplength,
                justify="left",
            ).pack()
            # Measure natural size, then clamp into the monitor the widget is
            # actually on — handles both maximised single-monitor (tooltip
            # near right edge gets pushed leftward) and multi-monitor
            # (tooltip on monitor 2 stays on monitor 2).
            self._tip.update_idletasks()
            tw = self._tip.winfo_reqwidth()
            th = self._tip.winfo_reqheight()
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            bounds = self._widget_monitor_bounds()
            if bounds:
                left, top, right, bottom = bounds
                if x + tw > right - 4:
                    x = right - tw - 4
                if x < left + 4:
                    x = left + 4
                if y + th > bottom - 4:
                    y = self.widget.winfo_rooty() - th - 4
                if y < top + 4:
                    y = top + 4
            self._tip.wm_geometry(f"+{x}+{y}")
        except Exception:
            self._tip = None

    def _hide(self):
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# DownloadsDatabase moved to cratebuilder.db (imported above)


# scan_folder_newest_mp3 moved to cratebuilder.util (imported above)


# ─────────────────────────────────────────────────────────────────────────────
# Channel sidecar metadata (cratebuilder.json)
#
# Each downloaded channel folder gets a small JSON file recording the channel's
# *canonical* identity — its YouTube channel_id (UC…) and the spaceless URL we
# can reliably scan. This makes every folder self-describing, so the Watch List
# never has to guess a channel's handle from its (human-readable) folder name.
# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL_SIDECAR_NAME, channel_url_from_id, read_channel_sidecar,
# write_channel_sidecar moved to cratebuilder.sidecar (imported above)

# normalize_track_key moved to cratebuilder.util (imported above)


# ─────────────────────────────────────────────────────────────────────────────
# Log colours (used by the viewer)
LOG_COL = {
    "DOWNLOADED": {"fg": "#22c55e",  "tag": "dl"},     # green
    "SKIPPED":    {"fg": "#6b7280",  "tag": "sk"},     # grey
    "ERROR":      {"fg": "#ff3b3b",  "tag": "er"},     # red
    "TIMESTAMP":  {"fg": "#555e6e",  "tag": "ts"},     # muted blue-grey
    "DEFAULT":    {"fg": "#bbbbbb",  "tag": "df"},     # medium text
}

FILTER_OPTIONS = ["All", "Downloaded", "Skipped", "Errors"]

# ─────────────────────────────────────────────────────────────────────────────
class _BaseLogViewerWindow(tk.Toplevel):
    """Shared machinery for the two dark-themed log-viewer windows.

    Holds the search/navigation/clipboard logic and the common window
    lifecycle that ``LogViewerWindow`` and ``DebugLogViewerWindow`` use
    verbatim. Subclasses supply their own ``_build_ui`` / ``load_log`` /
    filtering, and set ``WINDOW_TITLE`` / ``WINDOW_W`` / ``WINDOW_H``.
    """

    _SEARCH_HL  = "#f59e0b"   # amber highlight for search matches
    _SEARCH_FG  = "#000000"

    # Overridden per subclass.
    WINDOW_TITLE = ""
    WINDOW_W     = 1000
    WINDOW_H     = 640

    def __init__(self, parent, log_path):
        super().__init__(parent)
        self._log_path   = log_path
        self._parent     = parent
        self._search_var = tk.StringVar()
        self._match_idx  = []   # list of (col_start, col_end) for search hits
        self._match_pos  = -1   # currently-focused match index
        self._init_state()

        self.title(self.WINDOW_TITLE)
        self.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")
        self.minsize(700, 400)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Centre over parent
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.WINDOW_W) // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.WINDOW_H) // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.load_log()
        # Open scrolled to the bottom so the most-recent entries are visible.
        self.after_idle(lambda: self._txt.yview_moveto(1.0))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.focus_force()

    def _init_state(self):
        """Hook for subclass-specific state created before ``_build_ui``."""
        pass

    def _tb_btn(self, parent, label, cmd, side="left", padx=(2,2)):
        """Helper: small flat toolbar button matching the app palette."""
        b = tk.Button(parent, text=label,
                      font=("Segoe UI", 9), relief="flat", bd=0,
                      bg=SURFACE2, fg=TEXT_DIM, activebackground=BORDER,
                      activeforeground=TEXT, padx=8, pady=4,
                      cursor="hand2", command=cmd)
        b.pack(side=side, padx=padx, pady=6)
        return b

    # ── Search ────────────────────────────────────────────────────────────────
    def _run_search(self):
        self._txt.tag_remove("search_hl",  "1.0", "end")
        self._txt.tag_remove("search_cur", "1.0", "end")
        self._match_idx = []
        self._match_pos = -1

        query = self._search_var.get()
        if not query:
            self._match_lbl.config(text="")
            return

        start = "1.0"
        while True:
            pos = self._txt.search(query, start, stopindex="end",
                                   nocase=True, regexp=False)
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            self._txt.tag_add("search_hl", pos, end)
            self._match_idx.append((pos, end))
            start = end

        total = len(self._match_idx)
        if total:
            self._match_pos = 0
            self._highlight_current()
            self._match_lbl.config(text=f"1 / {total}")
        else:
            self._match_lbl.config(text="no match")

    def _highlight_current(self):
        self._txt.tag_remove("search_cur", "1.0", "end")
        if not self._match_idx:
            return
        pos, end = self._match_idx[self._match_pos]
        self._txt.tag_add("search_cur", pos, end)
        self._txt.see(pos)
        n = len(self._match_idx)
        self._match_lbl.config(text=f"{self._match_pos+1} / {n}")

    def _find_next(self):
        if not self._match_idx:
            self._run_search(); return
        self._match_pos = (self._match_pos + 1) % len(self._match_idx)
        self._highlight_current()

    def _find_prev(self):
        if not self._match_idx:
            self._run_search(); return
        self._match_pos = (self._match_pos - 1) % len(self._match_idx)
        self._highlight_current()

    def _clear_search(self, silent=False):
        self._txt.tag_remove("search_hl",  "1.0", "end")
        self._txt.tag_remove("search_cur", "1.0", "end")
        self._match_idx = []
        self._match_pos = -1
        self._match_lbl.config(text="")
        if not silent:
            self._search_var.set("")

    # ── Actions ───────────────────────────────────────────────────────────────
    def _jump_top(self):
        self._txt.yview_moveto(0.0)

    def _jump_end(self):
        self._txt.yview_moveto(1.0)

    def _toggle_wrap(self):
        """Toggle word-wrap on or off in the log text area."""
        self._wrap_on = not self._wrap_on
        if self._wrap_on:
            self._txt.config(wrap="word")
            self._wrap_btn.config(text="Wrap: On", bg="#14532d", fg=SUCCESS)
        else:
            self._txt.config(wrap="none")
            self._wrap_btn.config(text="Wrap: Off", bg=SURFACE2, fg=TEXT_DIM)

    def _copy_all(self):
        content = self._txt.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(content)
        self._stats_bar.config(text="  ✓  Copied to clipboard.")
        self._after_copy()

    def _after_copy(self):
        """Restore the stats bar a moment after a Copy All.

        Subclasses differ in how they recompute the bar, so this hook is
        overridden rather than shared.
        """
        self.after(2000, self.refresh)

    def _open_external(self):
        try:
            if sys.platform == "win32":
                os.startfile(self._log_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self._log_path])
            else:
                subprocess.Popen(["xdg-open", self._log_path])
        except Exception as exc:
            messagebox.showerror("Could Not Open",
                                 f"Failed to open in system viewer:\n{exc}",
                                 parent=self)


# ─────────────────────────────────────────────────────────────────────────────
class LogViewerWindow(_BaseLogViewerWindow):
    """Standalone dark-themed log viewer window."""

    WINDOW_TITLE = "📋  Activity Log  —  DJ CrateBuilder"
    WINDOW_W     = 1000
    WINDOW_H     = 640

    def _init_state(self):
        self._filter_var = tk.StringVar(value="All")

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Top toolbar ───────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=SURFACE2,
                           highlightthickness=1, highlightbackground=BORDER)
        toolbar.pack(fill="x", side="top")

        # Left cluster: filter buttons
        tk.Label(toolbar, text="Show:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(12, 6), pady=8)

        self._filter_btns = {}
        _filter_tips = {
            "All":        "Show every log entry.",
            "Downloaded": "Show only successfully downloaded tracks.",
            "Skipped":    "Show only tracks that were skipped.",
            "Errors":     "Show only entries that failed with an error.",
        }
        for opt in FILTER_OPTIONS:
            b = tk.Button(
                toolbar, text=opt,
                font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                command=lambda o=opt: self._set_filter(o))
            b.pack(side="left", padx=2, pady=6)
            self._filter_btns[opt] = b
            Tooltip(b, _filter_tips.get(opt, opt))
        self._paint_filter_btns()

        # Separator
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="left", fill="y",
                                                    padx=10, pady=6)

        # Word-wrap toggle
        self._wrap_on = True   # matches the initial wrap="word"
        self._wrap_btn = tk.Button(
            toolbar, text="Wrap: On",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
            bg="#14532d", fg=SUCCESS,
            activebackground=BORDER, activeforeground=TEXT,
            command=self._toggle_wrap)
        self._wrap_btn.pack(side="left", padx=2, pady=6)
        Tooltip(self._wrap_btn, "Toggle line wrapping for long entries.")

        # Separator
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="left", fill="y",
                                                    padx=10, pady=6)

        # Search box
        tk.Label(toolbar, text="Search:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))

        search_frame = tk.Frame(toolbar, bg=SURFACE2)
        search_frame.pack(side="left", pady=6)

        self._search_entry = tk.Entry(
            search_frame, textvariable=self._search_var,
            font=("Segoe UI", 9), bg=SURFACE, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=YT_RED, width=26)
        self._search_entry.pack(side="left", ipady=4, padx=(0, 4))
        self._search_entry.bind("<Return>",   lambda e: self._find_next())
        self._search_entry.bind("<KP_Enter>", lambda e: self._find_next())
        self._search_var.trace_add("write",   lambda *_: self._run_search())

        self._prev_btn = self._tb_btn(search_frame, "▲", self._find_prev)
        self._next_btn = self._tb_btn(search_frame, "▼", self._find_next)
        self._clear_btn= self._tb_btn(search_frame, "✕", self._clear_search)
        Tooltip(self._prev_btn,  "Jump to the previous search match.")
        Tooltip(self._next_btn,  "Jump to the next search match.")
        Tooltip(self._clear_btn, "Clear the search and its highlights.")

        self._match_lbl = tk.Label(search_frame, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=SURFACE2, width=10)
        self._match_lbl.pack(side="left", padx=(4, 0))

        # Right cluster: action buttons
        _sysview_btn = self._tb_btn(toolbar, "↗  System Viewer",
                                    self._open_external,
                                    side="right", padx=(0,10))
        Tooltip(_sysview_btn, "Open this log in your default text editor.")
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="right", fill="y",
                                                    padx=2, pady=6)
        _copy_btn = self._tb_btn(toolbar, "⎘  Copy All", self._copy_all,
                                 side="right")
        _refresh_btn = self._tb_btn(toolbar, "⟳  Refresh", self.refresh,
                                    side="right")
        _end_btn = self._tb_btn(toolbar, "⤓  Jump to End", self._jump_end,
                                side="right")
        _top_btn = self._tb_btn(toolbar, "⤒  Jump to Top", self._jump_top,
                                side="right")
        Tooltip(_copy_btn,    "Copy the visible log text to the clipboard.")
        Tooltip(_refresh_btn, "Reload the log file from disk.")
        Tooltip(_end_btn,     "Scroll to the newest entries at the bottom.")
        Tooltip(_top_btn,     "Scroll to the oldest entries at the top.")

        # ── Stats bar ─────────────────────────────────────────────────────────
        self._stats_bar = tk.Label(
            self, text="", font=("Segoe UI", 8),
            fg=TEXT_DIM, bg=SURFACE2, anchor="w", padx=12, pady=3,
            highlightthickness=1, highlightbackground=BORDER)
        self._stats_bar.pack(fill="x", side="bottom")

        # ── Log path bar ──────────────────────────────────────────────────────
        short = self._log_path.replace(os.path.expanduser("~"), "~")
        tk.Label(self, text=f"  {short}", font=("Consolas", 8),
                 fg=TEXT_DIM, bg=SURFACE2, anchor="w", pady=3,
                 highlightthickness=1, highlightbackground=BORDER
                 ).pack(fill="x", side="bottom")

        # ── Text area ─────────────────────────────────────────────────────────
        txt_frame = tk.Frame(self, bg=BG)
        txt_frame.pack(fill="both", expand=True)

        self._txt = tk.Text(
            txt_frame,
            font=("Consolas", 9), bg="#0a0a0a", fg=TEXT_MED,
            insertbackground=TEXT, relief="flat",
            wrap="word", state="disabled",
            selectbackground=BORDER, selectforeground=TEXT,
            padx=12, pady=8)

        v_scroll = ttk.Scrollbar(txt_frame, orient="vertical",
                                  command=self._txt.yview)
        h_scroll = ttk.Scrollbar(txt_frame, orient="horizontal",
                                  command=self._txt.xview)
        self._txt.configure(yscrollcommand=v_scroll.set,
                            xscrollcommand=h_scroll.set)

        h_scroll.pack(side="bottom", fill="x")
        v_scroll.pack(side="right",  fill="y")
        self._txt.pack(side="left",  fill="both", expand=True)

        # Configure colour tags
        self._txt.tag_configure("dl", foreground=LOG_COL["DOWNLOADED"]["fg"])
        self._txt.tag_configure("sk", foreground=LOG_COL["SKIPPED"]["fg"])
        self._txt.tag_configure("er", foreground=LOG_COL["ERROR"]["fg"])
        self._txt.tag_configure("ts", foreground=LOG_COL["TIMESTAMP"]["fg"])
        self._txt.tag_configure("df", foreground=LOG_COL["DEFAULT"]["fg"])
        self._txt.tag_configure("sep_y", foreground="#f59e0b")
        self._txt.tag_configure("sep_r", foreground=YT_RED)
        self._txt.tag_configure("search_hl",
            background=self._SEARCH_HL, foreground=self._SEARCH_FG)
        self._txt.tag_configure("search_cur",
            background="#fb923c", foreground=self._SEARCH_FG)   # orange for current hit

        # Bind mousewheel on the text widget
        _bind_wheel(self._txt, lambda e: self._txt.yview_scroll(
            int(-1*(_wheel_delta(e)/120)), "units"))

    # ── Log loading & rendering ───────────────────────────────────────────────
    def load_log(self):
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                self._all_lines = f.readlines()
        except Exception as exc:
            self._all_lines = [f"[Error reading log: {exc}]\n"]
        self._render()

    def refresh(self):
        """Re-read the log file and re-render."""
        self.load_log()
        self._run_search()

    def _render(self):
        """Apply filter and paint coloured lines into the Text widget."""
        filt   = self._filter_var.get()
        lines  = self._all_lines

        if filt == "Downloaded":
            lines = [l for l in lines if "DOWNLOADED" in l]
        elif filt == "Skipped":
            lines = [l for l in lines if "SKIPPED"    in l]
        elif filt == "Errors":
            lines = [l for l in lines if "ERROR"      in l]

        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")

        dl = sk = er = 0
        for line in lines:
            if "DOWNLOADED" in line:
                tag = "dl"; dl += 1
            elif "SKIPPED"  in line:
                tag = "sk"; sk += 1
            elif "ERROR"    in line:
                tag = "er"; er += 1
            elif "════" in line:
                tag = "sep_r" if "CANCELLED" in line else "sep_y"
            else:
                tag = "df"

            # Dim the timestamp portion (everything up to and including the first |)
            # Separator lines are rendered whole without splitting
            if tag in ("sep_y", "sep_r"):
                self._txt.insert("end", line, tag)
            else:
                pipe = line.find("|")
                if pipe != -1:
                    self._txt.insert("end", line[:pipe+1], "ts")
                    self._txt.insert("end", line[pipe+1:], tag)
                else:
                    self._txt.insert("end", line, tag)

        self._txt.config(state="disabled")
        self._update_stats(dl, sk, er, len(lines))
        self._clear_search(silent=True)

    def _update_stats(self, dl, sk, er, shown):
        total = len(self._all_lines)
        parts = [
            f"  {shown} lines shown  (total: {total})",
            f"  ✓ {dl} downloaded",
            f"  ⊘ {sk} skipped",
            f"  ✗ {er} errors",
        ]
        self._stats_bar.config(text="     |     ".join(parts))

    # ── Filter ────────────────────────────────────────────────────────────────
    def _set_filter(self, opt):
        self._filter_var.set(opt)
        self._paint_filter_btns()
        self._render()
        self._run_search()
        # Selecting a new view should land on the most-recent entries, not jump
        # back to the top. If a search is active, _run_search already positioned
        # on the current match, so only auto-scroll when nothing is matched.
        if not self._match_idx:
            self.after_idle(lambda: self._txt.yview_moveto(1.0))

    def _paint_filter_btns(self):
        active = self._filter_var.get()
        colours = {
            "All":        (SURFACE,  TEXT),
            "Downloaded": ("#14532d", LOG_COL["DOWNLOADED"]["fg"]),
            "Skipped":    ("#1f2937", LOG_COL["SKIPPED"]["fg"]),
            "Errors":     ("#3b0000", LOG_COL["ERROR"]["fg"]),
        }
        for opt, btn in self._filter_btns.items():
            bg, fg = colours.get(opt, (SURFACE2, TEXT_DIM))
            if opt == active:
                btn.config(bg=bg, fg=fg,
                           relief="solid", bd=1,
                           highlightbackground=fg)
            else:
                btn.config(bg=SURFACE2, fg=TEXT_DIM,
                           relief="flat", bd=0)

    # ── Actions ───────────────────────────────────────────────────────────────
    def _after_copy(self):
        # Restore the coloured activity-log stats line after a Copy All.
        self.after(2000, lambda: self._update_stats(*self._count_stats()))

    def _count_stats(self):
        content = self._txt.get("1.0", "end")
        lines   = content.splitlines()
        dl = sum(1 for l in lines if "DOWNLOADED" in l)
        sk = sum(1 for l in lines if "SKIPPED"    in l)
        er = sum(1 for l in lines if "ERROR"      in l)
        return dl, sk, er, len(lines)


class DebugLogViewerWindow(_BaseLogViewerWindow):
    """Standalone dark-themed debug log viewer window."""

    WINDOW_TITLE = "🔍  Debug Log  —  DJ CrateBuilder"
    WINDOW_W     = 1100
    WINDOW_H     = 680

    # Colour tags for log levels
    _LEVEL_COLORS = {
        "INFO":    TEXT_MED,
        "DEBUG":   "#6b7280",
        "WARNING": "#f59e0b",
        "ERROR":   "#ef4444",
    }

    def _build_ui(self):
        # ── Toolbar ───────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=SURFACE2,
                           highlightthickness=1, highlightbackground=BORDER)
        toolbar.pack(fill="x", side="top")

        # Filter buttons
        tk.Label(toolbar, text="Show:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(12, 6), pady=8)

        self._filter_var = tk.StringVar(value="All")
        self._filter_btns = {}
        _filter_tips = {
            "All":   "Show every debug log line.",
            "INFO":  "Show only INFO-level lines.",
            "ERROR": "Show only ERROR-level lines.",
            "DEBUG": "Show only DEBUG-level lines.",
        }
        for opt in ["All", "INFO", "ERROR", "DEBUG"]:
            b = tk.Button(
                toolbar, text=opt,
                font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                command=lambda o=opt: self._set_filter(o))
            b.pack(side="left", padx=2, pady=6)
            self._filter_btns[opt] = b
            Tooltip(b, _filter_tips.get(opt, opt))
        self._paint_filter_btns()

        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="left", fill="y",
                                                    padx=10, pady=6)

        # Word-wrap toggle
        self._wrap_on = True
        self._wrap_btn = tk.Button(
            toolbar, text="Wrap: On",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
            bg="#14532d", fg=SUCCESS,
            activebackground=BORDER, activeforeground=TEXT,
            command=self._toggle_wrap)
        self._wrap_btn.pack(side="left", padx=2, pady=6)
        Tooltip(self._wrap_btn, "Toggle line wrapping for long entries.")

        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="left", fill="y",
                                                    padx=10, pady=6)

        # Search box
        tk.Label(toolbar, text="Search:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))

        search_frame = tk.Frame(toolbar, bg=SURFACE2)
        search_frame.pack(side="left", pady=6)

        self._search_entry = tk.Entry(
            search_frame, textvariable=self._search_var,
            font=("Segoe UI", 9), bg=SURFACE, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=YT_RED, width=26)
        self._search_entry.pack(side="left", ipady=4, padx=(0, 4))
        self._search_entry.bind("<Return>",   lambda e: self._find_next())
        self._search_entry.bind("<KP_Enter>", lambda e: self._find_next())
        self._search_var.trace_add("write", lambda *_: self._run_search())

        _search_tips = {"▲": "Jump to the previous search match.",
                        "▼": "Jump to the next search match.",
                        "✕": "Clear the search and its highlights."}
        for sym, cmd in [("▲", self._find_prev), ("▼", self._find_next),
                         ("✕", self._clear_search)]:
            sb = tk.Button(search_frame, text=sym, font=("Segoe UI", 9, "bold"),
                           relief="flat", bd=0, padx=6, pady=2, cursor="hand2",
                           bg=SURFACE2, fg=TEXT_DIM,
                           activebackground=BORDER, activeforeground=TEXT,
                           command=cmd)
            sb.pack(side="left", padx=1)
            Tooltip(sb, _search_tips[sym])

        self._match_lbl = tk.Label(search_frame, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=SURFACE2, width=10)
        self._match_lbl.pack(side="left", padx=(4, 0))

        # Right cluster
        _right_tips = {
            "↗  System Viewer": "Open this log in your default text editor.",
            "⎘  Copy All":      "Copy the visible log text to the clipboard.",
            "⟳  Refresh":       "Reload the debug log from disk.",
            "⤓  End":           "Scroll to the newest entries at the bottom.",
            "⤒  Top":           "Scroll to the oldest entries at the top.",
        }
        for txt, cmd in [("↗  System Viewer", self._open_external),
                         ("⎘  Copy All", self._copy_all),
                         ("⟳  Refresh", self.refresh),
                         ("⤓  End", self._jump_end),
                         ("⤒  Top", self._jump_top)]:
            rb = tk.Button(toolbar, text=txt, font=("Segoe UI", 9, "bold"),
                           relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                           bg=SURFACE2, fg=TEXT_DIM,
                           activebackground=BORDER, activeforeground=TEXT,
                           command=cmd)
            rb.pack(side="right", padx=2, pady=6)
            Tooltip(rb, _right_tips.get(txt, txt))

        # ── Stats bar ─────────────────────────────────────────────────────────
        self._stats_bar = tk.Label(
            self, text="", font=("Segoe UI", 8),
            fg=TEXT_DIM, bg=SURFACE2, anchor="w", padx=12, pady=3,
            highlightthickness=1, highlightbackground=BORDER)
        self._stats_bar.pack(fill="x", side="bottom")

        # ── Log path bar ──────────────────────────────────────────────────────
        short = self._log_path.replace(os.path.expanduser("~"), "~")
        tk.Label(self, text=f"  {short}", font=("Consolas", 8),
                 fg=TEXT_DIM, bg=SURFACE2, anchor="w", pady=3,
                 highlightthickness=1, highlightbackground=BORDER
                 ).pack(fill="x", side="bottom")

        # ── Text area ─────────────────────────────────────────────────────────
        txt_frame = tk.Frame(self, bg=BG)
        txt_frame.pack(fill="both", expand=True)

        self._txt = tk.Text(
            txt_frame,
            font=("Consolas", 9), bg="#0a0a0a", fg=TEXT_MED,
            insertbackground=TEXT, relief="flat",
            wrap="word", state="disabled",
            selectbackground=BORDER, selectforeground=TEXT,
            padx=12, pady=8)

        v_scroll = ttk.Scrollbar(txt_frame, orient="vertical",
                                  command=self._txt.yview)
        self._txt.configure(yscrollcommand=v_scroll.set)
        v_scroll.pack(side="right", fill="y")
        self._txt.pack(fill="both", expand=True)

        # Colour tags
        for level, colour in self._LEVEL_COLORS.items():
            self._txt.tag_configure(level, foreground=colour)
        self._txt.tag_configure("separator",
                                foreground="#4a5568",
                                font=("Consolas", 9, "bold"))
        self._txt.tag_configure("search_hl",
                                background=self._SEARCH_HL,
                                foreground=self._SEARCH_FG)
        self._txt.tag_configure("search_cur",
                                background="#dc2626",
                                foreground="#ffffff")

    def load_log(self):
        if not os.path.exists(self._log_path):
            self._txt.config(state="normal")
            self._txt.delete("1.0", "end")
            self._txt.insert("end", "(debug log is empty)")
            self._txt.config(state="disabled")
            self._stats_bar.config(text="  No log data.")
            return

        with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        filt = self._filter_var.get()
        lines = content.splitlines(keepends=True)
        if filt != "All":
            lines = [l for l in lines
                     if f"| {filt}" in l or "═" in l]

        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        for line in lines:
            tag = None
            if "═" in line:
                tag = "separator"
            elif "| ERROR" in line:
                tag = "ERROR"
            elif "| WARNING" in line or "| WARN" in line:
                tag = "WARNING"
            elif "| DEBUG" in line:
                tag = "DEBUG"
            else:
                tag = "INFO"
            self._txt.insert("end", line, (tag,) if tag else ())
        self._txt.config(state="disabled")

        total = len(lines)
        errs = sum(1 for l in lines if "| ERROR" in l)
        warns = sum(1 for l in lines if "| WARN" in l)
        self._stats_bar.config(
            text=f"  {total} lines  |  {errs} errors  |  {warns} warnings")

    def refresh(self):
        self._clear_search(silent=True)
        self.load_log()

    def _set_filter(self, opt):
        self._filter_var.set(opt)
        self._paint_filter_btns()
        self._clear_search(silent=True)
        self.load_log()
        # Land on the newest entries when switching views, not back at the top.
        self.after_idle(lambda: self._txt.yview_moveto(1.0))

    def _paint_filter_btns(self):
        cur = self._filter_var.get()
        for name, btn in self._filter_btns.items():
            if name == cur:
                btn.config(bg=YT_RED, fg="#ffffff",
                           activebackground="#b91c1c", activeforeground="#ffffff")
            else:
                btn.config(bg=SURFACE2, fg=TEXT_DIM,
                           activebackground=BORDER, activeforeground=TEXT)


# ─────────────────────────────────────────────────────────────────────────────
COOKIE_HOWTO_TEXTS = {
    "Chrome": """\
Setting Up a Dedicated Chrome Profile for DJ-CrateBuilder
══════════════════════════════════════════════════════════

Step 1 — Open Chrome Profile Manager

  Open Chrome and click your profile icon in the top-right corner
  (the small circular image next to the three-dot menu).
  In the dropdown, click "Add" at the bottom of the profile list.

Step 2 — Create the Profile

  Click "Continue without an account" for now.
  Give the profile a recognizable name like "DJ-CrateBuilder"
  or "YT-Download." Pick a distinct color or icon so you can
  easily identify it. Click "Done."

  A new Chrome window opens using the new profile. This is a
  completely separate browser environment — its own cookies,
  history, bookmarks, and saved logins, entirely isolated
  from your personal profile.

Step 3 — Create the Throwaway Google Account

  In the new profile window, go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 4 — Log into YouTube

  Still in the DJ-CrateBuilder profile window, go to:
      https://www.youtube.com

  You should be signed in with the account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 5 — Find Your Profile Name

  Open a new tab in the DJ-CrateBuilder profile and type:
      chrome://version

  Look for the line labeled "Profile Path." It will show
  something like:

      C:\\Users\\YourName\\AppData\\Local\\Google\\Chrome\\User Data\\Profile 2

  The profile name is the last folder in that path.
  In this example, it is:  Profile 2

  IMPORTANT: The display name you chose ("DJ-CrateBuilder")
  is NOT the folder name. You need the actual folder name
  from this path (e.g. "Profile 2", "Profile 3", etc.)

Step 6 — Close the Profile Window

  You can close this Chrome window. The profile and its
  cookies persist permanently. You don't need to keep it
  open for the app to read the cookies. Switch back to your
  personal Chrome profile and browse normally.

Step 7 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Browser:  Chrome
       Profile:  Profile 2  (or whatever chrome://version showed)

Step 8 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  You'll know because the app will start getting "login required"
  errors again. To refresh:

    1. Click your Chrome profile icon
    2. Switch to the DJ-CrateBuilder profile
    3. Go to youtube.com — make sure you're still signed in
    4. Close the window and continue as normal

  The cookies refresh automatically when you visit the site.
""",

    "Firefox": """\
Setting Up a Dedicated Firefox Profile for DJ-CrateBuilder
═══════════════════════════════════════════════════════════

Step 1 — Open Firefox Profile Manager

  Press  Win+R  (or open a terminal) and run:
      firefox -P

  This opens the Firefox Profile Manager dialog.

Step 2 — Create the Profile

  Click "Create Profile…" and follow the wizard.
  Name it something recognizable like "DJ-CrateBuilder".
  Click "Finish."

  Select the new profile and click "Start Firefox."
  A new Firefox window opens with a clean profile — its own
  cookies, history, and extensions, isolated from your
  personal profile.

Step 3 — Create the Throwaway Google Account

  In the new profile window, go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 4 — Log into YouTube

  Still in the DJ-CrateBuilder profile window, go to:
      https://www.youtube.com

  You should be signed in with the account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 5 — Find Your Profile Folder Name

  In the DJ-CrateBuilder profile, type in the address bar:
      about:profiles

  Look for the profile you just created. The "Root Directory"
  path will show something like:

      C:\\Users\\YourName\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles\\ab12cd34.DJ-CrateBuilder

  The profile name for DJ-CrateBuilder is the folder name,
  e.g.:  ab12cd34.DJ-CrateBuilder

  IMPORTANT: You need the full folder name including the
  random prefix (e.g. "ab12cd34.DJ-CrateBuilder"), not just
  the display name.

Step 6 — Close the Profile Window

  You can close this Firefox window. The profile and its
  cookies persist permanently. You don't need to keep it open.
  Next time you open Firefox normally, it will use your
  personal profile as usual.

Step 7 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Browser:  Firefox
       Profile:  ab12cd34.DJ-CrateBuilder  (from about:profiles)

Step 8 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  To refresh:

    1. Run  firefox -P  and launch the DJ-CrateBuilder profile
    2. Go to youtube.com — make sure you're still signed in
    3. Close the window and continue as normal

  The cookies refresh automatically when you visit the site.
""",

    "Edge": """\
Setting Up a Dedicated Edge Profile for DJ-CrateBuilder
═══════════════════════════════════════════════════════

Step 1 — Open Edge Profile Manager

  Open Edge and click your profile icon in the top-right corner.
  In the dropdown, click "Add profile" then "Add."

Step 2 — Create the Profile

  Choose "Start without your data" for a clean profile.
  Give it a recognizable name like "DJ-CrateBuilder."

  A new Edge window opens using the new profile — its own
  cookies, history, and saved logins, isolated from your
  personal profile.

Step 3 — Create the Throwaway Google Account

  In the new profile window, go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 4 — Log into YouTube

  Still in the DJ-CrateBuilder profile window, go to:
      https://www.youtube.com

  You should be signed in with the account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 5 — Find Your Profile Name

  Open a new tab in the DJ-CrateBuilder profile and type:
      edge://version

  Look for the line labeled "Profile Path." It will show
  something like:

      C:\\Users\\YourName\\AppData\\Local\\Microsoft\\Edge\\User Data\\Profile 2

  The profile name is the last folder in that path.
  In this example, it is:  Profile 2

  IMPORTANT: The display name you chose ("DJ-CrateBuilder")
  is NOT the folder name. You need the actual folder name
  from this path (e.g. "Profile 2", "Profile 3", etc.)

Step 6 — Close the Profile Window

  You can close this Edge window. The profile and its cookies
  persist permanently. Switch back to your personal Edge
  profile and browse normally.

Step 7 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Browser:  Edge
       Profile:  Profile 2  (or whatever edge://version showed)

Step 8 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  To refresh:

    1. Click your Edge profile icon
    2. Switch to the DJ-CrateBuilder profile
    3. Go to youtube.com — make sure you're still signed in
    4. Close the window and continue as normal

  The cookies refresh automatically when you visit the site.
""",

    "Brave": """\
Setting Up a Dedicated Brave Profile for DJ-CrateBuilder
════════════════════════════════════════════════════════

Step 1 — Open Brave Profile Manager

  Open Brave and click your profile icon in the top-right corner.
  In the dropdown, click "Add" to create a new profile.

Step 2 — Create the Profile

  Click "Continue without an account."
  Give it a recognizable name like "DJ-CrateBuilder."
  Pick a distinct color so you can easily identify it.

  A new Brave window opens using the new profile — its own
  cookies, history, and saved logins, isolated from your
  personal profile.

Step 3 — Create the Throwaway Google Account

  In the new profile window, go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 4 — Log into YouTube

  Still in the DJ-CrateBuilder profile window, go to:
      https://www.youtube.com

  You should be signed in with the account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 5 — Find Your Profile Name

  Open a new tab in the DJ-CrateBuilder profile and type:
      brave://version

  Look for the line labeled "Profile Path." It will show
  something like:

      C:\\Users\\YourName\\AppData\\Local\\BraveSoftware\\Brave-Browser\\User Data\\Profile 2

  The profile name is the last folder in that path.
  In this example, it is:  Profile 2

  IMPORTANT: The display name you chose ("DJ-CrateBuilder")
  is NOT the folder name. You need the actual folder name
  from this path (e.g. "Profile 2", "Profile 3", etc.)

Step 6 — Close the Profile Window

  You can close this Brave window. The profile and its cookies
  persist permanently. Switch back to your personal Brave
  profile and browse normally.

Step 7 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Browser:  Brave
       Profile:  Profile 2  (or whatever brave://version showed)

Step 8 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  To refresh:

    1. Click your Brave profile icon
    2. Switch to the DJ-CrateBuilder profile
    3. Go to youtube.com — make sure you're still signed in
    4. Close the window and continue as normal

  The cookies refresh automatically when you visit the site.
""",

    "Opera": """\
Setting Up a Dedicated Opera Profile for DJ-CrateBuilder
════════════════════════════════════════════════════════

IMPORTANT: Opera does not support multiple profiles the same
way Chrome or Firefox do. Instead, use a cookie file.

Step 1 — Install a Cookie Export Extension

  Open Opera and install the "Get cookies.txt LOCALLY"
  extension from the Chrome Web Store (Opera supports
  Chrome extensions).

Step 2 — Create the Throwaway Google Account

  Go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 3 — Log into YouTube

  Go to:
      https://www.youtube.com

  Sign in with the throwaway account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 4 — Export Cookies

  While on youtube.com, click the "Get cookies.txt LOCALLY"
  extension icon and export the cookies for the current site.
  Save the file somewhere convenient, e.g.:
      C:\\Users\\YourName\\cookies.txt

Step 5 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Method:  Cookie File
       File:    C:\\Users\\YourName\\cookies.txt

Step 6 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  To refresh:

    1. Go to youtube.com in Opera — sign in if needed
    2. Re-export cookies using the extension
    3. Overwrite the old cookies.txt file

  The app will pick up the refreshed cookies automatically.
""",

    "Chromium": """\
Setting Up a Dedicated Chromium Profile for DJ-CrateBuilder
══════════════════════════════════════════════════════════

Step 1 — Open Chromium Profile Manager

  Open Chromium and click your profile icon in the top-right
  corner. In the dropdown, click "Add" at the bottom of the
  profile list.

Step 2 — Create the Profile

  Click "Continue without an account" for now.
  Give the profile a recognizable name like "DJ-CrateBuilder."
  Pick a distinct color or icon so you can easily identify it.
  Click "Done."

  A new Chromium window opens using the new profile — its own
  cookies, history, and saved logins, isolated from your
  personal profile.

Step 3 — Create the Throwaway Google Account

  In the new profile window, go to:
      https://accounts.google.com/signup

  Create a new Google account using a throwaway email.
  Complete the signup process.

Step 4 — Log into YouTube

  Still in the DJ-CrateBuilder profile window, go to:
      https://www.youtube.com

  You should be signed in with the account you just created.
  Watch a video or two briefly and accept any terms prompts.
  This establishes a valid session with cookies.

Step 5 — Find Your Profile Name

  Open a new tab in the DJ-CrateBuilder profile and type:
      chrome://version

  Look for the line labeled "Profile Path." It will show
  something like:

      C:\\Users\\YourName\\AppData\\Local\\Chromium\\User Data\\Profile 2

  The profile name is the last folder in that path.
  In this example, it is:  Profile 2

  IMPORTANT: The display name you chose ("DJ-CrateBuilder")
  is NOT the folder name. You need the actual folder name
  from this path (e.g. "Profile 2", "Profile 3", etc.)

Step 6 — Close the Profile Window

  You can close this Chromium window. The profile and its
  cookies persist permanently. Switch back to your personal
  Chromium profile and browse normally.

Step 7 — Configure the App

  In DJ-CrateBuilder Settings → Download Behavior:
    ✓  Enable "Use browser cookies"
       Browser:  Chromium
       Profile:  Profile 2  (or whatever chrome://version showed)

Step 8 — When Cookies Expire

  Every few weeks or months, the session cookies will expire.
  To refresh:

    1. Click your Chromium profile icon
    2. Switch to the DJ-CrateBuilder profile
    3. Go to youtube.com — make sure you're still signed in
    4. Close the window and continue as normal

  The cookies refresh automatically when you visit the site.
""",
}


class CookieHowToWindow(tk.Toplevel):
    """Standalone dark-themed how-to viewer for browser profile setup."""

    def __init__(self, parent, browser="Chrome"):
        super().__init__(parent)
        self.title(f"📖  How-To: Setting Up a Dedicated {browser} Profile")
        self.geometry("700x540")
        self.minsize(500, 350)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Centre over parent
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 700) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 540)  // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        # Text area
        txt_frame = tk.Frame(self, bg=BG)
        txt_frame.pack(fill="both", expand=True)

        self._txt = tk.Text(
            txt_frame,
            font=("Consolas", 10), bg="#0a0a0a", fg=TEXT_MED,
            insertbackground=TEXT, relief="flat",
            wrap="word", state="disabled",
            selectbackground=BORDER, selectforeground=TEXT,
            padx=16, pady=12)

        v_scroll = ttk.Scrollbar(txt_frame, orient="vertical",
                                  command=self._txt.yview)
        self._txt.configure(yscrollcommand=v_scroll.set)
        v_scroll.pack(side="right", fill="y")
        self._txt.pack(side="left", fill="both", expand=True)

        # Colour tags
        self._txt.tag_configure("title", foreground=TEXT,
                                 font=("Consolas", 11, "bold"))
        self._txt.tag_configure("divider", foreground="#4a5568")
        self._txt.tag_configure("step", foreground="#f59e0b",
                                 font=("Consolas", 10, "bold"))
        self._txt.tag_configure("url", foreground=SUCCESS)
        self._txt.tag_configure("important", foreground=YT_RED,
                                 font=("Consolas", 10, "bold"))
        self._txt.tag_configure("body", foreground=TEXT_MED)

        # Populate with browser-specific text
        howto_text = COOKIE_HOWTO_TEXTS.get(browser, COOKIE_HOWTO_TEXTS["Chrome"])
        self._txt.config(state="normal")
        for line in howto_text.splitlines(True):
            stripped = line.strip()
            if stripped.startswith("══"):
                self._txt.insert("end", line, "divider")
            elif stripped.startswith("Setting Up"):
                self._txt.insert("end", line, "title")
            elif stripped.startswith("Step "):
                self._txt.insert("end", line, "step")
            elif stripped.startswith("https://"):
                self._txt.insert("end", line, "url")
            elif stripped.startswith("IMPORTANT:"):
                self._txt.insert("end", line, "important")
            else:
                self._txt.insert("end", line, "body")
        self._txt.config(state="disabled")

        # Mousewheel
        _bind_wheel(self._txt, lambda e: self._txt.yview_scroll(
            int(-1*(_wheel_delta(e)/120)), "units"))

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.focus_force()


class _FoldersCleanupSession:
    """Drives Folders Cleanup ‹Smart› across one or more channels: per-channel
    scan (with a modal progress dialog), classify, then a review/delete window.
    Owned by a DatabaseViewerWindow; reaches the App only for data-layer helpers
    (scan URL building, cookie opts, save-dir, loggers) — never its Watch List
    UI."""

    def __init__(self, viewer, cids):
        self.viewer = viewer            # DatabaseViewerWindow
        self.app = viewer._parent       # main App (data-layer helpers)
        self.db = viewer._db
        self.cids = list(cids)
        self.total = len(self.cids)
        self.idx = 0
        self.cancelled = False
        self.removed_total = 0
        self.channels_cleaned = 0
        self.channels_skipped = 0
        self._progress = None
        self._done = False
        # Private cancel signal — deliberately NOT the App's shared _cancel_flag,
        # so cancelling a cleanup run can't abort an unrelated main-window scan
        # or download (and vice-versa). The Database Viewer is non-modal, so both
        # can be in flight at once.
        self._cancel_event = threading.Event()

    # ── public entry ──────────────────────────────────────────────────────
    def start(self):
        self._cancel_event.clear()
        self._next_channel()

    # ── per-channel pump ──────────────────────────────────────────────────
    def _next_channel(self):
        if self.cancelled or self.idx >= self.total:
            self._finish()
            return
        cid = self.cids[self.idx]
        ch = self.db.get_watchlist_channel(cid)
        if not ch:
            self.channels_skipped += 1
            self.idx += 1
            self._next_channel()
            return
        self._show_progress(ch)
        self.app._run_bg(self._scan_worker, ch)

    def _scan_worker(self, ch):
        """Background thread: flat-scan the channel, then hand results back to
        the main thread."""
        try:
            import yt_dlp
            opts = {
                "extract_flat":  "in_playlist",
                "skip_download": True,
                "lazy_playlist": True,
                "quiet":         True,
                "no_warnings":   True,
            }
            self.app._apply_cookie_opts(opts)
            platform = ch.get("platform") or "YouTube"
            url = watch_fetch_url(platform, ch["url"])
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = list(info.get("entries") or [])
            err = None
        except Exception as exc:
            entries = []
            err = str(exc)[:160]
        if self._cancel_event.is_set():
            self.cancelled = True
        self.viewer.after(0, lambda: self._on_scan_done(ch, entries, err))

    def _on_scan_done(self, ch, entries, err):
        if not self.viewer.winfo_exists():
            return
        self._hide_progress()
        if self.cancelled:
            self._finish()
            return
        if err is not None:
            self.app._dbg.info(
                f"CLEANUP SCAN FAIL | {ch.get('display_name')} | {err}")
            self._log_channel(ch, removed=0, kept=0, errors=0,
                              note=f"skipped (scan error: {err})")
            self.channels_skipped += 1
            self._advance()
            return
        folder = self.app._resolve_save_dir(
            ch.get("genre") or "(none)", ch.get("display_name"),
            platform=ch.get("platform") or "YouTube")
        folder_files, db_map = self._gather_folder(folder)
        if not is_scan_trustworthy(len(entries), len(folder_files)):
            self.app._dbg.info(
                f"CLEANUP SKIP | {ch.get('display_name')} | "
                f"scan={len(entries)} folder={len(folder_files)} (untrusted)")
            self._log_channel(ch, removed=0, kept=len(folder_files), errors=0,
                              note="skipped (scan returned too few videos)")
            self.channels_skipped += 1
            self._advance()
            return
        flagged = classify_local_files(entries, folder_files, db_map)
        if not flagged:
            self._log_channel(ch, removed=0, kept=len(folder_files), errors=0,
                              note="clean (nothing to remove)")
            self._advance()
            return
        # Hand off to the review window (Task 8).
        self.viewer._open_cleanup_review(self, ch, flagged, len(folder_files))

    # ── helpers ───────────────────────────────────────────────────────────
    def _gather_folder(self, folder):
        """Return (folder_files, db_video_id_by_path) for *folder*.
        folder_files: list of (filename, full_path, size, mtime) for each .mp3.
        db_map: full_path -> video_id from the downloads table."""
        folder_files = []
        try:
            for fn in os.listdir(folder):
                if not fn.lower().endswith(".mp3"):
                    continue
                full = os.path.join(folder, fn)
                try:
                    st = os.stat(full)
                    folder_files.append((fn, full, st.st_size, int(st.st_mtime)))
                except OSError:
                    folder_files.append((fn, full, 0, 0))
        except OSError:
            pass
        norm_folder = os.path.normpath(folder)
        db_map = {}
        for d in self.db.get_all_downloads():
            fp = d.get("file_path")
            if fp and os.path.dirname(os.path.normpath(fp)) == norm_folder:
                db_map[os.path.normpath(fp)] = d.get("video_id")
        # Re-key db_map onto the exact full paths we built for folder_files, so
        # lookups in classify_local_files match regardless of separator/casing.
        remap = {}
        for fn, full, _sz, _mt in folder_files:
            nf = os.path.normpath(full)
            if nf in db_map:
                remap[full] = db_map[nf]
        return folder_files, remap

    def _log_channel(self, ch, *, removed, kept, errors, note=""):
        plat = ch.get("platform") or "YouTube"
        genre = ch.get("genre") or "(none)"
        name = ch.get("display_name") or ""
        tail = f" — {note}" if note else ""
        self.app._logger.info(
            f"Folder Cleanup | {plat} / {genre} / {name}: "
            f"{removed} removed, {kept} kept, {errors} errors{tail}")

    # ── progress dialog ───────────────────────────────────────────────────
    def _show_progress(self, ch):
        self._hide_progress()
        dlg = tk.Toplevel(self.viewer)
        dlg.title("Folders Cleanup")
        dlg.configure(bg=BG)
        dlg.transient(self.viewer)
        dlg.resizable(False, False)
        tk.Label(dlg, text=f"Scanning  {ch.get('display_name') or ''}…",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=TEXT
                 ).pack(padx=24, pady=(18, 4))
        tk.Label(dlg, text=f"Channel {self.idx + 1} of {self.total}",
                 font=("Segoe UI", 9), bg=BG, fg=TEXT_DIM).pack(pady=(0, 8))
        bar = ttk.Progressbar(dlg, mode="indeterminate", length=260)
        bar.pack(padx=24, pady=(0, 10))
        bar.start(12)
        tk.Button(dlg, text="Cancel", font=("Segoe UI", 9),
                  relief="flat", bd=0, bg=SURFACE2, fg=TEXT,
                  activebackground=BORDER, activeforeground=TEXT,
                  padx=12, pady=4, cursor="hand2",
                  command=self._cancel).pack(pady=(0, 16))
        dlg.protocol("WM_DELETE_WINDOW", self._cancel)
        dlg.update_idletasks()
        px = self.viewer.winfo_x() + (self.viewer.winfo_width() - dlg.winfo_width()) // 2
        py = self.viewer.winfo_y() + (self.viewer.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
        self._progress = (dlg, bar)

    def _hide_progress(self):
        if self._progress is not None:
            dlg, bar = self._progress
            try:
                bar.stop()
                dlg.destroy()
            except Exception:
                pass
            self._progress = None

    def _cancel(self):
        """Cancel button / dialog close: abort the in-flight scan immediately.
        Prior confirmed deletions stay (already trashed). Deliberately does NOT
        call _finish() — the in-flight worker's late _on_scan_done sees
        `cancelled` and finishes exactly once, avoiding a double-finish."""
        self.cancelled = True
        self._cancel_event.set()
        self._hide_progress()

    # ── advance / finish ──────────────────────────────────────────────────
    def _advance(self):
        self.idx += 1
        self._next_channel()

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._hide_progress()
        if self.viewer.winfo_exists():
            self.viewer._finish_folders_cleanup(self)


class _CleanupReviewWindow(tk.Toplevel):
    """Per-channel review: list flagged files with checkboxes; Confirm / Skip /
    Cancel. Strong-confidence rows start checked; weak rows unchecked."""

    _COLS = {
        "sel":      ("",          34,  "center"),
        "filename": ("File",      360, "w"),
        "size":     ("Size",      90,  "e"),
        "modified": ("Modified",  130, "w"),
        "reason":   ("Reason",    260, "w"),
    }

    def __init__(self, viewer, session, ch, flagged, folder_count):
        super().__init__(viewer)
        self.viewer = viewer
        self.session = session
        self.ch = ch
        self.flagged = flagged
        self.folder_count = folder_count
        self.result = None              # "confirm" | "skip" | "cancel"
        self.selected = []              # full paths chosen, set on confirm
        self.checked = {}               # full_path -> bool

        name = ch.get("display_name") or ""
        self.title(f"Folders Cleanup — {name}  "
                   f"({session.idx + 1} of {session.total})")
        self.configure(bg=BG)
        self.geometry("900x520")
        self.transient(viewer)
        self.grab_set()                 # modal
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build(name)
        self._populate()

        # Center over the viewer (matches the progress dialog's placement).
        self.update_idletasks()
        px = viewer.winfo_x() + (viewer.winfo_width() - self.winfo_width()) // 2
        py = viewer.winfo_y() + (viewer.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

    def _build(self, name):
        top = tk.Frame(self, bg=SURFACE2)
        top.pack(fill="x")
        tk.Label(top,
                 text=f"{len(self.flagged)} file(s) on disk are no longer on "
                      f"“{name}”. Ticked files go to the Recycle Bin.",
                 font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
                 anchor="w").pack(side="left", padx=12, pady=8)
        tk.Button(top, text="Deselect All", font=("Segoe UI", 9),
                  relief="flat", bd=0, bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  padx=8, pady=4, cursor="hand2",
                  command=lambda: self._set_all(False)).pack(side="right", padx=(0, 8))
        tk.Button(top, text="Select All", font=("Segoe UI", 9),
                  relief="flat", bd=0, bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  padx=8, pady=4, cursor="hand2",
                  command=lambda: self._set_all(True)).pack(side="right", padx=(0, 4))

        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True)
        cols = list(self._COLS)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 style="DB.Treeview", selectmode="none")
        for cid, (head, w, anchor) in self._COLS.items():
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid == "reason"))
        self.tree.tag_configure("weakrow", foreground=TEXT_DIM)
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click, add="+")

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", pady=10)
        tk.Button(btns, text="Confirm Deletions", font=("Segoe UI", 10, "bold"),
                  relief="flat", bd=0, bg=WL_BLUE_DARK, fg=TEXT,
                  activebackground=WL_BLUE, activeforeground=TEXT,
                  padx=14, pady=6, cursor="hand2",
                  command=self._confirm).pack(side="left", padx=(16, 6))
        if self.session.total > 1:
            tk.Button(btns, text="Skip Channel", font=("Segoe UI", 10),
                      relief="flat", bd=0, bg=SURFACE2, fg=TEXT,
                      activebackground=BORDER, activeforeground=TEXT,
                      padx=14, pady=6, cursor="hand2",
                      command=self._skip).pack(side="left", padx=6)
            cancel_text = "Cancel Scans"
        else:
            cancel_text = "Cancel Scan"
        tk.Button(btns, text=cancel_text, font=("Segoe UI", 10),
                  relief="flat", bd=0, bg=SURFACE2, fg=TEXT,
                  activebackground=BORDER, activeforeground=TEXT,
                  padx=14, pady=6, cursor="hand2",
                  command=self._cancel).pack(side="right", padx=(6, 16))

    def _populate(self):
        for f in self.flagged:
            start = (f["confidence"] == "strong")
            self.checked[f["full_path"]] = start
            tag = "weakrow" if f["confidence"] == "weak" else ""
            self.tree.insert(
                "", "end", iid=f["full_path"], tags=(tag,) if tag else (),
                values=("☑" if start else "☐",
                        f["filename"],
                        self._fmt_size(f["size_bytes"]),
                        self._fmt_mtime(f["mtime"]),
                        f["reason"]))

    @staticmethod
    def _fmt_size(n):
        n = n or 0
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0

    @staticmethod
    def _fmt_mtime(ts):
        if not ts:
            return ""
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    def _on_click(self, event):
        if (self.tree.identify_region(event.x, event.y) != "cell"
                or self.tree.identify_column(event.x) != "#1"):
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.checked[row] = not self.checked.get(row, False)
        self.tree.set(row, "sel", "☑" if self.checked[row] else "☐")
        return "break"

    def _set_all(self, value):
        for path in self.checked:
            self.checked[path] = value
            self.tree.set(path, "sel", "☑" if value else "☐")

    def _selected_paths(self):
        return [p for p, on in self.checked.items() if on]

    def _confirm(self):
        self.result = "confirm"
        self.selected = self._selected_paths()
        self.grab_release()
        self.destroy()

    def _skip(self):
        self.result = "skip"
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.result = "cancel"
        self.grab_release()
        self.destroy()

    def _on_close(self):
        # [X] = the safe no-delete option: skip (multi) / cancel (single).
        self.result = "skip" if self.session.total > 1 else "cancel"
        self.grab_release()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# DatabaseViewerWindow — browse the downloads history + watch list
#   A dark-themed Toplevel (sibling to the log viewers) that presents the
#   downloads database as a file-explorer-style tree and the watch list as a
#   sortable table. Read-only: organize, filter, sort, and export — never edit.
# ─────────────────────────────────────────────────────────────────────────────
class DatabaseViewerWindow(tk.Toplevel):
    """Standalone dark-themed database browser window."""

    # Group-by presets for the Downloads tree: label -> ordered hierarchy keys.
    GROUP_PRESETS = {
        "Platform › Genre › Channel": ["platform", "genre", "channel_name"],
        "Genre › Channel":            ["genre", "channel_name"],
        "Channel":                    ["channel_name"],
        "Platform › Channel":         ["platform", "channel_name"],
    }

    # Downloads detail columns: id -> (heading, width, anchor)
    _DL_COLS = {
        "channel":    ("Channel",    160, "w"),
        "genre":      ("Genre",      110, "w"),
        "platform":   ("Platform",    80, "w"),
        "upload":     ("Upload",     110, "w"),
        "downloaded": ("Downloaded", 140, "w"),
        "bitrate":    ("Bitrate",     70, "e"),
    }

    # Artwork columns: id -> (heading, width, anchor)
    _ART_COLS = {
        "title":      ("Track",        260, "w"),
        "channel":    ("Channel",      150, "w"),
        "platform":   ("Platform",      80, "w"),
        "embedded":   ("Embedded",      80, "center"),
        "sidecar":    ("Sidecar",      170, "w"),
        "on_disk":    ("On Disk",       70, "center"),
        "thumb_url":  ("Thumbnail URL", 240, "w"),
    }

    # Artwork filter presets: label -> predicate over an (row, state) pair.
    _ART_FILTERS = (
        "All tracks",
        "Has artwork",
        "Missing artwork",
        "Embedded only",
        "Sidecar missing on disk",
    )

    # Watch List columns: id -> (heading, width, anchor)
    _WL_COLS = {
        "sel":        ("",             34, "center"),
        "channel":    ("Channel",      180, "w"),
        "link":       ("URL Link",     220, "w"),
        "folder":     ("Folder",       260, "w"),
        "platform":   ("Platform",      80, "w"),
        "genre":      ("Genre",        110, "w"),
        "cutoff":     ("Cutoff",       130, "w"),
        "last_scan":  ("Last scan",    120, "w"),
        "pending":    ("Pending new",   90, "e"),
        "total":      ("Total dl'd",    80, "e"),
        "status":     ("Status",        90, "w"),
    }

    def __init__(self, parent, db):
        super().__init__(parent)
        self._db        = db
        self._parent    = parent
        self._downloads = []   # list of dicts, loaded from the DB
        self._channels  = []   # watch list rows
        self._row_data  = {}   # downloads tree: item_id -> download dict (leaves)

        # Downloads view state
        self._group_var  = tk.StringVar(value=list(self.GROUP_PRESETS)[0])
        self._plat_var   = tk.StringVar(value="All platforms")
        self._genre_var  = tk.StringVar(value="All genres")
        self._search_var = tk.StringVar()
        self._dl_sort_col  = "downloaded"
        self._dl_sort_desc = True

        # Watch List view state
        self._wl_sort_col  = "channel"
        self._wl_sort_desc = False

        # Artwork view state
        self._art_filter_var = tk.StringVar(value=self._ART_FILTERS[0])
        self._art_search_var = tk.StringVar()
        self._art_sort_col   = "title"
        self._art_sort_desc  = False
        self._art_row_data   = {}   # artwork tree: item_id -> download dict
        self._art_ctx_item   = None
        # Held to stop Tk garbage-collecting the PhotoImage out of the label.
        self._art_preview_img = None

        self.title("🗂  Database  —  DJ CrateBuilder")
        self.geometry("1100x680")
        self.minsize(820, 460)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Centre over parent
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 1100) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 680)  // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

        self._configure_styles()
        self._build_ui()
        self.load_data()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.focus_force()

    # ── Column-width persistence ───────────────────────────────────────────────
    # Remember each column's width between sessions so the viewer reopens laid
    # out the way the user left it.
    _DL_WIDTH_KEY  = "db_dl_col_widths"
    _WL_WIDTH_KEY  = "db_wl_col_widths"
    _ART_WIDTH_KEY = "db_art_col_widths"

    @staticmethod
    def _saved_col_widths(key):
        """Return the saved {column_id: width} dict for *key* (empty if none)."""
        val = load_config().get(key)
        return val if isinstance(val, dict) else {}

    def _persist_col_widths(self):
        """Save both trees' current column widths to the config file."""
        def widths(tree, col_ids):
            out = {}
            for cid in col_ids:
                try:
                    out[cid] = int(tree.column(cid, "width"))
                except Exception:
                    pass
            return out
        try:
            cfg = load_config()
            cfg[self._DL_WIDTH_KEY] = widths(
                self._dl_tree, ["#0", *self._DL_COLS])
            cfg[self._WL_WIDTH_KEY] = widths(self._wl_tree, list(self._WL_COLS))
            cfg[self._ART_WIDTH_KEY] = widths(self._art_tree,
                                              list(self._ART_COLS))
            save_config(cfg)
        except Exception:
            pass   # column widths are a nicety; never block closing on them

    def _on_close(self):
        self._hide_wl_celltip()
        # If a Folders Cleanup run is in flight, abort its scan so a late
        # background callback can't fire against this destroyed window.
        sess = getattr(self, "_cleanup_session", None)
        if sess is not None and not sess._done:
            sess.cancelled = True
            sess._cancel_event.set()
        self._persist_col_widths()
        self.destroy()

    # ── Column drag-to-reorder ─────────────────────────────────────────────────
    # Drag a header onto another header to reorder columns; the order is
    # remembered between sessions. Reordering uses Treeview's displaycolumns, so
    # the underlying column ids (and saved widths) are untouched.
    _DL_ORDER_KEY  = "db_dl_col_order"
    _WL_ORDER_KEY  = "db_wl_col_order"
    _ART_ORDER_KEY = "db_art_col_order"

    @staticmethod
    def _display_order(tree, all_cols):
        """Current left-to-right order of the data columns."""
        dc = tree.cget("displaycolumns")
        if not dc or "#all" in dc:
            return list(all_cols)
        return list(dc)

    def _apply_saved_order(self, tree, all_cols, key):
        """Restore a saved column order if it's still a valid permutation."""
        order = load_config().get(key)
        if isinstance(order, list) and sorted(order) == sorted(all_cols):
            tree.configure(displaycolumns=order)

    @staticmethod
    def _save_col_order(key, order):
        try:
            cfg = load_config()
            cfg[key] = list(order)
            save_config(cfg)
        except Exception:
            pass

    def _enable_col_reorder(self, tree, all_cols, order_key, pinned=()):
        """Bind header drag-and-drop reordering onto *tree*. A plain click still
        sorts (we only intercept when the column is dropped on a different one).
        Columns named in *pinned* cannot be moved and cannot be displaced."""
        state = {"src": None}

        def on_press(e):
            if tree.identify_region(e.x, e.y) == "heading":
                state["src"] = tree.identify_column(e.x)

        def name_at(disp, order):
            # disp is "#0" (the tree column, not reorderable) or "#N" into order.
            if not disp or disp == "#0":
                return None
            i = int(disp[1:]) - 1
            return order[i] if 0 <= i < len(order) else None

        def on_release(e):
            src, state["src"] = state["src"], None
            if src is None or tree.identify_region(e.x, e.y) != "heading":
                return
            tgt = tree.identify_column(e.x)
            if not tgt or tgt == src:
                return                       # pure click → let the sort fire
            order = self._display_order(tree, all_cols)
            src_name = name_at(src, order)
            if src_name is None or src_name in pinned:
                return                       # can't move tree (#0) or a pinned col
            tgt_name = name_at(tgt, order)
            if tgt_name in pinned:
                return                       # can't displace a pinned col
            new_order = self._reorder_columns(order, src_name, tgt_name)
            if new_order == order:
                return                       # nothing actually moved
            tree.configure(displaycolumns=new_order)
            self._save_col_order(order_key, new_order)
            return "break"                   # suppress the sort on a real drag

        tree.bind("<ButtonPress-1>", on_press, add="+")
        tree.bind("<ButtonRelease-1>", on_release, add="+")

    @staticmethod
    def _reorder_columns(order, src_name, tgt_name):
        """Return a new column order with *src_name* moved onto *tgt_name*'s
        visual slot. *tgt_name* may be None (a drop on the non-reorderable tree
        column) — the source then goes to the front.

        INVARIANT (load-bearing): the target index is read from the ORIGINAL
        order BEFORE src is removed. Reading it after the remove shifts it left
        by one whenever src sat to the target's left, which makes rightward
        drags land one column short while leftward drags stay correct."""
        order = list(order)
        if src_name not in order:
            return order
        insert_at = (order.index(tgt_name)
                     if (tgt_name and tgt_name in order) else 0)
        order.remove(src_name)
        order.insert(insert_at, src_name)
        return order

    # ── Theming ───────────────────────────────────────────────────────────────
    def _configure_styles(self):
        """Dark-theme the ttk.Treeview widgets used in this window."""
        s = ttk.Style(self)
        s.configure("DB.Treeview",
                    background=DB_FIELD, fieldbackground=DB_FIELD,
                    foreground=TEXT_MED, rowheight=24, borderwidth=0,
                    font=("Segoe UI", 10))
        # relief="solid" + borderwidth draws a 1px box around each heading cell,
        # giving a vertical divider between every column header. bordercolor sets
        # those dividers to the light-grey hairline tone.
        s.configure("DB.Treeview.Heading",
                    background=SURFACE2, foreground=TEXT,
                    relief="solid", borderwidth=1, bordercolor=DB_GRID,
                    font=("Segoe UI", 10, "bold"), padding=(6, 4))
        s.map("DB.Treeview",
              background=[("selected", YT_DARK)],
              foreground=[("selected", "#ffffff")])
        s.map("DB.Treeview.Heading",
              background=[("active", BORDER)])

    # ── Build UI ──────────────────────────────────────────────────────────────
    # Tooltip content for the Help button on the tab-bar row.
    _HELP_TOOLTIP = (
        "DOWNLOADS TAB\n"
        "• Group by — change how rows nest (e.g. Platform › Genre › Channel).\n"
        "• Platform / Genre filters — hide rows that don't match the selection.\n"
        "• Search — live-filter rows by any column text.\n"
        "• ⊞ / ⊟ — expand or collapse every group at once.\n"
        "• Click a column header to sort; drag a header to reorder columns.\n"
        "• Double-click a row to open the file.\n"
        "• Right-click a row for Open File / Open Containing Folder / Copy Path.\n"
        "• ⤓ Export CSV — write the current view to a .csv file.\n"
        "• ⟳ Refresh — reload from the database.\n"
        "\n"
        "WATCH LIST TAB\n"
        "• Every tracked channel with its scan, pending-new, and download history.\n"
        "• URL Link column — the channel's source page (YouTube/SoundCloud).\n"
        "• Folder column — the local save folder for that channel.\n"
        "• Click a column header to sort; drag a header to reorder columns.\n"
        "• Right-click a row to open or copy the channel URL, or Open Folder\n"
        "  to reveal the channel's folder in the system file manager.\n"
        "• ⟳ Refresh — reload from the database.\n"
        "\n"
        "ARTWORK TAB\n"
        "• Every track and the cover art the database has on record for it.\n"
        "• Embedded — is the image actually written into the MP3's ID3 tag.\n"
        "  This is the only thing Explorer and Android media players read.\n"
        "• Sidecar — the archival JPEG in the channel's hidden .artwork folder.\n"
        "• On Disk — whether that sidecar JPEG is still where the DB expects it.\n"
        "• Filter — narrow to tracks missing art, or to sidecars that have gone\n"
        "  missing off disk.\n"
        "• Select a row to preview the image; right-click for Open Image,\n"
        "  Open Containing Folder, Copy Image Path, or Copy Thumbnail URL.\n"
        "• 🖼 Fetch Missing Artwork — find and embed art for every track that\n"
        "  has none.\n"
        "\n"
        "Column widths and order are remembered between sessions."
    )

    def _build_ui(self):
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        dl_tab  = tk.Frame(self._notebook, bg=BG)
        wl_tab  = tk.Frame(self._notebook, bg=BG)
        art_tab = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(dl_tab,  text="   ⬇  Downloads   ")
        self._notebook.add(wl_tab,  text="   👁  Watch List   ")
        self._notebook.add(art_tab, text="   🖼  Artwork   ")

        self._build_downloads_tab(dl_tab)
        self._build_watchlist_tab(wl_tab)
        self._build_artwork_tab(art_tab)

        # Help button floats over the right end of the tab strip. place() on
        # the Toplevel + lift() puts it above the notebook's chrome at the
        # absolute right of the window, on the same row as the tabs.
        self._help_btn = tk.Button(
            self, text="❔  Help",
            font=("Segoe UI", 9), relief="flat", bd=0,
            bg=SURFACE2, fg=TEXT_DIM,
            activebackground=BORDER, activeforeground=TEXT,
            padx=10, pady=4, cursor="hand2",
            command=lambda: None)
        self._help_btn.place(relx=1.0, y=2, x=-8, anchor="ne")
        self._help_btn.lift()
        Tooltip(self._help_btn, self._HELP_TOOLTIP, wraplength=420)

    def _tb_btn(self, parent, label, cmd, side="left", padx=(2, 2)):
        """Small flat toolbar button matching the app palette."""
        b = tk.Button(parent, text=label, font=("Segoe UI", 9),
                      relief="flat", bd=0, bg=SURFACE2, fg=TEXT_DIM,
                      activebackground=BORDER, activeforeground=TEXT,
                      padx=8, pady=4, cursor="hand2", command=cmd)
        b.pack(side=side, padx=padx, pady=6)
        return b

    def _mk_combo(self, parent, var, width):
        c = ttk.Combobox(parent, textvariable=var, state="readonly",
                         width=width)
        c.pack(side="left", padx=(0, 10), pady=6)
        return c

    # ── Downloads tab ───────────────────────────────────────────────────────
    def _build_downloads_tab(self, parent):
        toolbar = tk.Frame(parent, bg=SURFACE2,
                           highlightthickness=1, highlightbackground=BORDER)
        toolbar.pack(fill="x", side="top")

        tk.Label(toolbar, text="Group by:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(12, 6))
        self._group_combo = self._mk_combo(toolbar, self._group_var, 22)
        self._group_combo["values"] = list(self.GROUP_PRESETS)
        self._group_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._rebuild_downloads_tree())

        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="left", fill="y",
                                                   padx=8, pady=6)

        tk.Label(toolbar, text="Platform:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))
        self._plat_combo = self._mk_combo(toolbar, self._plat_var, 12)
        self._plat_combo.bind("<<ComboboxSelected>>",
                              lambda e: self._rebuild_downloads_tree())

        tk.Label(toolbar, text="Genre:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))
        self._genre_combo = self._mk_combo(toolbar, self._genre_var, 16)
        self._genre_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._rebuild_downloads_tree())

        tk.Label(toolbar, text="Search:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))
        self._search_entry = tk.Entry(
            toolbar, textvariable=self._search_var, font=("Segoe UI", 9),
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=YT_RED, width=18)
        self._search_entry.pack(side="left", ipady=3, pady=6)
        self._search_var.trace_add("write",
                                   lambda *_: self._rebuild_downloads_tree())

        # Right cluster: actions
        self._tb_btn(toolbar, "⤓  Export CSV", self._export_csv, side="right",
                     padx=(0, 10))
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="right", fill="y",
                                                   padx=4, pady=6)
        self._tb_btn(toolbar, "⟳  Refresh", self.refresh, side="right")
        _collapse_btn = self._tb_btn(toolbar, "⊟", self._collapse_all,
                                     side="right")
        _expand_btn = self._tb_btn(toolbar, "⊞", self._expand_all,
                                   side="right")
        Tooltip(_collapse_btn, "Collapse all groups")
        Tooltip(_expand_btn, "Expand all groups")

        # ── Stats bar (bottom) ────────────────────────────────────────────────
        self._dl_stats = tk.Label(parent, text="", font=("Segoe UI", 8),
                                  fg=TEXT_DIM, bg=SURFACE2, anchor="w",
                                  padx=12, pady=3, highlightthickness=1,
                                  highlightbackground=BORDER)
        self._dl_stats.pack(fill="x", side="bottom")

        # ── Tree ──────────────────────────────────────────────────────────────
        # 1px light-grey outline around the grid so the whole entry area reads as
        # a framed box.
        tree_frame = tk.Frame(parent, bg=BG,
                              highlightthickness=1, highlightbackground=DB_GRID)
        tree_frame.pack(fill="both", expand=True)

        col_ids = list(self._DL_COLS)
        self._dl_tree = ttk.Treeview(
            tree_frame, columns=col_ids, show="tree headings",
            style="DB.Treeview", selectmode="browse")
        # stretch=False on every column (incl. #0) so dragging one header never
        # rebalances the others — columns keep their width and run off-screen,
        # where the horizontal scrollbar takes over. Saved widths win over the
        # built-in defaults.
        saved = self._saved_col_widths(self._DL_WIDTH_KEY)
        self._dl_tree.heading("#0", text="Title  /  Group",
                              command=lambda: self._sort_downloads("title"))
        self._dl_tree.column("#0", width=saved.get("#0", 340), minwidth=180,
                             anchor="w", stretch=False)
        for cid, (head, width, anchor) in self._DL_COLS.items():
            self._dl_tree.heading(
                cid, text=head, command=lambda c=cid: self._sort_downloads(c))
            self._dl_tree.column(cid, width=saved.get(cid, width), minwidth=50,
                                 anchor=anchor, stretch=False)

        self._dl_tree.tag_configure("group", foreground=TEXT,
                                    font=("Segoe UI", 10, "bold"))
        self._dl_tree.tag_configure("leaf", foreground=TEXT_MED)
        # Zebra striping makes each leaf entry its own visible box. The stripe
        # tags set ONLY background, so they compose with group/leaf (which set
        # only fg/font) without conflict. Re-applied on expand/collapse since
        # that changes which rows are visible.
        self._dl_tree.tag_configure("oddrow", background=DB_STRIPE)
        self._dl_tree.tag_configure("evenrow", background=DB_FIELD)
        self._dl_tree.bind("<<TreeviewOpen>>",
                           lambda e: self.after(1, self._restripe_dl_tree))
        self._dl_tree.bind("<<TreeviewClose>>",
                           lambda e: self.after(1, self._restripe_dl_tree))

        # Drag a header to reorder columns; restore any saved order.
        self._apply_saved_order(self._dl_tree, col_ids, self._DL_ORDER_KEY)
        self._enable_col_reorder(self._dl_tree, col_ids, self._DL_ORDER_KEY)

        vs = ttk.Scrollbar(tree_frame, orient="vertical",
                           command=self._dl_tree.yview)
        hs = ttk.Scrollbar(tree_frame, orient="horizontal",
                           command=self._dl_tree.xview)
        self._dl_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self._dl_tree.pack(side="left", fill="both", expand=True)

        self._dl_tree.bind("<Double-1>", self._on_dl_double_click)
        self._dl_tree.bind("<Button-3>", self._on_dl_right_click)
        self._bind_tree_wheel(self._dl_tree)

        # Context menu for leaf rows
        self._dl_menu = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                                activebackground=YT_DARK,
                                activeforeground="#ffffff", bd=0)
        self._dl_menu.add_command(label="Open File",
                                  command=lambda: self._ctx_action("file"))
        self._dl_menu.add_command(label="Open Containing Folder",
                                  command=lambda: self._ctx_action("folder"))
        self._dl_menu.add_command(label="Copy Path",
                                  command=lambda: self._ctx_action("copy"))
        self._ctx_item = None

    # ── Watch List tab ────────────────────────────────────────────────────────
    def _build_watchlist_tab(self, parent):
        bar = tk.Frame(parent, bg=SURFACE2, highlightthickness=1,
                       highlightbackground=BORDER)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="Watched channels — click a column to sort",
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=SURFACE2
                 ).pack(side="left", padx=12, pady=8)
        self._tb_btn(bar, "⟳  Refresh", self.refresh, side="right")
        clean_btn = self._tb_btn(
            bar, "🧹  Folders Cleanup ‹Smart›",
            self._start_folders_cleanup, side="right")
        Tooltip(clean_btn,
                "Scans each ticked channel's live YouTube/SoundCloud listing, "
                "then flags downloaded tracks in that channel's folder that no "
                "longer appear on the channel. You review and confirm every "
                "deletion per channel before anything is removed (files go to "
                "the Recycle Bin).", wraplength=360)

        # Folders Cleanup selection state: cid -> bool (checked).
        self._wl_checked = {}
        # cid -> (eligible: bool, reason: str) for the cleanup checkbox.
        self._wl_eligible = {}

        self._wl_stats = tk.Label(parent, text="", font=("Segoe UI", 8),
                                  fg=TEXT_DIM, bg=SURFACE2, anchor="w",
                                  padx=12, pady=3, highlightthickness=1,
                                  highlightbackground=BORDER)
        self._wl_stats.pack(fill="x", side="bottom")

        frame = tk.Frame(parent, bg=BG,
                         highlightthickness=1, highlightbackground=DB_GRID)
        frame.pack(fill="both", expand=True)

        col_ids = list(self._WL_COLS)
        self._wl_tree = ttk.Treeview(
            frame, columns=col_ids, show="headings",
            style="DB.Treeview", selectmode="browse")
        # stretch=False + a horizontal scrollbar: resizing one header keeps the
        # other columns put and lets the row run off-screen. Saved widths win.
        saved = self._saved_col_widths(self._WL_WIDTH_KEY)
        for cid, (head, width, anchor) in self._WL_COLS.items():
            self._wl_tree.heading(
                cid, text=head, command=lambda c=cid: self._sort_watchlist(c))
            self._wl_tree.column(cid, width=saved.get(cid, width), minwidth=50,
                                 anchor=anchor, stretch=False)

        # Zebra striping so each channel entry reads as its own box.
        self._wl_tree.tag_configure("oddrow", background=DB_STRIPE)
        self._wl_tree.tag_configure("evenrow", background=DB_FIELD)
        self._wl_tree.tag_configure("wl_disabled", foreground=TEXT_DIM)

        vs = ttk.Scrollbar(frame, orient="vertical",
                           command=self._wl_tree.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal",
                           command=self._wl_tree.xview)
        self._wl_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self._wl_tree.pack(side="left", fill="both", expand=True)

        # Drag a header to reorder columns; restore any saved order. The "sel"
        # checkbox column is pinned to position 0 — never reorderable.
        self._apply_saved_order(self._wl_tree, col_ids, self._WL_ORDER_KEY)
        # Force sel back to the front in case a saved order or default placed it
        # elsewhere (older configs predate the column).
        disp = [c for c in self._display_order(self._wl_tree, col_ids)
                if c != "sel"]
        self._wl_tree.configure(displaycolumns=["sel"] + disp)
        self._enable_col_reorder(self._wl_tree, col_ids, self._WL_ORDER_KEY,
                                 pinned=("sel",))

        self._bind_tree_wheel(self._wl_tree)

        # Right-click the Link column to open or copy a channel's URL.
        self._wl_menu = tk.Menu(self._wl_tree, tearoff=0)
        self._wl_tree.bind("<Button-3>", self._on_wl_right_click)

        # Left-click toggles the cleanup checkbox; hover shows why a disabled
        # row can't be ticked.
        self._wl_tree.bind("<Button-1>", self._on_wl_left_click, add="+")
        self._wl_tree.bind("<Motion>", self._on_wl_motion, add="+")
        self._wl_tree.bind("<Leave>",
                           lambda _e: self._hide_wl_celltip(), add="+")
        self._wl_celltip = None   # transient tooltip Toplevel for disabled cells
        self._wl_celltip_lbl = None

    # ── Artwork tab ───────────────────────────────────────────────────────────
    # A second view over the same downloads rows, keyed on their cover art. The
    # DB records three artwork facts per track (sidecar path, embedded flag,
    # source thumbnail URL); this tab surfaces them and flags the two states
    # that need attention — no art at all, and a sidecar the DB points at that
    # has since been deleted off disk.

    # Preview box edge, in pixels. Square: art is centre-cropped to 1:1 in the
    # default mode, and an 'original' 16:9 image letterboxes inside it.
    _ART_PREVIEW_PX = 260

    def _build_artwork_tab(self, parent):
        toolbar = tk.Frame(parent, bg=SURFACE2,
                           highlightthickness=1, highlightbackground=BORDER)
        toolbar.pack(fill="x", side="top")

        tk.Label(toolbar, text="Filter:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(12, 6))
        self._art_filter_combo = self._mk_combo(toolbar, self._art_filter_var, 22)
        self._art_filter_combo["values"] = list(self._ART_FILTERS)
        self._art_filter_combo.bind("<<ComboboxSelected>>",
                                    lambda e: self._rebuild_artwork_tree())

        tk.Label(toolbar, text="Search:", font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=SURFACE2).pack(side="left", padx=(0, 6))
        self._art_search_entry = tk.Entry(
            toolbar, textvariable=self._art_search_var, font=("Segoe UI", 9),
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=YT_RED, width=18)
        self._art_search_entry.pack(side="left", ipady=3, pady=6)
        self._art_search_var.trace_add("write",
                                       lambda *_: self._rebuild_artwork_tree())

        self._tb_btn(toolbar, "⟳  Refresh", self.refresh, side="right",
                     padx=(0, 10))
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="right", fill="y",
                                                   padx=4, pady=6)
        fetch_btn = self._tb_btn(toolbar, "🖼  Fetch Missing Artwork",
                                 self._art_fetch_missing, side="right")
        Tooltip(fetch_btn,
                "Finds cover art for every track that has none and embeds it "
                "into the file. Uses the recorded thumbnail URL, an existing "
                "sidecar, or the source page — whichever is cheapest. Cancel "
                "any time; tracks already done are kept.", wraplength=360)

        self._art_stats = tk.Label(parent, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=SURFACE2, anchor="w",
                                   padx=12, pady=3, highlightthickness=1,
                                   highlightbackground=BORDER)
        self._art_stats.pack(fill="x", side="bottom")

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True)

        # ── Preview pane (right) ──────────────────────────────────────────────
        # Packed before the tree so it keeps its width when the window shrinks;
        # the tree is the elastic half.
        side = tk.Frame(body, bg=SURFACE2, width=self._ART_PREVIEW_PX + 24,
                        highlightthickness=1, highlightbackground=BORDER)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        tk.Label(side, text="PREVIEW", font=("Segoe UI", 8, "bold"),
                 fg=TEXT_DIM, bg=SURFACE2).pack(anchor="w", padx=12, pady=(10, 6))

        self._art_canvas = tk.Label(
            side, bg=DB_FIELD, fg=TEXT_DIM, font=("Segoe UI", 9),
            text="Select a track", width=self._ART_PREVIEW_PX,
            height=self._ART_PREVIEW_PX, highlightthickness=1,
            highlightbackground=DB_GRID)
        self._art_canvas.pack(padx=12)
        self._art_canvas.pack_propagate(False)

        self._art_caption = tk.Label(
            side, text="", font=("Segoe UI", 8), fg=TEXT_DIM, bg=SURFACE2,
            justify="left", anchor="nw", wraplength=self._ART_PREVIEW_PX)
        self._art_caption.pack(fill="x", padx=12, pady=(8, 12))

        # ── Tree (left) ───────────────────────────────────────────────────────
        frame = tk.Frame(body, bg=BG,
                         highlightthickness=1, highlightbackground=DB_GRID)
        frame.pack(side="left", fill="both", expand=True)

        col_ids = list(self._ART_COLS)
        self._art_tree = ttk.Treeview(
            frame, columns=col_ids, show="headings",
            style="DB.Treeview", selectmode="browse")
        saved = self._saved_col_widths(self._ART_WIDTH_KEY)
        for cid, (head, width, anchor) in self._ART_COLS.items():
            self._art_tree.heading(
                cid, text=head, command=lambda c=cid: self._sort_artwork(c))
            self._art_tree.column(cid, width=saved.get(cid, width), minwidth=50,
                                  anchor=anchor, stretch=False)

        self._art_tree.tag_configure("oddrow", background=DB_STRIPE)
        self._art_tree.tag_configure("evenrow", background=DB_FIELD)
        # A row whose sidecar the DB points at but which is gone from disk — the
        # one state here that is actually broken rather than merely absent.
        self._art_tree.tag_configure("art_broken", foreground=YT_RED)
        self._art_tree.tag_configure("art_none", foreground=TEXT_DIM)

        vs = ttk.Scrollbar(frame, orient="vertical",
                           command=self._art_tree.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal",
                           command=self._art_tree.xview)
        self._art_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self._art_tree.pack(side="left", fill="both", expand=True)

        self._apply_saved_order(self._art_tree, col_ids, self._ART_ORDER_KEY)
        self._enable_col_reorder(self._art_tree, col_ids, self._ART_ORDER_KEY)
        self._bind_tree_wheel(self._art_tree)

        self._art_tree.bind("<<TreeviewSelect>>", self._on_art_select)
        self._art_tree.bind("<Double-1>", self._on_art_double_click)
        self._art_tree.bind("<Button-3>", self._on_art_right_click)

        self._art_menu = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                                 activebackground=YT_DARK,
                                 activeforeground="#ffffff", bd=0)
        self._art_menu.add_command(
            label="Open Image", command=lambda: self._art_ctx_action("image"))
        self._art_menu.add_command(
            label="Open Containing Folder",
            command=lambda: self._art_ctx_action("folder"))
        self._art_menu.add_separator()
        self._art_menu.add_command(
            label="Copy Image Path",
            command=lambda: self._art_ctx_action("copy_path"))
        self._art_menu.add_command(
            label="Copy Thumbnail URL",
            command=lambda: self._art_ctx_action("copy_url"))

    # ── Artwork row state ─────────────────────────────────────────────────────
    @staticmethod
    def _art_state(row):
        """Classify one download row's artwork into a state string.

        'embedded' — art is written into the MP3 (what players actually read).
        'sidecar'  — a sidecar JPEG is on record but nothing is embedded.
        'broken'   — the DB names a sidecar that is no longer on disk.
        'none'     — no artwork of any kind on record.

        Reads the filesystem (one isfile per row); cheap enough for a library of
        thousands and the only way to catch a sidecar deleted behind our back.
        """
        path = (row.get("artwork_path") or "").strip()
        embedded = bool(row.get("artwork_embedded"))
        on_disk = bool(path) and os.path.isfile(path)
        if path and not on_disk:
            return "broken"
        if embedded:
            return "embedded"
        if on_disk:
            return "sidecar"
        return "none"

    def _filtered_artwork(self):
        """Rows for the artwork tree as (row, state) pairs, filter + search
        applied."""
        choice = self._art_filter_var.get()
        needle = self._art_search_var.get().strip().lower()
        out = []
        for row in self._downloads:
            state = self._art_state(row)
            if choice == "Has artwork" and state == "none":
                continue
            if choice == "Missing artwork" and state != "none":
                continue
            # "Embedded" keys off the flag, not the state: a track whose sidecar
            # has gone missing still has its art inside the file, and hiding it
            # here would misreport that.
            if choice == "Embedded only" and not row.get("artwork_embedded"):
                continue
            if choice == "Sidecar missing on disk" and state != "broken":
                continue
            if needle:
                hay = (f"{row.get('title', '')} {row.get('channel_name', '')} "
                       f"{row.get('artwork_path', '') or ''}").lower()
                if needle not in hay:
                    continue
            out.append((row, state))
        return out

    def _art_sort_key(self, pair):
        row, state = pair
        col = self._art_sort_col
        if col == "channel":
            return (row.get("channel_name") or "").lower()
        if col == "platform":
            return (row.get("platform") or "").lower()
        if col == "embedded":
            return 1 if row.get("artwork_embedded") else 0
        if col == "sidecar":
            return os.path.basename(row.get("artwork_path") or "").lower()
        if col == "on_disk":
            return 0 if state in ("none", "broken") else 1
        if col == "thumb_url":
            return (row.get("thumbnail_url") or "").lower()
        return (row.get("title") or "").lower()

    def _rebuild_artwork_tree(self):
        tree = self._art_tree
        tree.delete(*tree.get_children())
        self._art_row_data.clear()
        self._art_clear_preview()

        pairs = sorted(self._filtered_artwork(), key=self._art_sort_key,
                       reverse=self._art_sort_desc)

        for i, (row, state) in enumerate(pairs):
            art_path = (row.get("artwork_path") or "").strip()
            url = (row.get("thumbnail_url") or "").strip()
            values = (
                row.get("title") or "(untitled)",
                row.get("channel_name") or "",
                row.get("platform") or "",
                "✔" if row.get("artwork_embedded") else "✘",
                os.path.basename(art_path) if art_path else "—",
                {"broken": "✘ gone", "none": "—"}.get(state, "✔"),
                url or "—",
            )
            tags = ["oddrow" if i % 2 else "evenrow"]
            if state == "broken":
                tags.append("art_broken")
            elif state == "none":
                tags.append("art_none")
            item = tree.insert("", "end", values=values, tags=tuple(tags))
            self._art_row_data[item] = row

        self._update_art_heading_arrows()
        self._update_art_stats(len(pairs))

    def _update_art_stats(self, shown):
        """Summarise the library's artwork health.

        'embedded' counts the flag directly rather than the exclusive state — a
        track whose sidecar was deleted still carries its art inside the file,
        and reporting it as un-embedded would understate what the user has.
        """
        total = len(self._downloads)
        embedded = broken = none = 0
        for row in self._downloads:
            if row.get("artwork_embedded"):
                embedded += 1
            state = self._art_state(row)
            if state == "broken":
                broken += 1
            elif state == "none":
                none += 1
        parts = [
            f"{total} track{'s' if total != 1 else ''}",
            f"{embedded} embedded",
            f"{none} without artwork",
        ]
        if broken:
            parts.append(f"{broken} sidecar{'s' if broken != 1 else ''} "
                         "missing on disk")
        if shown != total:
            parts.append(f"showing {shown}")
        self._art_stats.config(text="  ·  ".join(parts))

    def _sort_artwork(self, col):
        if self._art_sort_col == col:
            self._art_sort_desc = not self._art_sort_desc
        else:
            self._art_sort_col = col
            # Default direction: for the yes/no columns, show the interesting
            # (missing) rows first rather than burying them under the healthy ones.
            self._art_sort_desc = col not in ("embedded", "on_disk")
        self._rebuild_artwork_tree()

    def _update_art_heading_arrows(self):
        arrow = " ▼" if self._art_sort_desc else " ▲"
        for cid, (head, _w, _a) in self._ART_COLS.items():
            self._art_tree.heading(
                cid, text=head + (arrow if self._art_sort_col == cid else ""))

    # ── Artwork preview ───────────────────────────────────────────────────────
    def _art_clear_preview(self, message="Select a track"):
        self._art_preview_img = None
        self._art_canvas.config(image="", text=message)
        self._art_caption.config(text="")

    def _on_art_select(self, _event=None):
        sel = self._art_tree.selection()
        row = self._art_row_data.get(sel[0]) if sel else None
        if not row:
            self._art_clear_preview()
            return
        self._art_show_preview(row)

    def _art_show_preview(self, row):
        """Render the row's cover art into the preview box.

        Prefers the sidecar JPEG; falls back to the bytes embedded in the MP3 so
        a track whose sidecar was deleted still previews. Never raises — a
        preview that cannot be drawn degrades to an explanatory message.
        """
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._art_clear_preview("Preview needs Pillow")
            return

        art_path = (row.get("artwork_path") or "").strip()
        source = None
        note = ""
        if art_path and os.path.isfile(art_path):
            source = art_path
            note = os.path.basename(art_path)
        else:
            data = cb_artwork.extract_cover(row.get("file_path") or "")
            if data:
                source = io.BytesIO(data)
                note = ("embedded artwork (no sidecar on disk)" if art_path
                        else "embedded artwork")

        if source is None:
            self._art_clear_preview(
                "Sidecar file is gone" if art_path else "No artwork")
            self._art_caption.config(text=art_path or "")
            return

        try:
            with Image.open(source) as im:
                px = self._ART_PREVIEW_PX
                size = f"{im.width}×{im.height}"
                im = im.convert("RGB")
                im.thumbnail((px, px), Image.LANCZOS)
                photo = ImageTk.PhotoImage(im)
        except Exception:
            self._art_clear_preview("Image could not be read")
            return

        self._art_preview_img = photo    # keep a reference or Tk drops it
        self._art_canvas.config(image=photo, text="")
        self._art_caption.config(text=f"{note}\n{size}" if note else size)

    # ── Artwork actions ───────────────────────────────────────────────────────
    def _on_art_double_click(self, event):
        item = self._art_tree.identify_row(event.y)
        row = self._art_row_data.get(item)
        if row:
            self._art_open_image(row)

    def _on_art_right_click(self, event):
        item = self._art_tree.identify_row(event.y)
        if not item or item not in self._art_row_data:
            return
        self._art_tree.selection_set(item)
        self._art_ctx_item = item
        try:
            self._art_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._art_menu.grab_release()

    def _art_open_image(self, row):
        path = (row.get("artwork_path") or "").strip()
        if not path:
            messagebox.showinfo(
                "No Artwork",
                "This track has no cover art on record.\n\nRun ‹Fetch Missing "
                "Artwork› to try to find some.", parent=self)
            return
        self._open_path(path)

    def _art_ctx_action(self, what):
        row = self._art_row_data.get(self._art_ctx_item)
        if not row:
            return
        art_path = (row.get("artwork_path") or "").strip()
        if what == "image":
            self._art_open_image(row)
        elif what == "folder":
            # No sidecar on record — fall back to revealing the track itself, so
            # the menu item still does something useful for an art-less row.
            self._reveal_path(art_path or (row.get("file_path") or ""))
        elif what == "copy_path":
            self.clipboard_clear()
            self.clipboard_append(art_path)
        elif what == "copy_url":
            self.clipboard_clear()
            self.clipboard_append((row.get("thumbnail_url") or "").strip())

    def _art_fetch_missing(self):
        """Hand off to the App's backfill, then reload so the tab reflects it.

        The session runs on a background thread against a modal progress dialog,
        so we cannot simply refresh on return — we poll for it to finish and
        reload once it does.
        """
        app = self._parent
        app._fetch_missing_artwork()
        sess = getattr(app, "_artwork_session", None)
        if sess is None:
            return          # nothing to do, or the user cancelled the prompt

        def poll():
            if getattr(app, "_artwork_session", None) is None:
                if self.winfo_exists():
                    self.load_data()
                return
            self.after(500, poll)

        self.after(500, poll)

    # ── Link / Folder column helpers ──────────────────────────────────────────
    @staticmethod
    def _wl_display_url(ch):
        """The channel's real URL, or '' for unresolved/sentinel placeholders."""
        url = (ch.get("url") or "").strip()
        if not url or url.startswith(UNRESOLVED_URL_PREFIX):
            return ""
        return url

    def _wl_channel_folder(self, ch):
        """Compute the local folder path for a watch-list channel — pure, no
        side effects (the App's _resolve_save_dir helper would makedirs).
        Returns '' if the channel's platform isn't recognised or the parent
        app isn't reachable. Note: the path may not exist on disk yet
        (channel with no downloads); callers must handle that."""
        try:
            platform     = (ch.get("platform") or "").strip()
            genre        = (ch.get("genre") or "").strip()
            channel_name = (ch.get("display_name") or "").strip()
            if platform not in PLATFORMS:
                return ""
            parts = [self._parent._platform_dir(platform)]
            parts.append(genre if genre and genre != "(none)" else "_No Genre")
            if channel_name:
                safe = safe_filename(channel_name, strip=True)
                if safe:
                    parts.append(safe)
            return os.path.join(*parts)
        except Exception:
            return ""

    def _wl_cleanup_eligibility(self, ch):
        """Return (eligible, reason) for whether a channel can be cleaned.
        Ineligible when unresolved/error, folder missing, or folder empty."""
        if is_unresolved_channel(ch) or ch.get("status") in (
                "needs_resolve", "error"):
            return (False, "Unresolved — fix the channel link first.")
        folder = self._wl_channel_folder(ch)
        if not folder or not os.path.isdir(folder):
            return (False, "Folder missing — no downloads to clean.")
        try:
            has_mp3 = any(f.lower().endswith(".mp3") for f in os.listdir(folder))
        except OSError:
            has_mp3 = False
        if not has_mp3:
            return (False, "Folder empty — nothing to clean.")
        return (True, "")

    def _wl_open_folder(self, folder):
        """Open *folder* in the system file manager; report errors politely
        (the folder may not exist yet if the channel hasn't downloaded)."""
        if not folder:
            messagebox.showerror(
                "Folder Unknown",
                "Couldn't compute a folder path for this channel "
                "(missing platform or display name).")
            return
        if not os.path.isdir(folder):
            messagebox.showerror(
                "Folder Not Found",
                f"This channel's folder doesn't exist yet:\n\n{folder}\n\n"
                "Download at least one track for the channel to create it.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Folder",
                f"Unable to open the folder:\n{exc}\n\nPath: {folder}")

    def _on_wl_right_click(self, event):
        row = self._wl_tree.identify_row(event.y)
        if not row:
            return
        self._wl_tree.selection_set(row)
        url    = self._wl_tree.set(row, "link").strip()
        folder = self._wl_tree.set(row, "folder").strip()
        self._wl_menu.delete(0, "end")
        if url:
            self._wl_menu.add_command(
                label="Open link in browser",
                command=lambda u=url: webbrowser.open(u))
            self._wl_menu.add_command(
                label="Copy link",
                command=lambda u=url: self._wl_copy_link(u))
        else:
            self._wl_menu.add_command(
                label="(no link for this channel)", state="disabled")
        self._wl_menu.add_separator()
        self._wl_menu.add_command(
            label="Open Folder",
            command=lambda f=folder: self._wl_open_folder(f),
            state="normal" if folder else "disabled")
        self._wl_menu.tk_popup(event.x_root, event.y_root)

    def _on_wl_left_click(self, event):
        """Toggle the cleanup checkbox when the 'sel' cell is clicked. Other
        clicks fall through to normal selection/sort behaviour."""
        if self._wl_tree.identify_region(event.x, event.y) != "cell":
            return
        if self._wl_tree.identify_column(event.x) != "#1":
            return                     # sel is always display-column #1
        row = self._wl_tree.identify_row(event.y)
        if not row:
            return
        try:
            cid = int(row)
        except (TypeError, ValueError):
            return
        eligible, _reason = self._wl_eligible.get(cid, (False, ""))
        if not eligible:
            return                     # disabled — ignore the click
        self._wl_checked[cid] = not self._wl_checked.get(cid, False)
        self._wl_tree.set(
            row, "sel", "☑" if self._wl_checked[cid] else "☐")
        return "break"                 # don't also start a drag-select

    def _on_wl_motion(self, event):
        """Show a small tooltip explaining why a disabled checkbox can't be
        ticked, when hovering the 'sel' cell of an ineligible row."""
        if (self._wl_tree.identify_region(event.x, event.y) != "cell"
                or self._wl_tree.identify_column(event.x) != "#1"):
            self._hide_wl_celltip()
            return
        row = self._wl_tree.identify_row(event.y)
        try:
            cid = int(row)
        except (TypeError, ValueError):
            self._hide_wl_celltip()
            return
        # Unknown cid is a can't-happen fallback (rebuild always populates
        # _wl_eligible for every visible row); default to ineligible/no-tip.
        eligible, reason = self._wl_eligible.get(cid, (False, ""))
        if eligible or not reason:
            self._hide_wl_celltip()
            return
        self._show_wl_celltip(event.x_root + 12, event.y_root + 12, reason)

    def _show_wl_celltip(self, x, y, text):
        if self._wl_celltip is not None:
            self._wl_celltip_lbl.config(text=text)
            self._wl_celltip.geometry(f"+{x}+{y}")
            return
        tip = tk.Toplevel(self._wl_tree)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        lbl = tk.Label(tip, text=text, font=("Segoe UI", 8),
                       bg="#1f1f1f", fg=TEXT, bd=1, relief="solid",
                       padx=6, pady=3, justify="left")
        lbl.pack()
        tip.geometry(f"+{x}+{y}")
        self._wl_celltip = tip
        self._wl_celltip_lbl = lbl

    def _hide_wl_celltip(self):
        if getattr(self, "_wl_celltip", None) is not None:
            self._wl_celltip.destroy()
            self._wl_celltip = None

    def _start_folders_cleanup(self):
        """Entry point for the Folders Cleanup ‹Smart› button. Validates that
        at least one eligible channel is ticked, then launches the run."""
        checked = [cid for cid, on in self._wl_checked.items() if on]
        if not checked:
            messagebox.showinfo(
                "Folders Cleanup",
                "Tick at least one channel first.", parent=self)
            return
        self._run_folders_cleanup(checked)

    def _run_folders_cleanup(self, cids):
        existing = getattr(self, "_cleanup_session", None)
        if existing is not None and not existing._done:
            return                      # a cleanup run is already in flight
        self._cleanup_session = _FoldersCleanupSession(self, cids)
        self._cleanup_session.start()

    def _open_cleanup_review(self, session, ch, flagged, folder_count):
        win = _CleanupReviewWindow(self, session, ch, flagged, folder_count)
        self.wait_window(win)           # modal: block until the user chooses
        if not self.winfo_exists():     # viewer torn down while modal was open
            session.cancelled = True
            session._finish()
            return
        if win.result == "confirm":
            self._apply_cleanup_deletions(session, ch, win.selected,
                                          folder_count)
            session._advance()
        elif win.result == "skip":
            session._log_channel(ch, removed=0, kept=folder_count, errors=0,
                                 note="skipped by user")
            session.channels_skipped += 1
            session._advance()
        else:  # cancel
            session.cancelled = True
            session._finish()

    def _apply_cleanup_deletions(self, session, ch, paths, folder_count):
        """Send the selected files to the Recycle Bin and drop their DB rows."""
        if not paths:
            session._log_channel(ch, removed=0, kept=folder_count, errors=0,
                                 note="confirmed, nothing ticked")
            session.channels_cleaned += 1
            return
        try:
            from send2trash import send2trash
        except Exception:
            messagebox.showerror(
                "Folders Cleanup",
                "This feature needs the 'send2trash' package.\n\n"
                "Install it with:  pip install send2trash",
                parent=self)
            session._log_channel(ch, removed=0, kept=folder_count, errors=0,
                                 note="aborted (send2trash missing)")
            return
        trashed, errors = partition_trash(paths, send2trash)
        for p in trashed:
            self._parent._dbg.info(f"CLEANUP TRASH | {p}")
        for p, exc in errors:
            self._parent._dbg.info(f"CLEANUP TRASH FAIL | {p} | {exc}")
        removed_rows = self._db.delete_downloads_by_paths(trashed)
        self._parent._dbg.info(
            f"CLEANUP DB | removed {removed_rows} download row(s)")
        session.removed_total += len(trashed)
        session.channels_cleaned += 1
        # Errored files remain on disk but are reported under `errors`, not
        # `kept`, so removed + kept + errors == folder_count in the log line.
        session._log_channel(
            ch, removed=len(trashed),
            kept=folder_count - len(trashed) - len(errors), errors=len(errors))

    def _finish_folders_cleanup(self, session):
        skipped = session.channels_skipped
        extra = (f" {skipped} channel(s) skipped (see activity log)."
                 if skipped else "")
        messagebox.showinfo(
            "Folders Cleanup complete",
            f"{session.removed_total} file(s) removed across "
            f"{session.channels_cleaned} channel(s).{extra}",
            parent=self)
        self._wl_checked.clear()        # auto-untick
        self.load_data()                # refresh tree from disk/DB reality

    def _wl_copy_link(self, url):
        self.clipboard_clear()
        self.clipboard_append(url)

    # ── Data loading ────────────────────────────────────────────────────────
    def load_data(self):
        """Load both datasets from the DB and (re)populate the views."""
        try:
            self._downloads = self._db.get_all_downloads()
        except Exception:
            self._downloads = []
        self._backfill_missing_timestamps()
        try:
            self._channels = self._db.get_all_watchlist_channels()
        except Exception:
            self._channels = []

        # Refresh filter dropdowns from the data
        plats  = sorted({(d.get("platform") or "").strip()
                         for d in self._downloads if d.get("platform")})
        genres = sorted({self._genre_value(d) for d in self._downloads})
        self._plat_combo["values"]  = ["All platforms"] + plats
        self._genre_combo["values"] = ["All genres"] + genres
        if self._plat_var.get() not in self._plat_combo["values"]:
            self._plat_var.set("All platforms")
        if self._genre_var.get() not in self._genre_combo["values"]:
            self._genre_var.set("All genres")

        self._rebuild_downloads_tree()
        self._rebuild_watchlist_tree()
        self._rebuild_artwork_tree()

    def _backfill_missing_timestamps(self):
        """Tracks imported before the database feature existed have no download
        timestamp (stored as 0). Fill each in from the file's creation time on
        disk and persist it, so the 'Downloaded' column and its sort are
        meaningful. Rows whose file is gone are left blank."""
        updates = []
        for d in self._downloads:
            try:
                ts = int(d.get("download_timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts > 0:
                continue
            path = d.get("file_path") or ""
            if not path or not os.path.exists(path):
                continue
            try:
                ctime = int(os.path.getctime(path))
            except OSError:
                continue
            if ctime <= 0:
                continue
            d["download_timestamp"] = ctime          # update in-memory view
            if d.get("id") is not None:
                updates.append((ctime, d["id"]))      # and persist it
        if updates:
            try:
                self._db.backfill_missing_download_timestamps(updates)
            except Exception:
                pass   # display still works from the in-memory fill above

    def refresh(self):
        self.load_data()

    # ── Grouping helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _genre_value(row):
        g = (row.get("genre") or "").strip()
        return g if g and g != "(none)" else "(none)"

    def _group_value(self, row, key):
        if key == "platform":
            return (row.get("platform") or "").strip() or "(unknown)"
        if key == "genre":
            return self._genre_value(row)
        if key == "channel_name":
            return (row.get("channel_name") or "").strip() or "(unknown)"
        return "(unknown)"

    def _leaf_sort_key(self, row):
        """Return the sort key for one download row under the active column."""
        col = self._dl_sort_col
        if col == "title":
            return (row.get("title") or "").lower()
        if col == "channel":
            return (row.get("channel_name") or "").lower()
        if col == "genre":
            return self._genre_value(row).lower()
        if col == "platform":
            return (row.get("platform") or "").lower()
        if col == "upload":
            return row.get("upload_date") or ""
        if col == "downloaded":
            return int(row.get("download_timestamp") or 0)
        if col == "bitrate":
            digits = re.sub(r"\D", "", str(row.get("bitrate") or ""))
            return int(digits) if digits else 0
        return (row.get("title") or "").lower()

    # ── Filtering ─────────────────────────────────────────────────────────────
    def _filtered_downloads(self):
        plat   = self._plat_var.get()
        genre  = self._genre_var.get()
        needle = self._search_var.get().strip().lower()
        out = []
        for d in self._downloads:
            if plat != "All platforms" and (d.get("platform") or "") != plat:
                continue
            if genre != "All genres" and self._genre_value(d) != genre:
                continue
            if needle:
                hay = f"{d.get('title','')} {d.get('channel_name','')}".lower()
                if needle not in hay:
                    continue
            out.append(d)
        return out

    # ── Downloads tree build ──────────────────────────────────────────────────
    def _rebuild_downloads_tree(self):
        tree = self._dl_tree
        tree.delete(*tree.get_children())
        self._row_data.clear()

        rows     = self._filtered_downloads()
        hierarchy = self.GROUP_PRESETS[self._group_var.get()]
        self._insert_group(parent="", rows=rows, keys=hierarchy)

        # Stats
        n_shown = len(rows)
        n_total = len(self._downloads)
        chans   = len({(d.get("channel_name") or "") for d in rows})
        gens    = len({self._genre_value(d) for d in rows})
        plats   = len({(d.get("platform") or "") for d in rows})
        self._dl_stats.config(
            text=f"  Showing {n_shown} of {n_total} tracks   •   "
                 f"{chans} channel{'s' if chans != 1 else ''}   •   "
                 f"{gens} genre{'s' if gens != 1 else ''}   •   "
                 f"{plats} platform{'s' if plats != 1 else ''}")
        self._update_dl_heading_arrows()
        self._restripe_dl_tree()

    def _restripe_dl_tree(self):
        """Re-apply zebra striping to the currently-visible leaf rows. Walks the
        tree in display order, alternating the stripe tag on leaves only (group
        headers keep the base field colour)."""
        tree = self._dl_tree
        n = [0]

        def walk(node):
            for item in tree.get_children(node):
                tags = tree.item(item, "tags")
                if "leaf" in tags:
                    stripe = "oddrow" if n[0] % 2 else "evenrow"
                    tree.item(item, tags=("leaf", stripe))
                    n[0] += 1
                if tree.get_children(item) and \
                        self.tk.getboolean(tree.item(item, "open")):
                    walk(item)

        walk("")

    def _bind_tree_wheel(self, tree):
        """Scroll *tree* with the mouse wheel and STOP the event here ("break").
        The main window installs an application-wide <MouseWheel> binding (Tk's
        'all' bindtag reaches every window in the process); without this, wheel
        scrolling inside this viewer would also scroll the primary app behind
        it. We do the scroll ourselves so breaking the chain costs nothing."""
        def _on_wheel(e):
            tree.yview_scroll(int(-1 * (_wheel_delta(e) / 120)), "units")
            return "break"
        _bind_wheel(tree, _on_wheel)

    def _insert_group(self, parent, rows, keys):
        """Recursively insert grouped nodes; leaves at the deepest level."""
        if not keys:
            for row in sorted(rows, key=self._leaf_sort_key,
                              reverse=self._dl_sort_desc):
                self._insert_leaf(parent, row)
            return

        key, rest = keys[0], keys[1:]
        buckets = {}
        for row in rows:
            buckets.setdefault(self._group_value(row, key), []).append(row)

        for label in sorted(buckets, key=str.lower):
            members = buckets[label]
            node = self._dl_tree.insert(
                parent, "end", text=f"{label}  ({len(members)})",
                values=("", "", "", "", "", ""), tags=("group",), open=False)
            self._insert_group(node, members, rest)

    def _insert_leaf(self, parent, row):
        ts = row.get("download_timestamp")
        try:
            dl_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") \
                     if ts else ""
        except Exception:
            dl_str = ""
        up = row.get("upload_date") or ""
        up_str = format_yyyymmdd_readable(up) if up else ""
        values = (
            row.get("channel_name") or "",
            self._genre_value(row),
            row.get("platform") or "",
            up_str,
            dl_str,
            row.get("bitrate") or "",
        )
        item = self._dl_tree.insert(
            parent, "end", text=row.get("title") or "(untitled)",
            values=values, tags=("leaf",))
        self._row_data[item] = row

    # ── Sorting ───────────────────────────────────────────────────────────────
    def _sort_downloads(self, col):
        if self._dl_sort_col == col:
            self._dl_sort_desc = not self._dl_sort_desc
        else:
            self._dl_sort_col = col
            # Sensible default direction: newest/largest first for time/number.
            self._dl_sort_desc = col in ("downloaded", "upload", "bitrate")
        self._rebuild_downloads_tree()

    def _update_dl_heading_arrows(self):
        arrow = " ▼" if self._dl_sort_desc else " ▲"
        self._dl_tree.heading(
            "#0", text="Title  /  Group" +
            (arrow if self._dl_sort_col == "title" else ""))
        for cid, (head, _w, _a) in self._DL_COLS.items():
            self._dl_tree.heading(
                cid, text=head + (arrow if self._dl_sort_col == cid else ""))

    # ── Open file / folder ────────────────────────────────────────────────────
    def _on_dl_double_click(self, event):
        item = self._dl_tree.identify_row(event.y)
        if item and item in self._row_data:
            self._reveal_path(self._row_data[item].get("file_path") or "")

    def _on_dl_right_click(self, event):
        item = self._dl_tree.identify_row(event.y)
        if item and item in self._row_data:
            self._dl_tree.selection_set(item)
            self._ctx_item = item
            try:
                self._dl_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._dl_menu.grab_release()

    def _ctx_action(self, what):
        row = self._row_data.get(self._ctx_item)
        if not row:
            return
        path = row.get("file_path") or ""
        if what == "copy":
            self.clipboard_clear()
            self.clipboard_append(path)
        elif what == "file":
            self._open_path(path)
        elif what == "folder":
            self._reveal_path(path)

    def _open_path(self, path):
        """Open the file itself with the OS default application."""
        if not path or not os.path.exists(path):
            messagebox.showinfo(
                "File Not Found",
                f"This file is no longer on disk:\n{path or '(no path recorded)'}",
                parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("Could Not Open File", str(exc), parent=self)

    def _reveal_path(self, path):
        """Open (and on Windows, select) the file in its containing folder.
        Falls back to the nearest existing parent directory."""
        if not path:
            messagebox.showinfo("No Path",
                                "No file path is recorded for this track.",
                                parent=self)
            return
        try:
            if sys.platform == "win32" and os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                return
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            while folder and not os.path.isdir(folder):
                parent = os.path.dirname(folder)
                if parent == folder:
                    break
                folder = parent
            if not folder or not os.path.isdir(folder):
                messagebox.showinfo(
                    "Folder Not Found",
                    f"The containing folder no longer exists:\n{path}",
                    parent=self)
                return
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("Could Not Open Folder", str(exc), parent=self)

    # ── Expand / collapse ─────────────────────────────────────────────────────
    def _set_open_recursive(self, item, is_open):
        for child in self._dl_tree.get_children(item):
            self._dl_tree.item(child, open=is_open)
            self._set_open_recursive(child, is_open)

    def _expand_all(self):
        self._set_open_recursive("", True)
        # Setting `open` programmatically doesn't fire <<TreeviewOpen>>, so the
        # stripe binding never runs — re-stripe the now-visible rows by hand.
        self._restripe_dl_tree()

    def _collapse_all(self):
        self._set_open_recursive("", False)
        self._restripe_dl_tree()

    # ── Export ────────────────────────────────────────────────────────────────
    def _export_csv(self):
        rows = self._filtered_downloads()
        if not rows:
            messagebox.showinfo("Nothing to Export",
                                "No tracks match the current filters.",
                                parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export downloads to CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="cratebuilder_downloads.csv")
        if not path:
            return
        import csv
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Title", "Channel", "Genre", "Platform",
                            "Upload date", "Downloaded", "Bitrate", "File path"])
                for d in rows:
                    ts = d.get("download_timestamp")
                    try:
                        dl_str = datetime.fromtimestamp(
                            int(ts)).strftime("%Y-%m-%d %H:%M") if ts else ""
                    except Exception:
                        dl_str = ""
                    w.writerow([
                        d.get("title") or "", d.get("channel_name") or "",
                        self._genre_value(d), d.get("platform") or "",
                        d.get("upload_date") or "", dl_str,
                        d.get("bitrate") or "", d.get("file_path") or "",
                    ])
            messagebox.showinfo(
                "Export Complete",
                f"Exported {len(rows)} track{'s' if len(rows) != 1 else ''} to:\n{path}",
                parent=self)
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self)

    # ── Watch List tree build ──────────────────────────────────────────────────
    def _wl_sort_key(self, ch):
        col = self._wl_sort_col
        if col == "channel":
            return (ch.get("display_name") or "").lower()
        if col == "link":
            return self._wl_display_url(ch).lower()
        if col == "folder":
            return self._wl_channel_folder(ch).lower()
        if col == "platform":
            return (ch.get("platform") or "").lower()
        if col == "genre":
            return (ch.get("genre") or "").lower()
        if col == "cutoff":
            return ch.get("scan_cutoff_date") or ""
        if col == "last_scan":
            return int(ch.get("last_scanned_timestamp") or 0)
        if col == "pending":
            return int(ch.get("pending_new_count") or 0)
        if col == "total":
            return int(ch.get("total_downloaded") or 0)
        if col == "status":
            return (ch.get("status") or "").lower()
        return (ch.get("display_name") or "").lower()

    def _rebuild_watchlist_tree(self):
        tree = self._wl_tree
        tree.delete(*tree.get_children())
        rows = sorted(self._channels, key=self._wl_sort_key,
                      reverse=self._wl_sort_desc)
        for i, ch in enumerate(rows):
            cutoff = format_yyyymmdd_readable(ch.get("scan_cutoff_date", "")) \
                     if ch.get("scan_cutoff_date") else ""
            last = format_timestamp_relative(ch.get("last_scanned_timestamp"))
            cid = ch.get("id")
            eligible, reason = self._wl_cleanup_eligibility(ch)
            self._wl_eligible[cid] = (eligible, reason)
            checked = self._wl_checked.get(cid, False) and eligible
            glyph = "☑" if checked else "☐"
            zebra = "oddrow" if i % 2 else "evenrow"
            tags = (zebra,) if eligible else (zebra, "wl_disabled")
            tree.insert("", "end", iid=str(cid), tags=tags,
                        values=(
                glyph,
                ch.get("display_name") or "",
                self._wl_display_url(ch),
                self._wl_channel_folder(ch),
                ch.get("platform") or "",
                ch.get("genre") or "",
                cutoff,
                last,
                ch.get("pending_new_count") or 0,
                ch.get("total_downloaded") or 0,
                ch.get("status") or "",
            ))
        # Prune Folders Cleanup state for channels no longer in the list, so a
        # deleted channel's stale checkbox/eligibility can't linger or be acted
        # on by the cleanup run.
        live_cids = {ch.get("id") for ch in rows}
        self._wl_checked = {c: v for c, v in self._wl_checked.items()
                            if c in live_cids}
        self._wl_eligible = {c: v for c, v in self._wl_eligible.items()
                             if c in live_cids}
        total_pending = sum(int(c.get("pending_new_count") or 0)
                            for c in self._channels)
        self._wl_stats.config(
            text=f"  {len(self._channels)} watched channel"
                 f"{'s' if len(self._channels) != 1 else ''}   •   "
                 f"{total_pending} pending new track"
                 f"{'s' if total_pending != 1 else ''}")
        self._update_wl_heading_arrows()

    def _sort_watchlist(self, col):
        if self._wl_sort_col == col:
            self._wl_sort_desc = not self._wl_sort_desc
        else:
            self._wl_sort_col = col
            self._wl_sort_desc = col in ("pending", "total", "last_scan")
        self._rebuild_watchlist_tree()

    def _update_wl_heading_arrows(self):
        arrow = " ▼" if self._wl_sort_desc else " ▲"
        for cid, (head, _w, _a) in self._WL_COLS.items():
            self._wl_tree.heading(
                cid, text=head + (arrow if self._wl_sort_col == cid else ""))


# ══════════════════════════════════════════════════════════════════════════════
# _ArtworkBackfillSession — bulk "fetch missing artwork" over the existing library
# ══════════════════════════════════════════════════════════════════════════════
class _ArtworkBackfillSession:
    """Walks every downloads row that has no cover art yet and tries to find it.

    Tracks grabbed before the cover-art feature existed have no APIC frame and
    no sidecar. This session resolves art for them through a ladder that spends
    the least possible network, stopping at the first rung that works:

      1. the `thumbnail_url` recorded on the row (present for anything
         downloaded since the feature landed),
      2. a sidecar JPEG already sitting in `.artwork/` — embed it, no network,
      3. for YouTube, the thumbnail rebuilt from the video id
         (maxresdefault, falling back to hqdefault),
      4. the source URL read back out of the track's own ID3 tag, handed to
         yt-dlp for a metadata lookup. This is the only rung that rescues a
         legacy SoundCloud track, whose art URL cannot be derived from its id.

    Runs on one background thread with a modal progress dialog. Every failure is
    counted and moved past — a track we cannot find art for is not an error, and
    a bad row must never abort the run.
    """

    # Pause between network fetches. A backfill can walk thousands of tracks;
    # this keeps a long run from looking like a scrape to either platform.
    _FETCH_PAUSE_SEC = 0.25

    def __init__(self, app, rows, mode):
        self.app  = app
        self.rows = list(rows)
        self.mode = mode
        self.total = len(self.rows)
        self.embedded = 0        # art found and written onto the file
        self.repaired = 0        # file already had art; only the DB was stale
        self.not_found = 0       # no artwork available from any rung
        self.missing = 0         # the row's file is no longer on disk
        self.errors = 0
        self._progress = None
        self._cancel_event = threading.Event()
        self.cancelled = False

    # ── public entry ──────────────────────────────────────────────────────
    def start(self):
        self._cancel_event.clear()
        self._show_progress()
        self.app._run_bg(self._worker)

    # ── background worker ─────────────────────────────────────────────────
    def _worker(self):
        """One pass over every row. Never raises: a row that blows up is counted
        as an error and the walk continues."""
        for i, row in enumerate(self.rows):
            if self._cancel_event.is_set():
                self.cancelled = True
                break
            title = row.get("title") or ""
            self.app.after(0, lambda n=i, t=title: self._update_progress(n, t))
            try:
                self._process_row(row)
            except Exception as exc:  # pragma: no cover - defensive
                self.errors += 1
                self.app._dbg.warning(
                    f"ARTFILL FAIL  | {title!r}  {exc}")
        self.app.after(0, self._finish)

    def _process_row(self, row):
        """Resolve and embed artwork for a single downloads row."""
        path = row.get("file_path") or ""
        if not os.path.isfile(path):
            self.missing += 1
            return

        key = cb_artwork.artwork_key(row.get("video_id"), path)
        if not key:
            self.not_found += 1
            return

        art_dir = cb_artwork.thumbnail_dir(os.path.dirname(path))
        if not art_dir:
            self.errors += 1
            return

        # The file may already carry art from outside the app (or from a run
        # whose DB write was lost). Nothing to fetch — just correct the row.
        if cb_artwork.has_cover(path):
            self.app._db.set_download_artwork(
                path, cb_artwork.existing_sidecar(art_dir, key), 1,
                row.get("thumbnail_url"))
            self.repaired += 1
            return

        jpg = cb_artwork.existing_sidecar(art_dir, key)
        thumb_url = row.get("thumbnail_url")

        if not jpg:
            raw = os.path.join(art_dir, f"{key}.raw")
            thumb_url, jpg = self._fetch_sidecar(row, path, art_dir, key, raw)

        if not jpg:
            self.not_found += 1
            self.app._dbg.debug(
                f"ARTFILL NONE  | {row.get('title')!r}  no thumbnail found")
            return

        embedded = cb_artwork.embed_cover(path, jpg)
        self.app._db.set_download_artwork(path, jpg, embedded, thumb_url)
        if embedded:
            self.embedded += 1
            self.app._dbg.debug(f"ARTFILL OK    | {row.get('title')!r}  {jpg}")
        else:
            # Sidecar saved but not embedded — a non-MP3 (kept original format).
            self.not_found += 1

    def _fetch_sidecar(self, row, path, art_dir, key, raw):
        """Walk the network rungs of the ladder. Returns (thumb_url, jpg_path),
        either of which may be None."""
        candidates = list(cb_artwork.thumbnail_url_candidates(
            row.get("platform"), row.get("video_id"),
            row.get("thumbnail_url")))

        # Last rung: the source URL the app stamped into the track's own ID3
        # tag. Resolving it costs a yt-dlp metadata call, so it is only tried
        # when nothing cheaper produced a candidate.
        if not candidates:
            resolved = self._thumbnail_from_source_tag(path)
            if resolved:
                candidates = [resolved]

        for url in candidates:
            if self._cancel_event.is_set():
                return None, None
            got = cb_artwork.download_thumbnail(url, raw)
            time.sleep(self._FETCH_PAUSE_SEC)
            if not got:
                continue
            jpg = cb_artwork.ingest_thumbnail(raw, art_dir, key, self.mode)
            if jpg:
                return url, jpg
        return None, None

    def _thumbnail_from_source_tag(self, path):
        """Read the source URL out of the track's ID3 tag and ask yt-dlp for its
        thumbnail. The fallback for legacy SoundCloud tracks. Returns a URL or
        None; never raises."""
        source_url = read_source_url(path)
        if not source_url:
            return None
        try:
            import yt_dlp
            opts = {"skip_download": True, "quiet": True, "no_warnings": True}
            self.app._apply_cookie_opts(opts)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source_url, download=False) or {}
            return info.get("thumbnail") or None
        except Exception as exc:
            self.app._dbg.debug(f"ARTFILL LOOKUP| {source_url}  {exc}")
            return None

    # ── progress dialog ───────────────────────────────────────────────────
    def _show_progress(self):
        dlg = tk.Toplevel(self.app)
        dlg.title("Fetch Missing Artwork")
        dlg.configure(bg=BG)
        dlg.transient(self.app)
        dlg.resizable(False, False)
        head = tk.Label(dlg, text="Fetching artwork…",
                        font=("Segoe UI", 11, "bold"), bg=BG, fg=TEXT)
        head.pack(padx=24, pady=(18, 4))
        sub = tk.Label(dlg, text=f"0 of {self.total}", font=("Segoe UI", 9),
                       bg=BG, fg=TEXT_DIM, wraplength=300)
        sub.pack(pady=(0, 8))
        bar = ttk.Progressbar(dlg, mode="determinate", length=300,
                              maximum=max(self.total, 1))
        bar.pack(padx=24, pady=(0, 10))
        tk.Button(dlg, text="Cancel", font=("Segoe UI", 9),
                  relief="flat", bd=0, bg=SURFACE2, fg=TEXT,
                  activebackground=BORDER, activeforeground=TEXT,
                  padx=12, pady=4, cursor="hand2",
                  command=self._cancel).pack(pady=(0, 16))
        dlg.protocol("WM_DELETE_WINDOW", self._cancel)
        dlg.update_idletasks()
        px = self.app.winfo_x() + (self.app.winfo_width() - dlg.winfo_width()) // 2
        py = self.app.winfo_y() + (self.app.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
        self._progress = (dlg, bar, sub)

    def _update_progress(self, n, title):
        if self._progress is None:
            return
        dlg, bar, sub = self._progress
        try:
            bar.config(value=n)
            sub.config(text=f"{n} of {self.total}  —  {title[:60]}")
        except Exception:
            pass

    def _hide_progress(self):
        if self._progress is not None:
            dlg, _bar, _sub = self._progress
            try:
                dlg.destroy()
            except Exception:
                pass
            self._progress = None

    def _cancel(self):
        """Cancel button / dialog close: stop after the row in flight. Art
        already embedded stays — every row is committed as it completes."""
        self.cancelled = True
        self._cancel_event.set()

    # ── completion ────────────────────────────────────────────────────────
    def _finish(self):
        self._hide_progress()
        summary = (
            f"{self.embedded} track{'s' if self.embedded != 1 else ''} "
            f"given cover art."
        )
        detail = []
        if self.repaired:
            detail.append(f"{self.repaired} already had art (database updated)")
        if self.not_found:
            detail.append(f"{self.not_found} had no artwork available")
        if self.missing:
            detail.append(f"{self.missing} no longer on disk")
        if self.errors:
            detail.append(f"{self.errors} failed")
        if detail:
            summary += "\n\n" + "\n".join(detail)
        if self.cancelled:
            summary = "Cancelled.\n\n" + summary

        self.app._dbg.info(
            f"ARTFILL DONE  | embedded={self.embedded} repaired={self.repaired} "
            f"none={self.not_found} missing={self.missing} "
            f"errors={self.errors} cancelled={self.cancelled}")
        messagebox.showinfo("Fetch Missing Artwork", summary, parent=self.app)
        self.app._artwork_session = None


# ─────────────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
# MP3DownloaderApp — main application window (Tk root)
#   __init__ / lifecycle, the four notebook tabs, the download engine, and the
#   Watch List automation all live on this class. Sections below are marked with
#   full-width ═ banners; method groups within a section use ── sub-banners.
# ══════════════════════════════════════════════════════════════════════════════
class MP3DownloaderApp(tk.Tk):

    # Log-size-limit dropdown choices (label order). 0 MB = Unlimited.
    _LOG_LIMIT_CHOICES = ("1MB", "2MB", "3MB", "4MB",
                          "5MB", "8MB", "10MB", "Unlimited")

    @staticmethod
    def _log_limit_label(mb):
        """Map a megabyte count (0 = unlimited) to its dropdown label."""
        return "Unlimited" if not mb else f"{int(mb)}MB"

    @staticmethod
    def _parse_log_limit_mb(label):
        """Map a dropdown label back to a megabyte count (0 = unlimited)."""
        if not label or label.strip().lower() == "unlimited":
            return 0
        try:
            return int(label.strip().lower().replace("mb", "").strip())
        except ValueError:
            return 0

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION_FULL}")
        self.geometry("850x950")
        self.minsize(640, 620)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Titlebar / taskbar icon. Windows gets this from the packaged .exe, but
        # Tk on Linux has no icon unless we set one explicitly — otherwise the
        # window shows the default feather. Held on self so Tk can't GC it.
        try:
            _icon_path = app_icon_path()
            if _icon_path:
                from PIL import Image, ImageTk
                self._window_icon = ImageTk.PhotoImage(Image.open(_icon_path))
                self.iconphoto(True, self._window_icon)
        except Exception:
            pass

        self.update_idletasks()
        x = (self.winfo_screenwidth()  - 850) // 2
        y = (self.winfo_screenheight() - 950) // 2
        self.geometry(f"+{x}+{y}")

        # Load persisted config
        cfg = load_config()
        self._base_dir      = cfg.get("base_dir", DEFAULT_BASE)
        self._downloading   = False
        self._cancel_flag   = threading.Event()
        self._pause_flag    = threading.Event()   # set = paused, clear = running
        self._wl_scan_active = 0   # count of in-flight Watch List scans
        self._wl_cancel_cids = set()  # channel ids the user asked to cancel
        self._queue         = []
        self._skip_existing   = tk.BooleanVar(value=cfg.get("skip_existing", True))
        _raw_skip = cfg.get("skip_mode", "In Folder Only")
        _migrated = {"In Logs ~ In Folder": "In Database ~ In Folder",
                     "In Logs Only": "In Database Only"}
        self._skip_mode       = tk.StringVar(value=_migrated.get(_raw_skip, _raw_skip))
        self._skip_existing.trace_add("write", self._autosave_skip_settings)
        self._skip_mode.trace_add("write",     self._autosave_skip_settings)
        self._platform_var    = tk.StringVar(value="YouTube")
        self._limit_enabled   = tk.BooleanVar(value=cfg.get("limit_enabled", True))
        self._limit_minutes   = tk.IntVar(value=cfg.get("limit_minutes", 8))
        self._limit_enabled.trace_add("write", self._autosave_limiter_settings)
        self._bitrate_quality = tk.StringVar(
            value=cfg.get("bitrate_quality", "192") + " kbps"
                  if not str(cfg.get("bitrate_quality", "192")).endswith("kbps")
                  else cfg.get("bitrate_quality", "192"))
        self._bitrate_quality.trace_add("write", self._autosave_bitrate_setting)
        self._no_conversion = tk.BooleanVar(value=cfg.get("no_conversion", False))
        self._no_conversion.trace_add("write", self._autosave_bitrate_setting)

        # Cover art — how the source thumbnail is fitted to the square art slot
        # every player renders. Stored as one of cb_artwork.COVER_ART_MODES;
        # presented in the UI as a friendly label via _COVER_ART_LABELS.
        _cfg_mode = str(cfg.get("cover_art_mode",
                                cb_artwork.DEFAULT_COVER_ART_MODE)).lower()
        if _cfg_mode not in cb_artwork.COVER_ART_MODES:
            _cfg_mode = cb_artwork.DEFAULT_COVER_ART_MODE
        self._cover_art_mode = tk.StringVar(value=_COVER_ART_LABELS[_cfg_mode])
        self._cover_art_mode.trace_add("write", self._autosave_cover_art_setting)
        # In-flight artwork backfill, or None. Guards against a second run being
        # started on top of one already walking the library.
        self._artwork_session = None

        # Log size limit — caps activity.log and debug.log (each) by trimming the
        # oldest lines; 0 = Unlimited. Default 2 MB keeps the logs from growing
        # without bound while preserving plenty of recent history.
        self._log_max_mb = int(cfg.get("log_max_mb", 2) or 0)
        self._log_max_bytes = self._log_max_mb * 1024 * 1024
        self._log_limit_var = tk.StringVar(
            value=self._log_limit_label(self._log_max_mb))

        # Download behavior settings
        self._geo_bypass      = tk.BooleanVar(value=cfg.get("geo_bypass", True))
        self._rotate_ua       = tk.BooleanVar(value=cfg.get("rotate_ua", True))
        self._sleep_enabled   = tk.BooleanVar(value=cfg.get("sleep_enabled", True))
        self._sleep_mode      = tk.StringVar(value=cfg.get("sleep_mode", "Auto"))
        self._sleep_preset    = tk.StringVar(value=cfg.get("sleep_preset", "Light  (1–5 s)"))
        self._sleep_min       = tk.IntVar(value=cfg.get("sleep_min", 1))
        self._sleep_max       = tk.IntVar(value=cfg.get("sleep_max", 5))
        self._geo_bypass.trace_add("write",    self._autosave_behavior_settings)
        self._rotate_ua.trace_add("write",     self._autosave_behavior_settings)
        self._sleep_enabled.trace_add("write",  self._autosave_behavior_settings)
        self._sleep_mode.trace_add("write",     self._autosave_behavior_settings)
        self._sleep_preset.trace_add("write",   self._autosave_behavior_settings)
        self._use_cookies     = tk.BooleanVar(value=cfg.get("use_cookies", False))
        self._cookie_method   = tk.StringVar(value=cfg.get("cookie_method", "Browser"))
        self._cookies_browser = tk.StringVar(value=cfg.get("cookies_browser", "Firefox"))
        self._cookies_profile = tk.StringVar(value=cfg.get("cookies_profile", ""))
        self._cookie_file     = tk.StringVar(value=cfg.get("cookie_file", ""))
        self._use_cookies.trace_add("write",     self._autosave_behavior_settings)
        self._cookie_method.trace_add("write",   self._autosave_behavior_settings)
        self._cookies_browser.trace_add("write", self._autosave_behavior_settings)

        # Watch List behavior
        self._auto_add_to_watchlist = tk.BooleanVar(
            value=cfg.get("auto_add_to_watchlist", True))
        self._auto_add_to_watchlist.trace_add(
            "write", self._autosave_behavior_settings)
        self._active_watchlist_batch = None   # set by _watchlist_download_*
        self._wl_download_active = False      # True while a Watch List batch runs
        self._wl_card_widgets = {}            # cid -> card frame, for in-place updates
        self._wl_fix_abort = False            # set by Cancel to stop a Fix-Channels pass
        # While a Watch List batch runs, the Main tab's Batch Queue container
        # shows these channels with the active one highlighted.
        self._wl_batch_channels = []          # ordered display names in the batch
        self._wl_batch_genres = []            # genre per channel, parallel list
        self._wl_batch_active_idx = -1        # index currently downloading

        # Automation settings (auto-download / startup / tray)
        # New keys, falling back to the older auto-check names so an existing
        # config keeps its interval / schedule anchor across the upgrade.
        self._auto_dl_interval = tk.StringVar(
            value=cfg.get("auto_download_interval",
                          cfg.get("auto_check_hours", "1 day")))
        self._run_at_startup = tk.BooleanVar(
            value=cfg.get("run_at_startup", False))
        if sys.platform == "win32":
            self._run_at_startup.set(cb_startup.startup_is_enabled())
        self._minimize_to_tray = tk.BooleanVar(
            value=cfg.get("minimize_to_tray", True))
        self._start_minimized = tk.BooleanVar(
            value=cfg.get("start_minimized", False))
        # Scan the Watch List for new uploads as soon as the app launches.
        # On by default; the user can opt out via Settings.
        self._watchlist_scan_on_startup = tk.BooleanVar(
            value=cfg.get("watchlist_scan_on_startup", True))
        # The auto-download schedule counts from when the app starts: the next
        # run is always (this launch time + interval), regardless of any stored
        # value from a previous session. A later Download All New re-anchors to
        # that download, so subsequent runs stay one interval apart.
        self._watchlist_last_download = int(time.time())
        self._auto_dl_after_id = None
        self._wl_next_dl_ts = None      # wall-clock of the next scheduled run
        self._tray_icon = None  # set when tray is active
        self._tray_title_after_id = None   # recurring hover-tooltip refresh
        self._tray_dl_label = "Download All New (0)"  # live tray menu label
        # How often to silently re-check GitHub for a newer nightly build. Set
        # from the About-tab dropdown; persisted as 'update_check_interval'.
        self._update_check_interval = tk.StringVar(
            value=cfg.get("update_check_interval", "6 hours"))
        self._update_check_after_id = None
        self._update_prompt_open = False    # an auto-check dialog is on screen
        self._update_in_progress = False    # download/stage worker is running
        self._next_update_check_ts = None
        self._next_update_check_var = tk.StringVar(value="")
        self._auto_dl_interval.trace_add("write", self._autosave_automation_settings)
        self._minimize_to_tray.trace_add("write", self._autosave_automation_settings)
        self._start_minimized.trace_add("write", self._autosave_automation_settings)
        self._watchlist_scan_on_startup.trace_add(
            "write", self._autosave_automation_settings)
        self._update_check_interval.trace_add(
            "write", self._on_update_interval_changed)

        # Ensure directory structure exists on startup
        self._url_history = cfg.get("url_history", [])[:6]
        self._ensure_dirs()
        self._setup_logger()

        self._build_styles()
        self._build_ui()
        self._apply_platform()      # paint initial platform colours
        self._check_deps_async()

        # Sweep out any blank "nameless" Watch List cards left by older
        # auto-add bugs BEFORE the list is first populated or rendered.
        self.after(900, self._watchlist_cleanup_blank_rows)
        # First-run: auto-populate the Watch List from existing channel folders
        self.after(1200, self._watchlist_populate_from_folders)
        self.after(1600, self._reschedule_auto_download)
        # Actively refresh new-track counts for every entry on each launch.
        self.after(2200, self._watchlist_startup_scan)

        # Close (X) always confirms quit; the Minimize button is what hides to
        # tray (when the option is enabled).
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.bind("<Unmap>", self._on_minimize)

        # Start in the System Tray only when the user ticked "Start App
        # Minimized to System Tray" — this is the sole control for it, and it
        # applies equally to a manual launch and a Windows run-at-startup
        # ("--startup") launch. When it's unticked, both launch paths just show
        # the open window. _hide_to_tray falls back to a taskbar minimise if the
        # tray is unavailable.
        if self._start_minimized.get():
            self.after(1700, self._hide_to_tray)

    # ══════════════════════════════════════════════════════════════════════════
    # Filesystem & logging — paths, the activity/debug loggers, dedup helpers
    # ══════════════════════════════════════════════════════════════════════════
    # ── Directory management ──────────────────────────────────────────────────
    def _ensure_dirs(self):
        """Create base + YouTube / SoundCloud sub-directories if missing."""
        os.makedirs(self._base_dir, exist_ok=True)
        for plat in PLATFORMS.values():
            os.makedirs(os.path.join(self._base_dir, plat["subdir"]),
                        exist_ok=True)

    def _platform_dir(self, platform=None):
        """Return the platform-specific root: base_dir/YouTube  or  base_dir/SoundCloud."""
        if platform is None:
            platform = self._platform_var.get()
        return os.path.join(self._base_dir, PLATFORMS[platform]["subdir"])

    def _scan_genres(self, platform=None):
        """Scan platform folder for existing genre sub-directories."""
        pdir = self._platform_dir(platform)
        if not os.path.isdir(pdir):
            return []
        return sorted(
            d for d in os.listdir(pdir)
            if os.path.isdir(os.path.join(pdir, d)) and d != "_No Genre"
        )

    def _channel_save_path(self, genre, channel_name=None, platform=None):
        """Build the save path  base/Platform[/Genre[/Channel]]  — pure, no
        side effects (does NOT create the directory). The path may not exist
        on disk yet; callers that need it created should use
        _resolve_save_dir instead."""
        parts = [self._platform_dir(platform)]
        if genre and genre != "(none)":
            parts.append(genre)
        else:
            parts.append("_No Genre")
        if channel_name:
            safe = safe_filename(channel_name, strip=True)
            if safe:
                parts.append(safe)
        return os.path.join(*parts)

    def _resolve_save_dir(self, genre, channel_name=None, platform=None):
        """Build the final save path:  base/Platform[/Genre[/Channel]]
        and create it on disk."""
        path = self._channel_save_path(genre, channel_name, platform=platform)
        os.makedirs(path, exist_ok=True)
        return path

    # ── Download logger ───────────────────────────────────────────────────────
    def _setup_logger(self):
        """Initialise (or re-initialise) the file logger.
        Called on startup and whenever the base directory changes."""
        os.makedirs(self._base_dir, exist_ok=True)
        # Place log in the program's install/script directory — or the per-user
        # data dir when the install dir isn't writable (e.g. a .deb under /opt)
        app_dir = runtime_data_dir()
        self._log_path = os.path.join(app_dir, "activity.log")

        logger = logging.getLogger("CrateBuilder")
        # Clear any existing handlers so re-init doesn't duplicate output
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False

        fh = _HeadTrimFileHandler(self._log_path, max_bytes=self._log_max_bytes,
                                  encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
        self._logger = logger
        self._log_fh = fh

        # ── Debug logger (separate file for diagnostics) ──────────────────────
        self._debug_log_path = os.path.join(app_dir, "debug.log")
        dbg = logging.getLogger("CrateBuilder.debug")
        dbg.handlers.clear()
        dbg.setLevel(logging.DEBUG)
        dbg.propagate = False

        dfh = _HeadTrimFileHandler(self._debug_log_path,
                                   max_bytes=self._log_max_bytes,
                                   encoding="utf-8")
        dfh.setLevel(logging.DEBUG)
        dfh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        dbg.addHandler(dfh)
        self._dbg = dbg
        self._dbg_fh = dfh
        self._dbg.info("═" * 80)
        self._dbg.info(f"SESSION START  —  {APP_NAME} v{APP_VERSION_FULL}")
        self._dbg.info(f"Platform: {sys.platform}  |  Python: {sys.version.split()[0]}")
        try:
            import yt_dlp
            self._dbg.info(f"yt-dlp version: {yt_dlp.version.__version__}")
        except Exception:
            self._dbg.info("yt-dlp version: unknown")
        self._dbg.info("═" * 80)

        # ── Downloads database (SQLite) ───────────────────────────────────────
        self._db_path = os.path.join(app_dir, "cratebuilder.db")
        self._db = DownloadsDatabase(self._db_path, debug_logger=self._dbg)

    def _dbg_cookie_config(self):
        """Log the current cookie configuration to debug.log."""
        enabled = self._use_cookies.get()
        self._dbg.info(f"COOKIE CONFIG | enabled={enabled}")
        if not enabled:
            return
        method = self._cookie_method.get()
        self._dbg.info(f"COOKIE CONFIG | method={method}")
        if method == "Cookie File":
            cfile = self._cookie_file.get().strip()
            exists = os.path.exists(cfile) if cfile else False
            self._dbg.info(f"COOKIE CONFIG | file={cfile!r}  exists={exists}")
            if exists:
                try:
                    size = os.path.getsize(cfile)
                    with open(cfile, "r", encoding="utf-8", errors="replace") as f:
                        first_line = f.readline().strip()
                    self._dbg.info(
                        f"COOKIE FILE   | size={size} bytes  "
                        f"first_line={first_line[:120]!r}")
                except Exception as e:
                    self._dbg.warning(f"COOKIE FILE   | read error: {e}")
        else:
            browser = self._cookies_browser.get()
            profile = self._cookies_profile.get().strip()
            self._dbg.info(
                f"COOKIE CONFIG | browser={browser!r}  "
                f"profile={profile!r}  "
                f"tuple={((browser.lower(), profile) if profile else (browser.lower(),))!r}")

    def _apply_js_runtime(self, opts):
        """Enable a JavaScript runtime so yt-dlp can solve YouTube's "n"
        signature challenge. Without it, YouTube returns only storyboard
        ("images") formats and every real download fails with "Requested
        format is not available". Node ≥22 plus the yt-dlp-ejs solver scripts
        are required; if neither is present yt-dlp simply falls back (the
        download then skips gracefully). See yt-dlp wiki: EJS.

        Only needed on opts that extract real formats (metadata/probe/
        download) — flat listing/scan opts don't trip the challenge."""
        opts["js_runtimes"] = {"node": {"path": None}}
        # Fallback for machines without the local yt-dlp-ejs package: let
        # yt-dlp fetch the solver scripts from GitHub on demand.
        opts.setdefault("remote_components", ["ejs:github"])
        return opts

    def _dbg_ydl_opts(self, label, opts):
        """Log yt-dlp options to debug.log with auth-bearing values redacted
        (delegates to cratebuilder.util.redact_ydl_opts)."""
        self._dbg.info(f"YDL OPTS ({label}) | {redact_ydl_opts(opts)}")

    def _apply_cookie_opts(self, opts):
        """Merge the user's cookie settings into a yt-dlp *opts* dict in place,
        but only when cookies are enabled (no-op otherwise). Single source of
        truth for the metadata / probe / download / scan cookie blocks."""
        if not self._use_cookies.get():
            return
        opts.update(build_cookie_opts(
            self._cookie_method.get(),
            self._cookie_file.get().strip(),
            self._cookies_browser.get(),
            self._cookies_profile.get().strip(),
        ))

    def _log_download(self, title, filepath, url, platform, genre,
                      quality="192 kbps MP3"):
        """Write one DOWNLOADED entry to the log file."""
        genre_str = genre if genre and genre != "(none)" else "—"
        self._logger.info(
            "DOWNLOADED  | "
            f"Platform: {platform:<11}| "
            f"Genre: {genre_str:<18}| "
            f"Title: {title} | "
            f"File: {filepath} | "
            f"URL: {url} | "
            f"Quality: {quality}"
        )

    def _log_skipped(self, title, filepath, reason="already exists"):
        """Write one SKIPPED entry to the log file."""
        self._logger.info(
            "SKIPPED     | "
            f"Reason: {reason:<20}| "
            f"Title: {title} | "
            f"File: {filepath}"
        )

    def _tag_track(self, path, title, url):
        """Best-effort: stamp Title / Encoded-by / source-URL ID3 tags onto a
        downloaded MP3 so the originating YouTube/SoundCloud link is later
        recoverable from the file's Details. Used both for freshly downloaded
        files and as a backfill on tracks that were skipped because they were
        already on disk. Never raises; a tag failure must not break a batch."""
        try:
            if write_track_tags(path, title=title, source_url=url):
                self._dbg.debug(f"ID3 TAGGED   | {title!r}  {path}")
        except Exception as exc:  # pragma: no cover - defensive
            self._dbg.warning(f"ID3 TAG FAIL | {title!r}  {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # Cover art
    # ══════════════════════════════════════════════════════════════════════════

    def _find_raw_thumbnail(self, audio_path):
        """Locate the thumbnail yt-dlp wrote beside *audio_path*.

        `writethumbnail` saves the image on the audio file's own outtmpl stem,
        so it sits next to the track with an image extension — `.webp` from
        YouTube, `.jpg` from SoundCloud, occasionally `.png`. Returns the first
        one found, or None."""
        if not audio_path:
            return None
        stem = os.path.splitext(audio_path)[0]
        for ext in (".webp", ".jpg", ".jpeg", ".png"):
            candidate = stem + ext
            if os.path.isfile(candidate):
                return candidate
        return None

    def _harvest_cover_art(self, audio_path, video_id, title):
        """Turn the thumbnail yt-dlp just wrote into cover art for one track.

        Converts the raw image into the channel folder's hidden `.artwork/`
        sidecar as `<video_id>.jpg`, then embeds it as the MP3's front-cover
        APIC frame — the embed is what actually makes the art appear in Windows
        Explorer, media players and on Android; the sidecar is the archival copy
        we can re-embed from later without going back to the network.

        Non-MP3 tracks (the "keep original format" path) still get the sidecar;
        only the embed is skipped, because Opus/MP4 cover art uses an entirely
        different container frame.

        Returns (artwork_path, embedded) — (None, False) when artwork is off,
        unavailable, or anything at all went wrong. Never raises: a cover-art
        failure must not fail a download."""
        mode = self._cover_art_mode_value()
        if mode == "off" or not cb_artwork.artwork_available():
            return None, False

        raw = self._find_raw_thumbnail(audio_path)
        if not raw:
            self._dbg.debug(f"COVER SKIP    | {title!r}  no thumbnail written")
            return None, False

        try:
            art_dir = cb_artwork.thumbnail_dir(os.path.dirname(audio_path))
            if not art_dir:
                return None, False

            art_path = cb_artwork.ingest_thumbnail(raw, art_dir, video_id, mode)
            if not art_path:
                self._dbg.warning(f"COVER FAIL    | {title!r}  ingest failed")
                return None, False

            embedded = cb_artwork.embed_cover(audio_path, art_path)
            if embedded:
                self._dbg.debug(f"COVER ART     | {title!r}  {art_path}")
            else:
                self._dbg.debug(
                    f"COVER SIDECAR | {title!r}  saved, not embedded "
                    f"(non-MP3 or tag write failed)")
            return art_path, embedded
        except Exception as exc:  # pragma: no cover - defensive
            self._dbg.warning(f"COVER FAIL    | {title!r}  {exc}")
            return None, False

    def _log_error(self, title, url, error):
        """Write one ERROR entry to the log file."""
        self._logger.error(
            "ERROR       | "
            f"Title: {title} | "
            f"URL: {url} | "
            f"Error: {error}"
        )

    def _log_separator(self, label=""):
        """Write a visual separator line to the log file."""
        if label:
            pad   = max(0, 74 - len(label))
            left  = pad // 2
            right = pad - left
            line  = f"{'═' * left}  {label}  {'═' * right}"
        else:
            line = "═" * 80
        self._logger.info(line)

    def _file_exists_on_disk(self, save_dir, title):
        """Check if a file matching *title* already exists in *save_dir*.
        Uses yt-dlp's sanitize_filename for an exact match first, then
        falls back to a case-insensitive prefix scan of existing .mp3 files."""
        try:
            from yt_dlp.utils import sanitize_filename as ytdl_sanitize
            ytdl_safe = ytdl_sanitize(title, restricted=False)
        except ImportError:
            ytdl_safe = safe_filename(title)

        # Exact match using yt-dlp's sanitization
        exact = os.path.join(save_dir, ytdl_safe + ".mp3")
        if os.path.exists(exact):
            return exact

        # Also try our own regex sanitization (for files we downloaded before
        # the fix, which used the old naming)
        regex_safe = safe_filename(title, strip=True)
        legacy = os.path.join(save_dir, regex_safe + ".mp3")
        if legacy != exact and os.path.exists(legacy):
            return legacy

        # Fallback: case-insensitive prefix scan of existing .mp3 files
        # This catches minor title variations between flat/full extraction
        try:
            prefix = ytdl_safe[:40].lower()
            for fname in os.listdir(save_dir):
                if fname.lower().endswith(".mp3") and \
                   fname[:-4].lower().startswith(prefix):
                    return os.path.join(save_dir, fname)
        except OSError:
            pass

        return None

    def _ask_redownload(self, title, result_holder, done_event):
        """
        Show a dark-themed re-download confirmation dialog with a 20-second
        countdown.  Defaults to Skip if the timer expires without interaction.
        Mouse interaction anywhere on the dialog cancels the countdown.
        Must be called on the main thread.
        """
        COUNTDOWN = 20
        _remaining  = [COUNTDOWN]
        _timer_live = [True]
        _after_id   = [None]

        dlg = tk.Toplevel(self)
        dlg.title("File Missing — Re-download?")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        # Centre over main window
        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width()  - dlg.winfo_reqwidth())  // 2
        py = self.winfo_y() + (self.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")

        outer = tk.Frame(dlg, bg=BG, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        # File title
        tk.Label(outer,
                 text=f'"{title[:72]}{"…" if len(title) > 72 else ""}"',
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG,
                 wraplength=460, justify="left"
                 ).pack(anchor="w", pady=(0, 10))

        tk.Label(outer,
                 text="This file was previously downloaded (found in log)\n"
                      "but is no longer present in the destination folder.",
                 font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG,
                 justify="left"
                 ).pack(anchor="w", pady=(0, 16))

        # Countdown label
        self._countdown_lbl = tk.Label(
            outer, text=f"Auto-skipping in  {COUNTDOWN}s…",
            font=("Segoe UI", 9), fg="#f59e0b", bg=BG)
        self._countdown_lbl.pack(anchor="w", pady=(0, 16))

        # Buttons
        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x")

        def _do_download(_event=None):
            _timer_live[0] = False
            if _after_id[0]:
                dlg.after_cancel(_after_id[0])
            result_holder.append(True)
            dlg.destroy()
            done_event.set()

        def _do_skip(_event=None):
            _timer_live[0] = False
            if _after_id[0]:
                dlg.after_cancel(_after_id[0])
            result_holder.append(False)
            dlg.destroy()
            done_event.set()

        def _stop_timer(_event=None):
            """Cancel countdown on any mouse interaction with the dialog."""
            if _timer_live[0]:
                _timer_live[0] = False
                if _after_id[0]:
                    dlg.after_cancel(_after_id[0])
                self._countdown_lbl.config(
                    text="Auto-skip cancelled — please choose:",
                    fg=TEXT_DIM)

        tk.Button(btn_row, text="↓  Download Again",
                   font=("Segoe UI", 10, "bold"),
                   bg=YT_DARK, fg=TEXT,
                   activebackground=YT_RED, activeforeground=TEXT,
                   relief="flat", padx=16, pady=7,
                   cursor="hand2", command=_do_download
                   ).pack(side="left", padx=(0, 10))

        tk.Button(btn_row, text="⊘  Skip",
                   font=("Segoe UI", 10),
                   bg=SURFACE2, fg=TEXT_DIM,
                   activebackground=BORDER, activeforeground=TEXT,
                   relief="flat", padx=16, pady=7,
                   cursor="hand2", command=_do_skip
                   ).pack(side="left")

        # Countdown tick
        def _tick():
            if not _timer_live[0]:
                return
            _remaining[0] -= 1
            if _remaining[0] <= 0:
                _timer_live[0] = False
                self._countdown_lbl.config(text="Time expired — skipping…",
                                            fg=TEXT_DIM)
                dlg.after(400, _do_skip)
                return
            self._countdown_lbl.config(
                text=f"Auto-skipping in  {_remaining[0]}s…")
            _after_id[0] = dlg.after(1000, _tick)

        # Bind mouse interaction on dialog and all children to stop timer
        def _bind_stop(widget):
            widget.bind("<Motion>",  _stop_timer, add="+")
            widget.bind("<Button>",  _stop_timer, add="+")
            for child in widget.winfo_children():
                _bind_stop(child)

        dlg.protocol("WM_DELETE_WINDOW", _do_skip)
        dlg.bind("<Motion>", _stop_timer, add="+")

        # Start the first tick after 1 second, bind children after render
        _after_id[0] = dlg.after(1000, _tick)
        dlg.after(50, lambda: _bind_stop(dlg))

        self.wait_window(dlg)

        # Safety net: if dialog closed without setting result (e.g. WM kill)
        if not result_holder:
            result_holder.append(False)
            done_event.set()

    # ══════════════════════════════════════════════════════════════════════════
    # Batch URL list — the multi-URL queue panel on the Main tab
    # ══════════════════════════════════════════════════════════════════════════
    # ── Batch URL list ────────────────────────────────────────────────────────
    def _build_batch_panel(self, parent):
        """Build the compact batch-URL list panel."""
        self._batch_urls = []   # list of {"url", "genre", "platform"} dicts

        hdr = ttk.Frame(parent)
        hdr.pack(fill="x", pady=(0, 4))
        self._batch_count_lbl = ttk.Label(hdr, text="Batch Queue  (0 URLs)",
                                           style="S.White.Section.TLabel")
        self._batch_count_lbl.pack(side="left")
        self._settings_help(
            hdr,
            "The Batch Queue is the list of links you've lined up to download "
            "together.\n\n"
            "•  Paste a link above, choose its Genre, then press "
            "'+ Add to Batch' (or Enter) to drop it in the queue.\n"
            "•  Repeat to stack as many links as you want — each can have its "
            "own genre.\n"
            "•  Use the ▲ ▼ buttons on a row to reorder it, or ✕ to remove it; "
            "'Clear All' empties the whole queue.\n"
            "•  When you press 'Downloads MP3's', every link in the queue is "
            "processed top to bottom.",
            wraplength=360).pack(side="left", padx=(8, 0))
        ttk.Button(hdr, text="Clear All", style="MainBrowse.TButton",
                   command=self._batch_clear).pack(side="right")
        self._batch_add_btn = ttk.Button(hdr, text="+ Add to Batch",
                   style="MainBrowse.TButton", command=self._batch_add)
        self._batch_add_btn.pack(side="right", padx=(0, 6))

        outer = tk.Frame(parent, bg=SURFACE2,
                         highlightthickness=1, highlightbackground=YT_RED)
        outer.pack(fill="x", pady=(0, 12))

        self._batch_canvas = tk.Canvas(outer, bg=SURFACE2, bd=0,
                                        highlightthickness=0, height=90)
        bscroll = ttk.Scrollbar(outer, orient="vertical",
                                 command=self._batch_canvas.yview)
        self._batch_canvas.configure(yscrollcommand=bscroll.set)
        bscroll.pack(side="right", fill="y")
        self._batch_canvas.pack(side="left", fill="x", expand=True)

        self._batch_frame = tk.Frame(self._batch_canvas, bg=SURFACE2)
        self._batch_cwin  = self._batch_canvas.create_window(
            (0, 0), window=self._batch_frame, anchor="nw")
        self._batch_frame.bind("<Configure>", lambda e:
            self._batch_canvas.configure(
                scrollregion=self._batch_canvas.bbox("all")))
        self._batch_canvas.bind("<Configure>", lambda e:
            self._batch_canvas.itemconfig(self._batch_cwin, width=e.width))
        _bind_wheel(self._batch_canvas, lambda e: (
            self._batch_canvas.yview_scroll(int(-1*(_wheel_delta(e)/120)), "units"),
            "break")[-1])

        self._batch_rebuild_rows()

    def _batch_add(self):
        """Validate the current URL entry and add it to the batch list."""
        if self._ph_active:
            return
        url = self._normalize_url(self._url_var.get().strip())

        if not url:
            messagebox.showwarning("No URL", "Please enter a YouTube or SoundCloud URL.")
            return
        platform = self._detect_platform(url)
        cfg      = PLATFORMS[platform]
        if not re.search(cfg["url_pattern"], url):
            messagebox.showwarning("Invalid URL", "That doesn't look like a YouTube or SoundCloud URL.")
            return

        genre = self._genre_var.get()
        if genre == "(none)":
            proceed = messagebox.askyesno(
                "No Genre Selected",
                "No genre is selected. Files will be saved to the\n"
                "'_No Genre' folder.\n\n"
                "Do you want to continue?")
            if not proceed:
                return

        self._batch_urls.append({
            "url":      url,
            "genre":    genre,
            "platform": platform,
        })
        self._record_url_history(url)
        self._batch_rebuild_rows()

        # Clear the entry field and restore placeholder
        self._url_entry.delete(0, "end")
        self._url_entry.insert(0, "https://www.youtube.com/  or  https://soundcloud.com/")
        self._url_entry.config(foreground=TEXT_DIM)
        self._ph_active = True

    def _batch_remove(self, idx):
        if 0 <= idx < len(self._batch_urls):
            self._batch_urls.pop(idx)
            self._batch_rebuild_rows()

    def _batch_move(self, idx, direction):
        """Move item at idx up (-1) or down (+1)."""
        new_idx = idx + direction
        if 0 <= new_idx < len(self._batch_urls):
            self._batch_urls[idx], self._batch_urls[new_idx] = \
                self._batch_urls[new_idx], self._batch_urls[idx]
            self._batch_rebuild_rows()

    def _batch_clear(self):
        self._batch_urls.clear()
        self._batch_rebuild_rows()

    def _batch_rebuild_rows(self):
        """Redraw every row in the batch panel."""
        for w in self._batch_frame.winfo_children():
            w.destroy()

        # While a Watch List batch is downloading, repurpose this otherwise-idle
        # panel to show that batch's channels with the active one highlighted.
        if getattr(self, "_wl_download_active", False) and self._wl_batch_channels:
            self._wl_batch_render_rows()
            self._batch_canvas.update_idletasks()
            self._batch_canvas.configure(
                scrollregion=self._batch_canvas.bbox("all"))
            return

        n = len(self._batch_urls)
        self._batch_count_lbl.config(
            text=f"Batch Queue  ({n} URL{'s' if n != 1 else ''})")

        if n == 0:
            tk.Label(self._batch_frame,
                     text="  No URLs in batch — add a URL above and press  '+ Add to Batch'",
                     font=("Segoe UI", 10), fg=TEXT_DIM, bg=SURFACE2,
                     anchor="w").pack(fill="x", padx=8, pady=8)
        else:
            for i, item in enumerate(self._batch_urls):
                self._batch_build_row(i, item)

        self._batch_canvas.update_idletasks()
        self._batch_canvas.configure(
            scrollregion=self._batch_canvas.bbox("all"))

    def _batch_build_row(self, idx, item):
        row = tk.Frame(self._batch_frame, bg=SURFACE2)
        row.pack(fill="x", padx=4, pady=1)

        plat_cfg = PLATFORMS[item["platform"]]
        icon_col = plat_cfg["accent"]

        tk.Label(row, text=f"{idx+1:>2}.", font=("Consolas", 10),
                  fg=TEXT_DIM, bg=SURFACE2, width=3, anchor="e"
                  ).pack(side="left", padx=(2, 4))

        tk.Label(row, text=plat_cfg["icon"], font=("Segoe UI", 10),
                  fg=icon_col, bg=SURFACE2, width=2
                  ).pack(side="left", padx=(0, 4))

        trunc = item["url"]
        if len(trunc) > 62:
            trunc = trunc[:59] + "…"
        tk.Label(row, text=trunc, font=("Segoe UI", 10),
                  fg=TEXT_MED, bg=SURFACE2, anchor="w"
                  ).pack(side="left", fill="x", expand=True)

        genre_str = item["genre"] if item["genre"] != "(none)" else ""
        tk.Label(row, text=genre_str, font=("Segoe UI", 9),
                  fg=TEXT_DIM, bg=SURFACE2, anchor="e"
                  ).pack(side="left", padx=(4, 6))

        for sym, delta, tip in [("▲", -1, "Move this URL up in the queue"),
                                 ("▼", 1, "Move this URL down in the queue")]:
            btn = tk.Button(row, text=sym, font=("Segoe UI", 8),
                       bg=SURFACE2, fg=TEXT_DIM, relief="flat", bd=0,
                       activebackground=BORDER, activeforeground=TEXT,
                       cursor="hand2", padx=3,
                       command=lambda i=idx, d=delta: self._batch_move(i, d))
            btn.pack(side="left")
            Tooltip(btn, tip)

        rm_btn = tk.Button(row, text="✕", font=("Segoe UI", 10),
                   bg=SURFACE2, fg=TEXT_DIM, relief="flat", bd=0,
                   activebackground="#3b0000", activeforeground=YT_RED,
                   cursor="hand2", padx=4,
                   command=lambda i=idx: self._batch_remove(i))
        rm_btn.pack(side="left", padx=(4, 2))
        Tooltip(rm_btn, "Remove this URL from the queue")

    def _wl_batch_render_rows(self):
        """Render the running Watch List batch's channels in the Batch Queue
        panel, marking finished channels, the one currently downloading, and
        those still pending. Display-only — driven by _wl_batch_active_idx."""
        chans  = self._wl_batch_channels
        n      = len(chans)
        active = self._wl_batch_active_idx
        pos    = (active + 1) if active >= 0 else 1
        self._batch_count_lbl.config(
            text=f"⬇  Watch List — downloading {min(pos, n)} of {n} "
                 f"channel{'s' if n != 1 else ''}")

        for i, name in enumerate(chans):
            if i < active:                       # finished
                sym, sym_col, name_col, bg = "✓", SUCCESS, TEXT_DIM, SURFACE2
            elif i == active:                    # currently downloading
                sym, sym_col, name_col, bg = "⬇", "#ffffff", "#ffffff", YT_DARK
            else:                                # pending
                sym, sym_col, name_col, bg = "○", TEXT_DIM, TEXT_DIM, SURFACE2

            row = tk.Frame(self._batch_frame, bg=bg)
            row.pack(fill="x", padx=4, pady=1)

            tk.Label(row, text=f"{i+1:>2}.", font=("Consolas", 10),
                     fg=TEXT_DIM, bg=bg, width=3, anchor="e"
                     ).pack(side="left", padx=(2, 4))
            tk.Label(row, text=sym, font=("Segoe UI", 10, "bold"),
                     fg=sym_col, bg=bg, width=2
                     ).pack(side="left", padx=(0, 4))

            disp = name if len(name) <= 60 else name[:57] + "…"
            tk.Label(row, text=disp,
                     font=("Segoe UI", 10, "bold" if i == active else "normal"),
                     fg=name_col, bg=bg, anchor="w"
                     ).pack(side="left", fill="x", expand=True)

            # Genre/folder this channel saves into, mirroring the Batch Queue's
            # static rows so the user can see where each download lands.
            genres = getattr(self, "_wl_batch_genres", [])
            gval = genres[i] if i < len(genres) else ""
            genre_str = gval if gval and gval != "(none)" else ""
            tk.Label(row, text=genre_str, font=("Segoe UI", 9),
                     fg=(name_col if i == active else TEXT_DIM), bg=bg,
                     anchor="e").pack(side="left", padx=(4, 6))

    # ══════════════════════════════════════════════════════════════════════════
    # UI construction — ttk styles, the notebook, and the Main tab
    # ══════════════════════════════════════════════════════════════════════════
    # ── Styles ────────────────────────────────────────────────────────────────
    def _build_styles(self):
        """Configure the ttk 'clam' theme and all custom widget styles."""
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure("TFrame",         background=BG)
        s.configure("TLabel",         background=BG,      foreground=TEXT,     font=("Segoe UI", 11))
        s.configure("Dim.TLabel",     background=BG,      foreground=TEXT_DIM, font=("Segoe UI", 10))
        s.configure("Title.TLabel",   background=BG,      foreground=TEXT,     font=("Segoe UI", 18, "bold"))
        s.configure("Sub.TLabel",     background=BG,      foreground=TEXT_DIM, font=("Segoe UI", 10))
        s.configure("Section.TLabel", background=BG,      foreground=TEXT_DIM, font=("Segoe UI", 11, "bold"))
        s.configure("White.Section.TLabel", background=BG, foreground=TEXT,    font=("Segoe UI", 11, "bold"))

        # Settings tab — 1pt larger for readability
        s.configure("S.TLabel",              background=BG, foreground=TEXT_MED, font=("Segoe UI", 11))
        s.configure("S.Dim.TLabel",          background=BG, foreground=TEXT_DIM, font=("Segoe UI", 11))
        s.configure("S.Title.TLabel",        background=BG, foreground=TEXT,     font=("Segoe UI", 19, "bold"))
        s.configure("S.TitleIcon.TLabel",    background=BG, foreground=LINK_COL, font=("Segoe UI", 19, "bold"))
        s.configure("S.White.Section.TLabel", background=BG, foreground=TEXT,   font=("Segoe UI", 12, "bold"))
        s.configure("S.Bold.TCheckbutton",
            background=BG, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        s.map("S.Bold.TCheckbutton",
            background=[("active", BG)], foreground=[("active", TEXT)])
        # Settings-tab option captions — dimmed + one size smaller so the white
        # bold section headers clearly outrank them. (Settings-only; the Main /
        # Watch List tabs keep S.Bold.TCheckbutton.)
        s.configure("S.Opt.TCheckbutton",
            background=BG, foreground=TEXT_MED, font=("Segoe UI", 10, "bold"))
        s.map("S.Opt.TCheckbutton",
            background=[("active", BG)], foreground=[("active", TEXT_MED)])

        # ── Notebook / tab styling ────────────────────────────────────────────
        TAB_BLACK = "#000000"
        s.configure("TNotebook", background=SURFACE2, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=SURFACE2, foreground=TEXT_DIM,
            font=("Segoe UI", 12, "bold"),
            padding=(50, 10), borderwidth=0)
        s.map("TNotebook.Tab",
            background=[("selected", TAB_BLACK), ("active", SURFACE)],
            foreground=[("selected", TEXT),       ("active", TEXT_MED)],
            padding= [("selected", (50, 10))],
            expand=  [("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))])
        # Remove the default dashed focus ring on tabs
        s.layout("TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                    ("Notebook.label", {"side": "top", "sticky": ""})
                ]})
            ]})
        ])

        # Platform toggle buttons
        for name, bg_col, fg_col in [
            ("YT.TButton",  YT_RED,    TEXT),
            ("SC.TButton",  SC_ORANGE, TEXT),
            ("Off.TButton", SURFACE2,  TEXT_DIM),
        ]:
            s.configure(name,
                background=bg_col, foreground=fg_col,
                font=("Segoe UI", 10, "bold"),
                relief="flat", borderwidth=0, padding=(12, 6))
            s.map(name,
                background=[("active", bg_col), ("disabled", SURFACE2)],
                foreground=[("active", TEXT),   ("disabled", "#444")])

        s.configure("TEntry",
            fieldbackground=SURFACE2, foreground=TEXT,
            insertcolor=TEXT, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER,
            font=("Segoe UI", 11), relief="flat", padding=(10, 8))

        s.configure("Settings.TEntry",
            fieldbackground=SURFACE2, foreground=TEXT,
            insertcolor=TEXT, bordercolor=TEXT_DIM,
            lightcolor=TEXT_DIM, darkcolor=TEXT_DIM,
            font=("Segoe UI", 11), relief="flat", padding=(10, 8))

        s.configure("Download.TButton",
            background=YT_DARK, foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
            relief="flat", borderwidth=0, padding=(16, 10))
        s.map("Download.TButton",
            background=[("active", YT_RED), ("disabled", "#2a1515")],
            foreground=[("disabled", "#555")])

        s.configure("Cancel.TButton",
            background=SURFACE2, foreground="#cccccc",
            font=("Segoe UI", 11),
            relief="flat", borderwidth=0, padding=(20, 12))
        s.map("Cancel.TButton",
            background=[("active", "#333"), ("disabled", SURFACE2)],
            foreground=[("disabled", "#aaaaaa")])

        s.configure("CancelActive.TButton",
            background=YT_DARK, foreground=TEXT,
            font=("Segoe UI", 11),
            relief="flat", borderwidth=0, padding=(20, 12))
        s.map("CancelActive.TButton",
            background=[("active", YT_RED), ("disabled", SURFACE2)],
            foreground=[("disabled", "#444")])

        s.configure("Pause.TButton",
            background="#78350f", foreground="#cccccc",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(12, 12))
        s.map("Pause.TButton",
            background=[("active", "#f59e0b"), ("disabled", SURFACE2)],
            foreground=[("active", "#1c1917"), ("disabled", "#aaaaaa")])

        # Resume = the 'currently paused' state. Dark-orange background flags it
        # as inactive/awaiting user action.
        s.configure("Resume.TButton",
            background="#78350f", foreground="#cccccc",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(12, 12))
        s.map("Resume.TButton",
            background=[("active", "#a8521a"), ("disabled", SURFACE2)],
            foreground=[("active", "#1c1917"), ("disabled", "#aaaaaa")])

        s.configure("Browse.TButton",
            background=SURFACE2, foreground=TEXT_DIM,
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("Browse.TButton",
            background=[("active", BORDER)], foreground=[("active", TEXT)])

        # Main-tab variant of Browse.TButton with white (not dim) text.
        s.configure("MainBrowse.TButton",
            background=SURFACE2, foreground=TEXT,
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("MainBrowse.TButton",
            background=[("active", BORDER)], foreground=[("active", TEXT)])

        s.configure("Save.TButton",
            background="#1ba34e", foreground=TEXT,
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("Save.TButton",
            background=[("active", SUCCESS)],
            foreground=[("active", TEXT)])

        s.configure("LightBlue.TButton",
            background="#38bdf8", foreground="#0c2340",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("LightBlue.TButton",
            background=[("active", "#7dd3fc")],
            foreground=[("active", "#0c2340")])

        s.configure("Orange.TButton",
            background="#ff7a00", foreground="#1a0f00",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("Orange.TButton",
            background=[("active", "#ff9633")],
            foreground=[("active", "#1a0f00")])

        # Action button for the Database row. DlBtn = the primary built-in
        # "open/view" button (light blue, matching the Watch List tab).
        s.configure("DlBtn.TButton",
            background=WL_BLUE_DARK, foreground=TEXT,
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("DlBtn.TButton",
            background=[("active", WL_BLUE)],
            foreground=[("active", TEXT)])

        s.configure("TCheckbutton",
            background=BG, foreground=TEXT_DIM, font=("Segoe UI", 10))
        s.map("TCheckbutton",
            background=[("active", BG)], foreground=[("active", TEXT)])

        s.configure("Bold.TCheckbutton",
            background=BG, foreground=TEXT, font=("Segoe UI", 10, "bold"))
        s.map("Bold.TCheckbutton",
            background=[("active", BG)], foreground=[("active", TEXT)])

        # Combobox (genre selector)
        s.configure("TCombobox",
            fieldbackground=SURFACE2, foreground=TEXT,
            background=SURFACE2, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER,
            arrowcolor=TEXT_DIM,
            font=("Segoe UI", 11), padding=(8, 6))
        s.map("TCombobox",
            fieldbackground=[("readonly", SURFACE2)],
            foreground=[("readonly", TEXT)],
            bordercolor=[("focus", BORDER)],
            lightcolor=[("focus", BORDER)])
        # URL entry on the Main tab — same as TCombobox but a red border so it
        # reads as the primary input.
        s.configure("URL.TCombobox",
            fieldbackground=SURFACE2, foreground=TEXT,
            background=SURFACE2, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER,
            arrowcolor=TEXT_DIM, borderwidth=1,
            font=("Segoe UI", 11), padding=(8, 6))
        s.map("URL.TCombobox",
            fieldbackground=[("readonly", SURFACE2)],
            foreground=[("readonly", TEXT)],
            bordercolor=[("focus", BORDER), ("active", BORDER)],
            lightcolor=[("focus", BORDER), ("active", BORDER)],
            darkcolor=[("focus", BORDER), ("active", BORDER)])
        # Dropdown list colours
        self.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", BORDER)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 11))

        for name, color in [("Accent.Horizontal.TProgressbar", YT_RED),
                             ("Maroon.Horizontal.TProgressbar", MAROON)]:
            s.configure(name,
                troughcolor=SURFACE2, background=color,
                bordercolor=SURFACE2, lightcolor=color,
                darkcolor=color, thickness=5)

        # Scrollbar: light-grey trough, medium-grey thumb
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            s.configure(sb,
                troughcolor="#d4d4d4",
                background="#888888",
                bordercolor="#d4d4d4",
                lightcolor="#888888",
                darkcolor="#888888",
                arrowcolor="#555555",
                relief="flat")
            s.map(sb,
                background=[("active", "#666666"), ("disabled", "#aaaaaa")])

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        """Build the notebook and populate the Main / Watch List / Settings / About tabs."""
        # ── Notebook (tab bar / menu bar) ──────────────────────────────────────
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        # ── Main tab ──────────────────────────────────────────────────────────
        main_frame = ttk.Frame(self._notebook)
        self._tab_main = main_frame
        self._notebook.add(main_frame, text="     ▶  Main     ")
        self._build_main_tab(main_frame)

        # ── Watch List tab ────────────────────────────────────────────────────
        watchlist_frame = ttk.Frame(self._notebook)
        self._tab_watchlist = watchlist_frame
        self._notebook.add(watchlist_frame, text="   👁  Watch List   ")
        self._build_watchlist_tab(watchlist_frame)

        # ── Settings tab ──────────────────────────────────────────────────────
        settings_frame = ttk.Frame(self._notebook)
        self._notebook.add(settings_frame, text="   ⚙  Settings   ")
        self._build_settings_tab(settings_frame)

        # ── About tab ────────────────────────────────────────────────────────
        about_frame = ttk.Frame(self._notebook)
        self._notebook.add(about_frame, text="    ℹ  About    ")
        self._build_about_tab(about_frame)

        # ── Status bar (always visible, below tabs) ───────────────────────────
        self._status_var = tk.StringVar(value="Ready")
        tk.Label(self, textvariable=self._status_var,
                  font=("Segoe UI", 9), fg=TEXT_DIM, bg=SURFACE2,
                  anchor="w", padx=12, pady=4,
                  highlightthickness=1, highlightbackground=BORDER
                  ).pack(fill="x", side="bottom")

    def _make_scrollable(self, parent, padding):
        """Build a vertical-scrolling canvas in *parent*; return (canvas, inner).
        The inner ttk.Frame (with *padding*) keeps the scrollregion in sync and
        tracks the canvas width. Shared by the Main / Settings / About / Watch
        List tab bodies; the caller stores the returned canvas because the
        global mousewheel handler routes scroll events to it by active tab."""
        sb = ttk.Scrollbar(parent, orient="vertical")
        sb.pack(side="right", fill="y")
        canvas = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0,
                           yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.config(command=canvas.yview)
        inner = ttk.Frame(canvas, padding=padding)
        cwin = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e:
                   canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cwin, width=e.width))
        return canvas, inner

    # ── Main tab ──────────────────────────────────────────────────────────────
    def _build_main_tab(self, parent):
        """Build the Main tab: URL/genre input, batch list, and download queue."""
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        self._main_canvas, outer = self._make_scrollable(
            wrapper, (28, 22, 28, 18))

        # Global mousewheel handler — routes scroll to the active tab's canvas.
        # Widgets with their own scroll (queue Text, batch Canvas) handle
        # their own events and return "break" to prevent double-scrolling.
        def _on_global_mousewheel(event):
            w = event.widget
            # bind_all reaches every window in the process. Ignore wheel events
            # that originate in another Toplevel (e.g. the Database / Log viewer
            # windows) so they don't scroll the main app underneath them.
            try:
                if w.winfo_toplevel() is not self:
                    return
            except Exception:
                return
            # Let Text widgets handle their own scrolling
            if isinstance(w, tk.Text):
                return
            # Let the batch canvas handle its own scrolling
            if w is self._batch_canvas:
                return
            # Route to whichever tab is active
            try:
                tab_idx = self._notebook.index(self._notebook.select())
            except Exception:
                return
            delta = _wheel_delta(event)
            if tab_idx == 0:
                self._main_canvas.yview_scroll(
                    int(-1 * (delta / 120)), "units")
            elif tab_idx == 1:
                self._wl_canvas.yview_scroll(
                    int(-1 * (delta / 120)), "units")
            elif tab_idx == 2:
                self._settings_canvas.yview_scroll(
                    int(-1 * (delta / 120)), "units")
            elif tab_idx == 3:
                self._about_canvas.yview_scroll(
                    int(-1 * (delta / 120)), "units")
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(_seq, _on_global_mousewheel)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(outer)
        hdr.pack(fill="x", pady=(0, 4))

        self._logo_lbl = tk.Label(hdr, text="♫", font=("Segoe UI", 20),
                                   fg=YT_RED, bg=BG)
        self._logo_lbl.pack(side="left", padx=(0, 10))

        self._title_lbl = ttk.Label(hdr, text="DJ-CrateBuilder",
                                     style="Title.TLabel")
        self._title_lbl.pack(side="left", pady=(2, 0))

        # Platform is now auto-detected from the URL — no toggle buttons needed

        self._sub_lbl = ttk.Label(outer, style="S.Dim.TLabel",
                                   text="YouTube or SoundCloud  •  single track  •  playlist  •  channel")
        self._sub_lbl.pack(anchor="w", pady=(0, 14))

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 14))

        # ── URL input ─────────────────────────────────────────────────────────
        url_lbl_row = ttk.Frame(outer)
        url_lbl_row.pack(fill="x", pady=(0, 6))
        ttk.Label(url_lbl_row, text="URL", style="S.White.Section.TLabel").pack(
            side="left")
        ttk.Label(url_lbl_row, text="To paste the link, press [CTRL+V] or Right-Click on it.",
                  style="S.Dim.TLabel").pack(side="left", padx=(10, 0))
        self._settings_help(
            url_lbl_row,
            "How the Main tab works, step by step:\n\n"
            "1.  Paste a YouTube or SoundCloud link — a single track, a "
            "playlist, or a whole channel.\n"
            "2.  Pick a Genre for it (or make one with '+ New'). The genre is "
            "saved into each track's tags.\n"
            "3.  Press '+ Add to Batch' (or Enter) to queue the link. Repeat "
            "to line up as many as you like.\n"
            "4.  Optionally tick 'Skip files already downloaded' so tracks you "
            "already have aren't grabbed again.\n"
            "5.  Press 'Downloads MP3's'. Each track is downloaded, converted "
            "to MP3, tagged, and saved to your download folder — watch the "
            "Queue and progress bars below.",
            wraplength=360).pack(side="left", padx=(10, 0))

        url_row = ttk.Frame(outer)
        url_row.pack(fill="x", pady=(0, 8))

        self._url_var   = tk.StringVar()
        self._url_entry = ttk.Combobox(url_row, textvariable=self._url_var,
                                        values=self._url_history,
                                        style="URL.TCombobox")
        self._url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._url_entry.bind("<Return>",   lambda e: self._batch_add())
        self._url_entry.bind("<FocusIn>",  self._url_focus_in)
        self._url_entry.bind("<FocusOut>", self._url_focus_out)
        self._url_entry.bind("<Button-3>", self._url_context_menu)
        self._url_entry.bind("<<ComboboxSelected>>", self._url_history_selected)
        self._ph_active = True   # placeholder currently showing
        self._url_entry.insert(0, "https://www.youtube.com/  or  https://soundcloud.com/")
        self._url_entry.config(foreground=TEXT_DIM)

        # Right-click context menu for the URL field
        self._url_menu = tk.Menu(self, tearoff=0,
                                  bg=SURFACE2, fg=TEXT, activebackground=BORDER,
                                  activeforeground=TEXT, relief="flat",
                                  font=("Segoe UI", 11))
        self._url_menu.add_command(label="Paste",      command=self._url_paste)
        self._url_menu.add_command(label="Cut",        command=self._url_cut)
        self._url_menu.add_command(label="Copy",       command=self._url_copy)
        self._url_menu.add_separator()
        self._url_menu.add_command(label="Select All", command=self._url_select_all)
        self._url_menu.add_separator()
        self._url_menu.add_command(label="Clear",      command=self._url_clear)

        # ── Genre selector ────────────────────────────────────────────────────
        genre_row = ttk.Frame(outer)
        genre_row.pack(fill="x", pady=(0, 12))

        ttk.Label(genre_row, text="Genre", style="S.White.Section.TLabel").pack(
            side="left", padx=(0, 10))

        self._genre_var = tk.StringVar(value="(none)")
        self._genre_combo = ttk.Combobox(
            genre_row, textvariable=self._genre_var,
            state="readonly", width=39)
        self._genre_combo.pack(side="left", padx=(0, 8))
        self._genre_combo.bind("<<ComboboxSelected>>", self._on_genre_selected)

        ttk.Button(genre_row, text="+ New", style="MainBrowse.TButton",
                   command=self._add_genre).pack(side="left", padx=(0, 16))

        self._refresh_genre_list()

        # ── Batch URL list ────────────────────────────────────────────────────
        self._build_batch_panel(outer)

        # ── Options row ───────────────────────────────────────────────────────
        opt = ttk.Frame(outer)
        opt.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(opt, text="Skip files already downloaded",
                        variable=self._skip_existing,
                        style="S.Bold.TCheckbutton").pack(side="left")

        self._skip_mode_combo = ttk.Combobox(
            opt,
            textvariable=self._skip_mode,
            values=["In Database ~ In Folder", "In Folder Only", "In Database Only"],
            state="readonly", width=20)
        self._skip_mode_combo.pack(side="left", padx=(14, 0))

        ttk.Button(opt, text="📂  Open Folder", style="MainBrowse.TButton",
                   command=self._open_download_dir).pack(side="right")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(4, 10))

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", pady=(0, 14))
        self._dl_btn = ttk.Button(btn_row, text="Downloads MP3's",
                                   style="Download.TButton", command=self._start)
        self._dl_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._cancel_btn = ttk.Button(btn_row, text="Cancel",
                                       style="Cancel.TButton",
                                       state="disabled", command=self._cancel)
        self._cancel_btn.pack(side="left", ipadx=12)
        self._pause_btn = ttk.Button(btn_row, text="⏸  Pause",
                                      style="Pause.TButton",
                                      state="disabled", command=self._toggle_pause)
        self._pause_btn.pack(side="left", padx=(8, 0))

        # ── Progress card ─────────────────────────────────────────────────────
        prog = tk.Frame(outer, bg=SURFACE, padx=16, pady=12,
                         highlightthickness=1, highlightbackground=YT_DARK,
                         height=130)
        prog.pack(fill="x", pady=(0, 14))
        prog.pack_propagate(False)

        cr = tk.Frame(prog, bg=SURFACE)
        cr.pack(fill="x", pady=(0, 5))
        tk.Label(cr, text="Current", font=("Segoe UI", 9),
                  fg=TEXT_DIM, bg=SURFACE).pack(side="left")
        self._cur_lbl = tk.Label(cr, text="—", font=("Segoe UI", 11, "bold"),
                                   fg=TEXT_MED, bg=SURFACE, anchor="w")
        self._cur_lbl.pack(side="left", padx=(8, 0), fill="x", expand=True)
        self._bitrate_lbl = tk.Label(cr, text="", font=("Consolas", 9),
                                      fg=SUCCESS, bg=SURFACE)
        self._bitrate_lbl.pack(side="right", padx=(8, 6))
        self._speed_lbl = tk.Label(cr, text="", font=("Consolas", 9),
                                    fg=TEXT_DIM, bg=SURFACE)
        self._speed_lbl.pack(side="right")

        self._vid_progress = ttk.Progressbar(prog, mode="determinate",
                                               style="Accent.Horizontal.TProgressbar")
        self._vid_progress.pack(fill="x", pady=(0, 10))

        ovr = tk.Frame(prog, bg=SURFACE)
        ovr.pack(fill="x", pady=(0, 5))
        tk.Label(ovr, text="Overall", font=("Segoe UI", 9),
                  fg=TEXT_DIM, bg=SURFACE).pack(side="left")
        self._ov_lbl = tk.Label(ovr, text="—", font=("Segoe UI", 10),
                                  fg=TEXT_DIM, bg=SURFACE)
        self._ov_lbl.pack(side="left", padx=(8, 0))
        self._ov_stats_lbl = tk.Label(ovr, text="", font=("Segoe UI", 9),
                                        fg=TEXT_DIM, bg=SURFACE, anchor="e")
        self._ov_stats_lbl.pack(side="right", padx=(0, 4))
        self._overall_progress = ttk.Progressbar(prog, mode="determinate",
                                                   style="Maroon.Horizontal.TProgressbar")
        self._overall_progress.pack(fill="x")

        # ── Queue ─────────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 10))
        qhdr = ttk.Frame(outer)
        qhdr.pack(fill="x", pady=(0, 6))
        ttk.Label(qhdr, text="Queue", style="White.Section.TLabel").pack(side="left")
        self._qcount_lbl = ttk.Label(qhdr, text="", style="Dim.TLabel")
        self._qcount_lbl.pack(side="left", padx=(8, 0))

        q_outer = tk.Frame(outer, bg=SURFACE2,
                            highlightthickness=1, highlightbackground=YT_DARK)
        q_outer.pack(fill="x")

        self._qtxt = tk.Text(
            q_outer, font=("Consolas", 9), bg=SURFACE2, fg=TEXT_DIM,
            relief="flat", state="disabled", wrap="none",
            selectbackground=BORDER, selectforeground=TEXT,
            padx=8, pady=4, height=12, cursor="arrow")

        qscroll = ttk.Scrollbar(q_outer, orient="vertical",
                                 command=self._qtxt.yview)
        self._qtxt.configure(yscrollcommand=qscroll.set)
        qscroll.pack(side="right", fill="y")
        self._qtxt.pack(side="left", fill="x", expand=True)

        # Bind mousewheel — return "break" to prevent main canvas from intercepting
        def _on_queue_mousewheel(e):
            self._qtxt.yview_scroll(int(-1*(_wheel_delta(e)/120)), "units")
            return "break"
        _bind_wheel(self._qtxt, _on_queue_mousewheel)

        # Queue text tags for row states
        self._qtxt.tag_configure("q_pending",  foreground=TEXT_DIM)
        self._qtxt.tag_configure("q_active",   foreground=TEXT)
        self._qtxt.tag_configure("q_done",     foreground=SUCCESS)
        self._qtxt.tag_configure("q_skipped",  foreground=SKIP_COL)
        self._qtxt.tag_configure("q_error",    foreground=YT_RED)

    # ══════════════════════════════════════════════════════════════════════════
    # Settings tab — the form plus the autosave handlers that back each control
    # ══════════════════════════════════════════════════════════════════════════
    # ── Settings tab ──────────────────────────────────────────────────────────
    def _settings_help(self, parent, tip, wraplength=360):
        """Return a tiny '?'-in-a-box help icon (the caller packs it) that shows
        *tip* on hover. Used at the end of a Settings option so the hover target
        is an explicit icon instead of the checkbox / dropdown / label itself."""
        icon = tk.Label(parent, text="?", font=("Segoe UI", 7, "bold"),
                        fg="#7F7F7F", bg=BG, padx=1, pady=0,
                        highlightthickness=1, highlightbackground="#7F7F7F",
                        cursor="question_arrow")
        Tooltip(icon, tip, wraplength=wraplength)
        return icon

    def _open_containing_folder(self, path):
        """Open the folder holding *path* in the system file manager."""
        folder = os.path.dirname(path) or path
        try:
            os.makedirs(folder, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Folder",
                f"Unable to open the folder:\n{exc}\n\nPath: {folder}")

    def _build_settings_tab(self, parent):
        """Build the Settings tab: save folder, bitrate, behavior, and automation controls."""
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        self._settings_canvas, outer = self._make_scrollable(
            wrapper, (28, 28, 28, 18))

        # ── Content (all original settings widgets go into outer) ─────────────

        # Title — gear icon tinted blue, "Settings" in the default title colour
        title_row = ttk.Frame(outer)
        title_row.pack(anchor="w", pady=(0, 16))
        ttk.Label(title_row, text="⚙", style="S.TitleIcon.TLabel").pack(side="left")
        ttk.Label(title_row, text="  Settings", style="S.Title.TLabel").pack(side="left")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 20))

        # ── Automation / Startup ──────────────────────────────────────────
        ttk.Label(outer, text="Automation/Startup",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 6))

        if sys.platform == "win32":
            startup_row = ttk.Frame(outer)
            startup_row.pack(fill="x", pady=(2, 4))
            ttk.Checkbutton(
                startup_row, text="Run App on Startup",
                variable=self._run_at_startup,
                command=self._on_run_at_startup_toggle,
                style="S.Opt.TCheckbutton").pack(side="left")

            start_min_row = ttk.Frame(outer)
            start_min_row.pack(fill="x", pady=(2, 4))
            ttk.Checkbutton(
                start_min_row, text="Start App Minimized to System Tray",
                variable=self._start_minimized,
                style="S.Opt.TCheckbutton").pack(side="left")

            tray_row = ttk.Frame(outer)
            tray_row.pack(fill="x", pady=(2, 4))
            ttk.Checkbutton(
                tray_row,
                text="Minimize to System Tray",
                variable=self._minimize_to_tray,
                style="S.Opt.TCheckbutton").pack(side="left")
            self._settings_help(tray_row,
                "Keeps Watch-List scheduler running in the background"
                ).pack(side="left", padx=(8, 0))

        # Scan Watch List on startup
        startup_scan_row = ttk.Frame(outer)
        startup_scan_row.pack(fill="x", pady=(2, 4))
        self._startup_scan_cb = ttk.Checkbutton(
            startup_scan_row,
            text="Scan Watch List for new uploads when the app starts",
            variable=self._watchlist_scan_on_startup,
            style="S.Opt.TCheckbutton")
        self._startup_scan_cb.pack(side="left")

        # Auto-add channels to Watch List (moved here from the Watch List section)
        autoadd_row = ttk.Frame(outer)
        autoadd_row.pack(fill="x", pady=(2, 4))
        self._auto_add_cb = ttk.Checkbutton(
            autoadd_row, text="Auto-add channels to Watch List after downloading",
            variable=self._auto_add_to_watchlist,
            style="S.Opt.TCheckbutton")
        self._auto_add_cb.pack(side="left")

        # Auto-download interval dropdown (moved to the bottom of the section)
        auto_row = ttk.Frame(outer)
        auto_row.pack(fill="x", pady=(4, 8))
        ttk.Label(auto_row, text="Auto-download Watch-List channels every:",
                  style="S.TLabel").pack(side="left", padx=(0, 10))
        self._auto_dl_combo = ttk.Combobox(
            auto_row, textvariable=self._auto_dl_interval,
            values=AUTO_DOWNLOAD_OPTIONS,
            state="readonly", width=10)
        self._auto_dl_combo.pack(side="left")
        self._settings_help(
            auto_row,
            "How often to automatically scan every watched channel for new "
            "uploads and download them. Each run scans all channels first, "
            "then downloads everything new. Default 1 day; 'Off' disables it.",
            wraplength=320).pack(side="left", padx=(8, 0))

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 18))

        # ── Time / Length Limiter ─────────────────────────────────────────────
        _row = ttk.Frame(outer)
        _row.pack(fill="x", pady=(0, 8))
        ttk.Label(_row, text="Time / Length Limiter",
                  style="S.White.Section.TLabel").pack(side="left")
        self._settings_help(_row,
            "Skip any file whose duration exceeds the limit below. "
            "Uncheck to allow files of any length.").pack(side="left", padx=(8, 0))

        limit_enable_row = ttk.Frame(outer)
        limit_enable_row.pack(fill="x", pady=(0, 8))

        ttk.Checkbutton(limit_enable_row,
                        text="Enable",
                        variable=self._limit_enabled,
                        command=self._on_limiter_toggle,
                        style="S.Opt.TCheckbutton"
                        ).pack(side="left", padx=(0, 20))

        tk.Label(limit_enable_row, text="Max Length:",
                 font=("Segoe UI", 10, "bold"), fg=TEXT_MED, bg=BG
                 ).pack(side="left", padx=(0, 10))

        self._limit_minus_btn = tk.Button(
            limit_enable_row, text="−", font=("Segoe UI", 8, "bold"),
            bg=SURFACE2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, width=2, pady=0, cursor="hand2",
            command=self._limit_decrement)
        self._limit_minus_btn.pack(side="left", padx=(0, 2))

        self._limit_slider = tk.Scale(
            limit_enable_row,
            variable=self._limit_minutes,
            from_=1, to=180,
            orient="horizontal",
            length=227,
            resolution=1,
            width=18,
            sliderlength=18,
            showvalue=0,
            bd=0,
            bg=BG, fg=TEXT, troughcolor=SURFACE2,
            highlightthickness=0,
            activebackground=YT_RED,
            font=("Segoe UI", 10),
            command=self._on_limiter_slider)
        self._limit_slider.pack(side="left")
        self._limit_slider.bind("<ButtonRelease-1>",
                                lambda e: self._autosave_limiter_settings())

        self._limit_plus_btn = tk.Button(
            limit_enable_row, text="+", font=("Segoe UI", 8, "bold"),
            bg=SURFACE2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, width=2, pady=0, cursor="hand2",
            command=self._limit_increment)
        self._limit_plus_btn.pack(side="left", padx=(2, 10))

        self._limit_value_lbl = tk.Label(
            limit_enable_row, text="", width=14,
            font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG, anchor="w")
        self._limit_value_lbl.pack(side="left")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── MP3 Bitrate Selector ──────────────────────────────────────────────
        ttk.Label(outer, text="Audio Output",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 8))

        bitrate_row = ttk.Frame(outer)
        bitrate_row.pack(fill="x", pady=(0, 10))

        tk.Label(bitrate_row, text="Output Quality:",
                 font=("Segoe UI", 10, "bold"), fg=TEXT_MED, bg=BG
                 ).pack(side="left", padx=(0, 12))

        self._bitrate_combo = ttk.Combobox(
            bitrate_row,
            textvariable=self._bitrate_quality,
            values=["128 kbps", "192 kbps", "224 kbps", "256 kbps", "320 kbps"],
            state="readonly", width=14)
        self._bitrate_combo.pack(side="left", padx=(0, 14))
        self._settings_help(bitrate_row,
            "192 kbps = good quality  •  320 kbps = maximum MP3 quality").pack(
                side="left", padx=(0, 0))

        # ── No-conversion checkbox ────────────────────────────────────────────
        no_conv_row = ttk.Frame(outer)
        no_conv_row.pack(fill="x", pady=(0, 4))
        self._no_conv_cb = ttk.Checkbutton(no_conv_row,
                        text="Keep original format (no conversion)",
                        variable=self._no_conversion,
                        command=self._on_no_conversion_toggle,
                        style="S.Opt.TCheckbutton")
        self._no_conv_cb.pack(side="left")
        self._settings_help(
            no_conv_row,
            "When enabled, files are saved in their original format "
            "and bitrate without conversion to MP3. YouTube typically serves "
            ".webm (Opus) or .m4a (AAC); SoundCloud serves .mp3 or .webm. "
            "Your folder will contain a mix of extensions.",
            wraplength=400).pack(side="left", padx=(8, 0))

        # Apply initial enabled/disabled state for the bitrate combo
        self._on_no_conversion_toggle()

        # ── Cover art ─────────────────────────────────────────────────────────
        cover_row = ttk.Frame(outer)
        cover_row.pack(fill="x", pady=(10, 4))

        tk.Label(cover_row, text="Cover Art:",
                 font=("Segoe UI", 10, "bold"), fg=TEXT_MED, bg=BG
                 ).pack(side="left", padx=(0, 12))

        self._cover_art_combo = ttk.Combobox(
            cover_row,
            textvariable=self._cover_art_mode,
            values=[_COVER_ART_LABELS[m] for m in cb_artwork.COVER_ART_MODES],
            state="readonly", width=28)
        self._cover_art_combo.pack(side="left", padx=(0, 14))
        self._settings_help(
            cover_row,
            "Embeds the YouTube/SoundCloud thumbnail into each MP3 so cover art "
            "shows in Windows Explorer, media players and on Android. A copy is "
            "also kept in a hidden .artwork folder beside the tracks. Cropping "
            "to square fills the art slot; keeping 16:9 letterboxes it.",
            wraplength=400).pack(side="left", padx=(0, 0))

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Download Behavior ─────────────────────────────────────────────────
        beh_title_row = ttk.Frame(outer)
        beh_title_row.pack(fill="x", pady=(0, 8))
        _lbl = ttk.Label(beh_title_row, text="Download Behavior",
                  style="S.White.Section.TLabel")
        _lbl.pack(side="left")
        ttk.Label(beh_title_row, text="(Experimental)",
                  style="S.Dim.TLabel").pack(side="left", padx=(8, 0))
        self._settings_help(beh_title_row,
            "Options that control how DJ-CrateBuilder connects and paces "
            "requests. These can help avoid throttling, geographic "
            "restrictions, or IP-banning from YouTube/SoundCloud when "
            "doing entire channel/batch downloads.").pack(side="left", padx=(8, 0))

        # Geo-bypass checkbox
        geo_row = ttk.Frame(outer)
        geo_row.pack(fill="x", pady=(0, 4))
        _cb = ttk.Checkbutton(geo_row,
                        text="Enable geo-bypass",
                        variable=self._geo_bypass,
                        style="S.Opt.TCheckbutton")
        _cb.pack(side="left")
        self._settings_help(geo_row,
            "Bypass geographic IP-based restrictions using a fake "
            "X-Forwarded-For header").pack(side="left", padx=(8, 0))

        # Rotate User-Agent checkbox
        ua_row = ttk.Frame(outer)
        ua_row.pack(fill="x", pady=(0, 4))
        _cb = ttk.Checkbutton(ua_row,
                        text="Rotate User-Agent",
                        variable=self._rotate_ua,
                        style="S.Opt.TCheckbutton")
        _cb.pack(side="left")
        self._settings_help(ua_row,
            "Send a randomized browser User-Agent string (consistent within "
            "each session)").pack(side="left", padx=(8, 0))

        # Sleep interval checkbox + mode selector
        sleep_row = ttk.Frame(outer)
        sleep_row.pack(fill="x", pady=(0, 4))
        _throttle_cb = ttk.Checkbutton(sleep_row,
                        text="Throttle Requests",
                        variable=self._sleep_enabled,
                        command=self._on_sleep_toggle,
                        style="S.Opt.TCheckbutton")
        _throttle_cb.pack(side="left")
        self._settings_help(
            sleep_row,
            "Pause between requests to avoid rate-limiting or IP bans during "
            "large channel / batch downloads. Auto picks delays from the "
            "selected preset; Manual lets you set exact min/max seconds.",
            wraplength=320).pack(side="left", padx=(8, 0))

        self._sleep_labels = []

        _sl = tk.Label(sleep_row, text="Mode:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _sl.pack(side="left", padx=(16, 4))
        self._sleep_labels.append(_sl)

        self._sleep_mode_combo = ttk.Combobox(
            sleep_row, textvariable=self._sleep_mode,
            values=["Auto", "Manual"],
            state="readonly", width=8)
        self._sleep_mode_combo.pack(side="left", padx=(0, 8))
        self._sleep_mode_combo.bind("<<ComboboxSelected>>",
                                     lambda _: self._on_sleep_toggle())

        self._preset_lbl = tk.Label(sleep_row, text="Preset:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        self._sleep_labels.append(self._preset_lbl)

        self._sleep_preset_combo = ttk.Combobox(
            sleep_row,
            textvariable=self._sleep_preset,
            values=list(THROTTLE_PRESETS.keys()),
            state="readonly", width=22)

        # Container for Auto/Manual sub-rows (stable pack position)
        self._sleep_detail = ttk.Frame(outer)
        self._sleep_detail.pack(fill="x")

        # Auto descriptions
        self._sleep_auto_row = ttk.Frame(self._sleep_detail)
        Tooltip(self._sleep_preset_combo,
                "Light = Downloading 50 files or less, per 24hrs.\n"
                "Moderate = Downloading between 50-200 files, per 24hrs.\n"
                "Aggressive = Downloading 200 files or more, per 24hrs.",
                wraplength=360)

        # Manual spinboxes
        self._sleep_manual_row = ttk.Frame(self._sleep_detail)
        self._sleep_manual_labels = []

        _sl = tk.Label(self._sleep_manual_row, text="      Wait between",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _sl.pack(side="left", padx=(0, 4))
        self._sleep_manual_labels.append(_sl)

        self._sleep_min_spin = tk.Spinbox(
            self._sleep_manual_row, from_=0, to=60,
            textvariable=self._sleep_min, width=4,
            font=("Segoe UI", 11),
            bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
            disabledbackground=SURFACE2, disabledforeground="#444444",
            buttonbackground=SURFACE2,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER,
            command=self._autosave_behavior_settings)
        self._sleep_min_spin.pack(side="left", padx=(0, 4))

        _sl = tk.Label(self._sleep_manual_row, text="and",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _sl.pack(side="left", padx=(0, 4))
        self._sleep_manual_labels.append(_sl)

        self._sleep_max_spin = tk.Spinbox(
            self._sleep_manual_row, from_=1, to=120,
            textvariable=self._sleep_max, width=4,
            font=("Segoe UI", 11),
            bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
            disabledbackground=SURFACE2, disabledforeground="#444444",
            buttonbackground=SURFACE2,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER,
            command=self._autosave_behavior_settings)
        self._sleep_max_spin.pack(side="left", padx=(0, 4))

        _sl = tk.Label(self._sleep_manual_row, text="seconds per download",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _sl.pack(side="left")
        self._sleep_manual_labels.append(_sl)

        self._on_sleep_toggle()

        # ── Browser Cookies ───────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 14))

        cookie_row = ttk.Frame(outer)
        cookie_row.pack(fill="x", pady=(0, 4))
        self._use_cookies_cb = ttk.Checkbutton(cookie_row,
                        text="Use browser cookies",
                        variable=self._use_cookies,
                        command=self._on_cookies_toggle,
                        style="S.Opt.TCheckbutton")
        self._use_cookies_cb.pack(side="left")
        self._settings_help(
            cookie_row,
            "Authenticate downloads using a browser login session (increases speed).\n\n"
            "⚠ It is strongly recommended to create a dedicated/throwaway account. "
            "Chrome 127+ blocks cookie extraction (DPAPI) — use Firefox or a "
            "cookie file instead. For cookie files: install the 'Get cookies.txt "
            "LOCALLY' browser extension.",
            wraplength=400).pack(side="left", padx=(8, 0))

        # Cookie detail widgets
        self._cookie_labels = []

        # Method row
        cd_method = ttk.Frame(outer)
        cd_method.pack(fill="x", pady=(0, 4))

        _cl = tk.Label(cd_method, text="      Method:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _cl.pack(side="left", padx=(0, 4))
        self._cookie_labels.append(_cl)

        self._cookie_method_combo = ttk.Combobox(
            cd_method, textvariable=self._cookie_method,
            values=["Browser", "Cookie File"],
            state="readonly", width=12)
        self._cookie_method_combo.pack(side="left", padx=(0, 8))
        self._cookie_method_combo.bind("<<ComboboxSelected>>",
            lambda _: (self._on_cookies_toggle(), self._autosave_behavior_settings()))

        self._open_yt_btn = tk.Button(
            cd_method, text="🌐  Open Browser",
            font=("Segoe UI", 8), bg="#7F7F7F", fg="#ffffff",
            activebackground="#949494", activeforeground=TEXT,
            relief="flat", bd=0, padx=8, pady=1, cursor="hand2",
            command=self._open_youtube_in_selected_browser)
        self._open_yt_btn.pack(side="left", padx=(0, 16))
        Tooltip(self._open_yt_btn,
                "Opens selected browser to YouTube. Play at least one "
                "video to activate the session's cookies.")

        # Container for Browser/File sub-rows (stable pack position)
        self._cookie_detail = ttk.Frame(outer)
        self._cookie_detail.pack(fill="x")

        # Browser row (inside container)
        self._cookie_browser_row = ttk.Frame(self._cookie_detail)

        _cl = tk.Label(self._cookie_browser_row, text="      Browser:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _cl.pack(side="left", padx=(0, 4))
        self._cookie_labels.append(_cl)
        self._browser_lbl = _cl

        self._cookies_browser_combo = ttk.Combobox(
            self._cookie_browser_row, textvariable=self._cookies_browser,
            values=["Firefox", "Chrome", "Edge", "Brave", "Opera", "Chromium"],
            state="readonly", width=12)
        self._cookies_browser_combo.pack(side="left", padx=(0, 16))
        self._cookies_browser_combo.bind("<<ComboboxSelected>>",
            lambda _: (self._update_howto_label(), self._autosave_behavior_settings()))

        self._profile_lbl = tk.Label(self._cookie_browser_row, text="Profile:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        self._profile_lbl.pack(side="left", padx=(0, 4))
        self._cookie_labels.append(self._profile_lbl)

        self._cookies_profile_entry = tk.Entry(
            self._cookie_browser_row, textvariable=self._cookies_profile, width=18,
            font=("Segoe UI", 11),
            bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
            disabledbackground=SURFACE2, disabledforeground="#444444",
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=YT_RED)
        self._cookies_profile_entry.pack(side="left", padx=(0, 6))
        self._cookies_profile_entry.bind("<FocusOut>",
            lambda _: self._autosave_behavior_settings())
        # Light-grey warning triangle carrying the profile hint (replaces the
        # tooltip that used to sit on the Profile entry itself).
        self._profile_warn = tk.Label(
            self._cookie_browser_row, text="⚠",
            font=("Segoe UI", 11), fg="#B3B3B3", bg=BG, cursor="hand2")
        self._profile_warn.pack(side="left", padx=(0, 8))
        Tooltip(self._profile_warn,
                "Leave blank to use the default browser profile!")

        # Cookie file row (inside container)
        self._cookie_file_row = ttk.Frame(self._cookie_detail)
        self._cookie_file_labels = []

        _cl = tk.Label(self._cookie_file_row, text="      File:",
                 font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG)
        _cl.pack(side="left", padx=(0, 4))
        self._cookie_file_labels.append(_cl)

        self._cookie_file_entry = tk.Entry(
            self._cookie_file_row, textvariable=self._cookie_file, width=42,
            font=("Segoe UI", 11),
            bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
            disabledbackground=SURFACE2, disabledforeground="#444444",
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=YT_RED)
        self._cookie_file_entry.pack(side="left", padx=(0, 8))
        self._cookie_file_entry.bind("<FocusOut>",
            lambda _: self._autosave_behavior_settings())

        self._cookie_browse_btn = tk.Button(
            self._cookie_file_row, text="Browse…",
            font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
            command=self._browse_cookie_file)
        self._cookie_browse_btn.pack(side="left", padx=(0, 8))

        self._settings_help(self._cookie_file_row,
            "Netscape/Mozilla cookie.txt format").pack(side="left", padx=(0, 8))

        howto_row = ttk.Frame(outer)
        howto_row.pack(fill="x", pady=(6, 0))
        self._howto_lbl = tk.Label(howto_row,
                 text=f"      How-To:  Setting Up a Dedicated {self._cookies_browser.get()} Profile",
                 font=("Segoe UI", 11, "bold"), fg=TEXT_DIM, bg=BG, anchor="w")
        self._howto_lbl.pack(side="left")
        self._howto_btn = tk.Button(howto_row, text="VIEW",
                  font=("Segoe UI", 8), bg="#7F7F7F", fg="#ffffff",
                  activebackground="#949494", activeforeground=TEXT,
                  relief="flat", bd=0, padx=8, pady=1, cursor="hand2",
                  command=self._open_cookie_howto)
        self._howto_btn.pack(side="left", padx=(10, 0))

        self._on_cookies_toggle()

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Base directory ────────────────────────────────────────────────────
        ttk.Label(outer, text="Default Save Directory",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 8))

        dir_row = ttk.Frame(outer)
        dir_row.pack(fill="x", pady=(0, 8))

        self._settings_dir_var = tk.StringVar(value=self._base_dir)
        self._settings_dir_entry = ttk.Entry(dir_row,
                                              textvariable=self._settings_dir_var,
                                              style="Settings.TEntry")
        self._settings_dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._settings_dir_entry.bind("<Return>",
            lambda _: self._save_settings())
        self._settings_dir_entry.bind("<FocusOut>",
            lambda _: self._save_settings())
        ttk.Button(dir_row, text="Browse…", style="Browse.TButton",
                   command=self._settings_browse).pack(side="left")

        # Save confirmation sits inline beside Browse so it doesn't reserve an
        # empty row beneath the directory field.
        self._settings_msg = ttk.Label(dir_row, text="", style="S.Dim.TLabel")
        self._settings_msg.pack(side="left", padx=(10, 0))

        # ── Logs & Database ───────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(8, 20))

        # Log size limit — caps activity.log and debug.log (each), trimming the
        # oldest lines from the top once a file passes the chosen size.
        limit_row = ttk.Frame(outer)
        limit_row.pack(fill="x", pady=(0, 14))
        ttk.Label(limit_row, text="Log Size Limit",
                  style="S.White.Section.TLabel").pack(side="left")
        self._log_limit_combo = ttk.Combobox(
            limit_row, textvariable=self._log_limit_var,
            values=list(self._LOG_LIMIT_CHOICES),
            state="readonly", width=12)
        self._log_limit_combo.pack(side="left", padx=(10, 0))
        self._log_limit_combo.bind(
            "<<ComboboxSelected>>", self._autosave_log_limit)
        self._settings_help(limit_row,
            "Caps the size of the Activity Log and Debug Log (each file "
            "separately). When a log grows past this size, the oldest lines at "
            "the top are removed to make room for the newest — so the file "
            "keeps the most recent activity and never grows without bound. "
            "'Unlimited' disables trimming.").pack(side="left", padx=(8, 0))

        _hdr = ttk.Frame(outer)
        _hdr.pack(anchor="w", pady=(0, 6))
        ttk.Label(_hdr, text="Activity Log",
                  style="S.White.Section.TLabel").pack(side="left")
        self._settings_help(_hdr,
            "A color-coded record of every downloaded, skipped, and failed "
            "file. View it here in the built-in viewer, or open it in your "
            "system's default text editor.").pack(side="left", padx=(8, 0))

        log_row = ttk.Frame(outer)
        log_row.pack(fill="x", pady=(0, 4))

        tk.Button(log_row, text="📋  View Log",
                  font=("Segoe UI", 10, "bold"),
                  bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._open_log_viewer).pack(side="left", padx=(0, 8))

        tk.Button(log_row, text="↗  Open in System Viewer",
                  font=("Segoe UI", 10, "bold"),
                  bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._open_log_external).pack(side="left", padx=(0, 16))

        # Resolved log path — click to reveal the file in the system file manager.
        self._log_path_lbl = tk.Label(
            log_row, text="", font=("Segoe UI", 11, "underline"),
            fg=LINK_COL, bg=BG, cursor="hand2", anchor="w")
        self._log_path_lbl.pack(side="left", fill="x", expand=True)
        self._log_path_lbl.bind(
            "<Button-1>", lambda _e: self._open_containing_folder(self._log_path))
        Tooltip(self._log_path_lbl, "Open this folder in your file explorer")
        self._refresh_log_path_label()

        # ── Debug log ─────────────────────────────────────────────────────────
        _hdr = ttk.Frame(outer)
        _hdr.pack(anchor="w", pady=(16, 6))
        ttk.Label(_hdr, text="Debug Log",
                  style="S.White.Section.TLabel").pack(side="left")
        self._settings_help(_hdr,
            "Detailed diagnostic log capturing cookie configuration, "
            "yt-dlp options, request/response data, and full error "
            "tracebacks. Useful for troubleshooting download failures.").pack(
                side="left", padx=(8, 0))

        dbg_row = ttk.Frame(outer)
        dbg_row.pack(fill="x", pady=(0, 4))

        tk.Button(dbg_row, text="🔍  View Log",
                  font=("Segoe UI", 10, "bold"),
                  bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._open_debug_log_viewer).pack(side="left", padx=(0, 8))

        tk.Button(dbg_row, text="↗  Open in System Viewer",
                  font=("Segoe UI", 10, "bold"),
                  bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._open_debug_log_external).pack(side="left", padx=(0, 16))

        self._debug_path_lbl = tk.Label(
            dbg_row, text="", font=("Segoe UI", 11, "underline"),
            fg=LINK_COL, bg=BG, cursor="hand2", anchor="w")
        self._debug_path_lbl.pack(side="left", fill="x", expand=True)
        self._debug_path_lbl.bind(
            "<Button-1>",
            lambda _e: self._open_containing_folder(self._debug_log_path))
        Tooltip(self._debug_path_lbl, "Open this folder in your file explorer")
        self._refresh_debug_path_label()

        # ── Database management ────────────────────────────────────────────────
        _hdr = ttk.Frame(outer)
        _hdr.pack(anchor="w", pady=(16, 6))
        ttk.Label(_hdr, text="Downloads Database",
                  style="S.White.Section.TLabel").pack(side="left")
        self._settings_help(_hdr,
            "The SQLite database tracks every download for fast "
            "lookups and Watch List history. If it gets corrupted "
            "or deleted, rebuild it from the files on disk.").pack(
                side="left", padx=(8, 0))

        db_row = ttk.Frame(outer)
        db_row.pack(fill="x", pady=(0, 4))

        self._open_db_btn = ttk.Button(
            db_row, text="🗂  Open Database",
            style="DlBtn.TButton",
            command=self._open_database_viewer)
        self._open_db_btn.pack(side="left", padx=(0, 8))

        self._rebuild_db_btn = ttk.Button(
            db_row, text="🔄  Rebuild Database from Files",
            style="Orange.TButton",
            command=self._rebuild_db_from_files)
        self._rebuild_db_btn.pack(side="left", padx=(0, 8))
        self._settings_help(db_row,
            "Scans the .mp3 files already in your library folders and "
            "rebuilds the database from them. This is safe to run at any "
            "time — it clears and rebuilds from scratch.").pack(
                side="left", padx=(0, 16))

        art_row = ttk.Frame(outer)
        art_row.pack(fill="x", pady=(0, 4))

        self._fetch_art_btn = ttk.Button(
            art_row, text="🖼  Fetch Missing Artwork",
            style="DlBtn.TButton",
            command=self._fetch_missing_artwork)
        self._fetch_art_btn.pack(side="left", padx=(0, 8))
        self._settings_help(art_row,
            "Finds cover art for tracks you downloaded before the Cover Art "
            "feature existed, and embeds it into them. Re-uses artwork already "
            "on disk where possible, so re-running it is cheap.").pack(
                side="left", padx=(0, 16))

        self._db_path_lbl = tk.Label(
            db_row, text="", font=("Segoe UI", 11, "underline"),
            fg=LINK_COL, bg=BG, cursor="hand2", anchor="w")
        self._db_path_lbl.pack(side="left", fill="x", expand=True)
        self._db_path_lbl.bind(
            "<Button-1>", lambda _e: self._open_containing_folder(self._db_path))
        Tooltip(self._db_path_lbl, "Open this folder in your file explorer")
        if hasattr(self, "_db_path"):
            short = self._db_path.replace(os.path.expanduser("~"), "~")
            self._db_path_lbl.config(text=short)

        self._refresh_limit_label()

    def _settings_browse(self):
        """Prompt for a new base save folder and apply it to the Settings field."""
        d = filedialog.askdirectory(title="Choose base save folder",
                                     initialdir=self._settings_dir_var.get())
        if d:
            self._settings_dir_var.set(d)
            self._save_settings()

    def _save_settings(self):
        """Validate and persist the base save folder, re-init dirs and logger."""
        new_base = self._settings_dir_var.get().strip()
        if not new_base:
            messagebox.showwarning("Empty Path",
                                    "Please enter a save directory.")
            return

        self._base_dir = new_base
        self._ensure_dirs()
        self._setup_logger()
        save_config({
            "base_dir":       self._base_dir,
            "limit_enabled":  self._limit_enabled.get(),
            "limit_minutes":  self._limit_minutes.get(),
            "bitrate_quality": self._bitrate_quality.get().split()[0],
            "no_conversion":  self._no_conversion.get(),
            "cover_art_mode": self._cover_art_mode_value(),
            "log_max_mb":     self._log_max_mb,
            "skip_existing":  self._skip_existing.get(),
            "skip_mode":      self._skip_mode.get(),
            "geo_bypass":     self._geo_bypass.get(),
            "rotate_ua":      self._rotate_ua.get(),
            "sleep_enabled":  self._sleep_enabled.get(),
            "sleep_mode":     self._sleep_mode.get(),
            "sleep_preset":   self._sleep_preset.get(),
            "sleep_min":      self._sleep_min.get(),
            "sleep_max":      self._sleep_max.get(),
            "use_cookies":      self._use_cookies.get(),
            "cookie_method":    self._cookie_method.get(),
            "cookies_browser":  self._cookies_browser.get(),
            "cookies_profile":  self._cookies_profile.get(),
            "cookie_file":      self._cookie_file.get(),
            "auto_add_to_watchlist": self._auto_add_to_watchlist.get(),
            "auto_download_interval": self._auto_dl_interval.get(),
            "run_at_startup":     self._run_at_startup.get(),
            "minimize_to_tray":   self._minimize_to_tray.get(),
            "start_minimized":    self._start_minimized.get(),
            "watchlist_scan_on_startup": self._watchlist_scan_on_startup.get(),
            "watchlist_last_download": self._watchlist_last_download,
        })
        self._refresh_genre_list()
        self._update_save_preview()
        self._refresh_log_path_label()
        self._settings_msg.config(text="✓  Settings saved", foreground=SUCCESS)
        self.after(3000, lambda: self._settings_msg.config(text=""))

    def _on_limiter_toggle(self):
        """Enable or disable the slider and +/- buttons based on the checkbox state."""
        state = "normal" if self._limit_enabled.get() else "disabled"
        self._limit_slider.config(state=state)
        if hasattr(self, "_limit_minus_btn"):
            self._limit_minus_btn.config(state=state)
        if hasattr(self, "_limit_plus_btn"):
            self._limit_plus_btn.config(state=state)
        self._refresh_limit_label()

    def _on_limiter_slider(self, _val=None):
        self._refresh_limit_label()

    def _limit_decrement(self):
        """Decrease the time limit by 1 minute, minimum 1."""
        val = max(1, self._limit_minutes.get() - 1)
        self._limit_minutes.set(val)
        self._refresh_limit_label()
        self._autosave_limiter_settings()

    def _limit_increment(self):
        """Increase the time limit by 1 minute, maximum 180."""
        val = min(180, self._limit_minutes.get() + 1)
        self._limit_minutes.set(val)
        self._refresh_limit_label()
        self._autosave_limiter_settings()

    def _refresh_limit_label(self):
        """Update the minutes readout label beside the slider."""
        if not hasattr(self, "_limit_value_lbl"):
            return
        if self._limit_enabled.get():
            mins = self._limit_minutes.get()
            hrs  = mins // 60
            rem  = mins % 60
            if hrs:
                display = f"{hrs}h {rem:02d}m  ({mins} min)"
            else:
                display = f"{mins} min"
            self._limit_value_lbl.config(text=display, fg=TEXT)
            self._limit_slider.config(state="normal")
        else:
            self._limit_value_lbl.config(text="No limit", fg=TEXT_DIM)
            self._limit_slider.config(state="disabled")

    def _autosave_limiter_settings(self, *_):
        """Auto-save limiter settings to config whenever either value changes."""
        cfg = load_config()
        cfg["limit_enabled"] = self._limit_enabled.get()
        cfg["limit_minutes"] = self._limit_minutes.get()
        save_config(cfg)

    def _autosave_bitrate_setting(self, *_):
        """Auto-save bitrate setting and no-conversion flag to config."""
        cfg = load_config()
        cfg["bitrate_quality"] = self._bitrate_quality.get().split()[0]
        cfg["no_conversion"]   = self._no_conversion.get()
        save_config(cfg)

    def _cover_art_mode_value(self):
        """The bare cover-art mode string ('crop'/'original'/'off') behind the
        friendly label the combobox displays. Falls back to the default when the
        variable holds anything unrecognised."""
        return _COVER_ART_MODES_BY_LABEL.get(
            self._cover_art_mode.get(), cb_artwork.DEFAULT_COVER_ART_MODE)

    def _autosave_cover_art_setting(self, *_):
        """Auto-save the cover-art mode to config whenever the combobox changes."""
        cfg = load_config()
        cfg["cover_art_mode"] = self._cover_art_mode_value()
        save_config(cfg)

    def _autosave_log_limit(self, *_):
        """Persist the log size limit and apply it to the live handlers,
        trimming immediately if a file already exceeds the new cap."""
        mb = self._parse_log_limit_mb(self._log_limit_var.get())
        self._log_max_mb = mb
        self._log_max_bytes = mb * 1024 * 1024
        cfg = load_config()
        cfg["log_max_mb"] = mb
        save_config(cfg)
        for fh in (getattr(self, "_log_fh", None), getattr(self, "_dbg_fh", None)):
            if fh is not None:
                fh.max_bytes = self._log_max_bytes
                fh.maybe_trim()

    def _on_no_conversion_toggle(self):
        """Grey out the bitrate combobox when 'no conversion' is enabled."""
        if hasattr(self, "_bitrate_combo"):
            if self._no_conversion.get():
                self._bitrate_combo.config(state="disabled")
            else:
                self._bitrate_combo.config(state="readonly")

    def _autosave_skip_settings(self, *_):
        """Auto-save skip checkbox and mode to config whenever either value changes."""
        cfg = load_config()
        cfg["skip_existing"] = self._skip_existing.get()
        cfg["skip_mode"]     = self._skip_mode.get()
        save_config(cfg)

    def _on_sleep_toggle(self):
        """Switch Auto/Manual content and grey out everything when disabled."""
        enabled = self._sleep_enabled.get()
        mode    = self._sleep_mode.get()
        GREY    = "#444444"

        # Mode combobox
        if hasattr(self, "_sleep_mode_combo"):
            self._sleep_mode_combo.config(
                state="readonly" if enabled else "disabled")

        # Mode label
        for lbl in getattr(self, "_sleep_labels", []):
            if lbl is not self._preset_lbl:
                lbl.config(fg=TEXT_DIM if enabled else GREY)

        # Switch visible content based on mode
        if mode == "Auto":
            # Show preset on main row, show auto descriptions, hide manual
            self._preset_lbl.pack(side="left", padx=(8, 4))
            self._sleep_preset_combo.pack(side="left", padx=(0, 8))
            self._sleep_auto_row.pack(fill="x", pady=(0, 4))
            self._sleep_manual_row.pack_forget()
            # Colors
            self._preset_lbl.config(fg=TEXT_DIM if enabled else GREY)
            self._sleep_preset_combo.config(
                state="readonly" if enabled else "disabled")
        else:
            # Hide preset from main row, hide auto descriptions, show manual
            self._preset_lbl.pack_forget()
            self._sleep_preset_combo.pack_forget()
            self._sleep_auto_row.pack_forget()
            self._sleep_manual_row.pack(fill="x", pady=(0, 4))
            # Colors
            for lbl in getattr(self, "_sleep_manual_labels", []):
                lbl.config(fg=TEXT_DIM if enabled else GREY)
            if hasattr(self, "_sleep_min_spin"):
                self._sleep_min_spin.config(
                    state="normal" if enabled else "disabled")
            if hasattr(self, "_sleep_max_spin"):
                self._sleep_max_spin.config(
                    state="normal" if enabled else "disabled")

    def _on_cookies_toggle(self):
        """Switch Browser/File content inside container and grey when disabled."""
        enabled = self._use_cookies.get()
        method  = self._cookie_method.get()
        GREY = "#444444"
        is_browser = method == "Browser"

        # Method combo + label
        if hasattr(self, "_cookie_method_combo"):
            self._cookie_method_combo.config(
                state="readonly" if enabled else "disabled")
        for lbl in getattr(self, "_cookie_labels", []):
            if lbl not in [getattr(self, "_browser_lbl", None),
                           getattr(self, "_profile_lbl", None)]:
                lbl.config(fg=TEXT_DIM if enabled else GREY)

        # Open-Browser button only makes sense for the Browser method;
        # hide it entirely when the user is on Cookie File.
        if hasattr(self, "_open_yt_btn"):
            if is_browser:
                self._open_yt_btn.pack(side="left", padx=(0, 16))
            else:
                self._open_yt_btn.pack_forget()

        # Switch visible row inside the container
        if is_browser:
            self._cookie_browser_row.pack(fill="x", pady=(0, 4))
            self._cookie_file_row.pack_forget()
            # Browser row colors
            for lbl in [self._browser_lbl, self._profile_lbl]:
                if lbl:
                    lbl.config(fg=TEXT_DIM if enabled else GREY)
            self._cookies_browser_combo.config(
                state="readonly" if enabled else "disabled")
            self._cookies_profile_entry.config(
                state="normal" if enabled else "disabled")
        else:
            self._cookie_file_row.pack(fill="x", pady=(0, 4))
            self._cookie_browser_row.pack_forget()
            # File row colors
            for lbl in getattr(self, "_cookie_file_labels", []):
                lbl.config(fg=TEXT_DIM if enabled else GREY)
            self._cookie_file_entry.config(
                state="normal" if enabled else "disabled")
            self._cookie_browse_btn.config(
                state="normal" if enabled else "disabled",
                fg=TEXT_DIM if enabled else GREY)

        # Warning notes replaced by tooltip on self._use_cookies_cb

        if hasattr(self, "_howto_lbl"):
            self._howto_lbl.config(fg=TEXT_DIM if enabled else GREY)
        if hasattr(self, "_howto_btn"):
            # Light text so it stays legible on the grey (#7F7F7F) button in
            # both states; dimmer when the cookie section is disabled.
            self._howto_btn.config(
                state="normal" if enabled else "disabled",
                fg="#ffffff" if enabled else "#cfcfcf")

    def _resolve_sleep_range(self):
        """Return (min, max) seconds based on the current throttle mode."""
        if self._sleep_mode.get() == "Auto":
            preset = self._sleep_preset.get()
            s_min, s_max = THROTTLE_PRESETS.get(preset, (1, 5))
        else:
            s_min = self._sleep_min.get()
            s_max = self._sleep_max.get()
        # Ensure max >= min
        if s_max < s_min:
            s_max = s_min
        return s_min, s_max

    def _autosave_behavior_settings(self, *_):
        """Auto-save download behavior settings whenever any value changes."""
        cfg = load_config()
        cfg["geo_bypass"]    = self._geo_bypass.get()
        cfg["rotate_ua"]     = self._rotate_ua.get()
        cfg["sleep_enabled"] = self._sleep_enabled.get()
        cfg["sleep_mode"]    = self._sleep_mode.get()
        cfg["sleep_preset"]  = self._sleep_preset.get()
        cfg["sleep_min"]     = self._sleep_min.get()
        cfg["sleep_max"]     = self._sleep_max.get()
        cfg["use_cookies"]      = self._use_cookies.get()
        cfg["cookie_method"]    = self._cookie_method.get()
        cfg["cookies_browser"]  = self._cookies_browser.get()
        cfg["cookies_profile"]  = self._cookies_profile.get()
        cfg["cookie_file"]      = self._cookie_file.get()
        cfg["auto_add_to_watchlist"] = self._auto_add_to_watchlist.get()
        save_config(cfg)

    def _autosave_automation_settings(self, *_):
        """Persist the auto-download interval, tray, startup-scan toggle, and the
        last-download schedule anchor."""
        cfg = load_config()
        cfg["auto_download_interval"] = self._auto_dl_interval.get()
        cfg["minimize_to_tray"] = self._minimize_to_tray.get()
        cfg["start_minimized"] = self._start_minimized.get()
        cfg["watchlist_scan_on_startup"] = self._watchlist_scan_on_startup.get()
        cfg["watchlist_last_download"] = self._watchlist_last_download
        save_config(cfg)
        # Reschedule the timer whenever the interval changes.
        self._reschedule_auto_download()

    # ══════════════════════════════════════════════════════════════════════════
    # Watch List — automation scheduler (periodic scan-all + auto-download timer)
    # ══════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _network_is_reachable(timeout=2.0):
        """Best-effort TCP reachability probe. Tries a couple of stable, high-
        availability endpoints and returns True on the first successful connect,
        False if none answer. Never raises — used only to defer the startup scan
        until the network is actually up."""
        import socket
        for host, port in (("www.youtube.com", 443),
                           ("1.1.1.1", 443),
                           ("8.8.8.8", 53)):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                continue
        return False

    def _watchlist_startup_scan(self):
        """On launch, scan every watched channel (all platforms) so the cards
        show current new-track counts. Runs in the background via
        _watchlist_scan_all; skipped if a scan/download is already underway.
        This only scans — it does not move the auto-download schedule anchor.

        Cold-boot guard: the scan is deferred until the network is reachable.
        When the app auto-starts at Windows login the connection is usually a
        few seconds behind, and scanning while offline fails every channel —
        which (per is_unresolved_channel) strands resolved cards as "needs
        channel ID". We poll for connectivity off the UI thread, then run the
        scan once the network is up (or give up quietly after the budget)."""
        if not self._watchlist_scan_on_startup.get():
            return
        try:
            channels = self._db.get_all_watchlist_channels()
        except Exception:
            return
        if not channels:
            return

        def _wait_then_scan():
            def _ui(fn):
                # Marshal to the UI thread, tolerating a root torn down while we
                # were waiting for the network (the app may be closed mid-wait).
                try:
                    self.after(0, fn)
                except RuntimeError:
                    pass
            for attempt in range(WATCHLIST_STARTUP_NET_TRIES):
                if self._network_is_reachable():
                    _ui(self._watchlist_startup_scan_now)
                    return
                if attempt == 0:
                    _ui(lambda: self._watchlist_log(
                        "🌐 Waiting for the network before the startup scan…",
                        "info"))
                time.sleep(WATCHLIST_STARTUP_NET_DELAY)
            _ui(lambda: self._watchlist_log(
                "Startup scan skipped — no network detected. Channels keep "
                "their links; scan once you're back online.", "info"))

        self._run_bg(_wait_then_scan)

    def _watchlist_startup_scan_now(self):
        """Run the deferred startup scan on the UI thread. Re-checks the busy
        guards, since a manual scan/download may have begun while we waited for
        the network."""
        if self._downloading or self._wl_download_active or self._wl_scan_active:
            return
        self._watchlist_log("🚀 Startup check: scanning all channels…", "info")
        self._watchlist_scan_all()

    def _reschedule_auto_download(self):
        """(Re)arm the periodic auto-download timer from the current interval and
        refresh the 'Next auto-download' label in the Watch List tab. Runs on
        startup, on interval change, and after each Download All New."""
        if self._auto_dl_after_id is not None:
            try:
                self.after_cancel(self._auto_dl_after_id)
            except Exception:
                pass
            self._auto_dl_after_id = None
        secs = interval_label_to_seconds(self._auto_dl_interval.get())
        if secs is None:
            self._wl_next_dl_ts = None      # 'Off' — no timer
            self._wl_update_next_dl_label()
            return
        now = int(time.time())
        elapsed = now - (self._watchlist_last_download or 0)
        delay_ms = 1000 if elapsed >= secs else int((secs - elapsed) * 1000)
        self._wl_next_dl_ts = now + delay_ms // 1000
        self._auto_dl_after_id = self.after(delay_ms, self._auto_download_tick)
        self._wl_update_next_dl_label()

    def _auto_download_tick(self):
        """Fire one scheduled run: scan all, then auto-download new tracks."""
        self._auto_dl_after_id = None
        secs = interval_label_to_seconds(self._auto_dl_interval.get())
        if secs is None:
            self._wl_next_dl_ts = None
            self._wl_update_next_dl_label()
            return
        # Skip (don't interrupt) if a manual scan/download is already running;
        # try again shortly.
        if self._downloading or self._wl_download_active or self._wl_scan_active:
            self._auto_dl_after_id = self.after(60_000, self._auto_download_tick)
            return
        self._watchlist_log("⏰ Scheduled auto-download starting…", "info")
        self._auto_dl_poll_count = 0
        self._watchlist_scan_all()
        # Poll for scan completion, then download + notify.
        self.after(2000, self._auto_download_after_scan)

    # Cap the post-scan wait so a stuck scan can't poll forever (~5 min @ 2s).
    _AUTO_DOWNLOAD_MAX_POLLS = 150

    def _auto_download_after_scan(self):
        """Once scans settle (or we give up waiting), download new tracks + notify.
        When a download starts, _watchlist_download_all_new owns the schedule
        anchor; otherwise advance it here so the next run is a full interval away."""
        if self._wl_scan_active > 0:
            self._auto_dl_poll_count += 1
            if self._auto_dl_poll_count <= self._AUTO_DOWNLOAD_MAX_POLLS:
                self.after(2000, self._auto_download_after_scan)
                return
            # Timed out waiting — record the attempt and reschedule next cycle.
            self._watchlist_log(
                "⏰ Auto-download gave up waiting for scans to finish.", "info")
            self._watchlist_last_download = int(time.time())
            self._autosave_automation_settings()  # persist anchor + reschedule
            return

        channels = self._db.get_all_watchlist_channels()
        total_new = sum(int(c.get("pending_new_count", 0)) for c in channels)
        if total_new > 0:
            n_ch = sum(1 for c in channels if int(c.get("pending_new_count", 0)) > 0)
            # Download All New stamps the anchor + reschedules for us.
            self._watchlist_download_all_new()
            self._notify_tray(
                "Watch List",
                f"{total_new} new track(s) downloading across {n_ch} channel(s)")
        else:
            self._watchlist_log("⏰ Auto-download complete — no new tracks.", "info")
            self._watchlist_last_download = int(time.time())
            self._autosave_automation_settings()  # persist anchor + reschedule

    def _wl_update_next_dl_label(self):
        """Refresh the 'Next auto-download' line under the Watch List toolbar."""
        lbl = getattr(self, "_wl_next_dl_lbl", None)
        if lbl is None:
            return
        ts = self._wl_next_dl_ts
        if not ts:
            txt = "⏰  Next auto-download:  Off"
        else:
            try:
                dt = datetime.fromtimestamp(ts)
                hour = dt.strftime("%I").lstrip("0") or "12"
                txt = (f"⏰  Next auto-download:  {dt.strftime('%a %b')} "
                       f"{dt.day}, {dt.strftime('%Y')}  ·  "
                       f"{hour}:{dt.strftime('%M %p')}")
            except Exception:
                txt = "⏰  Next auto-download:  —"
        try:
            lbl.config(text=txt)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # System tray & window lifecycle — hide/show, quit, run-at-startup toggle
    # ══════════════════════════════════════════════════════════════════════════
    def _notify_tray(self, title, msg):
        """Show a tray notification if the tray is active; always log it."""
        self._watchlist_log(f"🔔 {title}: {msg}", "info")
        if self._tray_icon is not None:
            self._tray_icon.notify(msg, title)

    def _load_tray_image(self):
        """Load the real app icon (icon.ico) as a PIL image for the tray, or
        None to let TrayIcon fall back to its runtime-drawn placeholder."""
        path = app_icon_path()
        if not path:
            return None
        try:
            from PIL import Image
            return Image.open(path)
        except Exception:
            return None

    def _ensure_tray(self):
        """Create and start the tray icon on first hide (lazy)."""
        if self._tray_icon is not None:
            return self._tray_icon
        from cratebuilder.tray import TrayIcon
        self._tray_icon = TrayIcon(
            schedule=lambda fn: self.after(0, fn),
            on_open=self._show_from_tray,
            on_scan=self._tray_scan_now,
            on_download=self._tray_download_all_new,
            on_quit=self._tray_close,
            download_text=lambda *_: self._tray_dl_label,
            image=self._load_tray_image())
        if not self._tray_icon.available or not self._tray_icon.start():
            self._tray_icon = None
        else:
            # Keep the hover tooltip fresh while the icon is alive.
            if self._tray_title_after_id is None:
                self._tray_title_after_id = self.after(0, self._tray_title_tick)
        return self._tray_icon

    def _tray_scan_now(self):
        """Tray 'Scan Now': focus the app, show the Watch List tab, then scan."""
        self._show_from_tray()
        try:
            self._notebook.select(self._tab_watchlist)
        except Exception:
            pass
        self._watchlist_scan_all()

    def _tray_download_all_new(self):
        """Tray 'Download All New': focus the app, show the Main tab (where the
        batch progress lives), then run the same action as the Watch List
        button."""
        self._show_from_tray()
        try:
            self._notebook.select(self._tab_main)
        except Exception:
            pass
        self._watchlist_download_all_new()

    def _tray_close(self):
        """Tray 'Close': focus the app, then run the normal close confirmation."""
        self._show_from_tray()
        self._on_window_close()

    def _tray_summary(self):
        """Build the multi-line hover tooltip from the live Progress / Queue /
        Watch List state. Runs on the Tk main thread (safe to read widgets)."""
        lines = [f"{APP_NAME}"]
        if self._downloading:
            cur = self._cur_lbl.cget("text").strip() or "working…"
            if len(cur) > 55:
                cur = cur[:54] + "…"
            lines.append(f"▶ {cur}")
            ov = self._ov_lbl.cget("text").strip()
            stats = self._ov_stats_lbl.cget("text").strip()
            ov_line = "  ".join(p for p in (ov, stats) if p)
            if ov_line:
                lines.append(f"Overall: {ov_line}")
            qc = self._qcount_lbl.cget("text").strip()
            if qc:
                lines.append(f"Queue: {qc} left")
        if self._wl_scan_active > 0:
            lines.append(f"👁 Watch List: scanning {self._wl_scan_active}…")
        elif getattr(self, "_wl_download_active", False):
            lines.append("👁 Watch List: downloading new tracks…")
        if len(lines) == 1:
            lines.append("Idle")
        # Windows tray tooltips cap at 127 chars; keep it well under.
        return "\n".join(lines)[:127]

    def _tray_title_tick(self):
        """Refresh the tray hover tooltip, rescheduling while the icon lives."""
        self._tray_title_after_id = None
        tray = self._tray_icon
        if tray is None:
            return
        try:
            tray.set_title(self._tray_summary())
        except Exception:
            pass
        self._tray_title_after_id = self.after(2000, self._tray_title_tick)

    def _hide_to_tray(self):
        """Withdraw the window; keep the app (and scheduler) running."""
        if self._ensure_tray() is not None:
            self.withdraw()
        else:
            self.iconify()  # tray unavailable — fall back to taskbar minimise

    def _show_from_tray(self):
        """Restore and focus the window from the tray menu."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self):
        """Real exit: stop tray, cancel timer, destroy."""
        if self._auto_dl_after_id is not None:
            try:
                self.after_cancel(self._auto_dl_after_id)
            except Exception:
                pass
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self.destroy()

    def _on_window_close(self):
        """WM_DELETE handler: always confirm before really quitting."""
        if messagebox.askyesno(
                "Close DJ-CrateBuilder",
                "Are you sure you want to close DJ-CrateBuilder?\n\n"
                "Auto-downloads won't run while it's closed.",
                parent=self):
            self._quit_app()
        # 'No' — leave the window exactly as it was.

    def _on_minimize(self, event):
        """Minimize button → hide to the system tray when that option is on.
        Only the toplevel's own iconify counts; child <Unmap> events and our
        own withdraw()/deiconify() (states 'withdrawn'/'normal') are ignored."""
        if (event.widget is self and self.state() == "iconic"
                and self._minimize_to_tray.get() and sys.platform == "win32"):
            # Defer so the iconify settles before we withdraw + show the tray.
            self.after(10, self._hide_to_tray)

    def _on_run_at_startup_toggle(self):
        """Add/remove the Windows Run entry to match the checkbox."""
        want = self._run_at_startup.get()
        ok = cb_startup.set_startup(want)
        if not ok and want:
            self._run_at_startup.set(False)  # revert if the write failed
            messagebox.showwarning(
                "Startup", "Could not register the app to run at startup.")
        # Persisted here (not in _autosave_automation_settings) because the
        # registry is the source of truth for this flag; keep them in sync.
        cfg = load_config()
        cfg["run_at_startup"] = self._run_at_startup.get()
        save_config(cfg)

    # ══════════════════════════════════════════════════════════════════════════
    # Self-update — nightly build channel (logic in cratebuilder/updater_core.py)
    # ══════════════════════════════════════════════════════════════════════════
    def _set_update_status(self, text):
        """Update the About-tab status label if it has been built yet."""
        var = getattr(self, "_update_status_var", None)
        if var is not None:
            var.set(text)

    def _set_update_btn_label(self, text):
        """Flip the About-tab updater button between check / update-now."""
        btn = getattr(self, "_update_btn", None)
        if btn is not None:
            btn.config(text=text)

    def _on_check_updates_clicked(self):
        """Manual 'Check for updates' button — always reports the outcome."""
        btn = getattr(self, "_update_btn", None)
        if btn is not None:
            btn.config(state="disabled")
        self._set_update_status("Checking for updates…")
        threading.Thread(target=self._check_updates_worker,
                         args=(True,), daemon=True).start()

    def _auto_check_for_updates(self):
        """Automatic check (launch + periodic timer). Besides refreshing the
        About-tab status text, a newer build now prompts to download — or
        sends a tray notification when the window is hidden. The exact
        prompt/notify/stay-silent rules live in _on_check_result."""
        threading.Thread(target=self._check_updates_worker,
                         args=(False,), daemon=True).start()

    def _on_update_interval_changed(self, *_):
        """Persist the chosen auto-update-check interval and re-arm the timer."""
        try:
            cfg = load_config()
            cfg["update_check_interval"] = self._update_check_interval.get()
            save_config(cfg)
        except Exception:
            pass
        self._reschedule_update_check()

    def _reschedule_update_check(self):
        """(Re)arm the periodic silent update-check timer from the current
        dropdown interval. Cancels any pending timer first so changing the
        interval takes effect immediately. Called on startup and whenever the
        interval changes."""
        if self._update_check_after_id is not None:
            try:
                self.after_cancel(self._update_check_after_id)
            except Exception:
                pass
            self._update_check_after_id = None
        secs = interval_label_to_seconds(self._update_check_interval.get())
        if not secs:
            self._next_update_check_ts = None
            self._refresh_next_update_check_label()
            return   # no/invalid interval — leave the timer disarmed
        self._update_check_after_id = self.after(
            int(secs * 1000), self._update_check_tick)
        self._next_update_check_ts = time.time() + secs
        self._refresh_next_update_check_label()

    def _refresh_next_update_check_label(self):
        """Update the 'Next check: …' label under the auto-check dropdown."""
        var = getattr(self, "_next_update_check_var", None)
        if var is None:
            return
        ts = getattr(self, "_next_update_check_ts", None)
        if not ts:
            var.set("")
            return
        try:
            stamp = time.strftime("%Y-%m-%d  %I:%M %p",
                                  time.localtime(ts)).lstrip("0")
            var.set(f"Next check: {stamp}")
        except Exception:
            var.set("")

    def _update_check_tick(self):
        """Fire one scheduled silent update check, then re-arm for the next
        interval. Skips (without breaking the schedule) while an update download
        is already in flight."""
        self._update_check_after_id = None
        if not getattr(self, "_update_in_progress", False):
            self._auto_check_for_updates()
        self._reschedule_update_check()

    def _check_updates_worker(self, manual):
        """Background thread: fetch the manifest, then marshal back to the UI."""
        url = UPDATE_MANIFEST_URL_LINUX if ucore.is_linux() else UPDATE_MANIFEST_URL
        manifest = ucore.fetch_manifest(url)
        try:
            cfg = load_config()
            cfg["last_update_check"] = time.time()
            save_config(cfg)
        except Exception:
            pass
        self.after(0, lambda: self._on_check_result(manifest, manual))

    def _on_check_result(self, manifest, manual):
        """UI thread: interpret the manifest and react."""
        btn = getattr(self, "_update_btn", None)
        if btn is not None:
            btn.config(state="normal")

        if manifest is None:
            self._set_update_btn_label(UPDATE_BTN_CHECK)
            self._set_update_status("Couldn't reach the update server.")
            if manual:
                messagebox.showinfo(
                    "Check for updates",
                    "Couldn't reach the update server. Check your internet "
                    "connection and try again.", parent=self)
            return

        ok, _reason = ucore.validate_manifest(manifest)
        if not ok:
            self._set_update_btn_label(UPDATE_BTN_CHECK)
            self._set_update_status("Update info unavailable.")
            if manual:
                messagebox.showinfo(
                    "Check for updates",
                    "The update information looks invalid right now. Please try "
                    "again later.", parent=self)
            return

        if not ucore.is_update_available(manifest, APP_BUILD):
            self._set_update_btn_label(UPDATE_BTN_CHECK)
            self._set_update_status(f"You're on the latest build ({APP_BUILD}).")
            if manual:
                messagebox.showinfo(
                    "Check for updates",
                    f"You're already on the latest build ({APP_BUILD}).",
                    parent=self)
            return

        build = int(manifest["build"])
        self._set_update_btn_label(UPDATE_BTN_UPDATE)
        self._set_update_status(
            f"Update available: build {build}.\nYou're on build {APP_BUILD}.")
        if manual:
            self._prompt_and_update(manifest, build)
            return
        # Automatic check: surface the update actively. Silent when running
        # from source (nothing to swap — prompting would only nag), while a
        # download is already in flight, or while a prompt is still open.
        if (not ucore.can_self_update() or self._update_in_progress
                or self._update_prompt_open):
            return
        if self.state() == "withdrawn":
            self._notify_tray(
                "Update available",
                f"Build {build} is available (you're on {APP_BUILD}). "
                "Open DJ-CrateBuilder to install it.")
            return
        self._update_prompt_open = True
        try:
            self._prompt_and_update(manifest, build)
        finally:
            self._update_prompt_open = False

    def _prompt_and_update(self, manifest, build):
        """Offer the update, then (if accepted) download and apply it.

        Three install shapes are handled: a genuine source/git checkout (no
        installed payload to swap — point the user at git), a Linux ``.deb``
        install (apt via pkexec), and the Windows packaged build (the separate
        updater.exe swap). The yes/no prompt is shared; only the apply step
        differs.
        """
        # No installable payload (plain source / git checkout): update via git.
        if not ucore.can_self_update():
            messagebox.showinfo(
                "Update available",
                f"Build {build} is available, but you're running from source.\n\n"
                "Update with git (pull the latest) instead of the in-app updater.",
                parent=self)
            return

        notes = str(manifest.get("notes", "")).strip()
        note_line = f"\n\nWhat's new:\n{notes}" if notes else ""
        if not messagebox.askyesno(
                "Update available",
                f"A newer build is available (build {build}; you have "
                f"{APP_BUILD}).{note_line}\n\nDownload and install it now?",
                parent=self):
            self._set_update_status(
                f"Update available: build {build}.\nYou're on build {APP_BUILD}.")
            return

        if ucore.is_linux() and not ucore.is_frozen():
            self._run_update_linux(manifest, build)
        else:
            self._run_update(manifest, build)

    def _run_update(self, manifest, build):
        """Download + verify + stage in a worker, with a small progress dialog."""
        self._update_in_progress = True
        ws = ucore.default_workspace()
        ucore.purge_dir(ws)
        os.makedirs(ws, exist_ok=True)

        dlg = tk.Toplevel(self)
        dlg.title("Updating DJ-CrateBuilder")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=BG)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)   # no close mid-update
        status_var = tk.StringVar(value="Starting download…")
        tk.Label(dlg, textvariable=status_var, font=("Segoe UI", 11),
                 fg=TEXT, bg=BG, anchor="w", width=44, justify="left"
                 ).pack(padx=20, pady=(18, 8), anchor="w")
        bar = ttk.Progressbar(dlg, length=320, mode="determinate", maximum=100)
        bar.pack(padx=20, pady=(0, 18))
        dlg.update_idletasks()

        # Center on the main window (so it lands on whatever monitor the app
        # is on) instead of the OS default top-left.
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px = self.winfo_x() + (self.winfo_width()  - w) // 2
        py = self.winfo_y() + (self.winfo_height() - h) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")

        def set_status(text):
            self.after(0, status_var.set, text)

        def set_pct(pct):
            self.after(0, lambda: bar.config(value=pct))

        def worker():
            try:
                zip_path = os.path.join(ws, f"build-{build}.zip")

                def prog(done, total):
                    if total:
                        set_pct(done * 100 // total)
                        set_status(f"Downloading build {build}… "
                                   f"{done // 1048576} / {total // 1048576} MB")
                    else:
                        set_status(f"Downloading build {build}… "
                                   f"{done // 1048576} MB")

                ucore.download(manifest["url"], zip_path, progress_cb=prog)

                set_status("Verifying download…")
                if not ucore.verify_sha256(zip_path, manifest["sha256"]):
                    raise ValueError(
                        "checksum mismatch — the download may be corrupt")

                set_status("Preparing files…")
                staged = os.path.join(ws, "staged")
                ucore.purge_dir(staged)
                ucore.extract_zip(zip_path, staged)

                self.after(0, lambda: self._launch_updater_and_quit(
                    dlg, staged, ws))
            except Exception as exc:   # noqa: BLE001 — report and recover
                self.after(0, lambda: self._update_failed(dlg, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _run_update_linux(self, manifest, build):
        """Download + verify the .deb, then install it via apt (pkexec prompt).

        The Linux counterpart to _run_update. There's no separate swap process:
        apt replaces the files under /opt while the app is still running (the
        live process keeps its old file handles), so once the install returns
        cleanly we just relaunch a fresh process to pick up the new build.

        pkexec raises a graphical PolicyKit password dialog; the user cancelling
        it is a non-zero exit, treated as a cancelled update with the app left
        untouched. The blocking install runs off the UI thread so the window
        stays responsive while the password dialog is up.
        """
        self._update_in_progress = True
        if not ucore.pkexec_available():
            self._update_in_progress = False
            messagebox.showinfo(
                "Update available",
                f"Build {build} is available, but automatic installation isn't "
                "possible here (pkexec was not found).\n\n"
                "Download the latest .deb and install it manually:\n"
                "https://github.com/Sintax/DJ-CrateBuilder/releases/tag/linux-v1.3",
                parent=self)
            return

        ws = ucore.default_workspace()
        ucore.purge_dir(ws)
        os.makedirs(ws, exist_ok=True)

        dlg = tk.Toplevel(self)
        dlg.title("Updating DJ-CrateBuilder")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=BG)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)   # no close mid-update
        status_var = tk.StringVar(value="Starting download…")
        tk.Label(dlg, textvariable=status_var, font=("Segoe UI", 11),
                 fg=TEXT, bg=BG, anchor="w", width=44, justify="left"
                 ).pack(padx=20, pady=(18, 8), anchor="w")
        bar = ttk.Progressbar(dlg, length=320, mode="determinate", maximum=100)
        bar.pack(padx=20, pady=(0, 18))
        dlg.update_idletasks()

        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px = self.winfo_x() + (self.winfo_width()  - w) // 2
        py = self.winfo_y() + (self.winfo_height() - h) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")

        def set_status(text):
            self.after(0, status_var.set, text)

        def set_pct(pct):
            self.after(0, lambda: bar.config(value=pct))

        def worker():
            try:
                deb_path = os.path.join(ws, f"dj-cratebuilder-{build}.deb")

                def prog(done, total):
                    if total:
                        set_pct(done * 100 // total)
                        set_status(f"Downloading build {build}… "
                                   f"{done // 1048576} / {total // 1048576} MB")
                    else:
                        set_status(f"Downloading build {build}… "
                                   f"{done // 1048576} MB")

                ucore.download(manifest["url"], deb_path, progress_cb=prog)

                set_status("Verifying download…")
                if not ucore.verify_sha256(deb_path, manifest["sha256"]):
                    raise ValueError(
                        "checksum mismatch — the download may be corrupt")

                set_status("Waiting for authorization…")
                result = subprocess.run(
                    ucore.build_deb_install_cmd(deb_path))
                if result.returncode != 0:
                    # Non-zero includes the user cancelling the pkexec dialog
                    # (126/127). Files are unchanged — treat as a failed update.
                    raise RuntimeError(
                        "installation was cancelled or did not complete "
                        f"(exit code {result.returncode})")

                self.after(0, lambda: self._relaunch_linux_and_quit(dlg))
            except Exception as exc:   # noqa: BLE001 — report and recover
                self.after(0, lambda: self._update_failed(dlg, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _relaunch_linux_and_quit(self, dlg):
        """Launch a fresh app process (post-apt swap) and exit the old one.

        The single-instance loopback lock is released *before* spawning the
        child so the new process can bind the port immediately — otherwise the
        child could lose the single-instance race against this still-exiting
        process and quit, leaving no window. (Windows avoids this by having
        updater.exe wait for the old PID; the Linux path has no such handoff.)
        """
        launcher = "/usr/bin/dj-cratebuilder"
        lock = getattr(self, "_instance_lock", None)
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass
        try:
            if os.path.exists(launcher) and os.access(launcher, os.X_OK):
                cmd = [launcher]
            else:
                cmd = [sys.executable, os.path.abspath(__file__)]
            subprocess.Popen(cmd, close_fds=True)
        except OSError as exc:
            self._update_failed(dlg, exc)
            return

        try:
            dlg.destroy()
        except Exception:
            pass
        self._quit_app()

    def _update_failed(self, dlg, exc):
        """Close the progress dialog and tell the user the update didn't apply."""
        self._update_in_progress = False
        try:
            dlg.destroy()
        except Exception:
            pass
        self._set_update_status("Update failed — still on build "
                                f"{APP_BUILD}.")
        messagebox.showerror(
            "Update failed",
            f"The update couldn't be installed:\n\n{exc}\n\n"
            "Your current version is unchanged. You can try again later or "
            "download the latest build from GitHub.", parent=self)

    def _launch_updater_and_quit(self, dlg, staged, ws):
        """Hand off to the separate updater process, then fully exit."""
        app_dir = ucore.install_dir()
        app_exe = sys.executable
        backup = os.path.join(ws, "backup")
        log = os.path.join(ws, "update.log")

        updater_exe = os.path.join(app_dir, "updater.exe")
        if os.path.exists(updater_exe):
            cmd = [updater_exe]
        else:
            # Dev fallback (running from source): drive updater.py with Python.
            cmd = [sys.executable, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "updater.py")]

        cmd += ["--pid", str(os.getpid()), "--src", staged, "--dst", app_dir,
                "--relaunch", app_exe, "--backup", backup, "--log", log]

        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        try:
            subprocess.Popen(cmd, close_fds=True, creationflags=flags,
                             cwd=app_dir)
        except OSError as exc:
            self._update_failed(dlg, exc)
            return

        try:
            dlg.destroy()
        except Exception:
            pass
        # Fully exit so the updater can replace the (now-unlocked) files and
        # the single-instance lock is released before it relaunches us.
        self._quit_app()

    # ══════════════════════════════════════════════════════════════════════════
    # About tab — version info, FAQ, and the log-viewer / folder launchers
    # ══════════════════════════════════════════════════════════════════════════
    def _build_about_tab(self, parent):
        """Build the About tab: version info, FAQ, and log/folder launchers."""
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        self._about_canvas, outer = self._make_scrollable(
            wrapper, (28, 28, 28, 18))

        # ── App title + version ───────────────────────────────────────────────
        ttk.Label(outer,
                  text=f"{APP_NAME}  v{APP_VERSION_FULL}",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 20))

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 24))

        # ── Top section: info text on the left, action buttons on the right ───
        # Two columns. The ABOUT_FIELDS rows (Application / Created by / Built
        # with) plus the bug/suggestion note live on the left; the three action
        # buttons stack on the right.
        top_sec = ttk.Frame(outer)
        top_sec.pack(fill="x", pady=(0, 4))

        def _about_btn(parent, label, command):
            return tk.Button(
                parent, text=label,
                font=("Segoe UI", 10, "bold"),
                bg=SURFACE2, fg=LINK_COL,
                activebackground=BORDER, activeforeground=TEXT,
                relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                command=command)

        # Right column — packed first so it claims the right edge. Stack:
        # View on GitHub, a gap, then the current-build status text directly
        # ABOVE the Check-for-Updates button.
        btn_col = tk.Frame(top_sec, bg=BG)
        btn_col.pack(side="right", anchor="n")

        self._github_btn = _about_btn(
            btn_col, "View on GitHub",
            lambda: webbrowser.open(GITHUB_URL))
        self._github_btn.pack(anchor="e", pady=(0, 6))
        Tooltip(self._github_btn,
                "Opens the DJ-CrateBuilder GitHub page in your browser. "
                "Check here for the latest releases and update notes.")

        self._update_status_var = tk.StringVar(
            value=f"You're on build {APP_BUILD}.")
        tk.Label(btn_col, textvariable=self._update_status_var,
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG,
                 anchor="e", justify="right").pack(anchor="e", pady=(22, 4))
        self._update_btn = _about_btn(
            btn_col, UPDATE_BTN_CHECK,
            self._on_check_updates_clicked)
        self._update_btn.pack(anchor="e", pady=(0, 2))
        Tooltip(self._update_btn,
                "Checks GitHub for a newer nightly build and, if one exists, "
                "downloads and installs it (the app restarts to finish).")

        # Auto-check interval — right-aligned, directly below the status text.
        auto_row = tk.Frame(btn_col, bg=BG)
        auto_row.pack(anchor="e", pady=(8, 0))
        tk.Label(auto_row, text="Auto-check for updates:",
                 font=("Segoe UI", 10), fg=TEXT_MED, bg=BG,
                 anchor="e").pack(side="left", padx=(0, 8))
        self._update_interval_combo = ttk.Combobox(
            auto_row, textvariable=self._update_check_interval,
            values=UPDATE_CHECK_OPTIONS, state="readonly", width=10)
        self._update_interval_combo.pack(side="left")
        Tooltip(self._update_interval_combo,
                "How often DJ-CrateBuilder quietly checks GitHub for a newer "
                "nightly build in the background.")

        tk.Label(btn_col, textvariable=self._next_update_check_var,
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=BG,
                 anchor="e", justify="right").pack(anchor="e", pady=(2, 0))
        self._refresh_next_update_check_label()

        # Left column — info rows (driven by ABOUT_FIELDS), then the Submit
        # Issues / Suggestions button, then its accompanying note.
        info_col = tk.Frame(top_sec, bg=BG)
        info_col.pack(side="left", anchor="n")

        for label, value in ABOUT_FIELDS:
            row = tk.Frame(info_col, bg=BG)
            row.pack(fill="x", pady=(0, 14))
            tk.Label(row, text=label, font=("Segoe UI", 12, "bold"),
                     fg=TEXT, bg=BG, width=14, anchor="w").pack(side="left")
            tk.Label(row, text=value, font=("Segoe UI", 11),
                     fg=TEXT, bg=BG, anchor="w").pack(side="left", padx=(8, 0))

        self._issues_btn = _about_btn(
            info_col, "  ↗  Submit Issues / Suggestions  ",
            lambda: webbrowser.open(GITHUB_ISSUES_URL))
        self._issues_btn.pack(anchor="w", pady=(4, 6))
        Tooltip(self._issues_btn,
                "Opens the GitHub 'Create new issue' form in your browser, "
                "where you can report a bug or suggest a feature.")

        tk.Label(info_col,
                 text="*(For any bugs encountered or suggestions you'd like to "
                      "make, submit them using the Submit Issues/Suggestions button.)",
                 font=("Segoe UI", 11), fg=TEXT_MED, bg=BG, anchor="w",
                 justify="left", wraplength=440).pack(anchor="w", pady=(0, 0))

        # ── FAQ ───────────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(20, 20))

        faq_hdr = ttk.Frame(outer)
        faq_hdr.pack(fill="x", pady=(0, 16))
        ttk.Label(faq_hdr, text="Frequently Asked Questions",
                  style="White.Section.TLabel").pack(side="left")

        faq = [
            ("Q: What is DJ-CrateBuilder?",
             "A: A tool that downloads audio from YouTube and SoundCloud as MP3 files, organized by platform, genre, "
             "and channel. It's designed for DJs and music collectors who want to build local music libraries from "
             "online sources."),

            ("Q: Do I need Python installed to run this?",
             "A: No. If you're using the installer version, everything is bundled — Python, yt-dlp, and FFmpeg are "
             "all included. Just install and run."),

            ("Q: What is FFmpeg and why is it needed?",
             "A: FFmpeg is the audio conversion engine that converts downloaded audio streams into MP3 files. It runs "
             "silently in the background. The installer includes it automatically."),

            ("Q: Why are some downloads marked \"login required\"?",
             "A: YouTube sometimes requires account authentication for certain content, especially when accessing from "
             "VPN or datacenter IP addresses. The most reliable fix is to enable \"Use browser cookies\" in the "
             "Settings tab and pick the browser you're signed into YouTube with — the app then borrows that session "
             "so downloads authenticate as you. (Use a throwaway account in a separate browser profile if you'd "
             "rather not risk your main one.) Otherwise, try disconnecting your VPN, switching to a different VPN "
             "server, or waiting a while before retrying."),

            ("Q: Why are some downloads marked \"unavailable\" or \"private\"?",
             "A: The video has been removed by the uploader, made private, or is restricted in your region. These "
             "cannot be downloaded."),

            ("Q: What does \"Skip files already downloaded\" do?",
             "A: It prevents re-downloading files you already have. There are three modes: \"In Database ~ In Folder\" "
             "skips if the file is found in either the downloads database or the destination folder. \"In Folder Only\" "
             "checks only whether the file exists on disk. \"In Database Only\" checks only the database. This "
             "feature acts as a resume function — if a large batch is interrupted, restart it and already-completed "
             "files will be skipped."),

            ("Q: What is the Time / Length Limiter?",
             "A: It automatically skips any track that exceeds the set duration. This is useful for filtering out "
             "DJ mixes, podcasts, or full album uploads when you only want individual tracks. The default is 8 "
             "minutes."),

            ("Q: What MP3 bitrate should I choose?",
             "A: 192 kbps is a good balance of quality and file size for most listening. 320 kbps is the maximum "
             "MP3 quality and is recommended if you plan to play the files on professional sound systems. Note that "
             "the output quality can never exceed the source — if YouTube serves audio at 128 kbps, converting to "
             "320 kbps won't improve it."),

            ("Q: What do the Download Behavior settings do?",
             "A: These are optional measures to reduce the chance of being throttled or blocked during large batch "
             "downloads. \"Rotate User-Agent\" makes your requests look like they're coming from different browsers. "
             "\"Throttle Requests\" adds a random delay between downloads to mimic human browsing behavior. "
             "\"Geo-bypass\" attempts to bypass geographic IP restrictions."),

            ("Q: What is the difference between Auto and Manual throttle modes?",
             "A: Auto mode offers three presets based on how many files you're downloading: Light (1–5 seconds) for "
             "under 50 files, Moderate (3–8 seconds) for 50–200 files, and Aggressive (5–15 seconds) for 200+ files. "
             "Manual mode lets you set your own minimum and maximum delay in seconds."),

            ("Q: Can I change settings while a download is running?",
             "A: Yes. The Time Limiter, MP3 Bitrate, and all Download Behavior settings can be changed mid-download "
             "by switching to the Settings tab. Changes take effect starting with the next file in the queue."),

            ("Q: Where are my downloaded files saved?",
             "A: By default, files are saved to your Music folder under \"DJ-CrateBuilder,\" organized by "
             "platform (YouTube/SoundCloud), then by genre and channel name. You can change the base directory in the "
             "Settings tab. The \"Open Folder\" button on the Main tab opens the current download directory."),

            ("Q: What is the Activity Log?",
             "A: A text file that records every downloaded, skipped, and failed file with timestamps. It lives in "
             "your base save directory as \"activity.log\". Under the \"Activity Log\" heading in the Settings tab, "
             "\"View Log\" opens it in the built-in color-coded viewer and \"Open in System Viewer\" opens it in your "
             "default text editor. The path beneath is a clickable link that opens its folder in your file explorer."),

            ("Q: Why does the Queue stop showing entries after a large number of files?",
             "A: This was a known issue in v1.0 caused by a Tk Canvas widget limitation. It has been fixed in v1.1 "
             "— the queue now uses a Text widget that handles any number of entries."),

            ("Q: Can I add URLs to the batch while a download is running?",
             "A: No. The URL field and Add to Batch button are disabled during downloads to prevent confusion. The "
             "batch is locked when you press \"Downloads MP3's\" and processes only the URLs that were queued at that "
             "moment. You can start a new batch after the current one finishes."),

            ("Q: Does pasting a YouTube channel URL download all videos?",
             "A: Yes. When you paste a bare channel URL (like https://www.youtube.com/@ChannelName), the app "
             "automatically appends /videos to ensure it fetches the full video list rather than just the channel's "
             "featured page."),

            ("Q: What does the bitrate display in the Queue mean?",
             "A: The format \"128k → 192k\" shows the source audio bitrate from YouTube followed by the bitrate your "
             "MP3 was saved at. This helps you see whether the source quality matched your output setting."),

            ("Q: What is the Watch List?",
             "A: It tracks YouTube and SoundCloud channels you care about and "
             "surfaces only genuinely-new uploads — tracks you haven't already "
             "downloaded — so you never re-grab your whole library. New-track counts "
             "refresh whenever you press 'Scan All', on each scheduled auto-download "
             "run, and (optionally) at launch if you enable 'Scan Watch List for new "
             "uploads when the app starts' in Settings."),
            ("Q: What's the difference between 'Scan All' and 'Download All New'?",
             "A: 'Scan All' only checks every watched channel for new uploads and "
             "updates the new-track counts on each card — it never downloads "
             "anything. 'Download All New' downloads the tracks already counted as "
             "new across all channels without first re-scanning. (The scheduled "
             "auto-download does both: it scans first, then downloads.) Each card "
             "also has its own buttons to scan or download just that one channel."),
            ("Q: How do channels get added to the Watch List?",
             "A: Three ways: manually with 'Add Channel' (paste a youtube.com or "
             "soundcloud.com channel/artist URL); automatically after you download "
             "from a channel or SoundCloud artist (if 'Auto-add channels' is on in "
             "Settings); and auto-discovered from your existing download folders each "
             "time the app starts."),
            ("Q: Can I watch SoundCloud artists too?",
             "A: Yes. SoundCloud artists are first-class Watch List entries. Add one "
             "via 'Add Channel' with a soundcloud.com profile URL, or let it be "
             "auto-added after you download from a SoundCloud artist. Each entry "
             "scans the artist's /tracks page and downloads new tracks into your "
             "SoundCloud folder."),
            ("Q: What does 'needs channel ID' / the 'Fix Link' button mean?",
             "A: An entry whose link couldn't be resolved to a usable target (often "
             "added from a folder name). For YouTube, click 'Fix Link', pick the "
             "right channel from the search results, and it's healed; for SoundCloud "
             "it simply asks you to paste the soundcloud.com URL. If the fix resolves "
             "to a channel you already track, the app detects the duplicate and "
             "offers to remove the redundant entry. Resolved entries don't show the "
             "button."),
            ("Q: How does automatic checking and downloading work?",
             "A: In Settings → Automation/Startup, set 'Auto-download Watch-List "
             "channels every…' (default 1 day; choose 'Off' to disable it). On that "
             "interval the app scans every watched channel first, then automatically "
             "downloads any new tracks into their folders using your "
             "bitrate/throttle/skip settings, and shows a tray notification "
             "summarising what it grabbed. The countdown runs from app launch and "
             "re-anchors each time a 'Download All New' completes; the Watch List tab "
             "shows when the next run is due."),
        ]

        for question, answer in faq:
            tk.Label(outer, text=question,
                     font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG,
                     anchor="w", justify="left", wraplength=660
                     ).pack(fill="x", pady=(0, 4))
            # Render the leading "A:" in the question's font/color, with the
            # answer body beside it (hanging indent so wrapped lines align under
            # the body, not the marker).
            arow = tk.Frame(outer, bg=BG)
            arow.pack(fill="x", pady=(0, 16))
            tk.Label(arow, text="A:",
                     font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG,
                     anchor="nw").pack(side="left", padx=(0, 5))
            tk.Label(arow, text=answer.removeprefix("A:").lstrip(),
                     font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG,
                     anchor="w", justify="left", wraplength=638
                     ).pack(side="left", fill="x")

    # ── Genre management ──────────────────────────────────────────────────────
    def _refresh_log_path_label(self):
        """Update the log-path label in the Settings tab to reflect the current path."""
        if hasattr(self, "_log_path_lbl"):
            short = self._log_path.replace(os.path.expanduser("~"), "~")
            self._log_path_lbl.config(text=short)

    def _refresh_debug_path_label(self):
        """Update the debug-log-path label in Settings to reflect current path."""
        if hasattr(self, "_debug_path_lbl") and hasattr(self, "_debug_log_path"):
            short = self._debug_log_path.replace(os.path.expanduser("~"), "~")
            self._debug_path_lbl.config(text=short)

    def _open_log_viewer(self):
        """Open the built-in dark-themed log viewer window."""
        if not os.path.exists(self._log_path):
            messagebox.showinfo(
                "Log Not Found",
                "No log file exists yet.\n\n"
                "The log is created automatically once your first download completes."
            )
            return
        # Re-use an existing viewer window if already open
        if hasattr(self, "_log_viewer") and self._log_viewer.winfo_exists():
            self._log_viewer.lift()
            self._log_viewer.focus_force()
            self._log_viewer.refresh()
            return
        self._log_viewer = LogViewerWindow(self, self._log_path)

    def _open_database_viewer(self):
        """Open the dark-themed database browser window."""
        # Re-use an existing viewer if it's already open; otherwise refresh data.
        if hasattr(self, "_db_viewer") and self._db_viewer.winfo_exists():
            self._db_viewer.lift()
            self._db_viewer.focus_force()
            self._db_viewer.refresh()
            return
        self._db_viewer = DatabaseViewerWindow(self, self._db)

    def _open_log_external(self):
        """Open activity.log in the OS default text viewer."""
        if not os.path.exists(self._log_path):
            messagebox.showinfo(
                "Log Not Found",
                "No log file exists yet.\n\n"
                "The log is created automatically once your first download completes."
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(self._log_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self._log_path])
            else:
                subprocess.Popen(["xdg-open", self._log_path])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Log",
                f"Unable to open the log file automatically:\n{exc}\n\n"
                f"You can open it manually at:\n{self._log_path}"
            )

    def _open_debug_log_viewer(self):
        """Open the built-in debug log viewer window."""
        if not os.path.exists(self._debug_log_path):
            messagebox.showinfo(
                "Debug Log Not Found",
                "No debug log exists yet.\n\n"
                "The debug log is created automatically when you start a download."
            )
            return
        if hasattr(self, "_debug_viewer") and self._debug_viewer.winfo_exists():
            self._debug_viewer.lift()
            self._debug_viewer.focus_force()
            self._debug_viewer.refresh()
            return
        self._debug_viewer = DebugLogViewerWindow(self, self._debug_log_path)

    def _open_debug_log_external(self):
        """Open debug.log in the OS default text viewer."""
        if not os.path.exists(self._debug_log_path):
            messagebox.showinfo(
                "Debug Log Not Found",
                "No debug log exists yet.\n\n"
                "The debug log is created automatically when you start a download."
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(self._debug_log_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self._debug_log_path])
            else:
                subprocess.Popen(["xdg-open", self._debug_log_path])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Debug Log",
                f"Unable to open the debug log automatically:\n{exc}\n\n"
                f"You can open it manually at:\n{self._debug_log_path}"
            )

    def _open_download_dir(self):
        """Open the current platform's download directory in the system file manager."""
        target = self._platform_dir()
        os.makedirs(target, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Folder",
                f"Unable to open the folder:\n{exc}\n\n"
                f"Path: {target}"
            )

    def _browse_cookie_file(self):
        """Open a file dialog to select a Netscape cookie.txt file."""
        path = filedialog.askopenfilename(
            title="Select cookie.txt file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialdir=os.path.expanduser("~"))
        if path:
            self._cookie_file.set(path)
            self._autosave_behavior_settings()

    def _update_howto_label(self):
        """Update the how-to label to reflect the currently selected browser."""
        browser = self._cookies_browser.get()
        if hasattr(self, "_howto_lbl"):
            self._howto_lbl.config(
                text=f"      How-To:  Setting Up a Dedicated {browser} Profile")

    def _open_cookie_howto(self):
        """Open the built-in browser profile setup guide window."""
        browser = self._cookies_browser.get()
        if hasattr(self, "_cookie_howto") and self._cookie_howto.winfo_exists():
            self._cookie_howto.destroy()
        self._cookie_howto = CookieHowToWindow(self, browser=browser)

    # exe filename per browser — used both by webbrowser.get() (which uses these
    # as PATH lookups) and by the Windows App Paths registry fallback below.
    _BROWSER_EXES = {
        "Firefox":  "firefox.exe",
        "Chrome":   "chrome.exe",
        "Edge":     "msedge.exe",
        "Brave":    "brave.exe",
        "Opera":    "opera.exe",
        "Chromium": "chrome.exe",
    }
    # Python stdlib webbrowser names that map to our dropdown labels.
    # Edge/Brave aren't in the stdlib registry, so they fall through to the
    # Windows registry lookup.
    _BROWSER_WEBBROWSER_NAMES = {
        "Firefox":  "firefox",
        "Chrome":   "chrome",
        "Opera":    "opera",
        "Chromium": "chromium",
    }

    def _open_youtube_in_selected_browser(self):
        """Launch youtube.com in whatever browser is selected in the Cookies
        dropdown. Tries Python's webbrowser module first, then falls back to
        looking the exe up via the Windows App Paths registry. Shows an
        error dialog if neither finds the browser."""
        browser = self._cookies_browser.get()
        url = "https://www.youtube.com"

        py_name = self._BROWSER_WEBBROWSER_NAMES.get(browser)
        if py_name:
            try:
                webbrowser.get(py_name).open(url, new=2)
                return
            except webbrowser.Error:
                pass

        if sys.platform == "win32":
            exe = self._BROWSER_EXES.get(browser)
            if exe:
                try:
                    import winreg
                except ImportError:
                    winreg = None
                if winreg is not None:
                    key = (rf"SOFTWARE\Microsoft\Windows\CurrentVersion"
                           rf"\App Paths\{exe}")
                    for hive in (winreg.HKEY_CURRENT_USER,
                                 winreg.HKEY_LOCAL_MACHINE):
                        try:
                            with winreg.OpenKey(hive, key) as k:
                                path, _ = winreg.QueryValueEx(k, None)
                        except OSError:
                            continue
                        if path and os.path.exists(path):
                            try:
                                subprocess.Popen([path, url])
                                return
                            except OSError:
                                break

        messagebox.showerror(
            "Browser Not Found",
            f"Could not locate {browser} on this system.\n\n"
            f"Make sure it is installed, or open {url} manually.")

    # ══════════════════════════════════════════════════════════════════════════
    # Input handling — genre picker, platform switch, URL entry & history
    # ══════════════════════════════════════════════════════════════════════════
    # ── Genre management ─────────────────────────────────────────────────────
    def _refresh_genre_list(self):
        """Rebuild the genre combobox values for the current platform."""
        genres = self._scan_genres()
        values = ["(none)"] + genres
        self._genre_combo["values"] = values
        if self._genre_var.get() not in values:
            self._genre_var.set("(none)")
        self._update_save_preview()

    def _on_genre_selected(self, _event=None):
        self._update_save_preview()

    def _add_genre(self):
        """Prompt user for a new genre name via a custom dark-themed dialog."""
        result = [None]

        dlg = tk.Toplevel(self)
        dlg.title("New Genre")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        # Centre over the main window
        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width()  - dlg.winfo_reqwidth())  // 2
        py = self.winfo_y() + (self.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{max(0,px)}+{max(0,py)}")

        outer = tk.Frame(dlg, bg=BG, padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Enter a genre / category name:",
                  font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG
                  ).pack(anchor="w", pady=(0, 8))

        entry_var = tk.StringVar()
        entry = tk.Entry(outer, textvariable=entry_var, width=39,
                          font=("Segoe UI", 11),
                          bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
                          relief="flat",
                          highlightthickness=1,
                          highlightbackground=TEXT_DIM,
                          highlightcolor=YT_RED)
        entry.pack(fill="x", pady=(0, 14))
        entry.focus_set()

        def _confirm(_event=None):
            result[0] = entry_var.get()
            dlg.destroy()

        def _cancel(_event=None):
            dlg.destroy()

        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x")

        tk.Button(btn_row, text="OK",
                   font=("Segoe UI", 10, "bold"),
                   bg="#1ba34e", fg=TEXT, activebackground=SUCCESS,
                   activeforeground=TEXT, relief="flat", padx=18, pady=6,
                   cursor="hand2", command=_confirm
                   ).pack(side="left", padx=(0, 8))

        tk.Button(btn_row, text="Cancel",
                   font=("Segoe UI", 10),
                   bg=SURFACE2, fg=TEXT_DIM, activebackground=BORDER,
                   activeforeground=TEXT, relief="flat", padx=14, pady=6,
                   cursor="hand2", command=_cancel
                   ).pack(side="left")

        entry.bind("<Return>", _confirm)
        entry.bind("<Escape>", _cancel)
        dlg.protocol("WM_DELETE_WINDOW", _cancel)

        self.wait_window(dlg)

        name = result[0]
        if not name:
            return
        safe = safe_filename(name, strip=True)
        if not safe:
            messagebox.showwarning("Invalid Name",
                                    "That name isn't usable as a folder.")
            return
        target = os.path.join(self._platform_dir(), safe)
        if os.path.exists(target):
            messagebox.showinfo("Already Exists",
                                 f"'{safe}' already exists.")
        else:
            os.makedirs(target, exist_ok=True)
        self._refresh_genre_list()
        self._genre_var.set(safe)
        self._update_save_preview()

    def _update_save_preview(self):
        """Show a short preview of where files will land."""
        genre = self._genre_var.get()
        path  = self._resolve_save_dir(genre)
        short = path.replace(os.path.expanduser("~"), "~")
        if hasattr(self, "_save_dir_preview"):
            self._save_dir_preview.config(text=f"→  {short}")

    def _apply_platform(self):
        """Set up initial app styling. Platform is auto-detected from URL."""
        s = ttk.Style(self)
        s.configure("Download.TButton", background=YT_DARK)
        s.map("Download.TButton",
              background=[("active", YT_RED), ("disabled", "#2a1515")])
        s.configure("Accent.Horizontal.TProgressbar",
            background=YT_RED, lightcolor=YT_RED, darkcolor=YT_RED)
        STATE_ICON[ST_ACTIVE] = ("◉", YT_RED)
        s.map("TEntry",
            bordercolor=[("focus", YT_RED), ("!focus", BORDER)],
            lightcolor=[("focus", YT_RED),  ("!focus", BORDER)])
        self.title(f"{APP_NAME}  v{APP_VERSION_FULL}")
        self._refresh_genre_list()

    # ── URL placeholder ───────────────────────────────────────────────────────
    def _url_focus_in(self, _e):
        if self._ph_active:
            self._url_entry.delete(0, "end")
            self._url_entry.config(foreground=TEXT)
            self._ph_active = False

    def _url_focus_out(self, _e):
        if not self._url_entry.get().strip():
            self._url_entry.insert(0, "https://www.youtube.com/  or  https://soundcloud.com/")
            self._url_entry.config(foreground=TEXT_DIM)
            self._ph_active = True

    # ── URL right-click context menu ─────────────────────────────────────────
    def _url_context_menu(self, event):
        """Show the right-click context menu at the cursor position."""
        # Clear placeholder on interaction so paste works cleanly
        if self._ph_active:
            self._url_entry.delete(0, "end")
            self._url_entry.config(foreground=TEXT)
            self._ph_active = False
        self._url_entry.focus_set()
        try:
            self._url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._url_menu.grab_release()

    def _url_paste(self):
        try:
            text = self.clipboard_get()
            # If there's a selection, replace it; otherwise insert at cursor
            try:
                self._url_entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            self._url_entry.insert("insert", text)
        except tk.TclError:
            pass

    def _url_cut(self):
        try:
            text = self._url_entry.selection_get()
            self.clipboard_clear()
            self.clipboard_append(text)
            self._url_entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def _url_copy(self):
        try:
            text = self._url_entry.selection_get()
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def _url_select_all(self):
        self._url_entry.select_range(0, "end")
        self._url_entry.icursor("end")

    def _url_clear(self):
        self._url_entry.delete(0, "end")

    def _url_history_selected(self, _event=None):
        """Handle selection from the URL history dropdown."""
        self._ph_active = False
        self._url_entry.config(foreground=TEXT)
        # Move cursor to end
        self._url_entry.icursor("end")
        self._url_entry.selection_clear()

    def _record_url_history(self, url):
        """Add a URL to the front of the history list (max 6, no duplicates)."""
        url = url.strip()
        if not url:
            return
        self._url_history = push_mru(self._url_history, url, 6)
        # Update the combobox dropdown values
        self._url_entry["values"] = self._url_history
        # Persist to config
        cfg = load_config()
        cfg["url_history"] = self._url_history
        save_config(cfg)

    @staticmethod
    def _detect_platform(url):
        """Return 'SoundCloud' or 'YouTube' based on the URL."""
        return detect_platform(url)

    @staticmethod
    def _normalize_url(url):
        """Append /videos to bare YouTube channel URLs so yt-dlp fetches
        the full video list instead of the channel's featured page."""
        # Match youtube.com/@ChannelName with no trailing path segment
        if re.match(r'https?://(www\.)?youtube\.com/@[^/]+/?$', url):
            url = url.rstrip("/") + "/videos"
        return url

    # ══════════════════════════════════════════════════════════════════════════
    # Download engine — dependency check, queue UI, and the worker pipeline
    # ══════════════════════════════════════════════════════════════════════════
    # ── Dep check ─────────────────────────────────────────────────────────────
    def _check_deps_async(self):
        """Check for yt-dlp/ffmpeg on a background thread; prompt if missing."""
        def _run():
            missing = check_dependencies()
            if not self.winfo_exists():   # root torn down (e.g. test teardown)
                return
            if missing:
                self.after(0, lambda: self._prompt_install(missing))
            else:
                self.after(0, lambda: self._set_status("✓ Ready"))
        self._run_bg(_run)

    def _prompt_install(self, missing):
        """Ask the user whether to pip-install the missing dependencies."""
        if messagebox.askyesno("Missing Dependencies",
                               f"Missing: {', '.join(missing)}\n\nInstall now?"):
            self._do_install(missing)
        else:
            self._set_status("⚠  yt-dlp not installed — run: pip install yt-dlp")

    def _do_install(self, pkgs):
        """pip-install the given packages on a background thread."""
        self._set_status(f"Installing {', '.join(pkgs)}…")
        def _run():
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install"] + pkgs,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.after(0, lambda: self._set_status("✓ Installed. Ready."))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"✗ Install failed: {e}"))
        self._run_bg(_run)

    def _set_status(self, msg):
        self._status_var.set(msg)

    @staticmethod
    def _run_bg(target, *args):
        """Run target(*args) on a daemon thread (fire-and-forget)."""
        threading.Thread(target=target, args=args, daemon=True).start()

    # ── Queue UI ──────────────────────────────────────────────────────────────
    def _clear_queue(self):
        self._qtxt.config(state="normal")
        self._qtxt.delete("1.0", "end")
        self._qtxt.config(state="disabled")
        self._queue.clear()
        self._qcount_lbl.config(text="")

    def _build_queue_ui(self, entries, item_word="item"):
        """Populate the queue Text widget with one row per entry."""
        self._clear_queue()
        self._qtxt.config(state="normal")
        for i, e in enumerate(entries):
            title = e.get("title") or f"{item_word.capitalize()} {i+1}"
            self._add_row(i, title)
        self._qtxt.config(state="disabled")
        n = len(entries)
        self._qcount_lbl.config(
            text=f"{n} {item_word}{'s' if n != 1 else ''}")

    def _format_queue_line(self, idx, icon, title, bitrate="", note=""):
        """Format a fixed-width queue line for the Text widget."""
        num    = f"{idx+1:>4}."
        trunc  = title[:48] + ("…" if len(title) > 48 else "")
        brate  = bitrate.rjust(14) if bitrate else " " * 14
        status = note.rjust(14) if note else " " * 14
        return f"{num} {icon}  {trunc:<50}{brate}  {status}\n"

    def _add_row(self, idx, title):
        """Append a pending row for one track to the queue widget."""
        line = self._format_queue_line(idx, "○", title)
        tag  = f"row_{idx}"
        # Insert without switching state (caller manages state)
        self._qtxt.insert("end", line, ("q_pending", tag))
        self._queue.append({"title": title, "state": ST_PENDING,
                             "bitrate": "", "note": ""})

    def _set_row_state(self, idx, state, note=""):
        """Update a queue row's icon/colour to reflect its download state."""
        if idx >= len(self._queue):
            return
        e = self._queue[idx]
        e["state"] = state
        e["note"]  = note
        icon_ch, _ = STATE_ICON[state]
        tag_map = {
            ST_PENDING: "q_pending",
            ST_ACTIVE:  "q_active",
            ST_DONE:    "q_done",
            ST_SKIPPED: "q_skipped",
            ST_ERROR:   "q_error",
        }
        line = self._format_queue_line(idx, icon_ch, e["title"],
                                        e["bitrate"], note)
        line_start = f"{idx+1}.0"
        line_end   = f"{idx+1}.end+1c"
        self._qtxt.config(state="normal")
        self._qtxt.delete(line_start, line_end)
        self._qtxt.insert(line_start, line, tag_map.get(state, "q_pending"))
        self._qtxt.config(state="disabled")
        self.after(80, lambda i=idx: self._scroll_to_row(i))

    def _set_row_bitrate(self, idx, text, color=None):
        """Update the bitrate text on a queue row and re-render the line."""
        if idx >= len(self._queue):
            return
        e = self._queue[idx]
        e["bitrate"] = text
        icon_ch, _ = STATE_ICON[e["state"]]
        tag_map = {
            ST_PENDING: "q_pending",
            ST_ACTIVE:  "q_active",
            ST_DONE:    "q_done",
            ST_SKIPPED: "q_skipped",
            ST_ERROR:   "q_error",
        }
        line = self._format_queue_line(idx, icon_ch, e["title"],
                                        text, e["note"])
        line_start = f"{idx+1}.0"
        line_end   = f"{idx+1}.end+1c"
        self._qtxt.config(state="normal")
        self._qtxt.delete(line_start, line_end)
        self._qtxt.insert(line_start, line, tag_map.get(e["state"], "q_pending"))
        self._qtxt.config(state="disabled")

    def _scroll_to_row(self, idx):
        """Scroll the queue Text widget so the given row is centered vertically."""
        try:
            total = len(self._queue)
            if not total:
                return
            # Calculate fraction to place idx in the middle of the
            # 12-line visible area (offset by ~5 lines above)
            frac = max(0.0, (idx - 5) / total)
            self._qtxt.yview_moveto(frac)
        except Exception:
            pass

    # ── Start / Cancel ────────────────────────────────────────────────────────
    def _start(self):
        """Collect URLs from the input/batch list and launch the download worker."""
        if self._downloading:
            return

        # Build the run list: use batch if populated, else fall back to URL field
        if self._batch_urls:
            run_batch = list(self._batch_urls)
        else:
            url = self._normalize_url(self._url_var.get().strip())
            if not url or self._ph_active:
                messagebox.showwarning("No URL",
                    "Add at least one URL to the batch queue, or enter a YouTube or SoundCloud URL.")
                return
            platform = self._detect_platform(url)
            cfg      = PLATFORMS[platform]
            if not re.search(cfg["url_pattern"], url):
                messagebox.showwarning("Invalid URL", "That doesn't look like a YouTube or SoundCloud URL.")
                return
            genre = self._genre_var.get()
            if genre == "(none)":
                proceed = messagebox.askyesno(
                    "No Genre Selected",
                    "No genre is selected. Files will be saved to the\n"
                    "'_No Genre' folder.\n\n"
                    "Do you want to continue?")
                if not proceed:
                    return
            run_batch = [{"url": url, "genre": genre,
                          "platform": platform}]
            self._record_url_history(url)

        self._begin_download_session("Preparing batch…")
        self._set_status(f"Starting batch of {len(run_batch)} URL(s)…")

        self._run_bg(self._batch_worker, run_batch)

    def _begin_download_session(self, preparing_text, *, watchlist=False):
        """Arm the shared download UI for a new batch: disable inputs, reset the
        progress bars and grand-total counters, clear the cancel/pause flags,
        and start the batch timer. *preparing_text* fills the current-track
        label. For a Watch List batch (*watchlist*=True) the Watch-List-active
        flag is set first so worker output mirrors into the scan log."""
        if watchlist:
            self._wl_download_active = True
        self._downloading = True
        self._cancel_flag.clear()
        self._pause_flag.clear()
        self._dl_btn.config(state="disabled")
        self._batch_add_btn.config(state="disabled")
        self._url_entry.config(state="disabled")
        self._cancel_btn.config(state="normal", style="CancelActive.TButton")
        self._pause_btn.config(state="normal", text="⏸  Pause", style="Pause.TButton")
        self._wl_update_cancel_btn_state()
        self._vid_progress["value"]     = 0
        self._overall_progress["value"] = 0
        self._cur_lbl.config(text=preparing_text)
        self._ov_lbl.config(text="")
        self._ov_stats_lbl.config(text="")
        self._speed_lbl.config(text="")
        self._grand_dl = 0
        self._grand_sk = 0
        self._grand_er = 0
        self._last_fatal_error = None
        self._batch_start = time.time()
        self._clear_queue()

    def _cancel(self):
        """Signal the download worker to stop after the current track."""
        self._cancel_flag.set()
        self._pause_flag.clear()   # unblock worker so it can see the cancel
        self._cancel_btn.config(state="disabled", style="Cancel.TButton")
        self._pause_btn.config(state="disabled")
        self._set_status("Cancelling after current track…")

    def _toggle_pause(self):
        """Toggle the download worker between paused and running."""
        if self._pause_flag.is_set():
            # Currently paused — resume
            self._pause_flag.clear()
            self._pause_btn.config(text="⏸  Pause", style="Pause.TButton")
            self._set_status("Resuming…")
        else:
            # Currently running — pause after current track
            self._pause_flag.set()
            self._pause_btn.config(text="▶  Resume", style="Resume.TButton")
            self._set_status("Pausing after current track…")

    # ── Batch worker ─────────────────────────────────────────────────────────
    def _batch_worker(self, run_batch):
        """Outer loop: iterate over all batch items, call _process_one_url for each."""
        total_urls  = len(run_batch)

        # Pick a consistent User-Agent for this entire batch session
        session_ua = random.choice(USER_AGENT_POOL) if self._rotate_ua.get() else None

        try:
            n = len(run_batch)
            self._log_separator(
                f"DOWNLOAD STARTED  —  {n} URL{'s' if n != 1 else ''}")

            fatal_error = None   # track fatal errors so summary doesn't hide them

            for url_idx, item in enumerate(run_batch):
                if self._cancel_flag.is_set():
                    break

                url      = item["url"]
                genre    = item["genre"]
                platform = item["platform"]
                cfg      = PLATFORMS[platform]

                label = f"Batch {url_idx+1} of {total_urls}  —  {url[:55]}…" \
                        if len(url) > 55 else \
                        f"Batch {url_idx+1} of {total_urls}  —  {url}"

                self.after(0, lambda l=label: (
                    self._set_status(l),
                    self._cur_lbl.config(text=l),
                ))

                # Highlight the active batch row (manual batch) or advance the
                # Watch List channel highlight in the Batch Queue panel.
                self.after(0, lambda i=url_idx: self._batch_set_active(i))

                dl, sk, er = self._process_one_url(
                    url, genre, platform, cfg, session_ua,
                    channel_name_override=item.get("channel_name"))
                if dl is None:   # fatal error inside _process_one_url
                    fatal_error = self._last_fatal_error
                    self._wl_dl_log(f"✗ Stopped: {fatal_error}", "err")
                    break

                # Mirror this track's outcome into the Watch List scan log
                # (only fires while a Watch List batch is active).
                wl_title = (item.get("title") or url)[:60]
                if er:
                    self._wl_dl_log(
                        f"✗ {wl_title} — {self._last_url_error or 'failed'}", "err")
                elif dl:
                    self._wl_dl_log(f"✓ {wl_title}", "ok")
                else:
                    self._wl_dl_log(f"⊘ {wl_title} (already have it)", "info")

                if self._cancel_flag.is_set():
                    break

                # Brief pause between URLs so the queue panel is readable
                if url_idx < total_urls - 1:
                    time.sleep(0.4)
                    self.after(0, self._clear_queue)

            # Mark all batch rows as done / cancelled
            self.after(0, self._batch_rebuild_rows)

            # Calculate elapsed time
            elapsed = time.time() - self._batch_start
            hrs  = int(elapsed) // 3600
            mins = (int(elapsed) % 3600) // 60
            secs = int(elapsed) % 60
            elapsed_str = f"{hrs}:{mins:02d}:{secs:02d}"

            grand_dl = self._grand_dl
            grand_sk = self._grand_sk
            grand_er = self._grand_er

            if fatal_error:
                # Show the actual error, don't overwrite with summary
                summary = f"✗ {fatal_error}  [{elapsed_str}]"
                self._log_separator(f"ERROR  —  batch stopped")
            else:
                summary_parts = [f"✓ {grand_dl} downloaded"]
                if grand_sk: summary_parts.append(f"⊘ {grand_sk} skipped")
                if grand_er: summary_parts.append(f"✗ {grand_er} failed")
                if self._cancel_flag.is_set():
                    summary_parts.insert(0, "Cancelled.")
                    self._log_separator("CANCELLED BY USER")
                else:
                    self._log_separator(
                        f"BATCH COMPLETE  —  {grand_dl} downloaded, "
                        f"{grand_sk} skipped, {grand_er} failed")
                if total_urls > 1:
                    summary_parts.append(f"({total_urls} URLs)")
                summary_parts.append(f"[{elapsed_str}]")
                summary = "  ".join(summary_parts)

            self._wl_dl_log(summary, "err" if fatal_error else "ok")

            self.after(0, lambda s=summary: (
                self._cur_lbl.config(text=s),
                self._speed_lbl.config(text=""),
                self._set_status(s),
            ))
            self.after(0, self._refresh_genre_list)

        except Exception as exc:
            self.after(0, lambda e=str(exc): (
                self._cur_lbl.config(text=f"✗ Batch error: {e[:60]}"),
                self._set_status(f"Error: {e[:60]}"),
            ))

        finally:
            # ── Watch List: update cutoff + clear pending after batch ──
            wl_batch = self._active_watchlist_batch
            if wl_batch:
                try:
                    for cid in wl_batch.get("channel_ids", []):
                        ch = self._db.get_watchlist_channel(cid)
                        if not ch:
                            continue
                        # Advance cutoff to today minus buffer
                        new_cutoff = subtract_days_from_yyyymmdd(
                            today_yyyymmdd(), WATCHLIST_CUTOFF_BUFFER_DAYS)
                        self._db.update_watchlist_cutoff(
                            ch["url"], new_cutoff)
                        self._db.clear_pending_for_channel(cid)
                        self._db.update_watchlist_status(cid, "idle")
                except Exception as wle:
                    self._dbg.error(
                        f"WL BATCH CLEANUP | error: {wle}")
                finally:
                    self._active_watchlist_batch = None
                # Update just the batched cards (status back to idle, Cancel
                # button gone) — per-card so the rest of the list isn't blanked.
                done_cids = list(wl_batch.get("channel_ids", []))
                self.after(200, lambda c=done_cids: self._watchlist_update_cards(c))
            self._wl_download_active = False
            # Restore the Batch Queue panel to the user's manual batch view.
            self._wl_batch_channels = []
            self._wl_batch_active_idx = -1
            self.after(0, self._batch_rebuild_rows)
            self.after(0, self._finish)

    def _batch_set_active(self, idx):
        """Mark batch item `idx` as the one currently processing. During a Watch
        List batch this advances the channel highlight in the Batch Queue panel;
        otherwise it dims all but the active manual-batch row."""
        if getattr(self, "_wl_download_active", False) and self._wl_batch_channels:
            self._wl_batch_active_idx = idx
            self._batch_rebuild_rows()
        else:
            self._batch_highlight(idx)

    def _batch_highlight(self, active_idx):
        """Dim all batch rows except the currently-processing one."""
        for i, child in enumerate(self._batch_frame.winfo_children()):
            col = TEXT_MED if i == active_idx else TEXT_DIM
            try:
                for widget in child.winfo_children():
                    if isinstance(widget, tk.Label):
                        widget.config(fg=col)
            except Exception:
                pass

    # ── Per-URL worker ────────────────────────────────────────────────────────
    def _process_one_url(self, url, genre, platform, cfg, session_ua=None,
                         channel_name_override=None):
        """
        Download all entries from a single URL.
        Returns (downloaded, skipped, errors) counts, or (None, None, None) on
        a fatal error.  Does NOT call _finish — that is the batch_worker's job.
        """
        self._last_url_error = None   # short reason if this URL fails softly
        try:
            import yt_dlp, time

            item_word = cfg["item_word"]

            self.after(0, lambda: self._cur_lbl.config(text=cfg["fetch_label"]))
            self.after(0, lambda: self._set_status(cfg["fetch_label"]))

            # Step 1 — fetch metadata without downloading
            meta_opts = {
                "quiet":         True,
                "no_warnings":   True,
                "extract_flat":  "in_playlist",
                "skip_download": True,
            }
            self._apply_cookie_opts(meta_opts)

            self._apply_js_runtime(meta_opts)
            self._dbg.info(f"─── METADATA FETCH ─── URL: {url}")
            self._dbg_cookie_config()
            self._dbg_ydl_opts("metadata", meta_opts)

            try:
                with yt_dlp.YoutubeDL(meta_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                self._dbg.info(
                    f"METADATA OK   | type={info.get('_type', 'single')!r}  "
                    f"title={info.get('title', '?')!r}  "
                    f"entries={len(info.get('entries') or [])}")
            except Exception as fetch_exc:
                raw_err = str(fetch_exc)
                self._dbg.error(f"METADATA FAIL | URL: {url}")
                self._dbg.error(f"METADATA FAIL | error: {raw_err}")
                import traceback
                self._dbg.debug(f"METADATA FAIL | traceback:\n{traceback.format_exc()}")
                # Log the full untruncated error to the log file
                self._logger.error(
                    f"FATAL ERROR | URL: {url} | Full error: {raw_err}")
                # Provide actionable hints for common cookie errors (UI)
                if self._use_cookies.get():
                    method = self._cookie_method.get()
                    err_lower = raw_err.lower()
                    if method == "Cookie File":
                        cfile = self._cookie_file.get().strip()
                        if "permission" in err_lower:
                            err = (f"Permission denied reading cookie file. "
                                   f"Move it to a user-writable folder. ({cfile})")
                        elif "no such file" in err_lower or "not found" in err_lower:
                            err = (f"Cookie file not found: {cfile}")
                        else:
                            err = f"Cookie file error ({raw_err[:100]})"
                    else:
                        bname = self._cookies_browser.get()
                        if "cookie" in err_lower or "decrypt" in err_lower:
                            err = (f"Cookie error ({bname}): {raw_err[:80]}. "
                                   "Try closing the browser first, or check the profile name.")
                        elif "permission" in err_lower or "locked" in err_lower:
                            err = (f"Cannot read {bname} cookies — the browser may need "
                                   "to be closed, or the profile name may be incorrect.")
                        elif "profile" in err_lower or "path" in err_lower:
                            pname = self._cookies_profile.get().strip()
                            err = (f"Browser profile not found. Check the profile name "
                                   f"in Settings. (got: '{pname or 'default'}')")
                        else:
                            err = f"Fetch failed ({raw_err[:100]})"
                else:
                    err = f"Fetch failed ({raw_err[:100]})"
                # A metadata-fetch failure is per-video (bot-check, age-gate,
                # removed, region-block, …) — skip this track and let the batch
                # continue rather than aborting everything on one bad video.
                self._last_url_error = err
                self._grand_er += 1
                self._log_error(channel_name_override or url, url, err)
                self.after(0, lambda e=err: (
                    self._cur_lbl.config(text=f"✗ {e}"),
                    self._set_status(f"Error: {e}"),
                ))
                return 0, 0, 1

            if self._cancel_flag.is_set():
                return 0, 0, 0

            # Normalise: single item vs collection
            is_collection = info.get("_type") in ("playlist", "channel")
            # Canonical channel identity (YouTube). Captured here so we can
            # stamp a cratebuilder.json sidecar into the channel folder — this
            # is what lets the Watch List scan reliably later instead of
            # guessing a handle from the folder name.
            coll_channel_id  = info.get("channel_id") or ""
            coll_handle      = info.get("uploader_id") or ""   # e.g. "@UKFDnB"
            coll_channel_url = (channel_url_from_id(coll_channel_id)
                                or info.get("channel_url")
                                or info.get("uploader_url") or "")
            if is_collection:
                entries = list(info.get("entries") or [])
                # Robust name fallback (title -> uploader -> @handle ->
                # channel_id) so a missing title never yields a blank channel
                # name — which would also drop downloads into the genre root
                # with no channel subfolder. " - Videos" suffix is stripped.
                collection_name = derive_collection_name(info)
            else:
                entries = [info]
                collection_name = ""

            if not entries:
                self.after(0, lambda: messagebox.showinfo(
                    "Nothing found",
                    f"No {item_word}s found at that URL."))
                return 0, 0, 0

            # Resolve save directory
            channel_sub = channel_name_override or (
                collection_name if is_collection else None)
            save_dir    = self._resolve_save_dir(genre, channel_sub, platform=platform)

            # Stamp the channel folder with its canonical identity so future
            # Watch List scans never have to guess the handle. Only meaningful
            # for YouTube channel/collection downloads where we have a UC id.
            if (is_collection and platform == "YouTube"
                    and coll_channel_id and save_dir):
                if write_channel_sidecar(
                        save_dir,
                        channel_id=coll_channel_id,
                        channel_url=coll_channel_url,
                        handle=coll_handle,
                        display_name=channel_sub or collection_name,
                        platform=platform,
                        genre=genre or "(none)"):
                    self._dbg.info(
                        f"SIDECAR WRITE | {channel_sub!r}  "
                        f"channel_id={coll_channel_id}")

            # Track the newest upload date seen in this run (for auto-add)
            _max_upload_date_this_run = None

            self.after(0, lambda sd=save_dir: self._set_status(
                f"Saving to  {sd.replace(os.path.expanduser('~'), '~')}"))

            total = len(entries)
            self.after(0, lambda: self._build_queue_ui(entries, item_word))
            self.after(0, lambda: self._overall_progress.config(maximum=total))
            self.after(0, lambda: self._ov_lbl.config(text=f"0 / {total}"))
            time.sleep(0.35)

            done = skipped = errors = 0

            # Watch List downloads decide "already owned" with the SAME test the
            # scan used (DB video_id + EXACT normalized-title key), so they never
            # skip a track the scan just surfaced as new. Build the channel
            # folder's key set once here. This deliberately avoids
            # _file_exists_on_disk's 40-char prefix fallback, which false-matches
            # different versions sharing a long title prefix — e.g. an original
            # "… - Cascade" against an existing "… - Cascade (Cutline Remix)".
            wl_dl = getattr(self, "_wl_download_active", False)
            wl_folder_keys = {}
            if wl_dl:
                try:
                    for _fn in os.listdir(save_dir):
                        if _fn.lower().endswith(".mp3"):
                            _k = normalize_track_key(_fn)
                            if _k:
                                wl_folder_keys.setdefault(_k, _fn)
                except OSError:
                    pass

            for idx, entry in enumerate(entries):
                if self._cancel_flag.is_set():
                    break

                # ── Pause gate ────────────────────────────────────────────────
                if self._pause_flag.is_set():
                    self.after(0, lambda: self._set_status(
                        "⏸  Paused — press Resume to continue…"))
                    while self._pause_flag.is_set():
                        if self._cancel_flag.is_set():
                            break
                        time.sleep(0.2)
                    if self._cancel_flag.is_set():
                        break
                    self.after(0, lambda: self._set_status("Resuming…"))

                item_url   = cfg["url_builder"](entry)
                item_title = entry.get("title") or \
                             f"{item_word.capitalize()} {idx+1}"

                # ── Time / Length Limiter check ───────────────────────────────
                if self._limit_enabled.get():
                    duration_sec = entry.get("duration")
                    max_sec      = self._limit_minutes.get() * 60
                    if duration_sec and duration_sec > max_sec:
                        dur_min = int(duration_sec) // 60
                        dur_sec = int(duration_sec) % 60
                        reason  = (f"exceeds limit "
                                   f"({dur_min}:{dur_sec:02d} > "
                                   f"{self._limit_minutes.get()}:00)")
                        skipped += 1
                        done    += 1
                        self._grand_sk += 1
                        self._log_skipped(item_title, "", reason=reason)
                        self.after(0, lambda i=idx: self._set_row_state(
                            i, ST_SKIPPED, "too long"))
                        self.after(0, lambda: self._vid_progress.config(value=100))
                        self.after(0, lambda d=done, t=total: (
                            self._overall_progress.config(value=d),
                            self._ov_lbl.config(text=f"{d} / {t}")))
                        self.after(0, self._update_ov_stats)
                        continue

                self.after(0, lambda i=idx, t=item_title: (
                    self._set_row_state(i, ST_ACTIVE),
                    self._cur_lbl.config(text=t[:80]),
                    self._vid_progress.config(value=0),
                ))

                safe          = safe_filename(item_title)
                expected_path = os.path.join(save_dir, safe + ".mp3")

                # ── Skip / re-download logic ──────────────────────────────────
                # Watch List "Download New" always skips tracks the scan would
                # call "owned" — regardless of the global Skip-Existing setting —
                # so it blows through the catalogue like a Main-tab download and
                # never re-grabs what you already own.
                if self._skip_existing.get() or wl_dl:
                    mode        = self._skip_mode.get()
                    video_id    = entry.get("id")
                    in_db       = self._db.is_video_downloaded(video_id)

                    if wl_dl:
                        # Mirror the scan EXACTLY: owned = DB video_id OR an
                        # EXACT normalized-title key in the channel folder. No
                        # prefix fallback (which false-matched remixes/VIPs) and
                        # no mid-batch prompt — so a track the scan surfaced as
                        # new is never skipped here.
                        wkey        = normalize_track_key(item_title)
                        found_name  = wl_folder_keys.get(wkey) if wkey else None
                        file_exists = found_name is not None
                        if found_name:
                            expected_path = os.path.join(save_dir, found_name)
                        should_skip = bool(in_db) or file_exists
                    else:
                        found_path  = self._file_exists_on_disk(save_dir, item_title)
                        file_exists = found_path is not None
                        # Use the actual found path for log/display if available
                        if found_path:
                            expected_path = found_path

                        should_skip = False
                        if mode == "In Database ~ In Folder":
                            should_skip = file_exists or in_db
                        elif mode == "In Folder Only":
                            should_skip = file_exists
                        elif mode == "In Database Only":
                            should_skip = in_db

                        if should_skip and in_db and not file_exists and mode in (
                                "In Database ~ In Folder", "In Database Only"):
                            result = []
                            evt    = threading.Event()
                            self.after(0, lambda t=item_title, r=result, e=evt:
                                self._ask_redownload(t, r, e))
                            evt.wait()
                            if result[0]:
                                should_skip = False

                    if should_skip:
                        skip_reason = (
                            "already on disk"    if mode == "In Folder Only" else
                            "in database"        if mode == "In Database Only" else
                            "in database + disk" if (in_db and file_exists) else
                            "already on disk"    if file_exists else
                            "in database"
                        )
                        skipped += 1
                        done    += 1
                        self._grand_sk += 1
                        self._log_skipped(item_title, expected_path,
                                          reason=skip_reason)
                        # Backfill ID3 tags on the file we already own, so the
                        # source URL is recoverable even for tracks grabbed
                        # before tagging existed. Only fills missing fields.
                        if file_exists and expected_path:
                            self._tag_track(expected_path, item_title, item_url)
                        self.after(0, lambda i=idx: self._set_row_state(
                            i, ST_SKIPPED, "skipped"))
                        self.after(0, lambda: self._vid_progress.config(value=100))
                        self.after(0, lambda d=done, t=total: (
                            self._overall_progress.config(value=d),
                            self._ov_lbl.config(text=f"{d} / {t}")))
                        self.after(0, self._update_ov_stats)
                        continue

                # Resolve configured output bitrate (strip any " kbps" suffix)
                output_kbps = self._bitrate_quality.get().split()[0]

                # ── Auto-upgrade MP3 bitrate when source exceeds user setting ──
                # If YouTube serves a higher-bitrate audio stream than the
                # user's configured MP3 output (e.g. 256 kbps AAC with a
                # Premium-authenticated account, while the user has selected
                # 192 kbps), encode the MP3 at that source bitrate instead
                # of downgrading. Only probe when cookies are enabled and
                # conversion is actually happening — free-tier YouTube maxes
                # out at 160 kbps Opus, so a probe would never yield an
                # upgrade and would just add an extra network round-trip.
                # Also skip the probe if the user has already chosen the
                # 320 kbps MP3 ceiling, since no source can exceed it.
                effective_kbps = output_kbps
                if (self._use_cookies.get()
                        and not self._no_conversion.get()
                        and int(output_kbps) < 320):
                    try:
                        probe_opts = {
                            "quiet":         True,
                            "no_warnings":   True,
                            "skip_download": True,
                        }
                        # Mirror cookie settings so the probe sees the
                        # same authenticated format ladder as the download.
                        self._apply_cookie_opts(probe_opts)
                        self._apply_js_runtime(probe_opts)
                        with yt_dlp.YoutubeDL(probe_opts) as probe:
                            probe_info = probe.extract_info(
                                item_url, download=False)
                        best_abr = 0
                        for f in (probe_info.get("formats") or []):
                            # Audio-only streams have vcodec == "none"
                            if f.get("vcodec") == "none":
                                abr_val = f.get("abr") or f.get("tbr")
                                if abr_val:
                                    try:
                                        best_abr = max(best_abr, int(abr_val))
                                    except (TypeError, ValueError):
                                        pass
                        # Cap at 320 (MP3 ceiling). Upgrade only if we
                        # actually found a higher bitrate than the user's.
                        if best_abr > int(output_kbps):
                            effective_kbps = str(min(best_abr, 320))
                            self._dbg.info(
                                f"BITRATE AUTO-UPGRADE | {item_title!r}  "
                                f"source={best_abr}k > setting={output_kbps}k  "
                                f"→ encoding MP3 at {effective_kbps}k")
                    except Exception as probe_exc:
                        # Probe failures are non-fatal; fall back to
                        # the user's configured bitrate.
                        self._dbg.warning(
                            f"BITRATE PROBE FAIL | {item_title!r}  "
                            f"{probe_exc}")

                # Track the best source bitrate seen during this download
                source_abr = [None]

                # Progress hook
                def make_hook(i=idx, abr_ref=source_abr):
                    _ansi_re = re.compile(r'\x1b\[[0-9;]*m')
                    def hook(d):
                        st = d.get("status")
                        if st == "downloading":
                            tb = d.get("total_bytes") or \
                                 d.get("total_bytes_estimate", 0)
                            db = d.get("downloaded_bytes", 0)
                            sp = _ansi_re.sub('', d.get("_speed_str", "")).strip()
                            eta = _ansi_re.sub('', d.get("_eta_str", "")).strip()
                            pct = _ansi_re.sub('', d.get("_percent_str", "")).strip()
                            # Capture source audio bitrate if yt-dlp provides it
                            abr = d.get("info_dict", {}).get("abr") or \
                                  d.get("info_dict", {}).get("tbr")
                            if abr and abr_ref[0] is None:
                                abr_ref[0] = abr
                                self.after(0, lambda b=abr:
                                    self._bitrate_lbl.config(
                                        text=f"src {int(b)}k → {effective_kbps}k"))
                                # Show source bitrate on the queue row
                                self.after(0, lambda b=abr, ii=i:
                                    self._set_row_bitrate(
                                        ii, f"{int(b)}k → {effective_kbps}k"))
                            if tb:
                                self.after(0, lambda p=db/tb*100:
                                    self._vid_progress.config(value=p))
                            if sp:
                                speed_txt = f"{sp}  {eta}" if eta else sp
                                self.after(0, lambda s=speed_txt:
                                    self._speed_lbl.config(text=s))
                            # Update queue row title from actual yt-dlp filename
                            # so the queue always matches the saved file name.
                            real_title = d.get("info_dict", {}).get("title", "")
                            if real_title and i < len(self._queue):
                                if self._queue[i]["title"] != real_title:
                                    self._queue[i]["title"] = real_title
                                    self.after(0, lambda ii=i, t=real_title: (
                                        self._cur_lbl.config(text=t[:80]),
                                        self._set_row_state(ii, ST_ACTIVE),
                                    ))
                        elif st == "finished":
                            self.after(0, lambda: (
                                self._vid_progress.config(value=95),
                                self._speed_lbl.config(text="converting…"),
                            ))
                    return hook

                ydl_opts = {
                    # Prefer highest-bitrate audio-only stream.
                    # abr>=160 targets Opus 160k (itag 251) or better;
                    # falls back to plain bestaudio, then muxed best.
                    "format":   "bestaudio[abr>=160]/bestaudio/best",
                    "outtmpl":  os.path.join(save_dir, "%(title)s.%(ext)s"),
                    "progress_hooks": [make_hook(idx)],
                    "quiet":       True,
                    "no_warnings": True,
                }

                # Point yt-dlp at the bundled FFmpeg (packaged build) so it does
                # not depend on FFmpeg being on PATH. None when run from source.
                _ffmpeg_dir = bundled_ffmpeg_dir()
                if _ffmpeg_dir:
                    ydl_opts["ffmpeg_location"] = _ffmpeg_dir

                # Ask yt-dlp to save the thumbnail beside the audio. We embed it
                # ourselves rather than using the EmbedThumbnail postprocessor,
                # so we control the crop and keep the .artwork sidecar copy.
                _cover_mode = self._cover_art_mode_value()
                if _cover_mode != "off" and cb_artwork.artwork_available():
                    ydl_opts["writethumbnail"] = True

                # Only add the FFmpeg MP3 postprocessor if conversion is enabled.
                # When "Keep original format" is checked, the file is saved
                # as-is in whatever container YouTube/SoundCloud served
                # (typically .webm/Opus or .m4a/AAC for YT; .mp3 or .webm for SC).
                if not self._no_conversion.get():
                    ydl_opts["postprocessors"] = [{
                        "key":              "FFmpegExtractAudio",
                        "preferredcodec":   "mp3",
                        "preferredquality": effective_kbps,
                    }]

                # ── Download behavior options ─────────────────────────
                if self._geo_bypass.get():
                    ydl_opts["geo_bypass"] = True

                if session_ua:
                    ydl_opts.setdefault("http_headers", {})["User-Agent"] = session_ua

                if self._sleep_enabled.get():
                    s_min, s_max = self._resolve_sleep_range()
                    ydl_opts["sleep_interval"]     = s_min
                    ydl_opts["max_sleep_interval"] = s_max

                using_cookies = self._use_cookies.get()
                self._apply_cookie_opts(ydl_opts)

                self._apply_js_runtime(ydl_opts)
                self._dbg.info(f"─── DOWNLOAD ─── {item_title!r}  URL: {item_url}")
                self._dbg_ydl_opts("download", ydl_opts)

                try:
                    # Transient-network retry loop. Handles ConnectionReset
                    # (Winsock 10054), timeouts, and "connection broken"
                    # errors — common with VPNs and rate-limiting. Up to 3
                    # attempts with exponential backoff (2s, 4s). Permanent
                    # errors (age-gate, unavailable, etc.) fall through on
                    # the first raise.
                    _attempt = 0
                    _max_attempts = 3
                    while True:
                        _attempt += 1
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                ydl.download([item_url])
                            break
                        except Exception as _net_exc:
                            _ne = str(_net_exc).lower()
                            _transient = (
                                "10054" in _ne
                                or "connection reset" in _ne
                                or "connection broken" in _ne
                                or "connection aborted" in _ne
                                or "connectionreseterror" in _ne
                                or "timed out" in _ne
                                or "read timeout" in _ne
                                or "temporary failure" in _ne
                                or "remote end closed" in _ne
                            )
                            if _transient and _attempt < _max_attempts:
                                _delay = 2 ** _attempt
                                self._dbg.warning(
                                    f"NET RETRY   | {item_title!r}  "
                                    f"attempt {_attempt}/{_max_attempts-1} "
                                    f"after transient error "
                                    f"(sleeping {_delay}s): "
                                    f"{str(_net_exc)[:120]}")
                                self._logger.info(
                                    f"NET RETRY   | "
                                    f"Title: {item_title} | "
                                    f"Attempt {_attempt}/{_max_attempts-1} "
                                    f"after network error, "
                                    f"retrying in {_delay}s")
                                time.sleep(_delay)
                                if self._cancel_flag.is_set():
                                    raise
                                continue
                            raise
                    done += 1
                    self._grand_dl += 1
                    src_str = (f"{int(source_abr[0])} kbps src → "
                               f"{effective_kbps} kbps MP3"
                               if source_abr[0] else f"{effective_kbps} kbps MP3")
                    self._dbg.info(
                        f"DOWNLOAD OK   | {item_title!r}  quality={src_str}")
                    self._log_download(item_title, expected_path, item_url,
                                       platform, genre, quality=src_str)
                    # Stamp ID3 tags (title / encoded-by / source URL) on the
                    # file just written. Resolve the real path first, since
                    # yt-dlp's sanitiser may differ from expected_path.
                    _real_path = (self._file_exists_on_disk(save_dir, item_title)
                                  or expected_path)
                    self._tag_track(_real_path, item_title, item_url)
                    # Embed the source thumbnail as cover art and keep the
                    # sidecar copy. Runs after _tag_track so the APIC frame is
                    # written onto the tag mutagen has already created.
                    _art_path, _art_embedded = self._harvest_cover_art(
                        _real_path, entry.get("id"), item_title)
                    # ── Record in the downloads database ──────────────
                    _vid_upload = entry.get("upload_date", "") or ""
                    self._db.add_download(
                        video_id=entry.get("id"),
                        title=item_title,
                        channel_name=channel_name_override or collection_name,
                        channel_url=url if is_collection else "",
                        channel_id=coll_channel_id or None,
                        platform=platform, genre=genre or "(none)",
                        file_path=expected_path,
                        upload_date=_vid_upload,
                        bitrate=src_str,
                        artwork_path=_art_path,
                        artwork_embedded=_art_embedded,
                        thumbnail_url=entry.get("thumbnail"))
                    if _vid_upload and (
                            _max_upload_date_this_run is None
                            or _vid_upload > _max_upload_date_this_run):
                        _max_upload_date_this_run = _vid_upload
                    brate_txt = (f"{int(source_abr[0])}k → {effective_kbps}k"
                                 if source_abr[0] else f"→ {effective_kbps}k")
                    self.after(0, lambda i=idx, b=brate_txt:
                        self._set_row_bitrate(i, b, SUCCESS))
                    self.after(0, lambda i=idx: self._set_row_state(
                        i, ST_DONE, "✓ done"))
                    self.after(0, lambda: (
                        self._vid_progress.config(value=100),
                        self._speed_lbl.config(text=""),
                    ))
                except Exception as exc:
                    raw_err = str(exc)
                    clean = re.sub(r'\x1b\[[0-9;]*m', '', raw_err).strip()
                    clean_lower = clean.lower()
                    self._dbg.error(
                        f"DOWNLOAD FAIL | {item_title!r}  URL: {item_url}")
                    self._dbg.error(f"DOWNLOAD FAIL | error: {clean}")
                    import traceback
                    self._dbg.debug(
                        f"DOWNLOAD FAIL | traceback:\n{traceback.format_exc()}")

                    # Age-restricted videos fail with cookies because
                    # YouTube forces the main player which requires age
                    # verification. Without cookies, yt-dlp uses the
                    # embedded player which bypasses age gates. Retry
                    # without cookies for age-restricted content.
                    is_age = ("age" in clean_lower or
                              "verify your age" in clean_lower or
                              "adult" in clean_lower)
                    if is_age and using_cookies:
                        self._dbg.info(
                            f"AGE-RETRY   | {item_title!r}  "
                            f"Retrying without cookies to bypass age gate")
                        self._logger.info(
                            f"AGE-RETRY   | "
                            f"Title: {item_title} | "
                            f"URL: {item_url} | "
                            f"Retrying without cookies to bypass age gate")
                        retry_opts = {
                            "format":   "bestaudio[abr>=160]/bestaudio/best",
                            "outtmpl":  os.path.join(save_dir,
                                            "%(title)s.%(ext)s"),
                            "postprocessors": [{
                                "key":              "FFmpegExtractAudio",
                                "preferredcodec":   "mp3",
                                "preferredquality": output_kbps,
                            }],
                            "progress_hooks": [make_hook(idx)],
                            "quiet":       True,
                            "no_warnings": True,
                        }
                        if _cover_mode != "off" and cb_artwork.artwork_available():
                            retry_opts["writethumbnail"] = True
                        if self._geo_bypass.get():
                            retry_opts["geo_bypass"] = True
                        if session_ua:
                            retry_opts.setdefault(
                                "http_headers", {})["User-Agent"] = \
                                session_ua
                        try:
                            self._apply_js_runtime(retry_opts)
                            with yt_dlp.YoutubeDL(retry_opts) as ydl:
                                ydl.download([item_url])
                            done += 1
                            self._grand_dl += 1
                            src_str = (
                                f"{int(source_abr[0])} kbps src → "
                                f"{output_kbps} kbps MP3"
                                if source_abr[0]
                                else f"{output_kbps} kbps MP3")
                            self._log_download(
                                item_title, expected_path, item_url,
                                platform, genre, quality=src_str)
                            _real_path = (
                                self._file_exists_on_disk(save_dir, item_title)
                                or expected_path)
                            self._tag_track(_real_path, item_title, item_url)
                            _art_path, _art_embedded = self._harvest_cover_art(
                                _real_path, entry.get("id"), item_title)
                            # ── Record retry success in DB ────────────
                            _vid_upload = entry.get("upload_date", "") or ""
                            self._db.add_download(
                                video_id=entry.get("id"),
                                title=item_title,
                                channel_name=(channel_name_override
                                              or collection_name),
                                channel_url=url if is_collection else "",
                                channel_id=coll_channel_id or None,
                                platform=platform,
                                genre=genre or "(none)",
                                file_path=expected_path,
                                upload_date=_vid_upload,
                                bitrate=src_str,
                                artwork_path=_art_path,
                                artwork_embedded=_art_embedded,
                                thumbnail_url=entry.get("thumbnail"))
                            if _vid_upload and (
                                    _max_upload_date_this_run is None
                                    or _vid_upload
                                       > _max_upload_date_this_run):
                                _max_upload_date_this_run = _vid_upload
                            brate_txt = (
                                f"{int(source_abr[0])}k → {output_kbps}k"
                                if source_abr[0]
                                else f"→ {output_kbps}k")
                            self.after(0, lambda i=idx, b=brate_txt:
                                self._set_row_bitrate(i, b, SUCCESS))
                            self.after(0, lambda i=idx:
                                self._set_row_state(
                                    i, ST_DONE, "✓ done"))
                            self.after(0, lambda: (
                                self._vid_progress.config(value=100),
                                self._speed_lbl.config(text=""),
                            ))
                            self.after(0, lambda d=done, t=total: (
                                self._overall_progress.config(value=d),
                                self._ov_lbl.config(text=f"{d} / {t}"),
                            ))
                            self.after(0, self._update_ov_stats)
                            continue   # skip the error handling below
                        except Exception as retry_exc:
                            raw_err = str(retry_exc)
                            clean = re.sub(
                                r'\x1b\[[0-9;]*m', '', raw_err).strip()
                            clean_lower = clean.lower()
                            # Fall through to error handling

                    self._dbg.error(
                        f"FINAL ERROR | {item_title!r}  "
                        f"URL: {item_url}  "
                        f"cookies={using_cookies}  "
                        f"full_error: {raw_err}")
                    self._logger.error(
                        f"ERROR       | "
                        f"Title: {item_title} | "
                        f"URL: {item_url} | "
                        f"Full error: {raw_err}")
                    if   "ffmpeg"        in clean_lower: err = "FFmpeg missing"
                    elif "sign in"       in clean_lower: err = "login required"
                    elif is_age:                         err = "age-restricted"
                    elif "unavailable"   in clean_lower: err = "unavailable"
                    elif "private"       in clean_lower: err = "private"
                    elif "copyright"     in clean_lower: err = "copyright claim"
                    elif "members"       in clean_lower: err = "members only"
                    elif "removed"       in clean_lower: err = "removed"
                    elif "blocked"       in clean_lower: err = "blocked"
                    elif "not available" in clean_lower: err = "format unavailable"
                    else:                                err = clean[:60]
                    errors += 1
                    done   += 1
                    self._grand_er += 1
                    self._log_error(item_title, item_url, err)
                    self.after(0, lambda i=idx, e=err:
                        self._set_row_state(i, ST_ERROR, e))

                self.after(0, lambda d=done, t=total: (
                    self._overall_progress.config(value=d),
                    self._ov_lbl.config(text=f"{d} / {t}"),
                ))
                self.after(0, self._update_ov_stats)

            # Per-URL summary in status bar
            actual = done - skipped - errors
            parts  = [f"✓ {actual} downloaded"]
            if skipped: parts.append(f"⊘ {skipped} skipped")
            if errors:  parts.append(f"✗ {errors} failed")
            if self._cancel_flag.is_set():
                parts.insert(0, "Cancelled.")
            self.after(0, lambda s="  ".join(parts): self._set_status(s))

            # ── Auto-add to Watch List (collections only) ─────────────
            actual_downloaded = done - skipped - errors
            if (is_collection and actual_downloaded > 0
                    and not channel_name_override):
                # Only auto-add when this is a normal user download, not
                # a Watch List "Download New" run (which has override set)
                self._watchlist_auto_add_if_enabled(
                    url,
                    channel_name_override or collection_name,
                    genre,
                    _max_upload_date_this_run,
                    channel_id=coll_channel_id)

            return actual_downloaded, skipped, errors

        except Exception as exc:
            err = str(exc)
            # Log full untruncated error
            self._logger.error(
                f"FATAL ERROR | Full error: {err}")
            if "ffmpeg" in err.lower():
                err = "FFmpeg not found — install FFmpeg and add it to PATH."
            self._last_fatal_error = err
            self.after(0, lambda e=err: (
                self._cur_lbl.config(text=f"✗ {e}"),
                self._set_status(f"Error: {e}"),
            ))
            return None, None, None

    def _update_ov_stats(self):
        """Refresh the real-time stats label next to the Overall progress bar."""
        parts = []
        if self._grand_dl: parts.append(f"Downloaded: {self._grand_dl}")
        if self._grand_sk: parts.append(f"Skipped: {self._grand_sk}")
        if self._grand_er: parts.append(f"Failed: {self._grand_er}")
        self._ov_stats_lbl.config(text="    ".join(parts) if parts else "")

    def _finish(self):
        """Reset controls and status when a download run completes or is cancelled."""
        self._downloading = False
        self._pause_flag.clear()
        self._dl_btn.config(state="normal")
        self._batch_add_btn.config(state="normal")
        self._url_entry.config(state="normal")
        self._cancel_btn.config(state="disabled", style="Cancel.TButton")
        self._pause_btn.config(state="disabled", text="⏸  Pause", style="Pause.TButton")
        self._bitrate_lbl.config(text="")
        self._wl_update_cancel_btn_state()

    # ══════════════════════════════════════════════════════════════════════════
    # Watch List tab — channel cards, scan/download, channel resolution, add/edit
    # ══════════════════════════════════════════════════════════════════════════
    def _cancel_all_updates(self):
        """Global cancel: stops in-progress downloads AND Watch List scans."""
        self._cancel_flag.set()
        self._pause_flag.clear()
        if self._downloading:
            try:
                self._cancel_btn.config(state="disabled", style="Cancel.TButton")
                self._pause_btn.config(state="disabled")
                self._set_status("Cancelling after current track…")
            except Exception:
                pass
        try:
            self._wl_cancel_btn.config(state="disabled",
                                       bg=WL_CANCEL_IDLE, fg=TEXT_DIM)
        except Exception:
            pass
        if self._wl_scan_active > 0:
            self._watchlist_log("Cancelling scans…", "info")

    def _wl_update_cancel_btn_state(self):
        """Enable the Watch List Cancel button only when Watch-List work is in
        flight (a Watch List batch download or a scan). A Main-tab download has
        its own Cancel button, so it must not light this one up."""
        try:
            active = (getattr(self, "_wl_download_active", False)
                      or self._wl_scan_active > 0)
            if active:
                self._wl_cancel_btn.config(
                    state="normal", bg=YT_DARK, fg=TEXT)
            else:
                self._wl_cancel_btn.config(
                    state="disabled", bg=WL_CANCEL_IDLE, fg=TEXT_DIM)
        except Exception:
            pass

    def _watchlist_cancel_card(self, cid):
        """Cancel just this card's in-flight work. A scan is stopped via its
        per-channel cancel flag; a download (which runs one batch at a time
        through the main pipeline) is cancelled as a whole."""
        ch = self._db.get_watchlist_channel(cid)
        name = ch.get("display_name") if ch else f"#{cid}"
        batch = self._active_watchlist_batch or {}
        is_downloading = bool(self._downloading) and \
            cid in batch.get("channel_ids", [])
        if is_downloading:
            # Downloads are serialised through the main pipeline — cancel it.
            self._cancel_flag.set()
            self._watchlist_log(f"Cancelling download: {name}…", "info")
            try:
                self._cancel_btn.config(state="disabled", style="Cancel.TButton")
                self._pause_btn.config(state="disabled")
            except Exception:
                pass
        else:
            # Scanning — signal just this channel's scan loop to stop.
            self._wl_cancel_cids.add(cid)
            self._watchlist_log(f"Cancelling scan: {name}…", "info")
        self._wl_update_cancel_btn_state()

    def _build_watchlist_tab(self, parent):
        """Build the Watch List tab: toolbar, scrollable channel cards, scan log."""
        # ── Pinned top: header + toolbar (always visible, never scroll) ───────
        top_fixed = ttk.Frame(parent, padding=(28, 22, 28, 0))
        top_fixed.pack(side="top", fill="x")

        hdr = ttk.Frame(top_fixed)
        hdr.pack(fill="x", pady=(0, 4))

        tk.Label(hdr, text="👁", font=("Segoe UI", 20),
                 fg=WL_BLUE, bg=BG).pack(side="left", padx=(0, 10))
        ttk.Label(hdr, text="Watch List",
                  style="Title.TLabel").pack(side="left", pady=(2, 0))

        ttk.Label(top_fixed,
                  text="Track YouTube channels and scan for new uploads. "
                       "Add channels here, then click Scan to check for new content.",
                  style="S.Dim.TLabel", wraplength=660
                  ).pack(anchor="w", pady=(0, 14))

        tk.Frame(top_fixed, height=1, bg=BORDER).pack(fill="x", pady=(0, 14))

        # ── Toolbar (pinned) ──────────────────────────────────────────────────
        toolbar = ttk.Frame(top_fixed)
        toolbar.pack(fill="x", pady=(0, 14))

        self._wl_add_btn = tk.Button(
            toolbar, text="  +  Add Channel  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=self._watchlist_open_add_dialog)
        self._wl_add_btn.pack(side="left", padx=(0, 6))

        self._wl_scan_all_btn = tk.Button(
            toolbar, text="  🔍  Scan All  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=self._watchlist_scan_all)
        self._wl_scan_all_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_scan_all_btn,
                "Check every channel for new uploads since the last scan.")

        # Check Links / Force Download All share one toolbar slot: Check Links
        # shows only while a channel still needs its URL resolved; otherwise
        # Force Download All takes its place. _wl_update_toolbar_buttons()
        # (driven from _wl_update_dl_all_count) packs the right one.
        self._wl_fix_btn = tk.Button(
            toolbar, text="  🛠  Check Links  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=self._watchlist_fix_broken)
        Tooltip(self._wl_fix_btn,
                "Look up the real YouTube channel for any folder that still "
                "needs one, so it can be scanned. Shows the top matches to "
                "choose from.")

        self._wl_force_btn = tk.Button(
            toolbar, text="  ⤓  Force Download All  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=self._watchlist_force_download_all)
        Tooltip(self._wl_force_btn,
                "Run every channel's full catalogue, not just new uploads. "
                "Tracks already on disk are skipped — and stamped with their "
                "source-URL ID3 tag — while anything missing is downloaded.")

        self._wl_dl_all_btn = tk.Button(
            toolbar, text="  ⬇  Download All New (0)  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=self._watchlist_download_all_new)
        self._wl_dl_all_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_dl_all_btn,
                "Download all pending new tracks across every channel.")

        self._wl_cancel_btn = tk.Button(
            toolbar, text="  ✕  Cancel  ",
            font=("Segoe UI", 10, "bold"),
            bg=WL_CANCEL_IDLE, fg=TEXT_DIM,
            activebackground=YT_DARK, activeforeground=TEXT,
            disabledforeground=TEXT_DIM,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            state="disabled",
            command=self._cancel_all_updates)
        self._wl_cancel_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_cancel_btn,
                "Stop all in-progress Watch List scans and downloads.")

        # Next scheduled auto-download, pinned just below the button row.
        self._wl_next_dl_lbl = tk.Label(
            top_fixed, text="", font=("Segoe UI", 11, "bold"),
            fg=WL_BLUE, bg=BG, anchor="w")
        self._wl_next_dl_lbl.pack(fill="x", pady=(8, 12))
        self._wl_update_next_dl_label()

        # ── Resizable split:  scrollable cards (top)  /  pinned log (bottom) ──
        paned = tk.PanedWindow(
            parent, orient="vertical", bg=BORDER, sashwidth=6, sashpad=0,
            bd=0, relief="flat", opaqueresize=True)
        paned.pack(side="top", fill="both", expand=True)
        self._wl_paned = paned

        # Pane 1 — scrollable channel cards.
        cards_area = tk.Frame(paned, bg=BG)
        self._wl_canvas, outer = self._make_scrollable(
            cards_area, (28, 14, 28, 18))

        self._wl_cards_frame = tk.Frame(outer, bg=BG)
        self._wl_cards_frame.pack(fill="x", pady=(0, 4))

        # Pane 2 — scan log, pinned at the bottom and always visible.
        log_frame = ttk.Frame(paned, padding=(28, 6, 28, 10))
        self._wl_log_frame = log_frame

        log_wrap = tk.Frame(log_frame, bg=BG)
        log_wrap.pack(fill="both", expand=True)
        log_sb = ttk.Scrollbar(log_wrap, orient="vertical")
        log_sb.pack(side="right", fill="y")
        self._wl_log_txt = tk.Text(
            log_wrap, height=10, font=("Consolas", 9),
            bg="#0a0a0a", fg=TEXT_DIM, relief="flat",
            wrap="word", state="disabled",
            selectbackground=BORDER, selectforeground=TEXT,
            padx=8, pady=6,
            highlightthickness=1, highlightbackground=BORDER,
            yscrollcommand=log_sb.set)
        self._wl_log_txt.pack(side="left", fill="both", expand=True)
        log_sb.config(command=self._wl_log_txt.yview)
        self._wl_log_txt.tag_configure("ok", foreground=SUCCESS)
        self._wl_log_txt.tag_configure("err", foreground=YT_RED)
        self._wl_log_txt.tag_configure("info", foreground=WL_BLUE)

        # Assemble the split: cards stretch to fill, log keeps its size.
        paned.add(cards_area, stretch="always", minsize=140)
        paned.add(log_frame, stretch="never", minsize=90)
        # Place the divider so the log starts ~150px tall once we know height.
        self.after(120, self._wl_init_log_sash)

        # Populate on first load
        self._watchlist_refresh()

    def _wl_init_log_sash(self):
        """Position the cards/log divider so the pinned scan log opens tall
        enough to show its full ~10 lines. The user can drag it from there."""
        try:
            paned = self._wl_paned
            paned.update_idletasks()
            h = paned.winfo_height()
            log_h = self._wl_log_frame.winfo_reqheight()
            # Only resize when the cards pane keeps at least its minimum height.
            if h - log_h > 140:
                paned.sash_place(0, 0, h - log_h)
        except Exception:
            pass

    def _watchlist_log(self, msg, tag="info"):
        """Append a timestamped line to the Watch List scan log.
        Auto-trims to the last ~500 lines so a long session can't grow it
        without bound."""
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self._wl_log_txt.config(state="normal")
            self._wl_log_txt.insert("end", f"[{ts}]  {msg}\n", tag)
            # Keep only the most recent 500 lines.
            line_count = int(self._wl_log_txt.index("end-1c").split(".")[0])
            if line_count > 500:
                self._wl_log_txt.delete("1.0", f"{line_count - 500}.0")
            self._wl_log_txt.see("end")
            self._wl_log_txt.config(state="disabled")
        except Exception:
            pass

    def _wl_dl_log(self, msg, tag="info"):
        """Mirror a download-progress line into the Watch List scan log, but
        only while a Watch List batch is running. Safe to call from the batch
        worker thread (marshals onto the Tk main thread)."""
        if getattr(self, "_wl_download_active", False):
            self.after(0, lambda: self._watchlist_log(msg, tag))

    def _wl_update_dl_all_count(self):
        """Refresh just the 'Download All New (N)' button count from the DB,
        without rebuilding any cards."""
        try:
            total_pending = self._db.get_total_pending_count()
            self._wl_dl_all_btn.config(
                text=f"  ⬇  Download All New ({total_pending})  ")
            # Mirror the count into the tray menu label (plain str read on the
            # tray thread; safe because it's only ever assigned from here).
            self._tray_dl_label = f"Download All New ({total_pending})"
        except Exception:
            pass
        self._wl_update_toolbar_buttons()

    def _wl_update_toolbar_buttons(self):
        """Show Check Links only while at least one channel still needs its URL
        resolved; otherwise show Force Download All in that same slot. Both are
        packed just before the Download All New button so the slot stays put."""
        fix = getattr(self, "_wl_fix_btn", None)
        force = getattr(self, "_wl_force_btn", None)
        anchor = getattr(self, "_wl_dl_all_btn", None)
        if fix is None or force is None or anchor is None:
            return
        try:
            has_broken = any(self._is_unresolved_channel(c)
                             for c in self._db.get_all_watchlist_channels())
        except Exception:
            has_broken = False
        show, hide = (fix, force) if has_broken else (force, fix)
        try:
            hide.pack_forget()
            if not show.winfo_ismapped():
                show.pack(side="left", padx=(0, 6), before=anchor)
        except Exception:
            pass

    def _watchlist_refresh(self):
        """Rebuild ALL channel cards from the database. Use this only for
        structural changes (add / remove / import / edit-save); per-channel
        status changes should call _watchlist_update_card so the rest of the
        list isn't torn down and rebuilt (which visibly blanks every card)."""
        # Clear existing cards
        for w in self._wl_cards_frame.winfo_children():
            w.destroy()
        self._wl_card_widgets = {}   # stale frames are gone; rebuild the index

        channels = self._db.get_all_watchlist_channels()

        if not channels:
            tk.Label(self._wl_cards_frame,
                     text="No channels in the Watch List yet.\n"
                          "Use  + Add Channel  or  ⎘ Import from Log  to get started.",
                     font=("Segoe UI", 11), fg=TEXT_DIM, bg=BG,
                     justify="center", pady=30
                     ).pack(fill="x")
        else:
            for ch in channels:
                self._watchlist_build_channel_card(self._wl_cards_frame, ch)

        self._wl_update_dl_all_count()

    def _watchlist_update_card(self, cid):
        """Redraw a single channel card in place — its status, details and
        action buttons — without disturbing the other cards. Falls back to a
        full refresh if the card frame is gone or the channel no longer exists
        (i.e. the card *set* changed)."""
        card = self._wl_card_widgets.get(cid)
        ch = self._db.get_watchlist_channel(cid)
        if card is None or not card.winfo_exists() or ch is None:
            self._watchlist_refresh()
            return
        for w in card.winfo_children():
            w.destroy()
        self._watchlist_fill_card(card, ch)
        self._wl_update_dl_all_count()

    def _watchlist_update_cards(self, cids):
        """Redraw several cards in place (used by the multi-channel download
        paths). Each is independent; the rest of the list is untouched."""
        for cid in cids:
            self._watchlist_update_card(cid)

    def _watchlist_build_channel_card(self, parent, ch):
        """Create one dark card frame for a watchlist channel, register it for
        in-place updates, and fill in its content."""
        cid = ch["id"]

        card = tk.Frame(parent, bg=SURFACE, padx=14, pady=10,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", pady=(0, 8))
        self._wl_card_widgets[cid] = card
        self._watchlist_fill_card(card, ch)

    def _watchlist_fill_card(self, card, ch):
        """Build the inner rows (name/status, details, action buttons) into a
        card frame. Called on first build and on every in-place update, so the
        card frame is reused while its contents are rebuilt from fresh DB data."""
        cid = ch["id"]

        # ── Row 1: Name + platform/genre ──────────────────────────────────
        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x")

        # Channel title. When the channel has a resolved URL, render it as a
        # clickable link that opens the channel's page; unresolved/sentinel
        # channels (no real URL yet) stay plain text.
        link_url = (ch.get("url") or "").strip()
        if link_url and not link_url.startswith(UNRESOLVED_URL_PREFIX):
            title_lbl = tk.Label(top, text=ch["display_name"],
                                  font=("Segoe UI", 12, "bold"), fg=LINK_COL,
                                  bg=SURFACE, anchor="w", cursor="hand2")
            title_lbl.pack(side="left")
            title_lbl.bind("<Button-1>",
                           lambda _e, u=link_url: webbrowser.open(u))
            title_lbl.bind("<Enter>", lambda _e, l=title_lbl: l.config(
                font=("Segoe UI", 12, "bold", "underline")))
            title_lbl.bind("<Leave>", lambda _e, l=title_lbl: l.config(
                font=("Segoe UI", 12, "bold")))
            Tooltip(title_lbl, f"Open channel page:\n{link_url}")
        else:
            tk.Label(top, text=ch["display_name"],
                     font=("Segoe UI", 12, "bold"), fg=TEXT, bg=SURFACE,
                     anchor="w").pack(side="left")

        plat_genre = f"{ch['platform']}  •  {ch.get('genre') or '(none)'}"
        tk.Label(top, text=plat_genre,
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=SURFACE
                 ).pack(side="left", padx=(12, 0))

        # Status indicator on the right
        status = ch.get("status", "idle")
        pending = ch.get("pending_new_count", 0)
        if status == "found" and pending > 0:
            st_text = f"✦ {pending} new"
            st_color = SUCCESS
        elif status == "scanning":
            st_text = "⟳ scanning…"
            st_color = WL_BLUE
        elif status == "needs_resolve":
            st_text = "⚠ needs channel ID"
            st_color = "#f59e0b"   # amber
        elif status == "error":
            st_text = "✗ error"
            st_color = YT_RED
        elif status == "idle":
            st_text = "○ idle"
            st_color = TEXT_DIM
        else:
            st_text = f"○ {status}"
            st_color = TEXT_DIM

        tk.Label(top, text=st_text,
                 font=("Segoe UI", 10, "bold"), fg=st_color, bg=SURFACE
                 ).pack(side="right")

        # ── Row 2: Details ────────────────────────────────────────────────
        mid = tk.Frame(card, bg=SURFACE)
        mid.pack(fill="x", pady=(4, 0))

        last_dl = format_timestamp_relative(ch.get("last_download_started"))
        cutoff_readable = format_yyyymmdd_readable(ch.get("scan_cutoff_date", ""))
        details = (f"Last download: {last_dl}  •  "
                   f"Cutoff: {cutoff_readable}  •  "
                   f"Total downloaded: {ch.get('total_downloaded', 0)}")
        if ch.get("auto_added"):
            details += "  •  auto-added"

        tk.Label(mid, text=details,
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=SURFACE,
                 anchor="w").pack(side="left")

        # ── Row 3: Action buttons ─────────────────────────────────────────
        btns = tk.Frame(card, bg=SURFACE)
        btns.pack(fill="x", pady=(6, 0))

        # Is this card busy right now? Scans set status "scanning"; downloads
        # list their channel(s) in the active batch while _downloading is set.
        batch = self._active_watchlist_batch or {}
        is_scanning = ch.get("status") == "scanning"
        is_downloading = bool(self._downloading) and \
            cid in batch.get("channel_ids", [])

        # (label, command, is_cancel)
        card_buttons = []
        if is_scanning or is_downloading:
            # Per-card Cancel — stops just this channel's scan/download.
            card_buttons.append(
                ("✕ Cancel", lambda c=cid: self._watchlist_cancel_card(c), True))
        card_buttons += [
            ("🔍 Scan",    lambda c=cid: self._watchlist_scan_channel(c), False),
            ("⚡ Force Download", lambda c=cid: self._watchlist_force_download(c), False),
            (f"⬇ Download New ({pending})",
                           lambda c=cid: self._watchlist_download_new(c), False),
        ]
        if is_unresolved_channel(ch):
            # Only unresolved channels need their link healed. Sits just left of
            # Edit so it reads as a per-channel link action.
            card_buttons.append(
                ("🛠 Fix Link", lambda c=cid: self._watchlist_resolve_dialog(c), False))
        card_buttons += [
            ("✏ Edit",     lambda c=cid: self._watchlist_edit_channel(c), False),
            ("✕ Remove",   lambda c=cid: self._watchlist_remove_channel(c), False),
        ]
        # Per-card Cancel is intentionally inert (disabled, dark orange): the
        # only working Cancel on this tab is the top primary one, which stops
        # the whole batch/scan. The card button just signals "busy".
        WL_CARD_CANCEL = "#78350f"   # dark orange
        for btn_text, btn_cmd, is_cancel in card_buttons:
            if is_cancel:
                b = tk.Button(btns, text=btn_text,
                              font=("Segoe UI", 9),
                              bg=WL_CARD_CANCEL, fg=TEXT_DIM,
                              disabledforeground=TEXT_DIM,
                              relief="flat", bd=0, padx=8, pady=3,
                              state="disabled")
            else:
                b = tk.Button(btns, text=btn_text,
                              font=("Segoe UI", 9),
                              bg=SURFACE2, fg=TEXT_MED,
                              activebackground=BORDER, activeforeground=TEXT,
                              relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
                              command=btn_cmd)
            b.pack(side="left", padx=(0, 4))

        # Show error if present
        if ch.get("last_error"):
            tk.Label(btns, text=f"  {ch['last_error'][:60]}",
                     font=("Segoe UI", 8), fg=YT_RED, bg=SURFACE,
                     anchor="w").pack(side="left", padx=(8, 0))

    # ── Channel resolution (heal legacy folders) ───────────────────────────────
    @staticmethod
    def _is_unresolved_channel(ch):
        """True if the channel has no canonical YouTube URL yet (needs Fix Link)."""
        return is_unresolved_channel(ch)

    def _resolve_channel_via_search(self, name, max_results=3):
        """Search YouTube for a channel by display name. Returns up to
        max_results candidate dicts {title, channel_id, url, handle,
        followers}. Raises on network/extractor failure."""
        import yt_dlp
        q = urllib.parse.quote(name)
        # sp=EgIQAg%3D%3D  →  YouTube's "Channel" search-results filter.
        search_url = (f"https://www.youtube.com/results?search_query={q}"
                      f"&sp=EgIQAg%3D%3D")
        opts = {
            "quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True,
            "playlist_items": f"1-{max_results}",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
        out = []
        for e in (info.get("entries") or []):
            cid = e.get("channel_id") or e.get("id") or ""
            if not str(cid).startswith("UC"):
                continue  # skip non-channel rows
            out.append({
                "title": e.get("title") or e.get("channel") or name,
                "channel_id": cid,
                "url": channel_url_from_id(cid),
                "handle": e.get("uploader_id") or "",
                "followers": e.get("channel_follower_count"),
            })
            if len(out) >= max_results:
                break
        return out

    @staticmethod
    def _channel_id_from_url(url):
        """Pull a UC… channel id straight out of a /channel/ URL, if present."""
        return channel_id_from_url(url)

    def _persist_resolved_channel(self, ch, channel_id, handle="", url=None):
        """Commit a resolved identity. Returns True if the row was updated.

        If the resolved URL already belongs to ANOTHER watchlist row, this is a
        duplicate: we don't fake success — we tell the user which entry it
        duplicates and offer to remove this redundant row."""
        platform = ch.get("platform") or "YouTube"
        store_url = url or channel_url_from_id(channel_id)

        # Pre-check for a collision so we can give a meaningful message instead
        # of a silent UNIQUE failure.
        owner = self._db.get_watchlist_channel_by_url(store_url)
        if owner and owner.get("id") != ch.get("id"):
            self._dbg.info(
                f"WL RESOLVE DUPLICATE | {ch.get('display_name')!r} → "
                f"{channel_id} already tracked as {owner.get('display_name')!r}")
            self._watchlist_offer_remove_duplicate(ch, owner)
            return False

        ok = self._db.update_watchlist_channel_fields(
            ch["id"], url=store_url, channel_id=channel_id,
            status="idle", last_error=None)
        if not ok:
            # Lost a race or another constraint — surface honestly.
            self._db.update_watchlist_status(
                ch["id"], "error", last_error="Could not save resolved link")
            self._watchlist_log(
                f"Couldn't save link for {ch.get('display_name')} "
                f"— it may duplicate another entry.", "err")
            self._watchlist_refresh()
            return False

        # DB update succeeded — now (and only now) stamp the folder sidecar.
        try:
            folder = self._resolve_save_dir(
                ch.get("genre") or "(none)", ch.get("display_name"),
                platform=platform)
            write_channel_sidecar(
                folder, channel_id=channel_id, channel_url=store_url,
                handle=handle, display_name=ch.get("display_name"),
                platform=platform, genre=ch.get("genre") or "(none)")
        except Exception as e:
            self._dbg.warning(f"WL RESOLVE | sidecar write failed: {e}")
        self._dbg.info(
            f"WL RESOLVE OK | {ch.get('display_name')!r} → {channel_id}")
        return True

    def _watchlist_offer_remove_duplicate(self, dup, owner):
        """A Fix Link resolved to a channel already tracked by `owner`.
        Offer to delete the redundant `dup` row."""
        keep = owner.get("display_name") or "another entry"
        remove = dup.get("display_name") or "this entry"
        msg = (f"“{remove}” is the same channel you already track as "
               f"“{keep}”.\n\nRemove the duplicate “{remove}”? "
               f"Its folder on disk is left untouched.")
        if messagebox.askyesno("Duplicate channel", msg, parent=self):
            self._db.remove_watchlist_channel(dup["id"])
            self._watchlist_log(
                f"Removed duplicate “{remove}” (already tracked as "
                f"“{keep}”).", "ok")
        else:
            # User kept it: park it clearly rather than leaving a phantom
            # unresolved row that keeps offering a Fix Link that can't succeed.
            self._db.update_watchlist_status(
                dup["id"], "error",
                last_error=f"Duplicate of “{keep}”")
            self._watchlist_log(
                f"Kept “{remove}”. It duplicates “{keep}” and can't be "
                f"resolved separately.", "info")
        self._watchlist_refresh()

    def _finish_resolve(self, ch, channel_id, handle="", url=None,
                        success_msg=None, close_fn=None):
        """after()-safe finisher for every resolve path: persist the identity,
        announce success ONLY if it actually stuck (a duplicate is handled
        inside the persist call, which returns False), then either close the
        resolve dialog with the real outcome or just refresh the list."""
        ok = self._persist_resolved_channel(ch, channel_id, handle, url=url)
        if ok and success_msg:
            self._watchlist_log(success_msg, "ok")
        if close_fn is not None:
            close_fn(ok)
        else:
            self._watchlist_refresh()
        return ok

    def _watchlist_apply_url(self, cid, new_url):
        """Point a watchlist row at a new channel/playlist URL (from Edit).
        Canonicalises a /channel/UC… URL immediately; for a handle or playlist
        URL, stores it as-is now and resolves the channel_id in the background
        so the sidecar can still be written. Best-effort and non-blocking."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch or not new_url:
            return
        direct = self._channel_id_from_url(new_url)
        if direct:
            self._finish_resolve(
                ch, direct, success_msg=f"Channel set: {ch['display_name']}")
            return
        # Store the URL as typed (works for @handles and playlists), then try
        # to look up the underlying channel_id without blocking the UI.
        self._db.update_watchlist_channel_fields(
            cid, url=new_url, status="idle", last_error=None)
        self._watchlist_log(
            f"URL updated for {ch['display_name']} — resolving channel id…",
            "info")
        self._watchlist_refresh()

        def _bg():
            try:
                import yt_dlp
                opts = {"quiet": True, "no_warnings": True,
                        "extract_flat": "in_playlist", "skip_download": True,
                        "playlist_items": "0"}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(new_url, download=False)
                ucid = info.get("channel_id") or \
                    self._channel_id_from_url(info.get("channel_url", ""))
                handle = info.get("uploader_id") or ""
                if ucid:
                    # Keep the user's URL (may be a playlist) but record the
                    # channel_id and write the folder sidecar.
                    self.after(0, lambda: self._finish_resolve(
                        ch, ucid, handle, url=new_url,
                        success_msg=f"Resolved channel id for "
                                    f"{ch['display_name']}."))
            except Exception as ex:
                msg = str(ex)[:80]
                self.after(0, lambda: self._watchlist_log(
                    f"Couldn't resolve id for {ch['display_name']}: {msg}",
                    "err"))

        self._run_bg(_bg)

    def _sc_track_search(self, name, limit=20):
        """Search SoundCloud *tracks* by name via yt-dlp and return their
        permalink URLs (each first path segment is the artist handle). Flat
        extraction keeps it fast. Raises on extractor/network failure."""
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True,
                "extract_flat": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{name}", download=False)
        out = []
        for e in (info.get("entries") or []):
            url = (e.get("url") or e.get("permalink_url")
                   or e.get("webpage_url") or "")
            if not url:
                continue
            title = (e.get("uploader") or e.get("channel") or "").strip()
            out.append({"url": url, "title": title})
        return out

    @staticmethod
    def _sc_web_search(name, limit=10):
        """Invisible (no-browser) web search for a SoundCloud profile. Queries
        DuckDuckGo's HTML endpoint scoped to soundcloud.com and scrapes the
        profile URLs out of the server-rendered results. Best-effort; raises on
        network failure so the caller can fall back to the track search."""
        query = urllib.parse.quote(f"{name} site:soundcloud.com")
        url = f"https://html.duckduckgo.com/html/?q={query}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", "replace")
        # DuckDuckGo wraps result links in a redirect with the real URL in a
        # percent-encoded 'uddg=' param, so decode the whole page before
        # scraping soundcloud.com links out of it.
        text = urllib.parse.unquote(html)
        hits, seen = [], set()
        for m in re.findall(
                r"https?://(?:www\.)?soundcloud\.com/[A-Za-z0-9_-]+", text):
            key = m.lower()
            if key in seen:
                continue
            seen.add(key)
            hits.append({"url": m})
            if len(hits) >= limit:
                break
        return hits

    def _resolve_soundcloud_via_search(self, name, max_results=8):
        """Find candidate SoundCloud artist profiles for a display name by
        combining an in-app track search with an invisible web search, then
        cross-referencing them. A profile surfaced by BOTH sources is the
        strongest signal and ranks first. Either source failing is tolerated;
        the other still yields results. Returns the merged candidate list (see
        ``merge_soundcloud_candidates``)."""
        track_hits, web_hits = [], []
        try:
            track_hits = self._sc_track_search(name)
        except Exception as ex:
            self._dbg.info(f"SC track search failed: {str(ex)[:120]}")
        try:
            web_hits = self._sc_web_search(name)
        except Exception as ex:
            self._dbg.info(f"SC web search failed: {str(ex)[:120]}")
        return merge_soundcloud_candidates(track_hits, web_hits, max_results)

    def _watchlist_soundcloud_link_dialog(self, cid, on_done=None):
        """Themed Fix Link for SoundCloud — the platform analogue of the YouTube
        resolve dialog. SoundCloud has no artist-search API, so this searches
        SoundCloud tracks AND the web (invisibly) for the display name,
        cross-references the two to surface likely artist profiles, and lets the
        user pick one — or paste the profile URL manually as a fallback.
        Deliberately separate from the YouTube resolve path.
        on_done(resolved: bool) fires when the dialog closes."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            if on_done:
                on_done(False)
            return

        dlg = tk.Toplevel(self)
        dlg.title("Fix Link — SoundCloud")
        dlg.geometry("600x560")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - 600) // 2
        py = self.winfo_y() + (self.winfo_height() - 560) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")

        outer = tk.Frame(dlg, bg=BG, padx=24, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Find the right SoundCloud profile",
                 font=("Segoe UI", 14, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w")
        tk.Label(outer,
                 text=f"Matching the folder “{ch['display_name']}”. Pick the "
                      f"correct artist below, or paste the profile URL.",
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=BG, wraplength=540,
                 justify="left").pack(anchor="w", pady=(2, 12))

        status_lbl = tk.Label(outer, text="🔍  Searching SoundCloud…",
                              font=("Segoe UI", 10), fg=SC_ORANGE, bg=BG)
        status_lbl.pack(anchor="w", pady=(0, 8))

        results_frame = tk.Frame(outer, bg=BG)
        results_frame.pack(fill="both", expand=True)

        choice_var = tk.StringVar(value="")
        manual_var = tk.StringVar()
        cand_by_url = {}     # profile url -> candidate dict
        # Paging for the "show more matches" toggle: fetch up to 8, reveal four
        # at a time.
        paging = {"all": [], "page": 0, "size": 4}

        def _close(resolved):
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass
            self._watchlist_refresh()
            if on_done:
                on_done(resolved)

        def _render(candidates):
            for w in results_frame.winfo_children():
                w.destroy()
            if candidates:
                choice_var.set(candidates[0]["url"])
                for c in candidates:
                    cand_by_url[c["url"]] = c
                    conf = c.get("confidence")
                    badge = ("✓ audio + web match" if conf == "both"
                             else "audio match" if conf == "tracks"
                             else "web match")
                    label = c.get("title") or c["handle"]
                    row = tk.Frame(results_frame, bg=SURFACE, padx=10, pady=6,
                                   highlightthickness=1,
                                   highlightbackground=BORDER)
                    row.pack(fill="x", pady=(0, 5))
                    tk.Radiobutton(
                        row, text=label, value=c["url"],
                        variable=choice_var, bg=SURFACE, fg=TEXT,
                        selectcolor=SURFACE, activebackground=SURFACE,
                        activeforeground=TEXT, font=("Segoe UI", 10, "bold"),
                        anchor="w", highlightthickness=0, bd=0
                    ).pack(anchor="w", fill="x")
                    info_row = tk.Frame(row, bg=SURFACE)
                    info_row.pack(anchor="w", fill="x")
                    badge_fg = SUCCESS if conf == "both" else TEXT_DIM
                    tk.Label(info_row, text=f"    {badge}  •  ",
                             font=("Segoe UI", 8), fg=badge_fg, bg=SURFACE,
                             anchor="w").pack(side="left")
                    link = tk.Label(
                        info_row, text=c["url"],
                        font=("Segoe UI", 8, "underline"),
                        fg=LINK_COL, bg=SURFACE, cursor="hand2", anchor="w")
                    link.pack(side="left")
                    link.bind("<Button-1>",
                              lambda e, u=c["url"]: webbrowser.open(u))
            else:
                status_lbl.config(
                    text="No close matches found — paste the profile URL "
                         "manually.", fg=YT_RED)

            # Always offer a manual-paste option.
            man_row = tk.Frame(results_frame, bg=SURFACE, padx=10, pady=6,
                               highlightthickness=1, highlightbackground=BORDER)
            man_row.pack(fill="x", pady=(6, 0))
            tk.Radiobutton(
                man_row, text="Paste a soundcloud.com profile URL manually:",
                value="__manual__", variable=choice_var,
                bg=SURFACE, fg=TEXT, selectcolor=SURFACE,
                activebackground=SURFACE, activeforeground=TEXT,
                font=("Segoe UI", 10), anchor="w", highlightthickness=0, bd=0
            ).pack(anchor="w")
            tk.Entry(man_row, textvariable=manual_var, font=("Segoe UI", 9),
                     bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER
                     ).pack(fill="x", ipady=3, pady=(4, 0))
            if not candidates:
                choice_var.set("__manual__")

        def _show_page():
            allc = paging["all"]
            size = paging["size"]
            page = paging["page"]
            start = page * size
            page_slice = allc[start:start + size]
            _render(page_slice)
            total = len(allc)
            if page_slice:
                status_lbl.config(
                    text=f"Found {total} match(es) — showing "
                         f"{start + 1}–{start + len(page_slice)} of {total}. "
                         f"Choose one:", fg=TEXT)
            if total > size:
                more_btn.config(
                    text=("  ↩ Back to first matches  " if page
                          else "  Show 4 more matches  "))
                if not more_btn.winfo_ismapped():
                    more_btn.pack(side="left", padx=(8, 0), before=skip_btn)
            else:
                more_btn.pack_forget()

        def _toggle_more():
            paging["page"] = 0 if paging["page"] else 1
            _show_page()

        def _search():
            try:
                cands = self._resolve_soundcloud_via_search(
                    ch["display_name"], max_results=8)
                paging["all"] = cands
                paging["page"] = 0
                dlg.after(0, _show_page)
            except Exception as ex:
                msg = str(ex)[:120]
                dlg.after(0, lambda: (status_lbl.config(
                    text=f"Search failed: {msg}", fg=YT_RED), _render([])))

        self._run_bg(_search)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x", pady=(12, 0))

        def _confirm():
            sel = choice_var.get()
            if not sel:
                return
            if sel == "__manual__":
                raw = manual_var.get().strip()
                if not raw:
                    status_lbl.config(
                        text="Enter a URL or pick a match.", fg=YT_RED)
                    return
                if "soundcloud.com" not in raw.lower():
                    status_lbl.config(
                        text="That doesn't look like a soundcloud.com URL.",
                        fg=YT_RED)
                    return
                self._watchlist_apply_url(cid, raw)
                _close(True)
                return
            # A search candidate was chosen — sel is its profile URL.
            self._watchlist_apply_url(cid, sel)
            _close(True)

        # Confirm button — themed like the main page's action buttons.
        tk.Button(btn_row, text="  ✓ Use This Profile  ",
                  font=("Segoe UI", 10, "bold"), bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_confirm).pack(side="left")
        more_btn = tk.Button(
                  btn_row, text="  Show 4 more matches  ",
                  font=("Segoe UI", 10), bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_toggle_more)
        skip_btn = tk.Button(
                  btn_row, text="  Skip  ",
                  font=("Segoe UI", 10), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=lambda: _close(False))
        skip_btn.pack(side="left", padx=(8, 0))

        def _cancel_all():
            # Stop the whole Fix-Channels pass (this channel + all remaining).
            self._wl_fix_abort = True
            _close(False)

        tk.Button(btn_row, text="  ✕ Cancel  ",
                  font=("Segoe UI", 10), bg=YT_RED, fg=TEXT,
                  activebackground=YT_RED, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_cancel_all).pack(side="left", padx=(8, 0))

        dlg.bind("<Escape>", lambda _e: _close(False))

    def _watchlist_resolve_dialog(self, cid, on_done=None):
        """Show the top-4 YouTube matches for a channel so the user can pick
        the right one (or paste a URL manually). on_done(resolved: bool) is
        called when the dialog closes — used to chain the batch 'Fix' flow."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            if on_done:
                on_done(False)
            return

        if (ch.get("platform") or "YouTube") == "SoundCloud":
            # SoundCloud has no channel-id search; just collect the URL.
            return self._watchlist_soundcloud_link_dialog(cid, on_done)

        dlg = tk.Toplevel(self)
        dlg.title("Fix Channel")
        dlg.geometry("600x520")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - 600) // 2
        py = self.winfo_y() + (self.winfo_height() - 520) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")

        outer = tk.Frame(dlg, bg=BG, padx=24, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Find the right YouTube channel",
                 font=("Segoe UI", 14, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w")
        tk.Label(outer,
                 text=f"Matching the folder “{ch['display_name']}”. "
                      f"Pick the correct channel below.",
                 font=("Segoe UI", 9), fg=TEXT_DIM, bg=BG, wraplength=540,
                 justify="left").pack(anchor="w", pady=(2, 12))

        status_lbl = tk.Label(outer, text="🔍  Searching YouTube…",
                              font=("Segoe UI", 10), fg=WL_BLUE, bg=BG)
        status_lbl.pack(anchor="w", pady=(0, 8))

        results_frame = tk.Frame(outer, bg=BG)
        results_frame.pack(fill="both", expand=True)

        choice_var = tk.StringVar(value="")
        manual_var = tk.StringVar()
        cand_by_id = {}   # channel_id -> candidate dict (for handle lookup)
        # Paging for the "show more matches" toggle: we fetch up to 8 candidates
        # and reveal them four at a time.
        paging = {"all": [], "page": 0, "size": 4}

        # Outcome bookkeeping so the chained batch flow knows what happened.
        state = {"resolved": False, "cancel_all": False}

        def _close(resolved):
            state["resolved"] = resolved
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass
            self._watchlist_refresh()
            if on_done:
                on_done(resolved)

        def _render(candidates):
            for w in results_frame.winfo_children():
                w.destroy()
            if candidates:
                status_lbl.config(
                    text=f"Found {len(candidates)} match(es) — choose one:",
                    fg=TEXT)
                choice_var.set(candidates[0]["channel_id"])
                for c in candidates:
                    cand_by_id[c["channel_id"]] = c
                    subs = (f"{c['followers']:,} subscribers"
                            if c.get("followers") else "subscriber count n/a")
                    label = f"{c['title']}"
                    if c.get("handle"):
                        label += f"   ({c['handle']})"
                    row = tk.Frame(results_frame, bg=SURFACE, padx=10, pady=6,
                                   highlightthickness=1,
                                   highlightbackground=BORDER)
                    row.pack(fill="x", pady=(0, 5))
                    tk.Radiobutton(
                        row, text=label, value=c["channel_id"],
                        variable=choice_var, bg=SURFACE, fg=TEXT,
                        selectcolor=SURFACE, activebackground=SURFACE,
                        activeforeground=TEXT, font=("Segoe UI", 10, "bold"),
                        anchor="w", highlightthickness=0, bd=0
                    ).pack(anchor="w", fill="x")
                    info_row = tk.Frame(row, bg=SURFACE)
                    info_row.pack(anchor="w", fill="x")
                    tk.Label(info_row, text=f"    {subs}  •  ",
                             font=("Segoe UI", 8), fg=TEXT_DIM, bg=SURFACE,
                             anchor="w").pack(side="left")
                    # Clickable URL — open in the browser to double-check the
                    # channel before committing.
                    link = tk.Label(
                        info_row, text=c["url"],
                        font=("Segoe UI", 8, "underline"),
                        fg=LINK_COL, bg=SURFACE, cursor="hand2", anchor="w")
                    link.pack(side="left")
                    link.bind("<Button-1>",
                              lambda e, u=c["url"]: webbrowser.open(u))
            else:
                status_lbl.config(
                    text="No channel matches found — paste the URL manually.",
                    fg=YT_RED)

            # Always offer a manual-paste option.
            man_row = tk.Frame(results_frame, bg=SURFACE, padx=10, pady=6,
                               highlightthickness=1, highlightbackground=BORDER)
            man_row.pack(fill="x", pady=(6, 0))
            tk.Radiobutton(
                man_row, text="Paste a channel URL manually:",
                value="__manual__", variable=choice_var,
                bg=SURFACE, fg=TEXT, selectcolor=SURFACE,
                activebackground=SURFACE, activeforeground=TEXT,
                font=("Segoe UI", 10), anchor="w", highlightthickness=0, bd=0
            ).pack(anchor="w")
            tk.Entry(man_row, textvariable=manual_var, font=("Segoe UI", 9),
                     bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER
                     ).pack(fill="x", ipady=3, pady=(4, 0))
            if not candidates:
                choice_var.set("__manual__")

        def _show_page():
            """Render the current page of four and update the toggle button."""
            allc = paging["all"]
            size = paging["size"]
            page = paging["page"]
            start = page * size
            page_slice = allc[start:start + size]
            _render(page_slice)
            total = len(allc)
            if page_slice:
                status_lbl.config(
                    text=f"Found {total} match(es) — showing "
                         f"{start + 1}–{start + len(page_slice)} of {total}. "
                         f"Choose one:", fg=TEXT)
            # The toggle only matters when a second page of results exists.
            if total > size:
                more_btn.config(
                    text=("  ↩ Back to first matches  " if page
                          else "  Show 4 more matches  "))
                if not more_btn.winfo_ismapped():
                    more_btn.pack(side="left", padx=(8, 0), before=skip_btn)
            else:
                more_btn.pack_forget()

        def _toggle_more():
            # Two pages of four — flip between them.
            paging["page"] = 0 if paging["page"] else 1
            _show_page()

        def _search():
            try:
                cands = self._resolve_channel_via_search(
                    ch["display_name"], max_results=8)
                paging["all"] = cands
                paging["page"] = 0
                dlg.after(0, _show_page)
            except Exception as ex:
                msg = str(ex)[:120]
                dlg.after(0, lambda: (status_lbl.config(
                    text=f"Search failed: {msg}", fg=YT_RED), _render([])))

        self._run_bg(_search)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x", pady=(12, 0))

        def _confirm():
            sel = choice_var.get()
            if not sel:
                return
            if sel == "__manual__":
                raw = manual_var.get().strip()
                if not raw:
                    status_lbl.config(text="Enter a URL or pick a match.",
                                      fg=YT_RED)
                    return
                cid_direct = self._channel_id_from_url(raw)
                if cid_direct:
                    self._finish_resolve(
                        ch, cid_direct,
                        success_msg=f"Resolved (manual): {ch['display_name']}",
                        close_fn=_close)
                    return
                # Need to look the URL up to get its channel_id.
                status_lbl.config(text="Resolving pasted URL…", fg=WL_BLUE)

                def _lookup():
                    try:
                        import yt_dlp
                        opts = {"quiet": True, "no_warnings": True,
                                "extract_flat": "in_playlist",
                                "skip_download": True, "playlist_items": "0"}
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(raw, download=False)
                        cid2 = info.get("channel_id") or \
                            self._channel_id_from_url(info.get("channel_url", ""))
                        handle = info.get("uploader_id") or ""
                        if cid2:
                            dlg.after(0, lambda: self._finish_resolve(
                                ch, cid2, handle,
                                success_msg=f"Resolved (manual): "
                                            f"{ch['display_name']}",
                                close_fn=_close))
                        else:
                            dlg.after(0, lambda: status_lbl.config(
                                text="Couldn't read a channel_id from that URL.",
                                fg=YT_RED))
                    except Exception as ex:
                        m = str(ex)[:120]
                        dlg.after(0, lambda: status_lbl.config(
                            text=f"Lookup failed: {m}", fg=YT_RED))

                self._run_bg(_lookup)
                return
            # A search candidate was chosen.
            handle = (cand_by_id.get(sel) or {}).get("handle", "")
            self._finish_resolve(
                ch, sel, handle,
                success_msg=f"Resolved: {ch['display_name']}", close_fn=_close)

        # Confirm button — themed like the main page's action buttons
        # (flat dark surface + light-blue text), distinct from the red Cancel.
        tk.Button(btn_row, text="  ✓ Use This Channel  ",
                  font=("Segoe UI", 10, "bold"), bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_confirm).pack(side="left")
        # 'Show 4 more matches' toggle — packed by _show_page only when a second
        # page of results exists; flips to 'Back to first matches' on page 2.
        more_btn = tk.Button(
                  btn_row, text="  Show 4 more matches  ",
                  font=("Segoe UI", 10), bg=SURFACE2, fg=LINK_COL,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_toggle_more)
        skip_btn = tk.Button(
                  btn_row, text="  Skip  ",
                  font=("Segoe UI", 10), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=lambda: _close(False))
        skip_btn.pack(side="left", padx=(8, 0))

        def _cancel_all():
            # Stop the whole Fix-Channels pass (this channel + all remaining).
            self._wl_fix_abort = True
            _close(False)

        tk.Button(btn_row, text="  ✕ Cancel  ",
                  font=("Segoe UI", 10), bg=YT_RED, fg=TEXT,
                  activebackground=YT_RED, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_cancel_all).pack(side="left", padx=(8, 0))

    def _watchlist_fix_broken(self):
        """Walk every unresolved channel through the resolve dialog, one at a
        time, so the user can heal all legacy folders in one pass."""
        broken = [c for c in self._db.get_all_watchlist_channels()
                  if self._is_unresolved_channel(c)]
        if not broken:
            messagebox.showinfo(
                "Nothing to fix",
                "All Watch List channels already have a valid YouTube ID.",
                parent=self)
            return
        self._watchlist_log(
            f"Fixing {len(broken)} channel(s) needing resolution…", "info")

        order = [c["id"] for c in broken]
        self._wl_fix_abort = False   # reset; Cancel in the dialog sets this True

        def _next(i):
            if self._wl_fix_abort:
                self._watchlist_log("Channel fix pass cancelled.", "info")
                self._watchlist_refresh()
                return
            if i >= len(order):
                self._watchlist_log("Channel fix pass complete.", "ok")
                self._watchlist_refresh()
                return
            self._watchlist_resolve_dialog(order[i], on_done=lambda _r: _next(i + 1))

        _next(0)

    # ── Add Channel dialog ────────────────────────────────────────────────────
    def _watchlist_open_add_dialog(self):
        """Open the dialog to add a new channel to the Watch List."""
        dlg = tk.Toplevel(self)
        dlg.title("Add Channel to Watch List")
        dlg.geometry("540x500")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        # Centre over parent
        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - 540) // 2
        py = self.winfo_y() + (self.winfo_height() - 500) // 2
        dlg.geometry(f"+{max(0,px)}+{max(0,py)}")

        outer = tk.Frame(dlg, bg=BG, padx=24, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Add Channel to Watch List",
                 font=("Segoe UI", 14, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 16))

        # URL
        tk.Label(outer, text="Channel URL",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        url_var = tk.StringVar()
        url_entry = tk.Entry(
            outer, textvariable=url_var,
            font=("Segoe UI", 10), bg=SURFACE, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER)
        url_entry.pack(fill="x", ipady=5, pady=(0, 10))
        tk.Label(outer,
                 text="Paste a YouTube channel URL (e.g. https://youtube.com/@ChannelName)",
                 font=("Segoe UI", 8), fg=TEXT_DIM, bg=BG
                 ).pack(anchor="w", pady=(0, 8))

        # Display name
        tk.Label(outer, text="Display Name",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        name_var = tk.StringVar()
        name_entry = tk.Entry(
            outer, textvariable=name_var,
            font=("Segoe UI", 10), bg=SURFACE, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER)
        name_entry.pack(fill="x", ipady=5, pady=(0, 4))

        # Auto-fetch name button
        def _fetch_name():
            raw_url = url_var.get().strip()
            if not raw_url:
                return
            name_var.set("Fetching…")
            def _do():
                try:
                    import yt_dlp
                    opts = {"quiet": True, "no_warnings": True,
                            "extract_flat": "in_playlist", "skip_download": True,
                            "playlist_items": "0"}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(raw_url, download=False)
                    title = derive_collection_name(info)
                    dlg.after(0, lambda: name_var.set(title or raw_url))
                except Exception:
                    dlg.after(0, lambda: name_var.set(""))
            self._run_bg(_do)

        fetch_btn = tk.Button(
            outer, text="Auto-fetch name",
            font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
            command=_fetch_name)
        fetch_btn.pack(anchor="w", pady=(0, 10))

        # Genre
        tk.Label(outer, text="Genre",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        genres = self._scan_genres()
        genre_var = tk.StringVar(value=genres[0] if genres else "(none)")
        genre_combo = ttk.Combobox(outer, textvariable=genre_var,
                                    values=genres, state="readonly")
        genre_combo.pack(fill="x", pady=(0, 10))

        # Download since
        tk.Label(outer, text="Download uploads since",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        since_var = tk.StringVar(value=SINCE_DATE_OPTIONS[0])
        since_combo = ttk.Combobox(outer, textvariable=since_var,
                                    values=SINCE_DATE_OPTIONS, state="readonly")
        since_combo.pack(fill="x", pady=(0, 16))
        Tooltip(since_combo,
                "Choose how far back to look for uploads. 'Today' means "
                "only future uploads will be detected.")

        # Buttons
        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x")

        def _save():
            raw_url = url_var.get().strip()
            if not raw_url:
                messagebox.showwarning("No URL", "Please enter a channel URL.",
                                       parent=dlg)
                return
            name = name_var.get().strip() or raw_url
            genre = genre_var.get()
            since = since_var.get()

            # Resolve the cutoff date from the "since" selection
            if since.startswith("Today"):
                cutoff = today_yyyymmdd()
            elif "30 days" in since:
                cutoff = days_ago_yyyymmdd(30)
            elif "90 days" in since:
                cutoff = days_ago_yyyymmdd(90)
            elif "6 months" in since:
                cutoff = days_ago_yyyymmdd(180)
            elif "1 year" in since:
                cutoff = days_ago_yyyymmdd(365)
            elif since.startswith("Custom"):
                custom = simpledialog.askstring(
                    "Custom Date",
                    "Enter date as YYYY-MM-DD:",
                    parent=dlg)
                if not custom:
                    return
                try:
                    dt = datetime.strptime(custom.strip(), "%Y-%m-%d").date()
                    cutoff = dt.strftime("%Y%m%d")
                except ValueError:
                    messagebox.showerror("Bad Date",
                                         "Please use YYYY-MM-DD format.",
                                         parent=dlg)
                    return
            elif since.startswith("Scan my"):
                # Scan music folder for newest mp3
                plat = self._detect_platform(raw_url)
                folder = os.path.join(self._base_dir, plat, genre or "_No Genre", name)
                count, newest = scan_folder_newest_mp3(folder)
                if newest:
                    cutoff = subtract_days_from_yyyymmdd(
                        newest, WATCHLIST_CUTOFF_BUFFER_DAYS)
                else:
                    cutoff = today_yyyymmdd()
            else:
                cutoff = today_yyyymmdd()

            result = self._db.add_watchlist_channel(
                url=raw_url, display_name=name,
                platform=self._detect_platform(raw_url), genre=genre,
                scan_cutoff_date=cutoff, auto_added=False)
            if result is None:
                messagebox.showinfo(
                    "Already Exists",
                    f"'{name}' is already in the Watch List.",
                    parent=dlg)
            else:
                self._watchlist_log(f"Added: {name}", "ok")
            dlg.destroy()
            self._watchlist_refresh()

        tk.Button(btn_row, text="  Save  ",
                  font=("Segoe UI", 10, "bold"),
                  bg=WL_BLUE_DARK, fg=TEXT,
                  activebackground=WL_BLUE, activeforeground=TEXT,
                  relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
                  command=_save).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="  Cancel  ",
                  font=("Segoe UI", 10),
                  bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
                  command=dlg.destroy).pack(side="left")

    # ── Edit Channel dialog ───────────────────────────────────────────────────
    def _watchlist_edit_channel(self, cid):
        """Open the dialog to edit an existing Watch List channel."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return

        dlg = tk.Toplevel(self)
        dlg.title(f"Edit — {ch['display_name']}")
        dlg.geometry("460x470")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - 460) // 2
        py = self.winfo_y() + (self.winfo_height() - 470) // 2
        dlg.geometry(f"+{max(0,px)}+{max(0,py)}")

        outer = tk.Frame(dlg, bg=BG, padx=24, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text=f"Edit: {ch['display_name']}",
                 font=("Segoe UI", 13, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 16))

        # Channel / Playlist URL
        tk.Label(outer, text="Channel / Playlist URL",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        # Don't surface the internal unresolved:// sentinel as an editable URL.
        existing_url = ch.get("url") or ""
        if existing_url.startswith(UNRESOLVED_URL_PREFIX):
            existing_url = ""
        url_var = tk.StringVar(value=existing_url)
        tk.Entry(outer, textvariable=url_var,
                 font=("Segoe UI", 10), bg=SURFACE, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER
                 ).pack(fill="x", ipady=5, pady=(0, 4))
        tk.Label(outer,
                 text="Paste the channel (…/@handle or …/channel/UC…) or a "
                      "playlist URL. Leave as-is to keep the current channel.",
                 font=("Segoe UI", 8), fg=TEXT_DIM, bg=BG, wraplength=400,
                 justify="left").pack(anchor="w", pady=(0, 6))

        # Open / Edit the link directly from here.
        def _open_link():
            u = url_var.get().strip()
            if u and not u.startswith(UNRESOLVED_URL_PREFIX):
                webbrowser.open(u)
            else:
                messagebox.showinfo(
                    "No Link", "This channel has no link to open yet — use "
                    "Edit Link to find it.", parent=dlg)

        def _edit_link():
            # Hand off to the Fix Link window (closes this edit dialog first to
            # avoid two modal grabs fighting over focus).
            dlg.destroy()
            self._watchlist_resolve_dialog(cid)

        def _open_folder():
            # Compute the folder purely (no creation) and open it if present.
            path = self._channel_save_path(
                ch.get("genre"), ch.get("display_name"),
                platform=ch.get("platform"))
            if path and os.path.isdir(path):
                try:
                    os.startfile(path)
                except Exception as e:
                    messagebox.showerror(
                        "Open Folder Failed",
                        f"Couldn't open the folder:\n\n{e}", parent=dlg)
            else:
                messagebox.showinfo(
                    "No Folder Yet",
                    "No download folder exists for this channel yet.\n"
                    "It's created automatically on the first download.",
                    parent=dlg)

        link_btn_row = tk.Frame(outer, bg=BG)
        link_btn_row.pack(fill="x", pady=(0, 12))
        tk.Button(link_btn_row, text="  🌐 Open Link  ",
                  font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                  command=_open_link).pack(side="left", padx=(0, 8))
        tk.Button(link_btn_row, text="  🛠 Smart-Edit Link  ",
                  font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                  command=_edit_link).pack(side="left", padx=(0, 8))
        tk.Button(link_btn_row, text="  📂 Open Folder  ",
                  font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                  command=_open_folder).pack(side="left")

        # Genre
        tk.Label(outer, text="Genre",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        genres = self._scan_genres()
        genre_var = tk.StringVar(value=ch.get("genre") or "(none)")
        ttk.Combobox(outer, textvariable=genre_var,
                     values=genres, state="readonly"
                     ).pack(fill="x", pady=(0, 12))

        # Cutoff date
        tk.Label(outer, text="Scan cutoff date (YYYY-MM-DD)",
                 font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG
                 ).pack(anchor="w", pady=(0, 4))
        current_cutoff = ch.get("scan_cutoff_date", "")
        try:
            display_date = datetime.strptime(current_cutoff, "%Y%m%d").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            display_date = current_cutoff
        cutoff_var = tk.StringVar(value=display_date)
        tk.Entry(outer, textvariable=cutoff_var,
                 font=("Segoe UI", 10), bg=SURFACE, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER
                 ).pack(fill="x", ipady=5, pady=(0, 16))

        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(fill="x")

        def _save():
            new_genre = genre_var.get()
            raw_date = cutoff_var.get().strip()
            try:
                dt = datetime.strptime(raw_date, "%Y-%m-%d").date()
                new_cutoff = dt.strftime("%Y%m%d")
            except ValueError:
                messagebox.showerror("Bad Date", "Use YYYY-MM-DD format.",
                                     parent=dlg)
                return
            self._db.update_watchlist_channel_fields(
                cid, genre=new_genre, scan_cutoff_date=new_cutoff)
            # If the URL changed, re-point (and re-resolve) the channel.
            new_url = url_var.get().strip()
            if new_url and new_url != (ch.get("url") or ""):
                self._watchlist_apply_url(cid, new_url)
            dlg.destroy()
            self._watchlist_refresh()

        tk.Button(btn_row, text="  Save  ",
                  font=("Segoe UI", 10, "bold"),
                  bg=WL_BLUE_DARK, fg=TEXT,
                  activebackground=WL_BLUE, activeforeground=TEXT,
                  relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
                  command=_save).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="  Cancel  ",
                  font=("Segoe UI", 10),
                  bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
                  command=dlg.destroy).pack(side="left")

    # ── Remove channel ────────────────────────────────────────────────────────
    def _watchlist_remove_channel(self, cid):
        """Confirm and remove a channel from the Watch List."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return
        ok = messagebox.askyesno(
            "Remove Channel",
            f"Remove '{ch['display_name']}' from the Watch List?\n\n"
            f"This does not delete any downloaded files.",
            parent=self)
        if ok:
            self._db.remove_watchlist_channel(cid)
            self._watchlist_log(f"Removed: {ch['display_name']}", "info")
            self._watchlist_refresh()

    # ── Scan one channel ──────────────────────────────────────────────────────
    def _watchlist_scan_channel(self, cid):
        """Threaded scan: use yt-dlp flat extraction with dateafter to find
        new uploads since the channel's scan_cutoff_date."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return

        # Guard: never hand yt-dlp a folder-name URL (a space or our
        # unresolved:// sentinel). That is precisely the bug that produced the
        # HTTP 404s. Route to resolution instead so the scan only ever runs on
        # a canonical /channel/UC… URL.
        if self._is_unresolved_channel(ch):
            self._db.update_watchlist_status(cid, "needs_resolve")
            if (ch.get("platform") or "YouTube") == "SoundCloud":
                need = ("needs its SoundCloud profile URL first — click "
                        "Fix Link and paste the soundcloud.com link.")
            else:
                need = ("needs its YouTube channel resolved first — click "
                        "Fix Link (or “Fix Channels”).")
            self._watchlist_log(f"“{ch['display_name']}” {need}", "err")
            self._watchlist_update_card(cid)
            return

        # If nothing else is in flight, this is a fresh user-initiated scan
        # — reset any leftover cancel flag from a prior aborted run.
        if not self._downloading and self._wl_scan_active == 0:
            self._cancel_flag.clear()

        if self._cancel_flag.is_set():
            return

        # Fresh scan for this channel — drop any stale per-card cancel request.
        self._wl_cancel_cids.discard(cid)

        self._db.update_watchlist_status(cid, "scanning")
        self._watchlist_update_card(cid)
        self._watchlist_log(f"Scanning: {ch['display_name']}…", "info")

        self._wl_scan_active += 1
        self.after(0, self._wl_update_cancel_btn_state)

        def _do_scan():
            try:
                if self._cancel_flag.is_set() or cid in self._wl_cancel_cids:
                    self._wl_cancel_cids.discard(cid)
                    self._db.update_watchlist_status(cid, "idle")
                    self.after(0, lambda: self._watchlist_log(
                        f"Scan cancelled: {ch['display_name']}", "info"))
                    return

                import yt_dlp

                # Apply the buffer: scan a few days before the cutoff
                # so approximate_date imprecision doesn't miss anything
                raw_cutoff = ch["scan_cutoff_date"]
                buffered = subtract_days_from_yyyymmdd(
                    raw_cutoff, WATCHLIST_CUTOFF_BUFFER_DAYS)

                # We decide "new" by whether the track is already on disk in
                # the channel folder (see below) — NOT by upload date, which
                # is unreliable in flat channel listings. So we enumerate the
                # full channel and cross-reference, rather than date-filtering.
                scan_opts = {
                    "extract_flat":   "in_playlist",
                    "skip_download":  True,
                    "lazy_playlist":  True,
                    "quiet":          True,
                    "no_warnings":    True,
                }

                # Use cookies if configured
                self._apply_cookie_opts(scan_opts)

                platform = ch.get("platform") or "YouTube"
                # Same encoded listing URL a Watch List "Download New" feeds
                # yt-dlp, so scan and download crawl the channel identically.
                url = watch_fetch_url(platform, ch["url"])

                self._dbg.info(
                    f"WL SCAN | {ch['display_name']}  url={url}  "
                    f"cutoff={raw_cutoff}  buffered={buffered}")

                with yt_dlp.YoutubeDL(scan_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

                if self._cancel_flag.is_set() or cid in self._wl_cancel_cids:
                    self._wl_cancel_cids.discard(cid)
                    self._db.update_watchlist_status(cid, "idle")
                    self.after(0, lambda: self._watchlist_log(
                        f"Scan cancelled: {ch['display_name']}", "info"))
                    return

                entries = list(info.get("entries") or [])

                # Build a lookup of tracks already on disk in this channel's
                # folder. Legacy downloads predate the database and have NO
                # video_id recorded anywhere, so their .mp3 filename (which is
                # the sanitised video title) is the only evidence we have that
                # the track is already owned.
                folder_keys = {}
                try:
                    folder = self._resolve_save_dir(
                        ch.get("genre") or "(none)", ch.get("display_name"),
                        platform=platform)
                    for fn in os.listdir(folder):
                        if fn.lower().endswith(".mp3"):
                            k = normalize_track_key(fn)
                            if k:
                                folder_keys.setdefault(
                                    k, os.path.join(folder, fn))
                except OSError:
                    pass

                # Cross-reference every channel video against (1) the DB by
                # video_id (exact), then (2) the folder by normalised title.
                # Already-owned tracks are hidden; their video_id is backfilled
                # into the DB so the next scan is instant and exact.
                # When the Time Limiter is on, also skip videos longer than the
                # limit so the "+ N new" badge matches what would actually
                # download. Entries with no duration (live, premiere, missing)
                # are kept — the download step filters them again as a backstop.
                now_ts = int(time.time())
                limit_on  = bool(self._limit_enabled.get())
                limit_sec = self._limit_minutes.get() * 60 if limit_on else None
                classified = classify_scan_entries(
                    entries,
                    is_downloaded=self._db.is_video_downloaded,
                    folder_keys=folder_keys,
                    limit_sec=limit_sec,
                    platform=platform)
                new_entries = classified["new"]
                # Backfill already-on-disk (legacy) tracks so future scans dedup
                # exactly by video_id; entries without an id can't be keyed, so
                # they are simply hidden (matching the original `if vid_id`).
                backfill_rows = [
                    {
                        "video_id":     od["id"],
                        "title":        od["title"],
                        "channel_name": ch.get("display_name") or "",
                        "channel_url":  ch.get("url") or "",
                        "channel_id":   ch.get("channel_id"),
                        "platform":     platform,
                        "genre":        ch.get("genre") or "(none)",
                        "file_path":    od["file_path"],
                        "upload_date":  od["upload_date"],
                        "ts":           now_ts,
                        "bitrate":      "",
                    }
                    for od in classified["on_disk"] if od["id"]
                ]

                if backfill_rows:
                    n_bf = self._db.backfill_downloads(backfill_rows)
                    self._dbg.info(
                        f"WL SCAN BACKFILL | {ch['display_name']}  "
                        f"recorded {n_bf} already-downloaded track(s) "
                        f"from folder")

                count = len(new_entries)
                status = "found" if count > 0 else "idle"
                self._db.update_watchlist_scan_result(
                    cid,
                    timestamp=int(time.time()),
                    pending_count=count,
                    pending_entries=new_entries,
                    status=status)

                tag = "ok" if count > 0 else "info"
                self.after(0, lambda: self._watchlist_log(
                    f"{ch['display_name']}: {count} new track{'s' if count != 1 else ''} found",
                    tag))

            except Exception as exc:
                err = str(exc)[:120]
                self._dbg.error(
                    f"WL SCAN FAIL | {ch['display_name']}  error: {err}")
                self._db.update_watchlist_scan_result(
                    cid,
                    timestamp=int(time.time()),
                    pending_count=0,
                    pending_entries=[],
                    status="error",
                    last_error=err)
                self.after(0, lambda: self._watchlist_log(
                    f"Error scanning {ch['display_name']}: {err}", "err"))
            finally:
                self._wl_cancel_cids.discard(cid)
                self._wl_scan_active = max(0, self._wl_scan_active - 1)
                self.after(0, self._wl_update_cancel_btn_state)
                # Re-render just THIS card from the final DB status on EVERY
                # exit — including the cancel-return paths above. Previously this
                # ran after the try/finally, so a cancelled scan returned early
                # and left its card stuck showing "scanning" even though the DB
                # row was already reset to "idle". Per-card (not a full refresh)
                # so a parallel Scan All doesn't blank every other card.
                self.after(0, lambda c=cid: self._watchlist_update_card(c))

        self._run_bg(_do_scan)

    # ── Scan all channels ─────────────────────────────────────────────────────
    def _watchlist_scan_all(self):
        """Scan every watched channel for new uploads (sequentially)."""
        channels = self._db.get_all_watchlist_channels()
        if not channels:
            self._watchlist_log("No channels to scan.", "info")
            return
        self._cancel_flag.clear()
        self._watchlist_log(
            f"Scanning {len(channels)} channel{'s' if len(channels) != 1 else ''}…",
            "info")
        self._wl_update_cancel_btn_state()

        def _do_all():
            def _wait(deadline):
                """Sleep until *deadline*, waking early on cancel. Returns
                True if cancelled."""
                while time.time() < deadline:
                    if self._cancel_flag.is_set():
                        return True
                    time.sleep(0.25)
                return self._cancel_flag.is_set()

            for i, ch in enumerate(channels):
                if self._cancel_flag.is_set():
                    self.after(0, lambda: self._watchlist_log(
                        "Scan All cancelled.", "info"))
                    break
                # Throttle: never let more than WATCHLIST_MAX_CONCURRENT_SCANS
                # scans run at once. Wait for an in-flight scan to finish before
                # starting the next, so a big watch list can't fire dozens of
                # simultaneous yt-dlp requests and time out.
                while self._wl_scan_active >= WATCHLIST_MAX_CONCURRENT_SCANS:
                    if self._cancel_flag.is_set():
                        break
                    time.sleep(0.25)
                if self._cancel_flag.is_set():
                    self.after(0, lambda: self._watchlist_log(
                        "Scan All cancelled.", "info"))
                    break
                # Use after(0,...) to call scan on main thread context
                # but the actual scan is threaded inside _watchlist_scan_channel
                self.after(0, lambda c=ch["id"]: self._watchlist_scan_channel(c))
                # Brief jittered pause: lets the scheduled scan register as
                # active (so the cap is counted accurately on the next loop)
                # and avoids firing requests in a tight burst.
                if _wait(time.time() + 1.0 + random.uniform(0.5, 1.5)):
                    self.after(0, lambda: self._watchlist_log(
                        "Scan All cancelled.", "info"))
                    break
        self._run_bg(_do_all)

    # ── Force a normal full download for one channel ──────────────────────────
    def _watchlist_force_download(self, cid):
        """Force a normal full download of a channel, exactly as if the user
        pasted its URL into the Main tab and pressed "Download MP3's". Used when
        a scan finds nothing (cutoff / bad yt-dlp scan data) but the user still
        wants to pull the channel down. Runs through the standard Main-tab path
        (NOT a watchlist=True session) so the post-download auto-add/dedup in
        _watchlist_auto_add_if_enabled attaches the run to this card."""
        if self._downloading:
            messagebox.showinfo(
                "Download in Progress",
                "A download is already running. Wait for it to finish first.",
                parent=self)
            return

        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return

        # Never force-download an unresolved card — its link is a folder-name
        # placeholder, not a real URL. Route the user to Fix Link first.
        if is_unresolved_channel(ch):
            messagebox.showinfo(
                "Link Needs Fixing",
                "This channel's link isn't resolved yet. Use 🛠 Fix Link on "
                "the card first, then Force Download.",
                parent=self)
            return

        platform  = ch.get("platform", "YouTube")
        genre     = ch.get("genre", "(none)")
        fetch_url = watch_fetch_url(platform, ch["url"])

        # Bail before touching the Main tab if there's no usable URL, so a blank
        # channel can't blank out the Main-tab fields.
        if not fetch_url:
            messagebox.showwarning(
                "No URL",
                "This channel has no usable URL to download.",
                parent=self)
            return

        # Show it in the Main tab so the user sees exactly what is downloading.
        self._url_var.set(fetch_url)
        self._genre_var.set(genre)
        self._record_url_history(fetch_url)
        self._notebook.select(self._tab_main)

        # Build a single-item batch and launch it through the SAME worker a
        # normal Main-tab download uses. Passing the explicit run_batch to
        # _batch_worker (rather than calling _start) means a queued batch in
        # self._batch_urls is ignored — exactly this one channel downloads.
        # No channel_name/override here, so _watchlist_auto_add_if_enabled runs.
        run_batch = [{"url": fetch_url, "genre": genre, "platform": platform}]

        self._begin_download_session("Preparing batch…")
        self._set_status(f"Starting forced download of {ch['display_name']}…")

        self._run_bg(self._batch_worker, run_batch)

    # ── Download new for one channel ──────────────────────────────────────────
    def _watchlist_download_new(self, cid):
        """Download the pending new tracks for one watched channel."""
        if self._downloading:
            messagebox.showinfo(
                "Download in Progress",
                "A download is already running. Wait for it to finish first.",
                parent=self)
            return

        ch = self._db.get_watchlist_channel(cid)
        if not ch or ch.get("pending_new_count", 0) == 0:
            messagebox.showinfo(
                "Nothing to Download",
                "No new tracks pending. Try scanning first.",
                parent=self)
            return

        pending = json.loads(ch.get("pending_entries_json", "[]"))
        if not pending:
            return
        pending_count = ch.get("pending_new_count", len(pending))

        # Feed the channel as ONE listing URL — exactly like pasting it into the
        # Main tab — instead of one yt-dlp job per track. The shared engine
        # extracts the catalogue once and the Watch List on-disk skip blows
        # through everything already in the folder, downloading only the new
        # tracks. Fast, not one-by-one.
        platform = ch.get("platform", "YouTube")
        genre = ch.get("genre", "(none)")
        run_batch = [{
            "url":          watch_fetch_url(platform, ch["url"]),
            "genre":        genre,
            "platform":     platform,
            "channel_name": ch["display_name"],
            "title":        ch["display_name"],
        }]

        # Set up the watchlist batch context for cleanup in _batch_worker
        self._active_watchlist_batch = {
            "channel_ids": [cid],
        }
        # Record when this channel last started updating (downloading). A single
        # card download does not move the global auto-download schedule anchor.
        self._db.set_watchlist_download_started([cid], int(time.time()))
        # Show this channel in the Main tab's Batch Queue panel.
        self._wl_batch_channels = [ch["display_name"]]
        self._wl_batch_genres = [genre]
        self._wl_batch_active_idx = 0

        self._watchlist_log(
            f"Downloading {pending_count} new track{'s' if pending_count != 1 else ''} "
            f"from {ch['display_name']}…", "info")

        # Stay on the Watch List tab — the download runs as background activity
        # and reports into the Watch List scan log (mirrored from the batch
        # worker via _wl_dl_log) instead of switching to the Main tab.
        self._begin_download_session("Preparing Watch List batch…",
                                     watchlist=True)
        self._set_status(f"Watch List: downloading {pending_count} new tracks…")
        self._batch_rebuild_rows()   # show the channel in the Batch Queue panel

        self._run_bg(self._batch_worker, run_batch)
        # Update just this card so the downloading channel shows a Cancel button.
        self._watchlist_update_card(cid)

    # ── Download all new across all channels ──────────────────────────────────
    def _watchlist_download_all_new(self):
        """Download the pending new tracks across all watched channels."""
        if self._downloading:
            messagebox.showinfo(
                "Download in Progress",
                "A download is already running. Wait for it to finish first.",
                parent=self)
            return

        # One listing URL per channel (like the Main tab), NOT one job per
        # track. Each channel's catalogue is extracted once and the Watch List
        # on-disk skip blows through everything already in the folder, so only
        # genuinely-new tracks download. Fast, not one-by-one.
        channels = self._db.get_all_watchlist_channels()
        run_batch = []
        channel_ids = []
        pending_total = 0
        for ch in channels:
            pending_count = ch.get("pending_new_count", 0)
            if pending_count == 0:
                continue
            platform = ch.get("platform", "YouTube")
            genre = ch.get("genre", "(none)")
            run_batch.append({
                "url":          watch_fetch_url(platform, ch["url"]),
                "genre":        genre,
                "platform":     platform,
                "channel_name": ch["display_name"],
                "title":        ch["display_name"],
            })
            channel_ids.append(ch["id"])
            pending_total += pending_count

        if not run_batch:
            messagebox.showinfo(
                "Nothing to Download",
                "No new tracks pending across any channels.\nTry Scan All first.",
                parent=self)
            return

        self._active_watchlist_batch = {
            "channel_ids": channel_ids,
        }
        # Record when these channels last started updating (downloading), and
        # anchor the auto-download schedule to now: any full Download All New
        # (manual or scheduled) resets the countdown to the next auto run.
        now = int(time.time())
        self._db.set_watchlist_download_started(channel_ids, now)
        self._watchlist_last_download = now
        self._autosave_automation_settings()  # persist anchor + reschedule + label
        # Show every channel in this batch in the Main tab's Batch Queue panel,
        # in the same order the worker processes them.
        self._wl_batch_channels = [item["channel_name"] for item in run_batch]
        self._wl_batch_genres = [item["genre"] for item in run_batch]
        self._wl_batch_active_idx = 0

        self._watchlist_log(
            f"Downloading {pending_total} new tracks across "
            f"{len(channel_ids)} channels…", "info")

        # Stay on the Watch List tab — the download runs as background activity
        # and reports into the Watch List scan log (mirrored from the batch
        # worker via _wl_dl_log) instead of switching to the Main tab.
        self._begin_download_session("Preparing Watch List batch…",
                                     watchlist=True)
        self._set_status(f"Watch List: downloading {pending_total} new tracks…")
        self._batch_rebuild_rows()   # show the channels in the Batch Queue panel

        self._run_bg(self._batch_worker, run_batch)
        # Update just the batched cards so they show a Cancel button.
        self._watchlist_update_cards(channel_ids)

    def _watchlist_force_download_all(self):
        """Run EVERY resolved channel's full catalogue — not just pending new
        uploads. The Watch List skip logic blows through tracks already on disk
        (stamping each with its source-URL ID3 tag) and downloads anything
        genuinely missing. Used to backfill tags across the whole library."""
        if self._downloading:
            messagebox.showinfo(
                "Download in Progress",
                "A download is already running. Wait for it to finish first.",
                parent=self)
            return

        channels = self._db.get_all_watchlist_channels()
        run_batch = []
        channel_ids = []
        for ch in channels:
            if self._is_unresolved_channel(ch):
                continue   # no canonical URL yet — Check Links first
            platform = ch.get("platform", "YouTube")
            run_batch.append({
                "url":          watch_fetch_url(platform, ch["url"]),
                "genre":        ch.get("genre", "(none)"),
                "platform":     platform,
                "channel_name": ch["display_name"],
                "title":        ch["display_name"],
            })
            channel_ids.append(ch["id"])

        if not run_batch:
            messagebox.showinfo(
                "Nothing to Download",
                "No resolved channels to process.",
                parent=self)
            return

        if not messagebox.askyesno(
                "Force Download All",
                f"Re-process the full catalogue of all {len(channel_ids)} "
                "channel(s)?\n\nTracks already on disk are skipped (and stamped "
                "with their source-URL tag); only missing tracks download. This "
                "can take a while on large channels.",
                parent=self):
            return

        self._active_watchlist_batch = {"channel_ids": channel_ids}
        now = int(time.time())
        self._db.set_watchlist_download_started(channel_ids, now)
        self._watchlist_last_download = now
        self._autosave_automation_settings()
        self._wl_batch_channels = [item["channel_name"] for item in run_batch]
        self._wl_batch_genres = [item["genre"] for item in run_batch]
        self._wl_batch_active_idx = 0

        self._watchlist_log(
            f"Force-downloading full catalogue across "
            f"{len(channel_ids)} channels…", "info")

        self._begin_download_session("Preparing Watch List batch…",
                                     watchlist=True)
        self._set_status(
            f"Watch List: forcing full catalogue ({len(channel_ids)} channels)…")
        self._batch_rebuild_rows()

        self._run_bg(self._batch_worker, run_batch)
        self._watchlist_update_cards(channel_ids)

    # ── Startup hygiene ────────────────────────────────────────────────────────
    def _watchlist_cleanup_blank_rows(self):
        """Remove any nameless Watch List cards (blank/whitespace display_name)
        left behind by older auto-add bugs. Runs at startup before the list is
        populated or rendered, so the user never sees a broken blank card."""
        try:
            removed = self._db.delete_blank_watchlist_channels()
        except Exception as e:
            self._dbg.error(f"WL CLEANUP | failed: {e}")
            return
        if removed:
            self._dbg.info(f"WL CLEANUP | removed {removed} blank card(s)")

    # ── Auto-add after a normal channel download ──────────────────────────────
    def _watchlist_auto_add_if_enabled(self, url, display_name, genre,
                                        max_upload_date, channel_id=None):
        """Called from _process_one_url after a successful collection download.
        If auto-add is enabled, adds the channel to the watchlist — or, if the
        channel is already tracked under ANY of its URL forms (@handle vs
        /channel/UC… vs …/videos), updates that existing row instead of
        creating a duplicate blank card."""
        if not self._auto_add_to_watchlist.get():
            return

        # Determine the cutoff: use the max upload date from this run
        # (minus buffer), or fall back to today
        if max_upload_date:
            cutoff = subtract_days_from_yyyymmdd(
                max_upload_date, WATCHLIST_CUTOFF_BUFFER_DAYS)
        else:
            cutoff = today_yyyymmdd()

        cid  = (channel_id or "").strip()
        name = (display_name or "").strip()
        platform = self._detect_platform(url)

        # ── Find an existing row, robust to differing URL forms ──────────────
        # (a) by canonical channel_id; (b) exact url; (c) canonical-key scan.
        existing = find_matching_watchlist_row(
            self._db.get_all_watchlist_channels(),
            url, channel_id=cid, platform=platform)

        if existing is not None:
            # Update the tracked row in place — never insert a second card.
            wl_id = existing["id"]
            fields = {}
            if cid and not (existing.get("channel_id") or "").strip():
                fields["channel_id"] = cid          # backfill canonical id
            if name and not (existing.get("display_name") or "").strip():
                fields["display_name"] = name       # backfill blank name
            if fields:
                self._db.update_watchlist_channel_fields(wl_id, **fields)
            if cutoff > (existing.get("scan_cutoff_date") or ""):
                self._db.update_watchlist_cutoff(existing["url"], cutoff)
            self._dbg.info(
                f"WL AUTO-UPDATE | {name or existing.get('display_name')!r}  "
                f"cutoff={cutoff}  fields={list(fields) or 'none'}")
            return

        # ── No existing row. Never create a nameless card. ───────────────────
        if not name:
            self._dbg.info(
                f"WL AUTO-ADD SKIP | blank name for {url!r} — not inserting")
            return

        result = self._db.add_watchlist_channel(
            url=url, display_name=name,
            platform=platform, genre=genre or "(none)",
            scan_cutoff_date=cutoff, auto_added=True,
            channel_id=cid or None)
        if result is None:
            return
        self._dbg.info(f"WL AUTO-ADD | {name!r}  cutoff={cutoff}")
        # A brand-new channel was added on the background download worker.
        # Marshal a structural card rebuild to the main thread so the card
        # appears immediately instead of only after a restart. No scan is
        # triggered here; scanning stays blocked during the download by the
        # self._downloading guard in _auto_download_tick.
        self.after(0, self._watchlist_refresh)

    # ══════════════════════════════════════════════════════════════════════════
    # Maintenance — first-run folder import and DB rebuild from the activity log
    # ══════════════════════════════════════════════════════════════════════════
    def _watchlist_populate_from_folders(self):
        """On first run (empty Watch List), populate it by scanning the
        existing folder hierarchy:  base/YouTube/<Genre>/<Channel>/*.mp3

        Each channel sub-folder becomes a Watch List entry. The scan cutoff
        is derived from the newest .mp3 in the folder (minus a small buffer)
        so future scans only surface genuinely new uploads."""
        # Only populate when the Watch List is empty.
        try:
            if self._db.get_all_watchlist_channels():
                return
        except Exception:
            return

        added = 0
        for platform in ("YouTube", "SoundCloud"):
            proot = self._platform_dir(platform)
            if not os.path.isdir(proot):
                continue
            for genre_dir in sorted(os.listdir(proot)):
                genre_path = os.path.join(proot, genre_dir)
                if not os.path.isdir(genre_path):
                    continue
                genre = "(none)" if genre_dir == "_No Genre" else genre_dir

                for channel_dir in sorted(os.listdir(genre_path)):
                    channel_path = os.path.join(genre_path, channel_dir)
                    if not os.path.isdir(channel_path):
                        continue

                    count, newest = scan_folder_newest_mp3(channel_path)
                    cutoff = (subtract_days_from_yyyymmdd(
                                  newest, WATCHLIST_CUTOFF_BUFFER_DAYS)
                              if newest else today_yyyymmdd())

                    sc = read_channel_sidecar(channel_path)
                    if sc and (sc.get("channel_url") or sc.get("channel_id")):
                        real_url = (sc.get("channel_url")
                                    or channel_url_from_id(sc.get("channel_id")))
                        result = self._db.add_watchlist_channel(
                            url=real_url,
                            channel_id=sc.get("channel_id"),
                            display_name=sc.get("display_name") or channel_dir,
                            platform=platform,
                            genre=genre,
                            scan_cutoff_date=cutoff,
                            auto_added=True,
                            status="idle")
                        status_note = "from sidecar"
                    else:
                        # No sidecar: park as needs_resolve with a unique
                        # sentinel so UNIQUE(url) holds and nothing bogus is
                        # scanned. YouTube can auto-resolve via Fix Link search;
                        # SoundCloud is fixed by pasting the soundcloud.com URL.
                        sentinel = (f"{UNRESOLVED_URL_PREFIX}{platform}/"
                                    f"{genre}/{channel_dir}")
                        result = self._db.add_watchlist_channel(
                            url=sentinel,
                            display_name=channel_dir,
                            platform=platform,
                            genre=genre,
                            scan_cutoff_date=cutoff,
                            auto_added=True,
                            status="needs_resolve")
                        status_note = "needs_resolve"

                    if result is not None:
                        added += 1
                        self._dbg.info(
                            f"WL FOLDER-POPULATE | {channel_dir!r}  "
                            f"platform={platform}  genre={genre}  "
                            f"cutoff={cutoff}  ({status_note})")

        if added:
            self._watchlist_log(
                f"Populated {added} channel(s) from existing folders", "ok")
            self._watchlist_refresh()

    # ══════════════════════════════════════════════════════════════════════════
    # Artwork backfill — find cover art for tracks that predate the feature
    # ══════════════════════════════════════════════════════════════════════════

    def _fetch_missing_artwork(self):
        """Entry point for the 'Fetch Missing Artwork' button. Validates that
        artwork is switched on and that there is anything to do, then hands off
        to an _ArtworkBackfillSession."""
        if getattr(self, "_artwork_session", None) is not None:
            messagebox.showinfo(
                "Fetch Missing Artwork",
                "An artwork run is already in progress.", parent=self)
            return

        mode = self._cover_art_mode_value()
        if mode == "off":
            messagebox.showinfo(
                "Fetch Missing Artwork",
                "Cover Art is switched off.\n\nChoose a Cover Art mode in "
                "Settings first, then run this again.", parent=self)
            return
        if not cb_artwork.artwork_available():
            messagebox.showwarning(
                "Fetch Missing Artwork",
                "Cover art needs Pillow and mutagen, which are not available "
                "in this install.", parent=self)
            return

        rows = self._db.get_downloads_missing_artwork()
        if not rows:
            messagebox.showinfo(
                "Fetch Missing Artwork",
                "Every track in your library already has cover art.",
                parent=self)
            return

        n = len(rows)
        ok = messagebox.askokcancel(
            "Fetch Missing Artwork",
            f"{n} track{'s' if n != 1 else ''} have no cover art.\n\n"
            "Artwork will be downloaded where available and embedded into each "
            "file. This can take a while for a large library, and you can "
            "cancel at any point — tracks already done are kept.\n\nContinue?",
            parent=self)
        if not ok:
            return

        self._dbg.info(f"ARTFILL START | {n} tracks missing artwork, mode={mode}")
        self._artwork_session = _ArtworkBackfillSession(self, rows, mode)
        self._artwork_session.start()

    # ══════════════════════════════════════════════════════════════════════════
    # Rebuild — rebuild the downloads table from the files already on disk
    # ══════════════════════════════════════════════════════════════════════════

    def _rebuild_db_from_files(self):
        """Rebuild the downloads table by scanning the actual .mp3 files in the
        folder hierarchy (base/<Platform>/<Genre>/<Channel>/*.mp3), rather than
        re-reading the activity log. The files on disk are the source of truth."""
        ok = messagebox.askokcancel(
            "Rebuild Database",
            "This action cannot be undone. Is it ok to continue?",
            parent=self)
        if not ok:
            return

        # A rebuild clears the table, which would otherwise orphan every
        # track's cover-art bookkeeping (the sidecar JPEG and the APIC frame
        # both survive on disk, but the rows pointing at them would not).
        # Snapshot the artwork columns by file path and re-attach them below.
        art_snapshot = self._db.get_artwork_by_path()

        rows = []
        for platform in ("YouTube", "SoundCloud"):
            proot = self._platform_dir(platform)
            if not os.path.isdir(proot):
                continue
            for genre_dir in sorted(os.listdir(proot)):
                genre_path = os.path.join(proot, genre_dir)
                if not os.path.isdir(genre_path):
                    continue
                genre = "(none)" if genre_dir == "_No Genre" else genre_dir

                for channel_dir in sorted(os.listdir(genre_path)):
                    channel_path = os.path.join(genre_path, channel_dir)
                    if not os.path.isdir(channel_path):
                        continue

                    # Prefer the channel's canonical identity from its sidecar.
                    sc = read_channel_sidecar(channel_path) or {}
                    channel_name = sc.get("display_name") or channel_dir
                    channel_id   = sc.get("channel_id")
                    channel_url  = (sc.get("channel_url")
                                    or channel_url_from_id(channel_id) or "")

                    for name in sorted(os.listdir(channel_path)):
                        if not name.lower().endswith(".mp3"):
                            continue
                        full = os.path.join(channel_path, name)
                        try:
                            mtime = int(os.path.getmtime(full))
                        except OSError:
                            continue
                        # Upload date / downloaded time aren't recorded in the
                        # file itself, so fall back to the file's mtime.
                        date_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d")
                        art_path, art_embedded, thumb_url = art_snapshot.get(
                            full, (None, 0, None))
                        rows.append({
                            "video_id":     None,
                            "title":        os.path.splitext(name)[0],
                            "channel_name": channel_name,
                            "channel_url":  channel_url,
                            "channel_id":   channel_id,
                            "platform":     platform,
                            "genre":        genre,
                            "file_path":    full,
                            "upload_date":  date_str,
                            "ts":           mtime,
                            "bitrate":      "",
                            "artwork_path":     art_path,
                            "artwork_embedded": art_embedded,
                            "thumbnail_url":    thumb_url,
                        })

        self._db.clear_all_downloads()
        count = self._db.backfill_downloads(rows)
        self._db.refresh_watchlist_totals()
        messagebox.showinfo(
            "Rebuild Complete",
            f"Indexed {count} track{'s' if count != 1 else ''} from the "
            "files on disk.",
            parent=self)
        self._dbg.info(f"DB REBUILD | indexed {count} files from disk")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Single-instance guard: a second launch (manual OR Windows --startup)
    # can't bind the loopback port the running instance already holds, so it
    # exits silently. The lock is parked on the app instance so it isn't
    # garbage-collected — that would close the socket and release the lock.
    _instance_lock = acquire_single_instance(SINGLE_INSTANCE_PORT)
    if _instance_lock is None:
        sys.exit(0)
    app = MP3DownloaderApp()
    app._instance_lock = _instance_lock
    # Purge any leftover update workspace from a prior update (e.g. the old
    # updater image that couldn't delete itself while running), then schedule a
    # throttled background update check once the UI has settled.
    ucore.purge_dir(ucore.default_workspace())
    app.after(3000, app._auto_check_for_updates)
    # Arm the recurring silent update check on the user's chosen interval.
    app.after(3500, app._reschedule_update_check)
    app.mainloop()

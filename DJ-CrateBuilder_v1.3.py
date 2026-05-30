import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import os
import sys
import subprocess
import re
import json
import random
import logging
import time
import webbrowser
import urllib.parse
from datetime import datetime, timedelta, date

from cratebuilder.util import (
    load_config, save_config, today_yyyymmdd,
    normalize_track_key, scan_folder_newest_mp3,
)
from cratebuilder.sidecar import (
    channel_url_from_id,
    read_channel_sidecar, write_channel_sidecar, is_unresolved_channel,
)
from cratebuilder.db import DownloadsDatabase
from cratebuilder import startup as cb_startup

# ══════════════════════════════════════════════════════════════════════════════
# ██  VERSION & ABOUT  ██  ── Edit these values to update the app info ──────
# ══════════════════════════════════════════════════════════════════════════════
APP_NAME    = "DJ-CrateBuilder"
APP_VERSION = "1.3"

ABOUT_CREATED_BY  = "CorruptSintax@Gmail.com"
ABOUT_DESCRIPTION = "Vibe-Coded entirely with Claude-AI"
GITHUB_URL        = "https://github.com/Sintax/DJ-CrateBuilder"

# ── Add or remove lines below to customize the About tab content. ──────────
# ── Each tuple is  ("Label", "Value")  and will display as a row. ──────────
ABOUT_FIELDS = [
    ("Application",  f"{APP_NAME}  v{APP_VERSION}"),
    ("Created by",   ABOUT_CREATED_BY),
    ("Built with",   ABOUT_DESCRIPTION),
    ("GitHub",       GITHUB_URL),
]
# ══════════════════════════════════════════════════════════════════════════════

# ── Dependency check ──────────────────────────────────────────────────────────
def check_dependencies():
    missing = []
    try:
        import yt_dlp
    except ImportError:
        missing.append("yt-dlp")
    return missing

# ── Color palette ─────────────────────────────────────────────────────────────
BG        = "#0f0f0f"
SURFACE   = "#1a1a1a"
SURFACE2  = "#242424"
BORDER    = "#2e2e2e"
TEXT      = "#f0f0f0"
TEXT_DIM  = "#888888"
TEXT_MED  = "#bbbbbb"
SUCCESS   = "#22c55e"
SKIP_COL  = "#6b7280"
LINK_COL  = "#60a5fa"   # light blue for clickable links

# Platform accent colours
YT_RED    = "#ff3b3b"
YT_DARK   = "#cc2222"
SC_ORANGE = "#ff5500"
SC_DARK   = "#cc4400"

# Watch List accent
WL_PURPLE = "#a78bfa"
WL_DARK   = "#7c3aed"

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
# today_yyyymmdd moved to cratebuilder.util (imported above)

def days_ago_yyyymmdd(days):
    d = date.today() - timedelta(days=int(days))
    return d.strftime("%Y%m%d")

def subtract_days_from_yyyymmdd(yyyymmdd, days):
    """Safely subtract *days* from a YYYYMMDD string, returning a new
    YYYYMMDD string. Returns the input unchanged if parsing fails."""
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        return (dt - timedelta(days=int(days))).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return yyyymmdd

def format_yyyymmdd_readable(yyyymmdd):
    """Convert '20260310' to 'March 10, 2026' (or return input if invalid)."""
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
        return dt.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        return str(yyyymmdd)

def format_timestamp_relative(ts):
    """Convert a unix timestamp into a short 'X days ago' / 'Never' string."""
    if not ts:
        return "Never"
    try:
        diff = int(time.time() - float(ts))
        if diff < 60:          return "Just now"
        if diff < 3600:
            m = diff // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if diff < 86400:
            h = diff // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        if diff < 86400 * 30:
            d = diff // 86400
            return f"{d} day{'s' if d != 1 else ''} ago"
        if diff < 86400 * 365:
            mo = diff // (86400 * 30)
            return f"{mo} month{'s' if mo != 1 else ''} ago"
        y = diff // (86400 * 365)
        return f"{y} year{'s' if y != 1 else ''} ago"
    except Exception:
        return "Unknown"


def auto_check_hours_to_seconds(value):
    """Map an interval dropdown label to seconds, or None for 'Off'/unknown."""
    try:
        if not value or value.strip().lower() == "off":
            return None
        hours = int(value.strip().split()[0])
        return hours * 3600
    except (ValueError, AttributeError, IndexError):
        return None


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

    def _show(self):
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self._tip = tk.Toplevel(self.widget)
            self._tip.wm_overrideredirect(True)
            self._tip.wm_geometry(f"+{x}+{y}")
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
        except Exception:
            self._tip = None

    def _hide(self):
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def update_text(self, new_text):
        self.text = new_text


# DownloadsDatabase moved to cratebuilder.db (imported above)


# ═════════════════════════════════════════════════════════════════════════════
# Log parser — extract DOWNLOADED entries from activity.log for import/rebuild
# ═════════════════════════════════════════════════════════════════════════════
def parse_activity_log_entries(log_path):
    entries = []
    if not os.path.exists(log_path):
        return entries
    ts_re = re.compile(r'^(\d{4})-(\d{2})-(\d{2}) \d{2}:\d{2}:\d{2}')
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "DOWNLOADED" not in line:
                    continue
                ts_m = ts_re.match(line)
                if not ts_m:
                    continue
                log_date = f"{ts_m.group(1)}{ts_m.group(2)}{ts_m.group(3)}"
                plat_m = re.search(r'Platform:\s*(\S+)', line)
                if not plat_m:
                    continue
                platform = plat_m.group(1).strip()
                genre_m = re.search(r'Genre:\s*([^|]+?)\s*\|', line)
                genre = genre_m.group(1).strip() if genre_m else "(none)"
                if genre == "—":
                    genre = "(none)"
                title_m = re.search(r'Title:\s*(.+?)\s*\|\s*File:', line)
                title = title_m.group(1).strip() if title_m else ""
                file_m = re.search(r'File:\s*(.+?)\s*\|\s*URL:', line)
                file_path = file_m.group(1).strip() if file_m else ""
                url_m = re.search(r'URL:\s*(\S+)', line)
                url = url_m.group(1).strip() if url_m else ""
                qual_m = re.search(r'Quality:\s*(.+?)$', line)
                quality = qual_m.group(1).strip() if qual_m else ""
                channel_name = _infer_channel_from_path(file_path, platform)
                entries.append({
                    "timestamp": line[:19], "log_date": log_date,
                    "platform": platform, "genre": genre,
                    "title": title, "file_path": file_path,
                    "channel_name": channel_name, "url": url,
                    "quality": quality,
                })
    except Exception:
        pass
    return entries


def _infer_channel_from_path(file_path, platform):
    if not file_path:
        return ""
    parts = file_path.replace("\\", "/").split("/")
    try:
        idx = parts.index(platform)
    except ValueError:
        return ""
    if idx + 3 >= len(parts):
        return ""
    return parts[idx + 2]


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
class LogViewerWindow(tk.Toplevel):
    """Standalone dark-themed log viewer window."""

    _SEARCH_HL  = "#f59e0b"   # amber highlight for search matches
    _SEARCH_FG  = "#000000"

    def __init__(self, parent, log_path):
        super().__init__(parent)
        self._log_path    = log_path
        self._parent      = parent
        self._filter_var  = tk.StringVar(value="All")
        self._search_var  = tk.StringVar()
        self._match_idx   = []   # list of (line, col_start, col_end) for search hits
        self._match_pos   = -1   # currently-focused match index

        self.title("📋  Downloads Log  —  DJ CrateBuilder")
        self.geometry("1000x640")
        self.minsize(700, 400)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Centre over parent
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 1000) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 640)  // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.load_log()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.focus_force()

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
        for opt in FILTER_OPTIONS:
            b = tk.Button(
                toolbar, text=opt,
                font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                command=lambda o=opt: self._set_filter(o))
            b.pack(side="left", padx=2, pady=6)
            self._filter_btns[opt] = b
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

        self._match_lbl = tk.Label(search_frame, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=SURFACE2, width=10)
        self._match_lbl.pack(side="left", padx=(4, 0))

        # Right cluster: action buttons
        self._tb_btn(toolbar, "↗  System Viewer", self._open_external,
                     side="right", padx=(0,10))
        tk.Frame(toolbar, width=1, bg=BORDER).pack(side="right", fill="y",
                                                    padx=2, pady=6)
        self._tb_btn(toolbar, "⎘  Copy All",    self._copy_all,   side="right")
        self._tb_btn(toolbar, "⟳  Refresh",     self.refresh,     side="right")
        self._tb_btn(toolbar, "⤓  Jump to End", self._jump_end,   side="right")
        self._tb_btn(toolbar, "⤒  Jump to Top", self._jump_top,   side="right")

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
        self._txt.bind("<MouseWheel>", lambda e: self._txt.yview_scroll(
            int(-1*(e.delta/120)), "units"))

    def _tb_btn(self, parent, label, cmd, side="left", padx=(2,2)):
        """Helper: small flat toolbar button matching the app palette."""
        b = tk.Button(parent, text=label,
                      font=("Segoe UI", 9), relief="flat", bd=0,
                      bg=SURFACE2, fg=TEXT_DIM, activebackground=BORDER,
                      activeforeground=TEXT, padx=8, pady=4,
                      cursor="hand2", command=cmd)
        b.pack(side=side, padx=padx, pady=6)
        return b

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
        # Brief visual confirmation on the button
        for b in self._filter_btns.values():
            pass   # no-op; confirmation via stats bar instead
        self._stats_bar.config(text="  ✓  Copied to clipboard.")
        self.after(2000, lambda: self._update_stats(
            *self._count_stats()))

    def _count_stats(self):
        content = self._txt.get("1.0", "end")
        lines   = content.splitlines()
        dl = sum(1 for l in lines if "DOWNLOADED" in l)
        sk = sum(1 for l in lines if "SKIPPED"    in l)
        er = sum(1 for l in lines if "ERROR"      in l)
        return dl, sk, er, len(lines)

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


class DebugLogViewerWindow(tk.Toplevel):
    """Standalone dark-themed debug log viewer window."""

    _SEARCH_HL  = "#f59e0b"
    _SEARCH_FG  = "#000000"

    # Colour tags for log levels
    _LEVEL_COLORS = {
        "INFO":    TEXT_MED,
        "DEBUG":   "#6b7280",
        "WARNING": "#f59e0b",
        "ERROR":   "#ef4444",
    }

    def __init__(self, parent, log_path):
        super().__init__(parent)
        self._log_path   = log_path
        self._parent     = parent
        self._search_var = tk.StringVar()
        self._match_idx  = []
        self._match_pos  = -1

        self.title("🔍  Debug Log  —  DJ CrateBuilder")
        self.geometry("1100x680")
        self.minsize(700, 400)
        self.configure(bg=BG)
        self.resizable(True, True)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 1100) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 680)  // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.load_log()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.focus_force()

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
        for opt in ["All", "INFO", "ERROR", "DEBUG"]:
            b = tk.Button(
                toolbar, text=opt,
                font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                command=lambda o=opt: self._set_filter(o))
            b.pack(side="left", padx=2, pady=6)
            self._filter_btns[opt] = b
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

        for sym, cmd in [("▲", self._find_prev), ("▼", self._find_next),
                         ("✕", self._clear_search)]:
            tk.Button(search_frame, text=sym, font=("Segoe UI", 9, "bold"),
                      relief="flat", bd=0, padx=6, pady=2, cursor="hand2",
                      bg=SURFACE2, fg=TEXT_DIM,
                      activebackground=BORDER, activeforeground=TEXT,
                      command=cmd).pack(side="left", padx=1)

        self._match_lbl = tk.Label(search_frame, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=SURFACE2, width=10)
        self._match_lbl.pack(side="left", padx=(4, 0))

        # Right cluster
        for txt, cmd in [("↗  System Viewer", self._open_external),
                         ("⎘  Copy All", self._copy_all),
                         ("⟳  Refresh", self.refresh),
                         ("⤓  End", self._jump_end),
                         ("⤒  Top", self._jump_top)]:
            tk.Button(toolbar, text=txt, font=("Segoe UI", 9, "bold"),
                      relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                      bg=SURFACE2, fg=TEXT_DIM,
                      activebackground=BORDER, activeforeground=TEXT,
                      command=cmd).pack(side="right", padx=2, pady=6)

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

    def _paint_filter_btns(self):
        cur = self._filter_var.get()
        for name, btn in self._filter_btns.items():
            if name == cur:
                btn.config(bg=YT_RED, fg="#ffffff",
                           activebackground="#b91c1c", activeforeground="#ffffff")
            else:
                btn.config(bg=SURFACE2, fg=TEXT_DIM,
                           activebackground=BORDER, activeforeground=TEXT)

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
            pos = self._txt.search(query, start, stopindex="end", nocase=True)
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

    def _jump_top(self):
        self._txt.yview_moveto(0.0)

    def _jump_end(self):
        self._txt.yview_moveto(1.0)

    def _toggle_wrap(self):
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
        self._txt.bind("<MouseWheel>", lambda e: self._txt.yview_scroll(
            int(-1*(e.delta/120)), "units"))

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.focus_force()


# ─────────────────────────────────────────────────────────────────────────────
class MP3DownloaderApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("850x950")
        self.minsize(640, 620)
        self.configure(bg=BG)
        self.resizable(True, True)

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
        _raw_skip = cfg.get("skip_mode", "In Database ~ In Folder")
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

        # Download behavior settings
        self._geo_bypass      = tk.BooleanVar(value=cfg.get("geo_bypass", False))
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

        # Automation settings (auto-check / startup / tray)
        self._auto_check_hours = tk.StringVar(
            value=cfg.get("auto_check_hours", "24 hours"))
        self._run_at_startup = tk.BooleanVar(
            value=cfg.get("run_at_startup", False))
        if sys.platform == "win32":
            self._run_at_startup.set(cb_startup.startup_is_enabled())
        self._minimize_to_tray = tk.BooleanVar(
            value=cfg.get("minimize_to_tray", False))
        self._watchlist_last_check = int(cfg.get("watchlist_last_check", 0))
        self._auto_check_after_id = None
        self._tray_icon = None  # set when tray is active
        self._auto_check_hours.trace_add("write", self._autosave_automation_settings)
        self._minimize_to_tray.trace_add("write", self._autosave_automation_settings)

        # Ensure directory structure exists on startup
        self._url_history = cfg.get("url_history", [])[:6]
        self._ensure_dirs()
        self._setup_logger()

        self._build_styles()
        self._build_ui()
        self._apply_platform()      # paint initial platform colours
        self._check_deps_async()

        # First-run: auto-populate the Watch List from existing channel folders
        self.after(1200, self._watchlist_populate_from_folders)
        self.after(1600, self._reschedule_auto_check)

        # Close button hides to tray (when enabled) instead of quitting.
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # If Windows auto-started us and tray mode is on, begin hidden.
        if (sys.platform == "win32" and self._minimize_to_tray.get()
                and "--startup" in sys.argv):
            self.after(1700, self._hide_to_tray)

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

    def _resolve_save_dir(self, genre, channel_name=None, platform=None):
        """Build the final save path:  base/Platform[/Genre[/Channel]]"""
        parts = [self._platform_dir(platform)]
        if genre and genre != "(none)":
            parts.append(genre)
        else:
            parts.append("_No Genre")
        if channel_name:
            safe = re.sub(r'[\\/*?:"<>|]', "_", channel_name).strip()
            if safe:
                parts.append(safe)
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    # ── Download logger ───────────────────────────────────────────────────────
    def _setup_logger(self):
        """Initialise (or re-initialise) the file logger.
        Called on startup and whenever the base directory changes."""
        os.makedirs(self._base_dir, exist_ok=True)
        # Place log in the program's install/script directory
        app_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._log_path = os.path.join(app_dir, "activity.log")

        logger = logging.getLogger("CrateBuilder")
        # Clear any existing handlers so re-init doesn't duplicate output
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False

        fh = logging.FileHandler(self._log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
        self._logger = logger

        # ── Debug logger (separate file for diagnostics) ──────────────────────
        self._debug_log_path = os.path.join(app_dir, "debug.log")
        dbg = logging.getLogger("CrateBuilder.debug")
        dbg.handlers.clear()
        dbg.setLevel(logging.DEBUG)
        dbg.propagate = False

        dfh = logging.FileHandler(self._debug_log_path, encoding="utf-8")
        dfh.setLevel(logging.DEBUG)
        dfh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        dbg.addHandler(dfh)
        self._dbg = dbg
        self._dbg.info("═" * 80)
        self._dbg.info(f"SESSION START  —  {APP_NAME} v{APP_VERSION}")
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
        """Log yt-dlp options dict to debug.log (redact large values)."""
        safe = {}
        for k, v in opts.items():
            if k == "progress_hooks":
                safe[k] = f"[{len(v)} hook(s)]"
            elif k == "postprocessors":
                safe[k] = v
            else:
                safe[k] = v
        self._dbg.info(f"YDL OPTS ({label}) | {safe}")

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

    def _was_logged(self, filepath, title=None):
        """Return True if *filepath* or *title* appears in the log as a
        DOWNLOADED entry.  Checks the full path first, then falls back to
        a title-based search for resilience against path changes."""
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if "DOWNLOADED" not in line:
                        continue
                    if filepath and filepath in line:
                        return True
                    if title and f"Title: {title} |" in line:
                        return True
        except FileNotFoundError:
            pass
        return False

    def _file_exists_on_disk(self, save_dir, title):
        """Check if a file matching *title* already exists in *save_dir*.
        Uses yt-dlp's sanitize_filename for an exact match first, then
        falls back to a case-insensitive prefix scan of existing .mp3 files."""
        try:
            from yt_dlp.utils import sanitize_filename as ytdl_sanitize
            ytdl_safe = ytdl_sanitize(title, restricted=False)
        except ImportError:
            ytdl_safe = re.sub(r'[\\/*?:"<>|]', "_", title)

        # Exact match using yt-dlp's sanitization
        exact = os.path.join(save_dir, ytdl_safe + ".mp3")
        if os.path.exists(exact):
            return exact

        # Also try our own regex sanitization (for files we downloaded before
        # the fix, which used the old naming)
        regex_safe = re.sub(r'[\\/*?:"<>|]', "_", title).strip()
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

    # ── Batch URL list ────────────────────────────────────────────────────────
    def _build_batch_panel(self, parent):
        """Build the compact batch-URL list panel."""
        self._batch_urls = []   # list of {"url", "genre", "platform"} dicts

        hdr = ttk.Frame(parent)
        hdr.pack(fill="x", pady=(0, 4))
        self._batch_count_lbl = ttk.Label(hdr, text="Batch Queue  (0 URLs)",
                                           style="S.White.Section.TLabel")
        self._batch_count_lbl.pack(side="left")
        ttk.Button(hdr, text="Clear All", style="Browse.TButton",
                   command=self._batch_clear).pack(side="right")
        self._batch_add_btn = ttk.Button(hdr, text="+ Add to Batch",
                   style="Browse.TButton", command=self._batch_add)
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
        self._batch_canvas.bind("<MouseWheel>", lambda e: (
            self._batch_canvas.yview_scroll(int(-1*(e.delta/120)), "units"),
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

        genre_str = item["genre"] if item["genre"] != "(none)" else "—"
        tk.Label(row, text=genre_str, font=("Segoe UI", 9),
                  fg=TEXT_DIM, bg=SURFACE2, anchor="e"
                  ).pack(side="left", padx=(4, 6))

        for sym, delta in [("▲", -1), ("▼", 1)]:
            tk.Button(row, text=sym, font=("Segoe UI", 8),
                       bg=SURFACE2, fg=TEXT_DIM, relief="flat", bd=0,
                       activebackground=BORDER, activeforeground=TEXT,
                       cursor="hand2", padx=3,
                       command=lambda i=idx, d=delta: self._batch_move(i, d)
                       ).pack(side="left")

        tk.Button(row, text="✕", font=("Segoe UI", 10),
                   bg=SURFACE2, fg="#555", relief="flat", bd=0,
                   activebackground="#3b0000", activeforeground=YT_RED,
                   cursor="hand2", padx=4,
                   command=lambda i=idx: self._batch_remove(i)
                   ).pack(side="left", padx=(4, 2))

    # ── Styles ────────────────────────────────────────────────────────────────
    def _build_styles(self):
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
        s.configure("S.TLabel",              background=BG, foreground=TEXT,     font=("Segoe UI", 12))
        s.configure("S.Dim.TLabel",          background=BG, foreground=TEXT_DIM, font=("Segoe UI", 11))
        s.configure("S.Title.TLabel",        background=BG, foreground=TEXT,     font=("Segoe UI", 19, "bold"))
        s.configure("S.White.Section.TLabel", background=BG, foreground=TEXT,   font=("Segoe UI", 12, "bold"))
        s.configure("S.Bold.TCheckbutton",
            background=BG, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        s.map("S.Bold.TCheckbutton",
            background=[("active", BG)], foreground=[("active", TEXT)])

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
            background=SURFACE2, foreground=TEXT_DIM,
            font=("Segoe UI", 11),
            relief="flat", borderwidth=0, padding=(20, 12))
        s.map("Cancel.TButton",
            background=[("active", "#333"), ("disabled", SURFACE2)],
            foreground=[("disabled", "#444")])

        s.configure("CancelActive.TButton",
            background=YT_DARK, foreground=TEXT,
            font=("Segoe UI", 11),
            relief="flat", borderwidth=0, padding=(20, 12))
        s.map("CancelActive.TButton",
            background=[("active", YT_RED), ("disabled", SURFACE2)],
            foreground=[("disabled", "#444")])

        s.configure("Pause.TButton",
            background="#78350f", foreground="#fcd34d",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(12, 12))
        s.map("Pause.TButton",
            background=[("active", "#f59e0b"), ("disabled", SURFACE2)],
            foreground=[("active", "#1c1917"), ("disabled", "#444")])

        s.configure("Resume.TButton",
            background="#14532d", foreground="#86efac",
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(12, 12))
        s.map("Resume.TButton",
            background=[("active", "#22c55e"), ("disabled", SURFACE2)],
            foreground=[("active", "#052e16"), ("disabled", "#444")])

        s.configure("Browse.TButton",
            background=SURFACE2, foreground=TEXT_DIM,
            font=("Segoe UI", 10), relief="flat", borderwidth=0, padding=(10, 8))
        s.map("Browse.TButton",
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
        # Dropdown list colours
        self.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", BORDER)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 11))

        for name, color in [("Accent.Horizontal.TProgressbar", YT_RED),
                             ("Green.Horizontal.TProgressbar",  SUCCESS)]:
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
        # ── Notebook (tab bar / menu bar) ──────────────────────────────────────
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        # ── Main tab ──────────────────────────────────────────────────────────
        main_frame = ttk.Frame(self._notebook)
        self._notebook.add(main_frame, text="     ▶  Main     ")
        self._build_main_tab(main_frame)

        # ── Watch List tab ────────────────────────────────────────────────────
        watchlist_frame = ttk.Frame(self._notebook)
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

    # ── Main tab ──────────────────────────────────────────────────────────────
    def _build_main_tab(self, parent):
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        self._main_scrollbar = ttk.Scrollbar(wrapper, orient="vertical")
        self._main_scrollbar.pack(side="right", fill="y")

        self._main_canvas = tk.Canvas(
            wrapper, bg=BG, bd=0, highlightthickness=0,
            yscrollcommand=self._main_scrollbar.set)
        self._main_canvas.pack(side="left", fill="both", expand=True)

        self._main_scrollbar.config(command=self._main_canvas.yview)

        outer = ttk.Frame(self._main_canvas, padding=(28, 22, 28, 18))
        self._main_cwin = self._main_canvas.create_window(
            (0, 0), window=outer, anchor="nw")

        def _on_main_frame_configure(e):
            self._main_canvas.configure(
                scrollregion=self._main_canvas.bbox("all"))
        outer.bind("<Configure>", _on_main_frame_configure)

        def _on_main_canvas_configure(e):
            self._main_canvas.itemconfig(self._main_cwin, width=e.width)
        self._main_canvas.bind("<Configure>", _on_main_canvas_configure)

        # Global mousewheel handler — routes scroll to the active tab's canvas.
        # Widgets with their own scroll (queue Text, batch Canvas) handle
        # their own events and return "break" to prevent double-scrolling.
        def _on_global_mousewheel(event):
            w = event.widget
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
            if tab_idx == 0:
                self._main_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
            elif tab_idx == 1:
                self._wl_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
            elif tab_idx == 2:
                self._settings_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
            elif tab_idx == 3:
                self._about_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
        self.bind_all("<MouseWheel>", _on_global_mousewheel)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(outer)
        hdr.pack(fill="x", pady=(0, 4))

        self._logo_lbl = tk.Label(hdr, text="♫", font=("Segoe UI", 20),
                                   fg=YT_RED, bg=BG)
        self._logo_lbl.pack(side="left", padx=(0, 10))

        self._title_lbl = ttk.Label(hdr, text="DJ-CrateBuilder → MP3",
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

        url_row = ttk.Frame(outer)
        url_row.pack(fill="x", pady=(0, 8))

        self._url_var   = tk.StringVar()
        self._url_entry = ttk.Combobox(url_row, textvariable=self._url_var,
                                        values=self._url_history)
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

        ttk.Button(genre_row, text="+ New", style="Browse.TButton",
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

        ttk.Button(opt, text="📂  Open Folder", style="Browse.TButton",
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
                                                   style="Green.Horizontal.TProgressbar")
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
            self._qtxt.yview_scroll(int(-1*(e.delta/120)), "units")
            return "break"
        self._qtxt.bind("<MouseWheel>", _on_queue_mousewheel)

        # Queue text tags for row states
        self._qtxt.tag_configure("q_pending",  foreground=TEXT_DIM)
        self._qtxt.tag_configure("q_active",   foreground=TEXT)
        self._qtxt.tag_configure("q_done",     foreground=SUCCESS)
        self._qtxt.tag_configure("q_skipped",  foreground=SKIP_COL)
        self._qtxt.tag_configure("q_error",    foreground=YT_RED)

    # ── Settings tab ──────────────────────────────────────────────────────────
    def _build_settings_tab(self, parent):
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        self._settings_scrollbar = ttk.Scrollbar(wrapper, orient="vertical")
        self._settings_scrollbar.pack(side="right", fill="y")

        self._settings_canvas = tk.Canvas(
            wrapper, bg=BG, bd=0, highlightthickness=0,
            yscrollcommand=self._settings_scrollbar.set)
        self._settings_canvas.pack(side="left", fill="both", expand=True)

        self._settings_scrollbar.config(command=self._settings_canvas.yview)

        outer = ttk.Frame(self._settings_canvas, padding=(28, 28, 28, 18))
        self._settings_cwin = self._settings_canvas.create_window(
            (0, 0), window=outer, anchor="nw")

        def _on_frame_configure(e):
            self._settings_canvas.configure(
                scrollregion=self._settings_canvas.bbox("all"))
        outer.bind("<Configure>", _on_frame_configure)

        def _on_canvas_configure(e):
            self._settings_canvas.itemconfig(
                self._settings_cwin, width=e.width)
        self._settings_canvas.bind("<Configure>", _on_canvas_configure)

        # ── Content (all original settings widgets go into outer) ─────────────

        # Title
        ttk.Label(outer, text="⚙  Settings", style="S.Title.TLabel").pack(
            anchor="w", pady=(0, 16))

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 20))

        # ── Automation ────────────────────────────────────────────────────
        ttk.Label(outer, text="Automation",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 6))

        auto_row = ttk.Frame(outer)
        auto_row.pack(fill="x", pady=(0, 8))
        ttk.Label(auto_row, text="Check watched channels every:",
                  style="S.TLabel").pack(side="left", padx=(0, 10))
        self._auto_check_combo = ttk.Combobox(
            auto_row, textvariable=self._auto_check_hours,
            values=["Off", "6 hours", "12 hours", "24 hours", "48 hours"],
            state="readonly", width=10)
        self._auto_check_combo.pack(side="left")
        Tooltip(self._auto_check_combo,
                "How often to scan watched channels for new uploads and "
                "auto-download them. Default 24 hours.", wraplength=320)

        if sys.platform == "win32":
            startup_row = ttk.Frame(outer)
            startup_row.pack(fill="x", pady=(2, 4))
            ttk.Checkbutton(
                startup_row, text="Run DJ-CrateBuilder when Windows starts",
                variable=self._run_at_startup,
                command=self._on_run_at_startup_toggle,
                style="S.Bold.TCheckbutton").pack(side="left")

            tray_row = ttk.Frame(outer)
            tray_row.pack(fill="x", pady=(0, 4))
            ttk.Checkbutton(
                tray_row,
                text="Minimize to system tray (keep Watch List running in background)",
                variable=self._minimize_to_tray,
                style="S.Bold.TCheckbutton").pack(side="left")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 18))

        # ── Time / Length Limiter ─────────────────────────────────────────────
        _lbl = ttk.Label(outer, text="Time / Length Limiter",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 8))
        Tooltip(_lbl, "Skip any file whose duration exceeds the limit below. "
                      "Uncheck to allow files of any length.", wraplength=360)

        limit_enable_row = ttk.Frame(outer)
        limit_enable_row.pack(fill="x", pady=(0, 8))

        ttk.Checkbutton(limit_enable_row,
                        text="Enable",
                        variable=self._limit_enabled,
                        command=self._on_limiter_toggle,
                        style="S.Bold.TCheckbutton"
                        ).pack(side="left", padx=(0, 20))

        tk.Label(limit_enable_row, text="Max Length:",
                 font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG
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
            length=340,
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
        _lbl = ttk.Label(outer, text="Audio Output",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 8))
        Tooltip(_lbl, "Bitrate used when converting the downloaded audio to MP3. "
                      "Higher values produce better quality and larger files.", wraplength=360)

        bitrate_row = ttk.Frame(outer)
        bitrate_row.pack(fill="x", pady=(0, 10))

        tk.Label(bitrate_row, text="Output Quality:",
                 font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG
                 ).pack(side="left", padx=(0, 12))

        self._bitrate_combo = ttk.Combobox(
            bitrate_row,
            textvariable=self._bitrate_quality,
            values=["128 kbps", "192 kbps", "224 kbps", "256 kbps", "320 kbps"],
            state="readonly", width=14)
        self._bitrate_combo.pack(side="left", padx=(0, 14))
        Tooltip(self._bitrate_combo, "192 kbps = good quality  •  320 kbps = maximum MP3 quality", wraplength=360)

        # ── No-conversion checkbox ────────────────────────────────────────────
        no_conv_row = ttk.Frame(outer)
        no_conv_row.pack(fill="x", pady=(0, 4))
        self._no_conv_cb = ttk.Checkbutton(no_conv_row,
                        text="Keep original format (no conversion)",
                        variable=self._no_conversion,
                        command=self._on_no_conversion_toggle,
                        style="S.Bold.TCheckbutton")
        self._no_conv_cb.pack(side="left")
        Tooltip(self._no_conv_cb,
                "When enabled, files are saved in their original format "
                "and bitrate without conversion to MP3. YouTube typically serves "
                ".webm (Opus) or .m4a (AAC); SoundCloud serves .mp3 or .webm. "
                "Your folder will contain a mix of extensions.", wraplength=400)

        # Apply initial enabled/disabled state for the bitrate combo
        self._on_no_conversion_toggle()

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Watch List settings ────────────────────────────────────────────────
        _lbl = ttk.Label(outer, text="Watch List",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 6))
        Tooltip(_lbl, "Channels you download are automatically tracked so you "
                      "can check for new uploads later from the Watch List tab.", wraplength=360)

        wl_row = ttk.Frame(outer)
        wl_row.pack(fill="x", pady=(0, 4))

        self._auto_add_cb = ttk.Checkbutton(
            wl_row, text="Auto-add channels to Watch List after downloading",
            variable=self._auto_add_to_watchlist,
            style="S.Bold.TCheckbutton")
        self._auto_add_cb.pack(side="left")
        Tooltip(self._auto_add_cb,
                "When enabled, any channel or playlist you download is "
                "automatically added to the Watch List so you can scan "
                "it for new uploads later.")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Download Behavior ─────────────────────────────────────────────────
        beh_title_row = ttk.Frame(outer)
        beh_title_row.pack(fill="x", pady=(0, 8))
        _lbl = ttk.Label(beh_title_row, text="Download Behavior",
                  style="S.White.Section.TLabel")
        _lbl.pack(side="left")
        Tooltip(_lbl, "Options that control how DJ-CrateBuilder connects and paces "
                      "requests. These can help avoid throttling, geographic "
                      "restrictions, or IP-banning from YouTube/SoundCloud when "
                      "doing entire channel/batch downloads.", wraplength=360)
        ttk.Label(beh_title_row, text="(Experimental)",
                  style="S.Dim.TLabel").pack(side="left", padx=(8, 0))

        # Geo-bypass checkbox
        geo_row = ttk.Frame(outer)
        geo_row.pack(fill="x", pady=(0, 4))
        _cb = ttk.Checkbutton(geo_row,
                        text="Enable geo-bypass",
                        variable=self._geo_bypass,
                        style="S.Bold.TCheckbutton")
        _cb.pack(side="left")
        Tooltip(_cb, "Bypass geographic IP-based restrictions using a fake X-Forwarded-For header", wraplength=360)

        # Rotate User-Agent checkbox
        ua_row = ttk.Frame(outer)
        ua_row.pack(fill="x", pady=(0, 4))
        _cb = ttk.Checkbutton(ua_row,
                        text="Rotate User-Agent",
                        variable=self._rotate_ua,
                        style="S.Bold.TCheckbutton")
        _cb.pack(side="left")
        Tooltip(_cb, "Send a randomized browser User-Agent string (consistent within each session)", wraplength=360)

        # Sleep interval checkbox + mode selector
        sleep_row = ttk.Frame(outer)
        sleep_row.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(sleep_row,
                        text="Throttle Requests",
                        variable=self._sleep_enabled,
                        command=self._on_sleep_toggle,
                        style="S.Bold.TCheckbutton"
                        ).pack(side="left")

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
                        style="S.Bold.TCheckbutton")
        self._use_cookies_cb.pack(side="left")
        Tooltip(self._use_cookies_cb,
                "Authenticate downloads using a browser login session (increases speed).\n\n"
                "⚠ It is strongly recommended to create a dedicated/throwaway account. "
                "Chrome 127+ blocks cookie extraction (DPAPI) — use Firefox or a "
                "cookie file instead. For cookie files: install the 'Get cookies.txt "
                "LOCALLY' browser extension.", wraplength=400)

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
        self._cookie_method_combo.pack(side="left", padx=(0, 16))
        self._cookie_method_combo.bind("<<ComboboxSelected>>",
            lambda _: (self._on_cookies_toggle(), self._autosave_behavior_settings()))

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
        self._cookies_profile_entry.pack(side="left", padx=(0, 8))
        self._cookies_profile_entry.bind("<FocusOut>",
            lambda _: self._autosave_behavior_settings())
        Tooltip(self._cookies_profile_entry, "(leave blank for default)")

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

        Tooltip(self._cookie_file_entry, "Netscape/Mozilla cookie.txt format")

        howto_row = ttk.Frame(outer)
        howto_row.pack(fill="x", pady=(6, 0))
        self._howto_lbl = tk.Label(howto_row,
                 text=f"      How-To:  Setting Up a Dedicated {self._cookies_browser.get()} Profile",
                 font=("Segoe UI", 11, "bold"), fg=TEXT_DIM, bg=BG, anchor="w")
        self._howto_lbl.pack(side="left")
        self._howto_btn = tk.Button(howto_row, text="VIEW",
                  font=("Segoe UI", 8), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=8, pady=1, cursor="hand2",
                  command=self._open_cookie_howto)
        self._howto_btn.pack(side="left", padx=(10, 0))

        self._on_cookies_toggle()

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Base directory ────────────────────────────────────────────────────
        _lbl = ttk.Label(outer, text="Default Save Directory",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 8))
        Tooltip(_lbl, "All downloads are organized under this folder.  "
                      "YouTube and SoundCloud sub-folders are created automatically.", wraplength=360)

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

        self._settings_msg = ttk.Label(outer, text="", style="S.Dim.TLabel")
        self._settings_msg.pack(anchor="w", pady=(0, 4))

        # ── Folder structure preview (directly under directory) ───────────────
        ttk.Label(outer, text="Folder Structure Preview",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 10))

        self._tree_lbl = tk.Label(outer, text="", font=("Consolas", 11),
                                    fg=TEXT_DIM, bg=SURFACE2, anchor="nw",
                                    justify="left", padx=16, pady=12,
                                    highlightthickness=1,
                                    highlightbackground=BORDER)
        self._tree_lbl.pack(fill="x", pady=(0, 4))

        # ── Downloads log ─────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(8, 20))

        _lbl = ttk.Label(outer, text="Downloads Log",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 6))
        Tooltip(_lbl, "A color-coded record of every downloaded, skipped, and failed "
                      "file. View it here in the built-in viewer, or open it in your "
                      "system's default text editor.", wraplength=360)

        log_row = ttk.Frame(outer)
        log_row.pack(fill="x", pady=(0, 4))

        ttk.Button(log_row, text="📋  View Log", style="Save.TButton",
                   command=self._open_log_viewer).pack(side="left", padx=(0, 8))

        ttk.Button(log_row, text="↗  Open in System Viewer", style="LightBlue.TButton",
                   command=self._open_log_external).pack(side="left", padx=(0, 16))

        # Show the resolved log path so the user always knows where it lives
        self._log_path_lbl = ttk.Label(log_row, text="", style="S.Dim.TLabel")
        self._log_path_lbl.pack(side="left", fill="x", expand=True)
        self._refresh_log_path_label()

        # ── Debug log ─────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(8, 20))

        _lbl = ttk.Label(outer, text="Debug Log",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 6))
        Tooltip(_lbl, "Detailed diagnostic log capturing cookie configuration, "
                      "yt-dlp options, request/response data, and full error "
                      "tracebacks. Useful for troubleshooting download failures.", wraplength=360)

        dbg_row = ttk.Frame(outer)
        dbg_row.pack(fill="x", pady=(0, 4))

        ttk.Button(dbg_row, text="🔍  View Debug Log", style="Save.TButton",
                   command=self._open_debug_log_viewer).pack(side="left", padx=(0, 8))

        ttk.Button(dbg_row, text="↗  Open in System Viewer", style="LightBlue.TButton",
                   command=self._open_debug_log_external).pack(side="left", padx=(0, 16))

        self._debug_path_lbl = ttk.Label(dbg_row, text="", style="S.Dim.TLabel")
        self._debug_path_lbl.pack(side="left", fill="x", expand=True)
        self._refresh_debug_path_label()

        # ── Database management ────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(8, 20))

        _lbl = ttk.Label(outer, text="Downloads Database",
                  style="S.White.Section.TLabel")
        _lbl.pack(anchor="w", pady=(0, 6))
        Tooltip(_lbl, "The SQLite database tracks every download for fast "
                      "lookups and Watch List history. If it gets corrupted "
                      "or deleted, rebuild it from the activity log.", wraplength=360)

        db_row = ttk.Frame(outer)
        db_row.pack(fill="x", pady=(0, 4))

        self._rebuild_db_btn = ttk.Button(
            db_row, text="🔄  Rebuild Database from Log",
            style="Save.TButton",
            command=self._rebuild_db_from_log)
        self._rebuild_db_btn.pack(side="left", padx=(0, 16))
        Tooltip(self._rebuild_db_btn,
                "Re-reads every DOWNLOADED entry from the activity log "
                "and re-inserts them into the database. This is safe to "
                "run at any time — it clears and rebuilds from scratch.")

        self._db_path_lbl = ttk.Label(db_row, text="", style="S.Dim.TLabel")
        self._db_path_lbl.pack(side="left", fill="x", expand=True)
        if hasattr(self, "_db_path"):
            short = self._db_path.replace(os.path.expanduser("~"), "~")
            self._db_path_lbl.config(text=short)

        self._update_tree_preview()
        self._refresh_limit_label()

    def _settings_browse(self):
        d = filedialog.askdirectory(title="Choose base save folder",
                                     initialdir=self._settings_dir_var.get())
        if d:
            self._settings_dir_var.set(d)
            self._save_settings()

    def _update_tree_preview(self):
        base = self._settings_dir_var.get() if hasattr(self, '_settings_dir_var') \
               else self._base_dir
        short = base.replace(os.path.expanduser("~"), "~")
        lines = [f"  {short}/"]
        for plat_key in ("YouTube", "SoundCloud"):
            subdir = PLATFORMS[plat_key]["subdir"]
            pdir   = os.path.join(base, subdir)
            lines.append(f"    ├── {subdir}/")
            if os.path.isdir(pdir):
                genres = sorted(d for d in os.listdir(pdir)
                                if os.path.isdir(os.path.join(pdir, d)))
                for i, g in enumerate(genres[:8]):
                    is_last = (i == len(genres[:8]) - 1 and len(genres) <= 8)
                    prefix  = "└──" if is_last else "├──"
                    lines.append(f"    │       {prefix} {g}/")
                if len(genres) > 8:
                    lines.append(f"    │       └── … +{len(genres)-8} more")
        if hasattr(self, '_tree_lbl'):
            self._tree_lbl.config(text="\n".join(lines))

    def _save_settings(self):
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
            "bitrate_quality": self._bitrate_quality.get(),
            "no_conversion":  self._no_conversion.get(),
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
            "auto_check_hours":   self._auto_check_hours.get(),
            "run_at_startup":     self._run_at_startup.get(),
            "minimize_to_tray":   self._minimize_to_tray.get(),
            "watchlist_last_check": self._watchlist_last_check,
        })
        self._update_tree_preview()
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
            pass  # auto description labels replaced by tooltip
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
            self._howto_btn.config(
                state="normal" if enabled else "disabled",
                fg=TEXT_DIM if enabled else GREY)

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
        """Persist auto-check interval, tray, and last-check time."""
        cfg = load_config()
        cfg["auto_check_hours"] = self._auto_check_hours.get()
        cfg["minimize_to_tray"] = self._minimize_to_tray.get()
        cfg["watchlist_last_check"] = self._watchlist_last_check
        save_config(cfg)
        # Reschedule the timer whenever the interval changes.
        self._reschedule_auto_check()

    def _reschedule_auto_check(self):
        """(Re)arm the periodic auto-check timer from the current interval."""
        if self._auto_check_after_id is not None:
            try:
                self.after_cancel(self._auto_check_after_id)
            except Exception:
                pass
            self._auto_check_after_id = None
        secs = auto_check_hours_to_seconds(self._auto_check_hours.get())
        if secs is None:
            return  # 'Off' — no timer
        now = int(time.time())
        elapsed = now - (self._watchlist_last_check or 0)
        delay_ms = 1000 if elapsed >= secs else int((secs - elapsed) * 1000)
        self._auto_check_after_id = self.after(delay_ms, self._auto_check_tick)

    def _auto_check_tick(self):
        """Fire one scheduled check: scan all, then auto-download new."""
        self._auto_check_after_id = None
        secs = auto_check_hours_to_seconds(self._auto_check_hours.get())
        if secs is None:
            return
        # Skip (don't interrupt) if a manual scan/download is already running.
        if self._downloading or self._wl_download_active or self._wl_scan_active:
            self._auto_check_after_id = self.after(60_000, self._auto_check_tick)
            return
        self._watchlist_log("⏰ Scheduled auto-check starting…", "info")
        self._auto_check_poll_count = 0
        self._watchlist_scan_all()
        # Poll for scan completion, then download + notify.
        self.after(2000, self._auto_check_after_scan)

    # Cap the post-scan wait so a stuck scan can't poll forever (~5 min @ 2s).
    _AUTO_CHECK_MAX_POLLS = 150

    def _auto_check_after_scan(self):
        """Once scans settle (or we give up waiting), download new tracks + notify."""
        if self._wl_scan_active > 0:
            self._auto_check_poll_count += 1
            if self._auto_check_poll_count <= self._AUTO_CHECK_MAX_POLLS:
                self.after(2000, self._auto_check_after_scan)
                return
            # Timed out waiting — record the attempt and reschedule next cycle.
            self._watchlist_log(
                "⏰ Auto-check gave up waiting for scans to finish.", "info")
        else:
            channels = self._db.get_all_watchlist_channels()
            total_new = sum(int(c.get("pending_new_count", 0)) for c in channels)
            if total_new > 0:
                n_ch = sum(1 for c in channels if int(c.get("pending_new_count", 0)) > 0)
                self._watchlist_download_all_new()
                self._notify_tray(
                    "Watch List",
                    f"{total_new} new track(s) downloading across {n_ch} channel(s)")
            else:
                self._watchlist_log("⏰ Auto-check complete — no new tracks.", "info")
        self._watchlist_last_check = int(time.time())
        self._autosave_automation_settings()  # persists last_check + reschedules

    def _notify_tray(self, title, msg):
        """Show a tray notification if the tray is active; always log it."""
        self._watchlist_log(f"🔔 {title}: {msg}", "info")
        if self._tray_icon is not None:
            self._tray_icon.notify(msg, title)

    def _ensure_tray(self):
        """Create and start the tray icon on first hide (lazy)."""
        if self._tray_icon is not None:
            return self._tray_icon
        from cratebuilder.tray import TrayIcon
        self._tray_icon = TrayIcon(
            schedule=lambda fn: self.after(0, fn),
            on_open=self._show_from_tray,
            on_scan=self._watchlist_scan_all,
            on_quit=self._quit_app)
        if not self._tray_icon.available or not self._tray_icon.start():
            self._tray_icon = None
        return self._tray_icon

    def _hide_to_tray(self):
        """Withdraw the window; keep the app (and scheduler) running."""
        if self._ensure_tray() is not None:
            self.withdraw()
        else:
            self.iconify()  # tray unavailable — fall back to taskbar minimise

    def _show_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self):
        """Real exit: stop tray, cancel timer, destroy."""
        if self._auto_check_after_id is not None:
            try:
                self.after_cancel(self._auto_check_after_id)
            except Exception:
                pass
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self.destroy()

    def _on_window_close(self):
        """WM_DELETE handler: to tray if enabled, else real quit."""
        if self._minimize_to_tray.get() and sys.platform == "win32":
            self._hide_to_tray()
        else:
            self._quit_app()

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

    def _build_about_tab(self, parent):
        # ── Scrollable wrapper ────────────────────────────────────────────────
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="both", expand=True)

        about_sb = ttk.Scrollbar(wrapper, orient="vertical")
        about_sb.pack(side="right", fill="y")

        self._about_canvas = tk.Canvas(
            wrapper, bg=BG, bd=0, highlightthickness=0,
            yscrollcommand=about_sb.set)
        self._about_canvas.pack(side="left", fill="both", expand=True)
        about_sb.config(command=self._about_canvas.yview)

        outer = ttk.Frame(self._about_canvas, padding=(28, 28, 28, 18))
        self._about_cwin = self._about_canvas.create_window(
            (0, 0), window=outer, anchor="nw")

        outer.bind("<Configure>", lambda e:
            self._about_canvas.configure(scrollregion=self._about_canvas.bbox("all")))
        self._about_canvas.bind("<Configure>", lambda e:
            self._about_canvas.itemconfig(self._about_cwin, width=e.width))

        # ── App title + version ───────────────────────────────────────────────
        ttk.Label(outer,
                  text=f"{APP_NAME}  v{APP_VERSION}",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 20))

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(0, 24))

        # ── Info rows (driven by ABOUT_FIELDS at top of file) ─────────────────
        for label, value in ABOUT_FIELDS:
            row = ttk.Frame(outer)
            row.pack(fill="x", pady=(0, 14))
            tk.Label(row, text=label, font=("Segoe UI", 12, "bold"),
                      fg=TEXT, bg=BG, width=14, anchor="w"
                      ).pack(side="left")
            val_lbl = tk.Label(row, text=value, font=("Segoe UI", 11),
                      fg=TEXT, bg=BG, anchor="w")
            val_lbl.pack(side="left", padx=(8, 0))

            # Make the GitHub row clickable
            if label == "GitHub":
                val_lbl.config(fg=LINK_COL, cursor="hand2")
                val_lbl.bind("<Button-1>",
                    lambda _e: webbrowser.open(GITHUB_URL))
                val_lbl.bind("<Enter>",
                    lambda _e, w=val_lbl: w.config(
                        font=("Segoe UI", 11, "underline")))
                val_lbl.bind("<Leave>",
                    lambda _e, w=val_lbl: w.config(
                        font=("Segoe UI", 11)))
                Tooltip(val_lbl,
                        "Click to open the DJ-CrateBuilder GitHub page — "
                        "check here for updates and releases.")

        # ── FAQ ───────────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(20, 20))

        faq_hdr = ttk.Frame(outer)
        faq_hdr.pack(fill="x", pady=(0, 16))
        ttk.Label(faq_hdr, text="Frequently Asked Questions",
                  style="White.Section.TLabel").pack(side="left")

        self._github_btn = tk.Button(
            faq_hdr, text="  ↗  View on GitHub — Updates & Releases  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=LINK_COL,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
            command=lambda: webbrowser.open(GITHUB_URL))
        self._github_btn.pack(side="right")
        Tooltip(self._github_btn,
                "Opens the DJ-CrateBuilder GitHub page in your browser. "
                "Check here for the latest releases and update notes.")

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
             "VPN or datacenter IP addresses. Try disconnecting your VPN, switching to a different VPN server, or "
             "waiting a while before retrying."),

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

            ("Q: What is the download log?",
             "A: A text file that records every downloaded, skipped, and failed file with timestamps. It lives in "
             "your base save directory as \"activity.log\" and can be viewed with the built-in log viewer "
             "in the Settings tab."),

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
        ]

        for question, answer in faq:
            tk.Label(outer, text=question,
                     font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG,
                     anchor="w", justify="left", wraplength=660
                     ).pack(fill="x", pady=(0, 4))
            tk.Label(outer, text=answer,
                     font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG,
                     anchor="w", justify="left", wraplength=660
                     ).pack(fill="x", pady=(0, 16))

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
        safe = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
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
        self._update_tree_preview()

    def _update_save_preview(self):
        """Show a short preview of where files will land."""
        genre = self._genre_var.get()
        path  = self._resolve_save_dir(genre)
        short = path.replace(os.path.expanduser("~"), "~")
        if hasattr(self, "_save_dir_preview"):
            self._save_dir_preview.config(text=f"→  {short}")

    # ── Platform switching ────────────────────────────────────────────────────
    def _switch_platform(self, name):
        """No-op — platform is now auto-detected from the URL."""
        pass

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
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self._refresh_genre_list()

    def _set_placeholder(self, text=None):
        """Replace placeholder text without disturbing a real URL."""
        ph = text or "https://www.youtube.com/  or  https://soundcloud.com/"
        if self._ph_active:
            self._url_entry.config(state="normal")
            self._url_entry.delete(0, "end")
            self._url_entry.insert(0, ph)
            self._url_entry.config(foreground=TEXT_DIM)
            self._ph_active = True

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
        # Remove if already present, then prepend
        self._url_history = [u for u in self._url_history if u != url]
        self._url_history.insert(0, url)
        self._url_history = self._url_history[:6]
        # Update the combobox dropdown values
        self._url_entry["values"] = self._url_history
        # Persist to config
        cfg = load_config()
        cfg["url_history"] = self._url_history
        save_config(cfg)

    @staticmethod
    def _detect_platform(url):
        """Return 'SoundCloud' or 'YouTube' based on the URL."""
        if re.search(r'soundcloud\.com', url, re.IGNORECASE):
            return "SoundCloud"
        return "YouTube"

    @staticmethod
    def _normalize_url(url):
        """Append /videos to bare YouTube channel URLs so yt-dlp fetches
        the full video list instead of the channel's featured page."""
        # Match youtube.com/@ChannelName with no trailing path segment
        if re.match(r'https?://(www\.)?youtube\.com/@[^/]+/?$', url):
            url = url.rstrip("/") + "/videos"
        return url

    # ── Dep check ─────────────────────────────────────────────────────────────
    def _check_deps_async(self):
        def _run():
            missing = check_dependencies()
            if missing:
                self.after(0, lambda: self._prompt_install(missing))
            else:
                self.after(0, lambda: self._set_status("✓ Ready"))
        threading.Thread(target=_run, daemon=True).start()

    def _prompt_install(self, missing):
        if messagebox.askyesno("Missing Dependencies",
                               f"Missing: {', '.join(missing)}\n\nInstall now?"):
            self._do_install(missing)
        else:
            self._set_status("⚠  yt-dlp not installed — run: pip install yt-dlp")

    def _do_install(self, pkgs):
        self._set_status(f"Installing {', '.join(pkgs)}…")
        def _run():
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install"] + pkgs,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.after(0, lambda: self._set_status("✓ Installed. Ready."))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"✗ Install failed: {e}"))
        threading.Thread(target=_run, daemon=True).start()

    def _set_status(self, msg):
        self._status_var.set(msg)

    # ── Queue UI ──────────────────────────────────────────────────────────────
    def _clear_queue(self):
        self._qtxt.config(state="normal")
        self._qtxt.delete("1.0", "end")
        self._qtxt.config(state="disabled")
        self._queue.clear()
        self._qcount_lbl.config(text="")

    def _build_queue_ui(self, entries, item_word="item"):
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
        line = self._format_queue_line(idx, "○", title)
        tag  = f"row_{idx}"
        # Insert without switching state (caller manages state)
        self._qtxt.insert("end", line, ("q_pending", tag))
        self._queue.append({"title": title, "state": ST_PENDING,
                             "bitrate": "", "note": ""})

    def _set_row_state(self, idx, state, note=""):
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
        self._cur_lbl.config(text="Preparing batch…")
        self._ov_lbl.config(text="")
        self._ov_stats_lbl.config(text="")
        self._speed_lbl.config(text="")
        self._grand_dl = 0
        self._grand_sk = 0
        self._grand_er = 0
        self._last_fatal_error = None
        self._batch_start = __import__("time").time()
        self._clear_queue()
        self._set_status(f"Starting batch of {len(run_batch)} URL(s)…")

        threading.Thread(target=self._batch_worker,
                          args=(run_batch,),
                          daemon=True).start()

    def _cancel(self):
        self._cancel_flag.set()
        self._pause_flag.clear()   # unblock worker so it can see the cancel
        self._cancel_btn.config(state="disabled", style="Cancel.TButton")
        self._pause_btn.config(state="disabled")
        self._set_status("Cancelling after current track…")

    def _toggle_pause(self):
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
        import time
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

                # Highlight the active batch row
                self.after(0, lambda i=url_idx: self._batch_highlight(i))

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
            self.after(0, self._update_tree_preview)
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
                # Refresh the Watch List UI if it exists
                self.after(200, self._watchlist_refresh)
            self._wl_download_active = False
            self.after(0, self._finish)

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
            if self._use_cookies.get():
                method = self._cookie_method.get()
                if method == "Cookie File":
                    cfile = self._cookie_file.get().strip()
                    if cfile and os.path.exists(cfile):
                        meta_opts["cookiefile"] = cfile
                else:
                    browser = self._cookies_browser.get().lower()
                    profile = self._cookies_profile.get().strip()
                    meta_opts["cookiesfrombrowser"] = (
                        (browser, profile) if profile
                        else (browser,)
                    )

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
                collection_name = info.get("title") or info.get("uploader") or ""
                # Strip "- Videos" suffix so the folder is just the channel name
                if collection_name.endswith(" - Videos"):
                    collection_name = collection_name[:-9].strip()
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

                safe          = re.sub(r'[\\/*?:"<>|]', "_", item_title)
                expected_path = os.path.join(save_dir, safe + ".mp3")

                # ── Skip / re-download logic ──────────────────────────────────
                if self._skip_existing.get():
                    mode        = self._skip_mode.get()
                    found_path  = self._file_exists_on_disk(save_dir, item_title)
                    file_exists = found_path is not None
                    video_id    = entry.get("id")
                    in_db       = self._db.is_video_downloaded(video_id)

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

                    if should_skip:
                        if in_db and not file_exists and mode in (
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
                        method = self._cookie_method.get()
                        if method == "Cookie File":
                            cfile = self._cookie_file.get().strip()
                            if cfile and os.path.exists(cfile):
                                probe_opts["cookiefile"] = cfile
                        else:
                            browser = self._cookies_browser.get().lower()
                            profile = self._cookies_profile.get().strip()
                            probe_opts["cookiesfrombrowser"] = (
                                (browser, profile) if profile
                                else (browser,)
                            )
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
                if using_cookies:
                    method = self._cookie_method.get()
                    if method == "Cookie File":
                        cfile = self._cookie_file.get().strip()
                        if cfile and os.path.exists(cfile):
                            ydl_opts["cookiefile"] = cfile
                    else:
                        browser = self._cookies_browser.get().lower()
                        profile = self._cookies_profile.get().strip()
                        ydl_opts["cookiesfrombrowser"] = (
                            (browser, profile) if profile
                            else (browser,)
                        )

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
                    import time as _t
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
                                _t.sleep(_delay)
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
                        bitrate=src_str)
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
                                bitrate=src_str)
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
                    _max_upload_date_this_run)

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
    # ██  WATCH LIST TAB  ██
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
                                       bg=SURFACE2, fg=TEXT_DIM)
        except Exception:
            pass
        if self._wl_scan_active > 0:
            self._watchlist_log("Cancelling scans…", "info")

    def _wl_update_cancel_btn_state(self):
        """Enable Watch List Cancel button if any download or scan is active."""
        try:
            active = self._downloading or self._wl_scan_active > 0
            if active:
                self._wl_cancel_btn.config(
                    state="normal", bg=YT_DARK, fg=TEXT)
            else:
                self._wl_cancel_btn.config(
                    state="disabled", bg=SURFACE2, fg=TEXT_DIM)
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
                 fg=WL_PURPLE, bg=BG).pack(side="left", padx=(0, 10))
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
            bg=WL_DARK, fg=TEXT,
            activebackground=WL_PURPLE, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            command=self._watchlist_open_add_dialog)
        self._wl_add_btn.pack(side="left", padx=(0, 6))

        self._wl_scan_all_btn = tk.Button(
            toolbar, text="  🔍  Scan All  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=TEXT_MED,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            command=self._watchlist_scan_all)
        self._wl_scan_all_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_scan_all_btn,
                "Check every channel for new uploads since the last scan.")

        self._wl_fix_btn = tk.Button(
            toolbar, text="  🛠  Fix Channels  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=TEXT_MED,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            command=self._watchlist_fix_broken)
        self._wl_fix_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_fix_btn,
                "Look up the real YouTube channel for any folder that still "
                "needs one, so it can be scanned. Shows the top matches to "
                "choose from.")

        self._wl_dl_all_btn = tk.Button(
            toolbar, text="  ⬇  Download All New (0)  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=TEXT_MED,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            command=self._watchlist_download_all_new)
        self._wl_dl_all_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_dl_all_btn,
                "Download all pending new tracks across every channel.")

        self._wl_cancel_btn = tk.Button(
            toolbar, text="  ✕  Cancel  ",
            font=("Segoe UI", 10, "bold"),
            bg=SURFACE2, fg=TEXT_DIM,
            activebackground=YT_DARK, activeforeground=TEXT,
            disabledforeground=TEXT_DIM,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            state="disabled",
            command=self._cancel_all_updates)
        self._wl_cancel_btn.pack(side="left", padx=(0, 6))
        Tooltip(self._wl_cancel_btn,
                "Stop all in-progress Watch List scans and downloads.")

        # ── Resizable split:  scrollable cards (top)  /  pinned log (bottom) ──
        paned = tk.PanedWindow(
            parent, orient="vertical", bg=BORDER, sashwidth=6, sashpad=0,
            bd=0, relief="flat", opaqueresize=True)
        paned.pack(side="top", fill="both", expand=True)
        self._wl_paned = paned

        # Pane 1 — scrollable channel cards.
        cards_area = tk.Frame(paned, bg=BG)
        wl_sb = ttk.Scrollbar(cards_area, orient="vertical")
        wl_sb.pack(side="right", fill="y")
        self._wl_canvas = tk.Canvas(
            cards_area, bg=BG, bd=0, highlightthickness=0,
            yscrollcommand=wl_sb.set)
        self._wl_canvas.pack(side="left", fill="both", expand=True)
        wl_sb.config(command=self._wl_canvas.yview)

        outer = ttk.Frame(self._wl_canvas, padding=(28, 14, 28, 18))
        self._wl_cwin = self._wl_canvas.create_window(
            (0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda e:
            self._wl_canvas.configure(scrollregion=self._wl_canvas.bbox("all")))
        self._wl_canvas.bind("<Configure>", lambda e:
            self._wl_canvas.itemconfig(self._wl_cwin, width=e.width))

        self._wl_cards_frame = tk.Frame(outer, bg=BG)
        self._wl_cards_frame.pack(fill="x", pady=(0, 4))

        # Pane 2 — scan log, pinned at the bottom and always visible.
        log_frame = ttk.Frame(paned, padding=(28, 6, 28, 10))

        tk.Frame(log_frame, height=1, bg=BORDER).pack(fill="x", pady=(0, 8))
        tk.Label(log_frame, text="Scan Log", font=("Segoe UI", 10, "bold"),
                 fg=TEXT_DIM, bg=BG, anchor="w").pack(anchor="w", pady=(0, 4))

        log_wrap = tk.Frame(log_frame, bg=BG)
        log_wrap.pack(fill="both", expand=True)
        log_sb = ttk.Scrollbar(log_wrap, orient="vertical")
        log_sb.pack(side="right", fill="y")
        self._wl_log_txt = tk.Text(
            log_wrap, height=6, font=("Consolas", 9),
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
        self._wl_log_txt.tag_configure("info", foreground=WL_PURPLE)

        # Assemble the split: cards stretch to fill, log keeps its size.
        paned.add(cards_area, stretch="always", minsize=140)
        paned.add(log_frame, stretch="never", minsize=90)
        # Place the divider so the log starts ~150px tall once we know height.
        self.after(120, self._wl_init_log_sash)

        # Populate on first load
        self._watchlist_refresh()

    def _wl_init_log_sash(self):
        """Position the cards/log divider so the pinned scan log starts about
        150px tall. The user can drag it from there."""
        try:
            paned = self._wl_paned
            paned.update_idletasks()
            h = paned.winfo_height()
            if h > 260:
                paned.sash_place(0, 0, h - 150)
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

    def _watchlist_refresh(self):
        """Rebuild all channel cards from the database."""
        # Clear existing cards
        for w in self._wl_cards_frame.winfo_children():
            w.destroy()

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

        # Update the Download All button count
        total_pending = self._db.get_total_pending_count()
        self._wl_dl_all_btn.config(
            text=f"  ⬇  Download All New ({total_pending})  ")

    def _watchlist_build_channel_card(self, parent, ch):
        """Build one dark card for a watchlist channel entry."""
        cid = ch["id"]

        card = tk.Frame(parent, bg=SURFACE, padx=14, pady=10,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", pady=(0, 8))

        # ── Row 1: Name + platform/genre ──────────────────────────────────
        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x")

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
            st_color = WL_PURPLE
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

        last_scan = format_timestamp_relative(ch.get("last_scanned_timestamp"))
        cutoff_readable = format_yyyymmdd_readable(ch.get("scan_cutoff_date", ""))
        details = (f"Last scan: {last_scan}  •  "
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
        if is_unresolved_channel(ch):
            # Only unresolved channels need their link healed.
            card_buttons.append(
                ("🛠 Fix Link", lambda c=cid: self._watchlist_resolve_dialog(c), False))
        card_buttons += [
            ("🔍 Scan",    lambda c=cid: self._watchlist_scan_channel(c), False),
            (f"⬇ Download New ({pending})",
                           lambda c=cid: self._watchlist_download_new(c), False),
            ("✏ Edit",     lambda c=cid: self._watchlist_edit_channel(c), False),
            ("✕ Remove",   lambda c=cid: self._watchlist_remove_channel(c), False),
        ]
        for btn_text, btn_cmd, is_cancel in card_buttons:
            b = tk.Button(btns, text=btn_text,
                          font=("Segoe UI", 9),
                          bg=(YT_DARK if is_cancel else SURFACE2),
                          fg=(TEXT if is_cancel else TEXT_DIM),
                          activebackground=(YT_RED if is_cancel else BORDER),
                          activeforeground=TEXT,
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
        return is_unresolved_channel(ch)

    def _resolve_channel_via_search(self, name, max_results=3):
        """Search YouTube for a channel by display name. Returns up to
        max_results candidate dicts {title, channel_id, url, handle,
        followers}. Raises on network/extractor failure."""
        import urllib.parse
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
        m = re.search(r"/channel/(UC[\w-]+)", url or "")
        return m.group(1) if m else None

    def _persist_resolved_channel(self, ch, channel_id, handle="", url=None):
        """Commit a resolved identity: update the watchlist row to a usable URL
        + channel_id (status idle), and stamp the channel folder's
        cratebuilder.json so it never needs resolving again.

        `url` lets a caller keep a specific URL (e.g. a playlist the user
        chose to track) instead of the canonical /channel/UC… form; when None
        we derive the canonical channel URL from the id."""
        store_url = url or channel_url_from_id(channel_id)
        self._db.update_watchlist_channel_fields(
            ch["id"], url=store_url, channel_id=channel_id,
            status="idle", last_error=None)
        # Stamp the existing folder (genre+name round-trips to the same path).
        try:
            folder = self._resolve_save_dir(
                ch.get("genre") or "(none)", ch.get("display_name"),
                platform="YouTube")
            write_channel_sidecar(
                folder, channel_id=channel_id, channel_url=store_url,
                handle=handle, display_name=ch.get("display_name"),
                platform="YouTube", genre=ch.get("genre") or "(none)")
        except Exception as e:
            self._dbg.warning(f"WL RESOLVE | sidecar write failed: {e}")
        self._dbg.info(
            f"WL RESOLVE OK | {ch.get('display_name')!r} → {channel_id}")

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
            self._persist_resolved_channel(ch, direct)
            self._watchlist_log(f"Channel set: {ch['display_name']}", "ok")
            self._watchlist_refresh()
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
                    self.after(0, lambda: (
                        self._persist_resolved_channel(
                            ch, ucid, handle, url=new_url),
                        self._watchlist_log(
                            f"Resolved channel id for {ch['display_name']}.",
                            "ok"),
                        self._watchlist_refresh()))
            except Exception as ex:
                msg = str(ex)[:80]
                self.after(0, lambda: self._watchlist_log(
                    f"Couldn't resolve id for {ch['display_name']}: {msg}",
                    "err"))

        threading.Thread(target=_bg, daemon=True).start()

    def _watchlist_resolve_dialog(self, cid, on_done=None):
        """Show the top-3 YouTube matches for a channel so the user can pick
        the right one (or paste a URL manually). on_done(resolved: bool) is
        called when the dialog closes — used to chain the batch 'Fix' flow."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            if on_done:
                on_done(False)
            return

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
                              font=("Segoe UI", 10), fg=WL_PURPLE, bg=BG)
        status_lbl.pack(anchor="w", pady=(0, 8))

        results_frame = tk.Frame(outer, bg=BG)
        results_frame.pack(fill="both", expand=True)

        choice_var = tk.StringVar(value="")
        manual_var = tk.StringVar()
        cand_by_id = {}   # channel_id -> candidate dict (for handle lookup)

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
                    tk.Label(row, text=f"    {subs}  •  {c['url']}",
                             font=("Segoe UI", 8), fg=TEXT_DIM, bg=SURFACE,
                             anchor="w").pack(anchor="w")
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

        def _search():
            try:
                cands = self._resolve_channel_via_search(ch["display_name"])
                dlg.after(0, lambda: _render(cands))
            except Exception as ex:
                msg = str(ex)[:120]
                dlg.after(0, lambda: (status_lbl.config(
                    text=f"Search failed: {msg}", fg=YT_RED), _render([])))

        threading.Thread(target=_search, daemon=True).start()

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
                    self._persist_resolved_channel(ch, cid_direct)
                    self._watchlist_log(
                        f"Resolved (manual): {ch['display_name']}", "ok")
                    _close(True)
                    return
                # Need to look the URL up to get its channel_id.
                status_lbl.config(text="Resolving pasted URL…", fg=WL_PURPLE)

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
                            dlg.after(0, lambda: (
                                self._persist_resolved_channel(ch, cid2, handle),
                                self._watchlist_log(
                                    f"Resolved (manual): {ch['display_name']}",
                                    "ok"),
                                _close(True)))
                        else:
                            dlg.after(0, lambda: status_lbl.config(
                                text="Couldn't read a channel_id from that URL.",
                                fg=YT_RED))
                    except Exception as ex:
                        m = str(ex)[:120]
                        dlg.after(0, lambda: status_lbl.config(
                            text=f"Lookup failed: {m}", fg=YT_RED))

                threading.Thread(target=_lookup, daemon=True).start()
                return
            # A search candidate was chosen.
            handle = (cand_by_id.get(sel) or {}).get("handle", "")
            self._persist_resolved_channel(ch, sel, handle)
            self._watchlist_log(f"Resolved: {ch['display_name']}", "ok")
            _close(True)

        tk.Button(btn_row, text="  ✓ Use This Channel  ",
                  font=("Segoe UI", 10, "bold"), bg=WL_DARK, fg=TEXT,
                  activebackground=WL_PURPLE, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=_confirm).pack(side="left")
        tk.Button(btn_row, text="  Skip  ",
                  font=("Segoe UI", 10), bg=SURFACE2, fg=TEXT_DIM,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                  command=lambda: _close(False)).pack(side="left", padx=(8, 0))

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

        def _next(i):
            if i >= len(order):
                self._watchlist_log("Channel fix pass complete.", "ok")
                self._watchlist_refresh()
                return
            self._watchlist_resolve_dialog(order[i], on_done=lambda _r: _next(i + 1))

        _next(0)

    # ── Add Channel dialog ────────────────────────────────────────────────────
    def _watchlist_open_add_dialog(self):
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
                    title = info.get("title") or info.get("uploader") or ""
                    if title.endswith(" - Videos"):
                        title = title[:-9].strip()
                    dlg.after(0, lambda: name_var.set(title or raw_url))
                except Exception:
                    dlg.after(0, lambda: name_var.set(""))
            threading.Thread(target=_do, daemon=True).start()

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
                plat = "YouTube"
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
                platform="YouTube", genre=genre,
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
                  bg=WL_DARK, fg=TEXT,
                  activebackground=WL_PURPLE, activeforeground=TEXT,
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
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return

        dlg = tk.Toplevel(self)
        dlg.title(f"Edit — {ch['display_name']}")
        dlg.geometry("460x420")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - 460) // 2
        py = self.winfo_y() + (self.winfo_height() - 420) // 2
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
        if existing_url.startswith("unresolved://"):
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
                 justify="left").pack(anchor="w", pady=(0, 12))

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
                  bg=WL_DARK, fg=TEXT,
                  activebackground=WL_PURPLE, activeforeground=TEXT,
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
            self._watchlist_log(
                f"“{ch['display_name']}” needs its YouTube channel resolved "
                f"first — click Resolve (or “Fix Channels”).", "err")
            self._watchlist_refresh()
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
        self._watchlist_refresh()
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
                if self._use_cookies.get():
                    method = self._cookie_method.get()
                    if method == "Cookie File":
                        cfile = self._cookie_file.get().strip()
                        if cfile and os.path.exists(cfile):
                            scan_opts["cookiefile"] = cfile
                    else:
                        browser = self._cookies_browser.get().lower()
                        profile = self._cookies_profile.get().strip()
                        scan_opts["cookiesfrombrowser"] = (
                            (browser, profile) if profile
                            else (browser,))

                url = ch["url"]
                # Ensure we're hitting the /videos tab for channels
                if "youtube.com" in url and "/videos" not in url:
                    if url.rstrip("/").split("/")[-1].startswith("@"):
                        url = url.rstrip("/") + "/videos"

                # URL-encode the path so handles containing spaces (e.g.
                # "@BASS ENTITY") aren't truncated by yt-dlp at the first
                # whitespace, which otherwise produces a 404 from YouTube.
                parsed = urllib.parse.urlsplit(url)
                url = urllib.parse.urlunsplit(parsed._replace(
                    path=urllib.parse.quote(parsed.path, safe="/@&")))

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
                        platform="YouTube")
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
                new_entries = []
                backfill_rows = []
                now_ts = int(time.time())
                for e in entries:
                    vid_id = e.get("id")
                    if vid_id and self._db.is_video_downloaded(vid_id):
                        continue
                    title = e.get("title") or ""
                    key = normalize_track_key(title)
                    if key and key in folder_keys:
                        # Already on disk (legacy). Record it so future scans
                        # dedup exactly, then hide it from the "new" list.
                        if vid_id:
                            backfill_rows.append({
                                "video_id":     vid_id,
                                "title":        title,
                                "channel_name": ch.get("display_name") or "",
                                "channel_url":  ch.get("url") or "",
                                "channel_id":   ch.get("channel_id"),
                                "platform":     "YouTube",
                                "genre":        ch.get("genre") or "(none)",
                                "file_path":    folder_keys[key],
                                "upload_date":  e.get("upload_date") or "",
                                "ts":           now_ts,
                                "bitrate":      "",
                            })
                        continue
                    new_entries.append({
                        "id":          vid_id or "",
                        "title":       title,
                        "url":         (e.get("url") or e.get("webpage_url")
                                        or f"https://www.youtube.com/watch?v={vid_id}"),
                        "upload_date": e.get("upload_date") or "",
                    })

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

            self.after(0, self._watchlist_refresh)

        threading.Thread(target=_do_scan, daemon=True).start()

    # ── Scan all channels ─────────────────────────────────────────────────────
    def _watchlist_scan_all(self):
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
        threading.Thread(target=_do_all, daemon=True).start()

    # ── Download new for one channel ──────────────────────────────────────────
    def _watchlist_download_new(self, cid):
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

        # Build batch items that will be processed by the existing pipeline
        platform = ch.get("platform", "YouTube")
        genre = ch.get("genre", "(none)")
        run_batch = []
        for entry in pending:
            run_batch.append({
                "url":          entry.get("url", ""),
                "genre":        genre,
                "platform":     platform,
                "channel_name": ch["display_name"],
                "title":        entry.get("title", ""),
            })

        # Set up the watchlist batch context for cleanup in _batch_worker
        self._active_watchlist_batch = {
            "channel_ids": [cid],
        }

        self._watchlist_log(
            f"Downloading {len(run_batch)} new track{'s' if len(run_batch) != 1 else ''} "
            f"from {ch['display_name']}…", "info")

        # Switch to Main tab and kick off the batch
        # Stay on the Watch List tab — the download runs as background activity
        # and reports into the Watch List scan log (mirrored from the batch
        # worker via _wl_dl_log) instead of switching to the Main tab.
        self._wl_download_active = True
        self._downloading = True
        self._cancel_flag.clear()
        self._pause_flag.clear()
        self._dl_btn.config(state="disabled")
        self._batch_add_btn.config(state="disabled")
        self._url_entry.config(state="disabled")
        self._cancel_btn.config(state="normal", style="CancelActive.TButton")
        self._pause_btn.config(state="normal", text="⏸  Pause",
                               style="Pause.TButton")
        self._wl_update_cancel_btn_state()
        self._vid_progress["value"]     = 0
        self._overall_progress["value"] = 0
        self._cur_lbl.config(text="Preparing Watch List batch…")
        self._ov_lbl.config(text="")
        self._ov_stats_lbl.config(text="")
        self._speed_lbl.config(text="")
        self._grand_dl = 0
        self._grand_sk = 0
        self._grand_er = 0
        self._last_fatal_error = None
        self._batch_start = time.time()
        self._clear_queue()
        self._set_status(f"Watch List: downloading {len(run_batch)} new tracks…")

        threading.Thread(target=self._batch_worker,
                          args=(run_batch,), daemon=True).start()
        # Rebuild cards so the downloading channel shows a Cancel button.
        self._watchlist_refresh()

    # ── Download all new across all channels ──────────────────────────────────
    def _watchlist_download_all_new(self):
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
            if ch.get("pending_new_count", 0) == 0:
                continue
            pending = json.loads(ch.get("pending_entries_json", "[]"))
            platform = ch.get("platform", "YouTube")
            genre = ch.get("genre", "(none)")
            for entry in pending:
                run_batch.append({
                    "url":          entry.get("url", ""),
                    "genre":        genre,
                    "platform":     platform,
                    "channel_name": ch["display_name"],
                    "title":        entry.get("title", ""),
                })
            channel_ids.append(ch["id"])

        if not run_batch:
            messagebox.showinfo(
                "Nothing to Download",
                "No new tracks pending across any channels.\nTry Scan All first.",
                parent=self)
            return

        self._active_watchlist_batch = {
            "channel_ids": channel_ids,
        }

        self._watchlist_log(
            f"Downloading {len(run_batch)} new tracks across "
            f"{len(channel_ids)} channels…", "info")

        # Stay on the Watch List tab — the download runs as background activity
        # and reports into the Watch List scan log (mirrored from the batch
        # worker via _wl_dl_log) instead of switching to the Main tab.
        self._wl_download_active = True
        self._downloading = True
        self._cancel_flag.clear()
        self._pause_flag.clear()
        self._dl_btn.config(state="disabled")
        self._batch_add_btn.config(state="disabled")
        self._url_entry.config(state="disabled")
        self._cancel_btn.config(state="normal", style="CancelActive.TButton")
        self._pause_btn.config(state="normal", text="⏸  Pause",
                               style="Pause.TButton")
        self._wl_update_cancel_btn_state()
        self._vid_progress["value"]     = 0
        self._overall_progress["value"] = 0
        self._cur_lbl.config(text="Preparing Watch List batch…")
        self._ov_lbl.config(text="")
        self._ov_stats_lbl.config(text="")
        self._speed_lbl.config(text="")
        self._grand_dl = 0
        self._grand_sk = 0
        self._grand_er = 0
        self._last_fatal_error = None
        self._batch_start = time.time()
        self._clear_queue()
        self._set_status(f"Watch List: downloading {len(run_batch)} new tracks…")

        threading.Thread(target=self._batch_worker,
                          args=(run_batch,), daemon=True).start()
        # Rebuild cards so the downloading channel(s) show a Cancel button.
        self._watchlist_refresh()

    # ── Auto-add after a normal channel download ──────────────────────────────
    def _watchlist_auto_add_if_enabled(self, url, display_name, genre,
                                        max_upload_date):
        """Called from _process_one_url after a successful collection download.
        If auto-add is enabled, adds the channel to the watchlist (or updates
        the cutoff if it already exists)."""
        if not self._auto_add_to_watchlist.get():
            return

        # Determine the cutoff: use the max upload date from this run
        # (minus buffer), or fall back to today
        if max_upload_date:
            cutoff = subtract_days_from_yyyymmdd(
                max_upload_date, WATCHLIST_CUTOFF_BUFFER_DAYS)
        else:
            cutoff = today_yyyymmdd()

        # Try to add; if duplicate, just update the cutoff
        result = self._db.add_watchlist_channel(
            url=url, display_name=display_name,
            platform="YouTube", genre=genre or "(none)",
            scan_cutoff_date=cutoff, auto_added=True)

        if result is None:
            # Already exists — update the cutoff if newer
            existing = self._db.get_watchlist_channel_by_url(url)
            if existing and cutoff > existing.get("scan_cutoff_date", ""):
                self._db.update_watchlist_cutoff(url, cutoff)
                self._dbg.info(
                    f"WL AUTO-UPDATE | {display_name!r}  "
                    f"cutoff updated to {cutoff}")
        else:
            self._dbg.info(
                f"WL AUTO-ADD | {display_name!r}  cutoff={cutoff}")

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

        yt_dir = self._platform_dir("YouTube")
        if not os.path.isdir(yt_dir):
            return

        added = 0
        for genre_dir in sorted(os.listdir(yt_dir)):
            genre_path = os.path.join(yt_dir, genre_dir)
            if not os.path.isdir(genre_path):
                continue
            genre = "(none)" if genre_dir == "_No Genre" else genre_dir

            for channel_dir in sorted(os.listdir(genre_path)):
                channel_path = os.path.join(genre_path, channel_dir)
                if not os.path.isdir(channel_path):
                    continue

                count, newest = scan_folder_newest_mp3(channel_path)
                if newest:
                    cutoff = subtract_days_from_yyyymmdd(
                        newest, WATCHLIST_CUTOFF_BUFFER_DAYS)
                else:
                    cutoff = today_yyyymmdd()

                # Prefer the folder's own cratebuilder.json sidecar — it holds
                # the canonical channel_id, so the URL is correct and the
                # channel is immediately scannable. Folder name is only a
                # display label, NOT a valid YouTube handle, so we never build
                # a scan URL from it.
                sc = read_channel_sidecar(channel_path)
                if sc and sc.get("channel_id"):
                    real_url = (sc.get("channel_url")
                                or channel_url_from_id(sc["channel_id"]))
                    result = self._db.add_watchlist_channel(
                        url=real_url,
                        channel_id=sc["channel_id"],
                        display_name=sc.get("display_name") or channel_dir,
                        platform="YouTube",
                        genre=genre,
                        scan_cutoff_date=cutoff,
                        auto_added=True,
                        status="idle")
                    status_note = "from sidecar"
                else:
                    # No sidecar: we genuinely don't know the real handle.
                    # Park it as needs_resolve with a unique sentinel URL so
                    # the UNIQUE constraint holds and no bogus 404 URL is ever
                    # scanned. The "Fix broken channels" pass resolves it.
                    sentinel = f"unresolved://YouTube/{genre}/{channel_dir}"
                    result = self._db.add_watchlist_channel(
                        url=sentinel,
                        display_name=channel_dir,
                        platform="YouTube",
                        genre=genre,
                        scan_cutoff_date=cutoff,
                        auto_added=True,
                        status="needs_resolve")
                    status_note = "needs_resolve"

                if result is not None:
                    added += 1
                    self._dbg.info(
                        f"WL FOLDER-POPULATE | {channel_dir!r}  "
                        f"genre={genre}  cutoff={cutoff}  ({status_note})")

        if added:
            self._watchlist_log(
                f"Populated {added} channel(s) from existing folders", "ok")
            self._watchlist_refresh()

    # ══════════════════════════════════════════════════════════════════════════
    # ██  REBUILD  ██
    # ══════════════════════════════════════════════════════════════════════════

    def _rebuild_db_from_log(self):
        """Rebuild the downloads table from the activity log."""
        ok = messagebox.askyesno(
            "Rebuild Database",
            "This will clear the downloads index and rebuild it by\n"
            "re-reading every DOWNLOADED entry from the activity log.\n\n"
            "Your Watch List channels will NOT be affected.\n"
            "Your actual downloaded files will NOT be touched.\n\n"
            "Continue?",
            parent=self)
        if not ok:
            return

        entries = parse_activity_log_entries(self._log_path)
        self._db.clear_all_downloads()
        count = 0
        for e in entries:
            self._db.add_download(
                video_id=None,
                title=e.get("title", ""),
                channel_name=e.get("channel_name", ""),
                channel_url="",
                platform=e.get("platform", "YouTube"),
                genre=e.get("genre", "(none)"),
                file_path=e.get("file_path", ""),
                upload_date=e.get("log_date", ""),
                bitrate=e.get("quality", ""))
            count += 1

        self._db.refresh_watchlist_totals()
        messagebox.showinfo(
            "Rebuild Complete",
            f"Imported {count} download records from the activity log.",
            parent=self)
        self._dbg.info(f"DB REBUILD | imported {count} entries from log")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = MP3DownloaderApp()
    app.mainloop()

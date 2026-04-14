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

# ══════════════════════════════════════════════════════════════════════════════
# ██  VERSION & ABOUT  ██  ── Edit these values to update the app info ──────
# ══════════════════════════════════════════════════════════════════════════════
APP_NAME    = "DJ-CrateBuilder"
APP_VERSION = "1.2"

ABOUT_CREATED_BY  = "CorruptSintax@Gmail.com"
ABOUT_DESCRIPTION = "Vibe-Coded entirely with Claude-AI"

# ── Add or remove lines below to customize the About tab content. ──────────
# ── Each tuple is  ("Label", "Value")  and will display as a row. ──────────
ABOUT_FIELDS = [
    ("Application",  f"{APP_NAME}  v{APP_VERSION}"),
    ("Created by",   ABOUT_CREATED_BY),
    ("Built with",   ABOUT_DESCRIPTION),
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

# Platform accent colours
YT_RED    = "#ff3b3b"
YT_DARK   = "#cc2222"
SC_ORANGE = "#ff5500"
SC_DARK   = "#cc4400"

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

# ── Config persistence ────────────────────────────────────────────────────────
CONFIG_NAME = ".dj_cratebuilder_config.json"

def _config_path():
    return os.path.join(os.path.expanduser("~"), CONFIG_NAME)

def load_config():
    p = _config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Migrate from old config name if it exists
    old_p = os.path.join(os.path.expanduser("~"), ".yt_dj_cratebuilder_config.json")
    if os.path.exists(old_p):
        try:
            with open(old_p, "r", encoding="utf-8") as f:
                data = json.load(f)
            save_config(data)   # write to new location
            return data
        except Exception:
            pass
    return {}

def save_config(data):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

DEFAULT_BASE = os.path.join(os.path.expanduser("~"), "Music", "DJ-CrateBuilder")

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

        self.title("📋  Download Log  —  DJ CrateBuilder")
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


# ─────────────────────────────────────────────────────────────────────────────
COOKIE_HOWTO_TEXT = """\
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
"""


class CookieHowToWindow(tk.Toplevel):
    """Standalone dark-themed how-to viewer for Chrome profile setup."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("📖  How-To: Setting Up a Dedicated Chrome Profile")
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

        # Populate
        self._txt.config(state="normal")
        for line in COOKIE_HOWTO_TEXT.splitlines(True):
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
        self._queue         = []
        self._skip_existing   = tk.BooleanVar(value=cfg.get("skip_existing", True))
        self._skip_mode       = tk.StringVar(value=cfg.get("skip_mode", "In Logs ~ In Folder"))
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

        # Ensure directory structure exists on startup
        self._url_history = cfg.get("url_history", [])[:6]
        self._ensure_dirs()
        self._setup_logger()

        self._build_styles()
        self._build_ui()
        self._apply_platform()      # paint initial platform colours
        self._check_deps_async()

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

    def _resolve_save_dir(self, genre, channel_name=None):
        """Build the final save path:  base/Platform[/Genre[/Channel]]"""
        parts = [self._platform_dir()]
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
        self._log_path = os.path.join(app_dir, "DJ-CrateBuilder.log")

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
                         highlightthickness=1, highlightbackground=BORDER)
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
        url      = self._normalize_url(self._url_var.get().strip())
        platform = self._platform_var.get()
        cfg      = PLATFORMS[platform]

        if not url or url == cfg["placeholder"]:
            messagebox.showwarning("No URL", f"Please enter a {platform} URL.")
            return
        if not re.search(cfg["url_pattern"], url):
            messagebox.showwarning("Invalid URL", cfg["bad_url_msg"])
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
        self._url_entry.insert(0, cfg["placeholder"])
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
            padding=(50, 10), borderwidth=0, width=12)
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
                self._settings_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
            elif tab_idx == 2:
                self._about_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units")
        self.bind_all("<MouseWheel>", _on_global_mousewheel)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(outer)
        hdr.pack(fill="x", pady=(0, 4))

        self._logo_lbl = tk.Label(hdr, text="▶", font=("Segoe UI", 20),
                                   fg=YT_RED, bg=BG)
        self._logo_lbl.pack(side="left", padx=(0, 10))

        self._title_lbl = ttk.Label(hdr, text="YouTube → MP3",
                                     style="Title.TLabel")
        self._title_lbl.pack(side="left", pady=(2, 0))

        # Platform buttons — right side of header row
        self._sc_btn = ttk.Button(hdr, text="◈  SoundCloud",
                                   style="Off.TButton",
                                   command=lambda: self._switch_platform("SoundCloud"))
        self._sc_btn.pack(side="right", padx=(6, 0))

        self._yt_btn = ttk.Button(hdr, text="▶  YouTube",
                                   style="YT.TButton",
                                   command=lambda: self._switch_platform("YouTube"))
        self._yt_btn.pack(side="right", padx=(6, 6))

        ttk.Label(hdr, text="Platform", style="White.Section.TLabel").pack(
            side="right", padx=(0, 10))

        self._sub_lbl = ttk.Label(outer, style="S.Dim.TLabel",
                                   text="Single video  •  channel URL  •  playlist")
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
            values=["In Logs ~ In Folder", "In Folder Only", "In Logs Only"],
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

        # ── Time / Length Limiter ─────────────────────────────────────────────
        ttk.Label(outer, text="Time / Length Limiter",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 8))

        ttk.Label(outer,
                  text="Skip any file whose duration exceeds the limit below. "
                       "Uncheck to allow files of any length.",
                  style="S.Dim.TLabel", wraplength=660).pack(anchor="w", pady=(0, 12))

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
        ttk.Label(outer, text="MP3 Bitrate",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 8))

        ttk.Label(outer,
                  text="Bitrate used when converting the downloaded audio to MP3. "
                       "Higher values produce better quality and larger files.",
                  style="S.Dim.TLabel", wraplength=660).pack(anchor="w", pady=(0, 10))

        bitrate_row = ttk.Frame(outer)
        bitrate_row.pack(fill="x", pady=(0, 10))

        tk.Label(bitrate_row, text="Output Quality:",
                 font=("Segoe UI", 11, "bold"), fg=TEXT, bg=BG
                 ).pack(side="left", padx=(0, 12))

        self._bitrate_combo = ttk.Combobox(
            bitrate_row,
            textvariable=self._bitrate_quality,
            values=["192 kbps", "224 kbps", "256 kbps", "320 kbps"],
            state="readonly", width=14)
        self._bitrate_combo.pack(side="left", padx=(0, 14))

        ttk.Label(bitrate_row,
                  text="192 kbps = good quality  •  320 kbps = maximum MP3 quality",
                  style="S.Dim.TLabel").pack(side="left")

        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(14, 20))

        # ── Download Behavior ─────────────────────────────────────────────────
        beh_title_row = ttk.Frame(outer)
        beh_title_row.pack(fill="x", pady=(0, 8))
        ttk.Label(beh_title_row, text="Download Behavior",
                  style="S.White.Section.TLabel").pack(side="left")
        ttk.Label(beh_title_row, text="(Experimental)",
                  style="S.Dim.TLabel").pack(side="left", padx=(8, 0))

        ttk.Label(outer,
                  text="Options that control how DJ-CrateBuilder connects and paces "
                       "requests. These can help avoid throttling, geographic "
                       "restrictions, or IP-banning from YouTube/SoundCloud when "
                       "doing entire channel/batch downloads.",
                  style="S.Dim.TLabel", wraplength=660).pack(anchor="w", pady=(0, 12))

        # Geo-bypass checkbox
        geo_row = ttk.Frame(outer)
        geo_row.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(geo_row,
                        text="Enable geo-bypass",
                        variable=self._geo_bypass,
                        style="S.Bold.TCheckbutton"
                        ).pack(side="left")
        ttk.Label(geo_row,
                  text="Bypass geographic IP-based restrictions using a fake X-Forwarded-For header",
                  style="S.Dim.TLabel").pack(side="left", padx=(12, 0))

        # Rotate User-Agent checkbox
        ua_row = ttk.Frame(outer)
        ua_row.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(ua_row,
                        text="Rotate User-Agent",
                        variable=self._rotate_ua,
                        style="S.Bold.TCheckbutton"
                        ).pack(side="left")
        ttk.Label(ua_row,
                  text="Send a randomized browser User-Agent string (consistent within each session)",
                  style="S.Dim.TLabel").pack(side="left", padx=(12, 0))

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
        self._sleep_auto_labels = []
        for desc in [
            "Light = Downloading 50 files or less, per 24hrs.",
            "Moderate = Downloading between 50-200 files, per 24hrs.",
            "Aggressive = Downloading 200 files or more, per 24hrs.",
        ]:
            _sl = tk.Label(self._sleep_auto_row, text=f"            {desc}",
                     font=("Segoe UI", 10), fg="#f59e0b", bg=BG, anchor="w")
            _sl.pack(fill="x")
            self._sleep_auto_labels.append(_sl)

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
        ttk.Checkbutton(cookie_row,
                        text="Use browser cookies",
                        variable=self._use_cookies,
                        command=self._on_cookies_toggle,
                        style="S.Bold.TCheckbutton"
                        ).pack(side="left")
        ttk.Label(cookie_row,
                  text="Authenticate downloads using a browser login session (increases speed)",
                  style="S.Dim.TLabel").pack(side="left", padx=(12, 0))

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
            lambda _: self._autosave_behavior_settings())

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

        _cl = tk.Label(self._cookie_browser_row, text="(leave blank for default)",
                 font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG)
        _cl.pack(side="left")
        self._cookie_labels.append(_cl)
        self._profile_hint_lbl = _cl

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

        _cl = tk.Label(self._cookie_file_row,
                 text="Netscape/Mozilla cookie.txt format",
                 font=("Segoe UI", 10), fg=TEXT_DIM, bg=BG)
        _cl.pack(side="left")
        self._cookie_file_labels.append(_cl)

        self._cookie_note_labels = []
        for note in [
            "⚠  It is strongly recommended to create a dedicated/throwaway account",
            "     for use with this feature, rather than using your personal account.",
            "     Chrome 127+ blocks cookie extraction (DPAPI). Use Firefox or a cookie file instead.",
            "     For cookie files: install the 'Get cookies.txt LOCALLY' browser extension.",
        ]:
            _cl = tk.Label(outer, text=f"      {note}",
                     font=("Segoe UI", 10), fg="#f59e0b", bg=BG, anchor="w")
            _cl.pack(fill="x")
            self._cookie_note_labels.append(_cl)

        howto_row = ttk.Frame(outer)
        howto_row.pack(fill="x", pady=(6, 0))
        self._howto_lbl = tk.Label(howto_row,
                 text="      How-To:  Setting Up a Dedicated Chrome Profile",
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
        ttk.Label(outer, text="Default Save Directory",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(outer,
                  text="All downloads are organized under this folder.  "
                       "YouTube and SoundCloud sub-folders are created automatically.",
                  style="S.Dim.TLabel", wraplength=660).pack(anchor="w", pady=(0, 10))

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

        # ── Download log ──────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(8, 20))

        ttk.Label(outer, text="Download Log",
                  style="S.White.Section.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(outer,
                  text="A color-coded record of every downloaded, skipped, and failed "
                       "file. View it here in the built-in viewer, or open it in your "
                       "system's default text editor.",
                  style="S.Dim.TLabel", wraplength=660).pack(anchor="w", pady=(0, 10))

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
        """Auto-save bitrate setting to config whenever the selection changes."""
        cfg = load_config()
        cfg["bitrate_quality"] = self._bitrate_quality.get().split()[0]
        save_config(cfg)

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
            for lbl in getattr(self, "_sleep_auto_labels", []):
                lbl.config(fg="#f59e0b" if enabled else GREY)
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
                           getattr(self, "_profile_lbl", None),
                           getattr(self, "_profile_hint_lbl", None)]:
                lbl.config(fg=TEXT_DIM if enabled else GREY)

        # Switch visible row inside the container
        if is_browser:
            self._cookie_browser_row.pack(fill="x", pady=(0, 4))
            self._cookie_file_row.pack_forget()
            # Browser row colors
            for lbl in [self._browser_lbl, self._profile_lbl,
                         self._profile_hint_lbl]:
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

        # Warning notes
        note_color = "#f59e0b" if enabled else GREY
        for lbl in getattr(self, "_cookie_note_labels", []):
            lbl.config(fg=note_color)

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
            tk.Label(row, text=value, font=("Segoe UI", 11),
                      fg=TEXT, bg=BG, anchor="w"
                      ).pack(side="left", padx=(8, 0))

        # ── FAQ ───────────────────────────────────────────────────────────────
        tk.Frame(outer, height=1, bg=BORDER).pack(fill="x", pady=(20, 20))

        ttk.Label(outer, text="Frequently Asked Questions",
                  style="White.Section.TLabel").pack(anchor="w", pady=(0, 16))

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
             "A: It prevents re-downloading files you already have. There are three modes: \"In Logs ~ In Folder\" "
             "skips if the file is found in either the download log or the destination folder. \"In Folder Only\" "
             "checks only whether the file exists on disk. \"In Logs Only\" checks only the download log. This "
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
             "your base save directory as \"DJ-CrateBuilder.log\" and can be viewed with the built-in log viewer "
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
        """Open DJ-CrateBuilder.log in the OS default text viewer."""
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

    def _open_cookie_howto(self):
        """Open the built-in Chrome profile setup guide window."""
        if hasattr(self, "_cookie_howto") and self._cookie_howto.winfo_exists():
            self._cookie_howto.lift()
            self._cookie_howto.focus_force()
            return
        self._cookie_howto = CookieHowToWindow(self)

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
        if self._downloading:
            return
        self._platform_var.set(name)
        self._apply_platform()

    def _apply_platform(self):
        name = self._platform_var.get()
        cfg  = PLATFORMS[name]
        acc  = cfg["accent"]
        dark = cfg["accent_dark"]

        # Header
        self._logo_lbl.config(text=cfg["icon"], fg=acc)
        self._title_lbl.config(text=cfg["label"])
        self._sub_lbl.config(text=cfg["sub"])

        # Toggle button styles
        s = ttk.Style(self)
        if name == "YouTube":
            self._yt_btn.config(style="YT.TButton")
            self._sc_btn.config(style="Off.TButton")
        else:
            self._yt_btn.config(style="Off.TButton")
            self._sc_btn.config(style="SC.TButton")

        # Download button colour
        s.configure("Download.TButton", background=dark)
        s.map("Download.TButton",
              background=[("active", acc), ("disabled", "#2a1515")])

        # Per-item progress bar colour
        s.configure("Accent.Horizontal.TProgressbar",
            background=acc, lightcolor=acc, darkcolor=acc)

        # Active queue row icon colour
        STATE_ICON[ST_ACTIVE] = ("◉", acc)

        # Update URL field placeholder
        self._set_placeholder(cfg["placeholder"])

        # Update entry focus ring
        s.map("TEntry",
            bordercolor=[("focus", acc), ("!focus", BORDER)],
            lightcolor=[("focus", acc),  ("!focus", BORDER)])

        # Window title
        self.title(f"{APP_NAME}  v{APP_VERSION}")

        # Refresh genre list for the new platform & update save preview
        self._refresh_genre_list()

    def _set_placeholder(self, text):
        """Replace placeholder text without disturbing a real URL."""
        current = self._url_entry.get()
        old_ph  = PLATFORMS["YouTube"]["placeholder"] if \
                  self._platform_var.get() == "SoundCloud" else \
                  PLATFORMS["SoundCloud"]["placeholder"]
        if self._ph_active or current == old_ph or current == "":
            self._url_entry.config(state="normal")
            self._url_entry.delete(0, "end")
            self._url_entry.insert(0, text)
            self._url_entry.config(foreground=TEXT_DIM)
            self._ph_active = True

    # ── URL placeholder ───────────────────────────────────────────────────────
    def _url_focus_in(self, _e):
        cfg = PLATFORMS[self._platform_var.get()]
        if self._url_entry.get() == cfg["placeholder"]:
            self._url_entry.delete(0, "end")
            self._url_entry.config(foreground=TEXT)
            self._ph_active = False

    def _url_focus_out(self, _e):
        if not self._url_entry.get().strip():
            cfg = PLATFORMS[self._platform_var.get()]
            self._url_entry.insert(0, cfg["placeholder"])
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

        platform = self._platform_var.get()
        cfg      = PLATFORMS[platform]

        # Build the run list: use batch if populated, else fall back to URL field
        if self._batch_urls:
            run_batch = list(self._batch_urls)
        else:
            url = self._normalize_url(self._url_var.get().strip())
            if not url or url == cfg["placeholder"]:
                messagebox.showwarning("No URL",
                    f"Add at least one URL to the batch queue, or enter a {platform} URL.")
                return
            if not re.search(cfg["url_pattern"], url):
                messagebox.showwarning("Invalid URL", cfg["bad_url_msg"])
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

                dl, sk, er = self._process_one_url(url, genre, platform, cfg, session_ua)
                if dl is None:   # fatal error inside _process_one_url
                    fatal_error = self._last_fatal_error
                    break

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
    def _process_one_url(self, url, genre, platform, cfg, session_ua=None):
        """
        Download all entries from a single URL.
        Returns (downloaded, skipped, errors) counts, or (None, None, None) on
        a fatal error.  Does NOT call _finish — that is the batch_worker's job.
        """
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

            try:
                with yt_dlp.YoutubeDL(meta_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as fetch_exc:
                raw_err = str(fetch_exc)
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
                self._last_fatal_error = err
                self.after(0, lambda e=err: (
                    self._cur_lbl.config(text=f"✗ {e}"),
                    self._set_status(f"Error: {e}"),
                ))
                return None, None, None

            if self._cancel_flag.is_set():
                return 0, 0, 0

            # Normalise: single item vs collection
            is_collection = info.get("_type") in ("playlist", "channel")
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
            channel_sub = collection_name if is_collection else None
            save_dir    = self._resolve_save_dir(genre, channel_sub)

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
                    in_log      = self._was_logged(expected_path, title=item_title)

                    # Use the actual found path for log/display if available
                    if found_path:
                        expected_path = found_path

                    # Determine whether this entry should be skipped
                    # based on the selected mode
                    should_skip = False
                    if mode == "In Logs ~ In Folder":
                        should_skip = file_exists or in_log
                    elif mode == "In Folder Only":
                        should_skip = file_exists
                    elif mode == "In Logs Only":
                        should_skip = in_log

                    if should_skip:
                        # File is in log but missing from disk — ask user
                        if in_log and not file_exists and mode in (
                                "In Logs ~ In Folder", "In Logs Only"):
                            result = []
                            evt    = threading.Event()
                            self.after(0, lambda t=item_title, r=result, e=evt:
                                self._ask_redownload(t, r, e))
                            evt.wait()
                            if result[0]:   # user chose to re-download
                                should_skip = False

                    if should_skip:
                        skip_reason = (
                            "already on disk"   if mode == "In Folder Only" else
                            "in log"            if mode == "In Logs Only" else
                            "in log + on disk"  if (in_log and file_exists) else
                            "already on disk"   if file_exists else
                            "in log"
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
                                        text=f"src {int(b)}k → {output_kbps}k"))
                                # Show source bitrate on the queue row
                                self.after(0, lambda b=abr, ii=i:
                                    self._set_row_bitrate(
                                        ii, f"{int(b)}k → {output_kbps}k"))
                            if tb:
                                self.after(0, lambda p=db/tb*100:
                                    self._vid_progress.config(value=p))
                            if sp:
                                speed_txt = f"{sp}  {eta}" if eta else sp
                                self.after(0, lambda s=speed_txt:
                                    self._speed_lbl.config(text=s))
                        elif st == "finished":
                            self.after(0, lambda: (
                                self._vid_progress.config(value=95),
                                self._speed_lbl.config(text="converting…"),
                            ))
                    return hook

                ydl_opts = {
                    "format":   "bestaudio/best",
                    "outtmpl":  os.path.join(save_dir, "%(title)s.%(ext)s"),
                    "postprocessors": [{
                        "key":              "FFmpegExtractAudio",
                        "preferredcodec":   "mp3",
                        "preferredquality": output_kbps,
                    }],
                    "progress_hooks": [make_hook(idx)],
                    "quiet":       True,
                    "no_warnings": True,
                }

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

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([item_url])
                    done += 1
                    self._grand_dl += 1
                    src_str = (f"{int(source_abr[0])} kbps src → "
                               f"{output_kbps} kbps MP3"
                               if source_abr[0] else f"{output_kbps} kbps MP3")
                    self._log_download(item_title, expected_path, item_url,
                                       platform, genre, quality=src_str)
                    brate_txt = (f"{int(source_abr[0])}k → {output_kbps}k"
                                 if source_abr[0] else f"→ {output_kbps}k")
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

                    # Age-restricted videos fail with cookies because
                    # YouTube forces the main player which requires age
                    # verification. Without cookies, yt-dlp uses the
                    # embedded player which bypasses age gates. Retry
                    # without cookies for age-restricted content.
                    is_age = ("age" in clean_lower or
                              "verify your age" in clean_lower or
                              "adult" in clean_lower)
                    if is_age and using_cookies:
                        self._logger.info(
                            f"AGE-RETRY   | "
                            f"Title: {item_title} | "
                            f"URL: {item_url} | "
                            f"Retrying without cookies to bypass age gate")
                        retry_opts = {
                            "format":   "bestaudio/best",
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

            return done - skipped - errors, skipped, errors

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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = MP3DownloaderApp()
    app.mainloop()

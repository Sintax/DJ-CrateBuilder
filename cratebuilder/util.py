"""Pure helpers: config persistence, date/path/title normalisation.

No tkinter imports — safe to unit-test in isolation.
"""
import json
import os
import re
from datetime import datetime, date

def detect_platform(url):
    """Return 'SoundCloud' for a soundcloud.com URL, else 'YouTube' (default)."""
    if url and re.search(r"soundcloud\.com", url, re.IGNORECASE):
        return "SoundCloud"
    return "YouTube"


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


def today_yyyymmdd():
    return date.today().strftime("%Y%m%d")


def scan_folder_newest_mp3(folder):
    if not folder or not os.path.isdir(folder):
        return 0, None
    newest = None
    count  = 0
    try:
        for name in os.listdir(folder):
            if not name.lower().endswith(".mp3"):
                continue
            full = os.path.join(folder, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            count += 1
            if newest is None or mtime > newest:
                newest = mtime
    except OSError:
        return 0, None
    if newest is None:
        return 0, None
    return count, datetime.fromtimestamp(newest).strftime("%Y%m%d")


def normalize_track_key(name):
    """Collapse a video title or .mp3 filename to a comparison key so a
    YouTube title and its saved (sanitised) filename match despite case,
    punctuation, spacing, or mangled special characters.

    Deliberately aggressive — only alphanumerics survive — because the chosen
    trade-off is to *hide* a track when a match is uncertain (avoid showing a
    track the user already owns) rather than risk re-listing it. A saved file
    like "1788-L - �THERSUIT.mp3" and the real title "1788-L - ÆTHERSUIT"
    both reduce to the same key once non-alphanumerics are stripped."""
    if not name:
        return ""
    s = re.sub(r"\.(mp3|m4a|opus|webm|wav|flac|aac)$", "", str(name),
               flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", s.lower())

"""Pure helpers: config persistence, date/path/title normalisation.

No tkinter imports — safe to unit-test in isolation.
"""
import json
import os
import re
import time
from datetime import datetime, date, timedelta

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


def safe_filename(name, strip=False):
    """Replace characters illegal in a filename ( \\ / * ? : " < > | ) with '_'.

    The default raw form mirrors the actual on-disk filename. With strip=True it
    also trims surrounding whitespace — used for folder names and for matching
    'legacy' files saved before yt-dlp's own sanitiser was adopted. Returns ''
    for empty/None input."""
    safe = re.sub(r'[\\/*?:"<>|]', "_", name or "")
    return safe.strip() if strip else safe


def push_mru(items, value, limit):
    """Return a new most-recently-used list with *value* at the front,
    de-duplicated and capped at *limit*. Does not mutate *items*."""
    rest = [x for x in (items or []) if x != value]
    return ([value] + rest)[:limit]


# Keys in a yt-dlp options dict whose values carry authentication material
# (a cookie file path leaks the user's home directory; the browser-cookie
# source names the profile) and must never be written to debug.log.
SENSITIVE_YDL_KEYS = ("cookiefile", "cookiesfrombrowser")


def build_cookie_opts(method, cookie_file, browser, profile):
    """Return the yt-dlp cookie option(s) for the user's settings as a dict to
    merge into an options dict. The 'Cookie File' method uses the file path
    only when it exists on disk; any other method reads cookies from the named
    browser (lower-cased), optionally scoped to a *profile*. Returns {} when no
    cookie source applies (e.g. a missing/blank cookie file). Callers decide
    whether cookies are enabled at all — this only formats the chosen source."""
    if method == "Cookie File":
        if cookie_file and os.path.exists(cookie_file):
            return {"cookiefile": cookie_file}
        return {}
    b = (browser or "").lower()
    return {"cookiesfrombrowser": (b, profile) if profile else (b,)}


def redact_ydl_opts(opts):
    """Return a shallow copy of a yt-dlp options dict made safe for debug
    logging. Auth-bearing values (cookie file path, browser-cookie source)
    are replaced with '<redacted>' when set; the progress-hook callback list
    is summarised by count; every other key passes through unchanged. Falsy
    auth values are left as-is so the log still shows whether cookies were
    configured. Returns {} for None/empty input."""
    safe = {}
    for k, v in (opts or {}).items():
        if k in SENSITIVE_YDL_KEYS:
            safe[k] = "<redacted>" if v else v
        elif k == "progress_hooks":
            safe[k] = f"[{len(v)} hook(s)]"
        else:
            safe[k] = v
    return safe

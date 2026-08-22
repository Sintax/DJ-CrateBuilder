"""Settings: single in-memory owner of ~/.dj_cratebuilder_config.json."""
import copy
import json
import os
import threading
from dataclasses import dataclass

from cratebuilder import util
from cratebuilder.artwork import COVER_ART_MODES, DEFAULT_COVER_ART_MODE


def _schema_defaults():
    """The declared schema: every known config key and its default when
    absent. Built fresh per Settings instance so mutable defaults are never
    shared and home-relative paths honour the environment at construction."""
    return {
        "base_dir": util.default_base_dir(),
        "skip_existing": True,
        "skip_mode": "In Folder Only",
        "limit_enabled": True,
        "limit_minutes": 8,
        "bitrate_quality": "192",
        "bitrate_auto_upgrade": False,
        "no_conversion": False,
        "cover_art_enabled": True,
        "cover_art_mode": DEFAULT_COVER_ART_MODE,
        "log_max_mb": 2,
        "geo_bypass": True,
        "rotate_ua": True,
        "sleep_enabled": True,
        "sleep_mode": "Auto",
        "sleep_preset": "Light  (1–5 s)",
        "sleep_min": 1,
        "sleep_max": 5,
        "use_cookies": False,
        "cookie_method": "Browser",
        "cookies_browser": "Firefox",
        "cookies_profile": "",
        "cookie_file": "",
        "auto_add_to_watchlist": True,
        "auto_download_interval": "1 day",
        "run_at_startup": False,
        "minimize_to_tray": True,
        "start_minimized": False,
        "watchlist_scan_on_startup": True,
        "watchlist_last_download": 0,
        "update_check_interval": "6 hours",
        "last_update_check": 0.0,
        "url_history": [],
        "dupe_check_enabled": True,
        "window_geometry": "",
        "window_maximized": False,
        "db_dl_col_widths": None,
        "db_wl_col_widths": None,
        "db_art_col_widths": None,
        "db_dl_col_order": None,
        "db_wl_col_order": None,
        "db_art_col_order": None,
    }


_SKIP_MODE_RENAMES = {
    "In Logs ~ In Folder": "In Database ~ In Folder",
    "In Logs Only": "In Database Only",
}


@dataclass(frozen=True)
class CookieConfig:
    """What a yt-dlp session authenticates with."""
    use_cookies: bool
    cookie_method: str
    cookies_browser: str
    cookies_profile: str
    cookie_file: str


@dataclass(frozen=True)
class DownloadPolicy:
    """Skip / limiter / bitrate / sleep / geo / UA / cover-art policy for a
    download batch."""
    skip_existing: bool
    skip_mode: str
    limit_enabled: bool
    limit_minutes: int
    bitrate_quality: str
    bitrate_auto_upgrade: bool
    no_conversion: bool
    sleep_enabled: bool
    sleep_mode: str
    sleep_preset: str
    sleep_min: int
    sleep_max: int
    geo_bypass: bool
    rotate_ua: bool
    cover_art_enabled: bool
    cover_art_mode: str


@dataclass(frozen=True)
class AutomationConfig:
    """Intervals, startup-scan, and tray behaviour."""
    auto_download_interval: str
    watchlist_scan_on_startup: bool
    minimize_to_tray: bool
    start_minimized: bool
    auto_add_to_watchlist: bool
    update_check_interval: str


class Settings:
    """The single in-memory owner of the user config file.

    Loads once at construction (applying legacy migrations in memory — they
    reach disk on the first write, matching the old behaviour), then serves
    every read from memory. Every write persists the WHOLE store atomically
    (temp file + os.replace), so no writer can drop another writer's keys.
    Unknown keys found in the file ride along on every write but are rejected
    by get/set with KeyError. Thread-safe; write failures are swallowed like
    the old save_config, with the in-memory store staying consistent."""

    def __init__(self, path=None):
        self._explicit_path = path is not None
        self._path = path if path is not None else util._config_path()
        self._defaults = _schema_defaults()
        self._lock = threading.Lock()
        self._data = self._load()

    @property
    def path(self):
        return self._path

    def get(self, key):
        """Return the current value for a schema key (its default when the
        file never set it). Raises KeyError for keys not in the schema."""
        with self._lock:
            return copy.deepcopy(self._value(key))

    def set(self, key, value):
        """Set one schema key and persist the whole store. Raises KeyError
        for keys not in the schema."""
        self.update({key: value})

    def update(self, mapping):
        """Set several schema keys and persist the whole store once. Raises
        KeyError (before changing anything) if any key is not in the schema,
        and TypeError if a value could not be written back as JSON — rejecting
        it here keeps one bad value from poisoning every later write."""
        with self._lock:
            for key in mapping:
                if key not in self._defaults:
                    raise KeyError(key)
            try:
                json.dumps(mapping)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"value is not JSON-serialisable: {exc}")
            for key, value in mapping.items():
                self._data[key] = copy.deepcopy(value)
            self._persist()

    def cookie_config(self):
        """A CookieConfig snapshot of the current values."""
        with self._lock:
            return CookieConfig(**{f: self._value(f)
                                   for f in CookieConfig.__dataclass_fields__})

    def download_policy(self):
        """A DownloadPolicy snapshot of the current values."""
        with self._lock:
            return DownloadPolicy(**{
                f: self._value(f)
                for f in DownloadPolicy.__dataclass_fields__})

    def automation_config(self):
        """An AutomationConfig snapshot of the current values."""
        with self._lock:
            return AutomationConfig(**{
                f: self._value(f)
                for f in AutomationConfig.__dataclass_fields__})

    def _value(self, key):
        if key not in self._defaults:
            raise KeyError(key)
        return self._data.get(key, self._defaults[key])

    def _load(self):
        """Read the config, falling back to the pre-rename file name. A
        successful legacy read is copied forward immediately, as the old
        load_config did, so the file only has to be found once."""
        from_legacy = False
        data = _read_json(self._path)
        if data is None and not self._explicit_path:
            data = _read_json(util._legacy_config_path())
            from_legacy = data is not None
        data = data if isinstance(data, dict) else {}
        _migrate(data)
        if from_legacy:
            self._data = data
            self._persist()
        return data

    def _persist(self):
        """Write the whole store atomically; swallow I/O failures — a config
        write must never crash the app (matching the old save_config)."""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_json(path):
    """Load a JSON object from *path*, or None when missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _migrate(data):
    """Apply the legacy config migrations to *data* in place: auto_check_hours
    seeding auto_download_interval, the skip_mode value renames, and the
    cover_art_mode 'off' tri-state split. (The legacy file-rename probe lives
    in Settings._load.)"""
    if "auto_download_interval" not in data and "auto_check_hours" in data:
        data["auto_download_interval"] = data["auto_check_hours"]

    if data.get("skip_mode") in _SKIP_MODE_RENAMES:
        data["skip_mode"] = _SKIP_MODE_RENAMES[data["skip_mode"]]

    if "cover_art_mode" in data:
        mode = str(data["cover_art_mode"]).lower()
        if mode not in COVER_ART_MODES:
            mode = DEFAULT_COVER_ART_MODE
        if mode == "off":
            data.setdefault("cover_art_enabled", False)
            mode = DEFAULT_COVER_ART_MODE
        data["cover_art_mode"] = mode

# DJ-CrateBuilder v1.3 Final — Watch List Automation, Settings & Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add unattended Watch List automation (periodic auto-check + auto-download, run-at-startup, system tray), the supporting Settings UI, and a "Fix Link" button — while cleaning the codebase for a final release (extract a `cratebuilder/` package, add a pytest safety net, tidy the main file).

**Architecture:** Single-file Tkinter app (`DJ-CrateBuilder_v1.3.py`). We add a sibling `cratebuilder/` package holding pure, Tk-free logic (util, sidecar, db) plus two new Windows-only modules (startup, tray). A pytest harness is written **first** against current behaviour so the extraction can't silently break anything. Features are then built into the cleaned structure; an `after()`-based scheduler reuses the existing scan/download machinery.

**Tech Stack:** Python 3.10+, tkinter/ttk, sqlite3, yt-dlp, pystray + Pillow (tray/notify, new), winreg (stdlib, Windows), pytest (dev-only).

**Reference spec:** `docs/superpowers/specs/2026-05-30-watchlist-automation-design.md`

**Note on line numbers:** Anchors below reflect the file at planning time (~7181 lines) and will drift as edits land. Always locate code by **symbol name** (function/method/constant), not by line number alone.

---

## Conventions for this plan

- **Verify after every code change:** `python -m py_compile DJ-CrateBuilder_v1.3.py` must pass, and `pytest -q` must stay green.
- **Headless UI smoke test** (the `__main__` guard prevents `mainloop`, so importing builds nothing on its own — use this snippet to construct the App without blocking):

```bash
python -c "import importlib.util as u, sys; spec=u.spec_from_file_location('cb','DJ-CrateBuilder_v1.3.py'); m=u.module_from_spec(spec); spec.loader.exec_module(m); app=m.MP3DownloaderApp(); app.update(); print('TABS', [app._notebook.tab(i,'text') for i in app._notebook.tabs()]); app.destroy()"
```

- **DB tests** run against a temp path — **never** the real `cratebuilder.db`.
- **Commit** at the end of every task.

---

# PHASE 0 — Test harness (write against CURRENT behaviour)

> These tests import the **current** single-file module via `importlib` so they pass before any extraction. In Phase 1 we re-point the imports at the new package; the assertions do not change. This is what makes the refactor safe.

### Task 0.1: Add pytest dev dependency + test scaffolding

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `requirements-dev.txt`**

```text
pytest>=8.0
```

- [ ] **Step 2: Install it**

Run: `pip install -r requirements-dev.txt`
Expected: pytest installed (or "already satisfied").

- [ ] **Step 3: Create `tests/__init__.py`** (empty file)

- [ ] **Step 4: Create `tests/conftest.py`** — a loader that imports the single-file app as module `cb`, so tests can reach its functions today and the package tomorrow.

```python
"""Shared fixtures. Loads the app's pure-logic functions.

Phase 0/1: `cb` is the single-file app module loaded via importlib.
After extraction, individual tests import from `cratebuilder.*` directly;
this loader remains as a fallback for code still living in the main file.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_ROOT, "DJ-CrateBuilder_v1.3.py")


def _load_main():
    spec = importlib.util.spec_from_file_location("cb_main", _MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def cb():
    return _load_main()


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Redirect the config file to a temp path for load/save tests."""
    cfg = tmp_path / "config.json"
    return cfg
```

- [ ] **Step 5: Run pytest to confirm collection works**

Run: `pytest -q`
Expected: "no tests ran" (exit 5) — scaffolding present, no test files yet.

- [ ] **Step 6: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py
git commit -m "test: add pytest scaffolding and app loader fixture"
```

---

### Task 0.2: Characterization tests — `normalize_track_key` & date/path helpers

**Files:**
- Test: `tests/test_util.py`

- [ ] **Step 1: Write the tests** (assert CURRENT behaviour of `normalize_track_key`, `today_yyyymmdd`)

```python
import re
import datetime as _dt


def test_normalize_strips_audio_extensions(cb):
    assert cb.normalize_track_key("My Track.mp3") == cb.normalize_track_key("My Track")
    for ext in ("m4a", "opus", "webm", "wav", "flac", "aac"):
        assert cb.normalize_track_key(f"Song.{ext}") == "song"


def test_normalize_collapses_punctuation_and_case(cb):
    assert cb.normalize_track_key("Drum & Bass!! (2024)") == "drumbass2024"
    assert cb.normalize_track_key("A_B-C") == "abc"


def test_normalize_handles_empty_and_none(cb):
    assert cb.normalize_track_key("") == ""
    assert cb.normalize_track_key(None) == ""


def test_today_yyyymmdd_format(cb):
    val = cb.today_yyyymmdd()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", val)
    # parses as a real date
    _dt.date.fromisoformat(val)
```

- [ ] **Step 2: Run, expect PASS** (these document current behaviour)

Run: `pytest tests/test_util.py -q`
Expected: PASS. If any assertion fails, the assertion is **wrong about current behaviour** — read the real function and correct the test (do NOT change app code in Phase 0).

- [ ] **Step 3: Commit**

```bash
git add tests/test_util.py
git commit -m "test: characterize normalize_track_key and date helper"
```

---

### Task 0.3: Characterization tests — config load/save round-trip

**Files:**
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the tests** — monkeypatch `_config_path` to a temp file and verify round-trip + missing-file behaviour.

```python
def test_save_then_load_roundtrip(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    cb.save_config({"base_dir": "X", "auto_add_to_watchlist": False})
    loaded = cb.load_config()
    assert loaded["base_dir"] == "X"
    assert loaded["auto_add_to_watchlist"] is False


def test_load_missing_returns_empty(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    # ensure no legacy file interferes
    monkeypatch.setattr(cb.os.path, "expanduser", lambda p: str(tmp_path))
    assert cb.load_config() == {}


def test_save_is_indented_json(cb, tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    monkeypatch.setattr(cb, "_config_path", lambda: str(cfg_file))
    cb.save_config({"a": 1})
    text = cfg_file.read_text(encoding="utf-8")
    assert "\n" in text  # indent=2 produces multi-line output
```

- [ ] **Step 2: Run, expect PASS**

Run: `pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "test: characterize config load/save round-trip"
```

---

### Task 0.4: Characterization tests — sidecar + unresolved predicate

**Files:**
- Test: `tests/test_sidecar.py`

- [ ] **Step 1: Write the tests** — sidecar round-trip, URL builder, and the `_is_unresolved_channel` truth table (currently a staticmethod on the App class).

```python
def test_channel_url_from_id(cb):
    assert cb.channel_url_from_id("UC123") == \
        "https://www.youtube.com/channel/UC123/videos"
    assert cb.channel_url_from_id("") == ""


def test_sidecar_write_then_read(cb, tmp_path):
    folder = tmp_path / "ChannelX"
    folder.mkdir()
    ok = cb.write_channel_sidecar(
        str(folder), channel_id="UCabc", handle="@chanx",
        display_name="Chan X", genre="DnB")
    assert ok is True
    data = cb.read_channel_sidecar(str(folder))
    assert data["channel_id"] == "UCabc"
    assert data["channel_url"] == "https://www.youtube.com/channel/UCabc/videos"


def test_read_sidecar_missing_returns_none(cb, tmp_path):
    assert cb.read_channel_sidecar(str(tmp_path / "nope")) is None


def test_is_unresolved_truth_table(cb):
    App = cb.MP3DownloaderApp
    assert App._is_unresolved_channel({"status": "needs_resolve", "url": "x"}) is True
    assert App._is_unresolved_channel({"status": "error", "url": "x"}) is True
    assert App._is_unresolved_channel({"status": "idle", "url": "unresolved://YouTube/x"}) is True
    assert App._is_unresolved_channel({"status": "idle", "url": "has space"}) is True
    assert App._is_unresolved_channel(
        {"status": "idle", "url": "https://www.youtube.com/channel/UC/videos"}) is False
```

- [ ] **Step 2: Run, expect PASS**

Run: `pytest tests/test_sidecar.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sidecar.py
git commit -m "test: characterize sidecar helpers and unresolved predicate"
```

---

### Task 0.5: Characterization tests — DownloadsDatabase

**Files:**
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the tests** — construct the DB against a temp path, verify schema idempotency, watchlist insert + UNIQUE(url) dedup, and `get_all_watchlist_channels`.

```python
def _new_db(cb, tmp_path):
    # DownloadsDatabase takes a db path; confirm signature in source if this fails.
    return cb.DownloadsDatabase(str(tmp_path / "test.db"))


def test_schema_init_idempotent(cb, tmp_path):
    db = _new_db(cb, tmp_path)
    # Re-initialising must not raise (idempotent CREATE/ALTER).
    db2 = cb.DownloadsDatabase(str(tmp_path / "test.db"))
    assert db2 is not None


def test_watchlist_insert_and_dedup(cb, tmp_path):
    db = _new_db(cb, tmp_path)
    row = dict(url="https://www.youtube.com/channel/UC1/videos",
               channel_id="UC1", display_name="One", platform="YouTube",
               genre="DnB", scan_cutoff_date="2026-01-01")
    first = db.add_watchlist_channel(**row)   # confirm method name in source
    second = db.add_watchlist_channel(**row)  # duplicate url
    chans = db.get_all_watchlist_channels()
    urls = [c["url"] for c in chans]
    assert urls.count(row["url"]) == 1  # UNIQUE(url) prevented a duplicate
```

> **Implementer note:** `add_watchlist_channel` is the assumed insert method. Open `DownloadsDatabase` in the source and use the **actual** method name/signature for inserting a watchlist row (e.g. it may be `add_to_watchlist`). Adjust the call but keep the dedup assertion.

- [ ] **Step 2: Run, expect PASS** (after matching the real method name)

Run: `pytest tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_db.py
git commit -m "test: characterize DownloadsDatabase schema and watchlist dedup"
```

- [ ] **Step 4: Run the FULL suite as the green baseline**

Run: `pytest -q`
Expected: all PASS. **This is the baseline the refactor must preserve.**

---

# PHASE 1 — Extract the `cratebuilder/` package

> Move pure logic out of the main file with **zero behaviour change**. After each extraction, run `pytest -q` + `py_compile`. The main file imports the moved names back so nothing else changes.

### Task 1.1: Create the package and extract `util.py`

**Files:**
- Create: `cratebuilder/__init__.py` (empty)
- Create: `cratebuilder/util.py`
- Modify: `DJ-CrateBuilder_v1.3.py` (remove moved defs, add import)
- Test: `tests/test_util.py`, `tests/test_config.py` (re-point imports)

- [ ] **Step 1: Create `cratebuilder/__init__.py`** (empty)

- [ ] **Step 2: Create `cratebuilder/util.py`** and move these functions **verbatim** from the main file into it: `_config_path`, `load_config`, `save_config`, `today_yyyymmdd`, `normalize_track_key`, `scan_folder_newest_mp3`. Add the imports they need at the top:

```python
"""Pure helpers: config persistence, date/path/title normalisation.

No tkinter imports — safe to unit-test in isolation.
"""
import datetime
import json
import os
import re

# ↓↓↓ paste the six functions here, unchanged ↓↓↓
# _config_path, load_config, save_config, today_yyyymmdd,
# normalize_track_key, scan_folder_newest_mp3
```

- [ ] **Step 3: In the main file, delete those six definitions** and add near the other imports:

```python
from cratebuilder.util import (
    _config_path, load_config, save_config, today_yyyymmdd,
    normalize_track_key, scan_folder_newest_mp3,
)
```

- [ ] **Step 4: Re-point the unit tests** to import from the package directly (so they test the real new home). In `tests/test_util.py` and `tests/test_config.py`, replace the `cb` fixture usage with `from cratebuilder import util` and call `util.normalize_track_key(...)`, `util.load_config()`, etc. For the config monkeypatch, patch `cratebuilder.util._config_path` and `cratebuilder.util.os.path.expanduser`.

- [ ] **Step 5: Run tests + compile**

Run: `pytest tests/test_util.py tests/test_config.py -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean compile.

- [ ] **Step 6: Headless UI smoke test** (confirm the app still builds)

Run the headless snippet from "Conventions". Expected: prints the 4 tab names, no exception.

- [ ] **Step 7: Commit**

```bash
git add cratebuilder/__init__.py cratebuilder/util.py DJ-CrateBuilder_v1.3.py tests/test_util.py tests/test_config.py
git commit -m "refactor: extract util helpers into cratebuilder.util"
```

---

### Task 1.2: Extract `sidecar.py` (incl. the unresolved predicate)

**Files:**
- Create: `cratebuilder/sidecar.py`
- Modify: `DJ-CrateBuilder_v1.3.py`
- Test: `tests/test_sidecar.py`

- [ ] **Step 1: Create `cratebuilder/sidecar.py`** and move **verbatim**: `channel_url_from_id`, `read_channel_sidecar`, `write_channel_sidecar`, plus the `CHANNEL_SIDECAR_NAME` constant. Then move the body of `MP3DownloaderApp._is_unresolved_channel` into a module function:

```python
"""Channel-folder sidecar (cratebuilder.json) helpers + resolution predicate."""
import json
import os

from cratebuilder.util import today_yyyymmdd

CHANNEL_SIDECAR_NAME = "cratebuilder.json"

# ↓↓↓ paste channel_url_from_id, read_channel_sidecar, write_channel_sidecar ↓↓↓
# (write_channel_sidecar already calls today_yyyymmdd — now imported above)


def is_unresolved_channel(ch):
    """True when a watchlist row has no usable YouTube identifier yet."""
    url = (ch.get("url") or "")
    return (ch.get("status") in ("needs_resolve", "error")
            or url.startswith("unresolved://")
            or " " in url)
```

- [ ] **Step 2: In the main file**, delete the moved defs + constant, add import, and replace the staticmethod with a thin delegator so existing call sites keep working:

```python
from cratebuilder.sidecar import (
    CHANNEL_SIDECAR_NAME, channel_url_from_id,
    read_channel_sidecar, write_channel_sidecar, is_unresolved_channel,
)
```
```python
    # inside MP3DownloaderApp:
    @staticmethod
    def _is_unresolved_channel(ch):
        return is_unresolved_channel(ch)
```

- [ ] **Step 3: Re-point `tests/test_sidecar.py`** to `from cratebuilder import sidecar` and call `sidecar.is_unresolved_channel(...)` etc. Keep one assertion through `cb.MP3DownloaderApp._is_unresolved_channel` to prove the delegator still works.

- [ ] **Step 4: Run tests + compile + headless smoke**

Run: `pytest tests/test_sidecar.py -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean compile.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/sidecar.py DJ-CrateBuilder_v1.3.py tests/test_sidecar.py
git commit -m "refactor: extract sidecar helpers + is_unresolved_channel"
```

---

### Task 1.3: Extract `db.py` (DownloadsDatabase)

**Files:**
- Create: `cratebuilder/db.py`
- Modify: `DJ-CrateBuilder_v1.3.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Create `cratebuilder/db.py`** and move the entire `DownloadsDatabase` class + its schema/SQL constants **verbatim**. Add imports it needs:

```python
"""SQLite persistence: downloads history + watchlist."""
import os
import sqlite3
import time

# ↓↓↓ paste DownloadsDatabase class and any module-level schema constants ↓↓↓
```
If the class uses `normalize_track_key` (for backfill matching), add `from cratebuilder.util import normalize_track_key`.

- [ ] **Step 2: In the main file**, delete the class, add `from cratebuilder.db import DownloadsDatabase`.

- [ ] **Step 3: Re-point `tests/test_db.py`** to `from cratebuilder.db import DownloadsDatabase` and instantiate it directly.

- [ ] **Step 4: Run tests + compile + headless smoke**

Run: `pytest -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: full suite PASS + clean compile.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/db.py DJ-CrateBuilder_v1.3.py tests/test_db.py
git commit -m "refactor: extract DownloadsDatabase into cratebuilder.db"
```

---

# PHASE 2 — Features

### Task 2.1: Swap Watch List and Settings tab order

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`_build_ui`, ~line 2887)
- Test: `tests/test_tabs.py`

- [ ] **Step 1: Write a failing UI test** that asserts the new order.

```python
import importlib.util, os, sys

def _app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    return m.MP3DownloaderApp()

def test_tab_order_is_main_watchlist_settings_about():
    try:
        app = _app()
    except Exception as e:
        import pytest; pytest.skip(f"no display: {e}")
    app.update()
    titles = [app._notebook.tab(i, "text") for i in app._notebook.tabs()]
    joined = " | ".join(titles)
    assert "Main" in titles[0]
    assert "Watch List" in titles[1]
    assert "Settings" in titles[2]
    assert "About" in titles[3]
    app.destroy()
```

- [ ] **Step 2: Run, expect FAIL** (current order is Settings before Watch List)

Run: `pytest tests/test_tabs.py -q`
Expected: FAIL on `titles[1]`/`titles[2]`.

- [ ] **Step 3: Reorder the `add()` calls** in `_build_ui` so the Watch List block precedes the Settings block:

```python
        # ── Watch List tab ────────────────────────────────────────────────
        watchlist_frame = ttk.Frame(self._notebook)
        self._notebook.add(watchlist_frame, text="   👁  Watch List   ")
        self._build_watchlist_tab(watchlist_frame)

        # ── Settings tab ──────────────────────────────────────────────────
        settings_frame = ttk.Frame(self._notebook)
        self._notebook.add(settings_frame, text="   ⚙  Settings   ")
        self._build_settings_tab(settings_frame)
```

- [ ] **Step 4: Run, expect PASS** (skips if the CI/host has no display)

Run: `pytest tests/test_tabs.py -q`
Expected: PASS (or SKIP if headless display unavailable).

- [ ] **Step 5: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py tests/test_tabs.py
git commit -m "feat: swap Watch List and Settings tab order"
```

---

### Task 2.2: Add new settings variables + persistence

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`__init__` var block ~2152; new autosave method near other `_autosave_*`)
- Test: `tests/test_settings_vars.py`

- [ ] **Step 1: Write a failing test** that the App exposes the new vars with correct defaults.

```python
import importlib.util, os, sys, pytest

def _app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    try:
        return m.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")

def test_new_settings_defaults():
    app = _app(); app.update()
    assert app._auto_check_hours.get() == "24 hours"
    assert app._run_at_startup.get() is False
    assert app._minimize_to_tray.get() is False
    app.destroy()
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError`)

Run: `pytest tests/test_settings_vars.py -q`
Expected: FAIL.

- [ ] **Step 3: Add the variables** in `__init__` after the Watch List behavior block (after `self._auto_add_to_watchlist` trace, ~line 2155):

```python
        # Automation settings (auto-check / startup / tray)
        self._auto_check_hours = tk.StringVar(
            value=cfg.get("auto_check_hours", "24 hours"))
        self._run_at_startup = tk.BooleanVar(
            value=cfg.get("run_at_startup", False))
        self._minimize_to_tray = tk.BooleanVar(
            value=cfg.get("minimize_to_tray", False))
        self._watchlist_last_check = int(cfg.get("watchlist_last_check", 0))
        self._auto_check_after_id = None
        self._tray_icon = None  # set when tray is active
        self._auto_check_hours.trace_add("write", self._autosave_automation_settings)
        self._minimize_to_tray.trace_add("write", self._autosave_automation_settings)
```

- [ ] **Step 4: Add the autosave method** next to the other `_autosave_*` methods (after `_autosave_behavior_settings`, ~line 3961):

```python
    def _autosave_automation_settings(self, *_):
        """Persist auto-check interval, tray, and last-check time."""
        cfg = load_config()
        cfg["auto_check_hours"] = self._auto_check_hours.get()
        cfg["minimize_to_tray"] = self._minimize_to_tray.get()
        cfg["watchlist_last_check"] = self._watchlist_last_check
        save_config(cfg)
        # Reschedule the timer whenever the interval changes.
        self._reschedule_auto_check()
```

> `_run_at_startup` is **not** auto-saved here — it is wired to a registry command in Task 2.5 and persisted there (registry is the source of truth). `_reschedule_auto_check` is defined in Task 2.4; until then it won't be called because the Settings UI isn't wired yet.

- [ ] **Step 5: Also persist the four keys in `_save_settings`** — add to the dict in `_save_settings` (~line 3756) so the "Save" button writes them too:

```python
            "auto_add_to_watchlist": self._auto_add_to_watchlist.get(),
            "auto_check_hours":   self._auto_check_hours.get(),
            "run_at_startup":     self._run_at_startup.get(),
            "minimize_to_tray":   self._minimize_to_tray.get(),
            "watchlist_last_check": self._watchlist_last_check,
```

- [ ] **Step 6: Temporarily stub `_reschedule_auto_check`** so Step 3's trace doesn't crash before Task 2.4 lands. Add near other Watch List methods:

```python
    def _reschedule_auto_check(self):
        pass  # replaced in Task 2.4
```

- [ ] **Step 7: Run test + compile**

Run: `pytest tests/test_settings_vars.py -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean compile.

- [ ] **Step 8: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py tests/test_settings_vars.py
git commit -m "feat: add automation settings vars + persistence"
```

---

### Task 2.3: Build the Settings → Automation UI section

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`_build_settings_tab`, after the title/divider ~line 3205)

- [ ] **Step 1: Add the Automation section** as the first content section in `_build_settings_tab` (right after the title label + divider, before "Time / Length Limiter"). Use the existing label/row/Combobox/Checkbutton styles already in that method:

```python
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
```

> `Tooltip`, `BORDER`, and the `S.*` styles already exist in the file. `_on_run_at_startup_toggle` is added in Task 2.5; add a temporary stub method `def _on_run_at_startup_toggle(self): pass` now so the build doesn't fail, to be replaced in 2.5.

- [ ] **Step 2: Verify with headless smoke test** — build the app and confirm the Settings tab constructs and the combo shows the right values:

```bash
python -c "import importlib.util as u,sys; spec=u.spec_from_file_location('cb','DJ-CrateBuilder_v1.3.py'); m=u.module_from_spec(spec); spec.loader.exec_module(m); a=m.MP3DownloaderApp(); a.update(); print('VALUES', a._auto_check_combo['values']); a.destroy()"
```
Expected: `VALUES ('Off', '6 hours', '12 hours', '24 hours', '48 hours')`.

- [ ] **Step 3: Compile**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat: add Settings Automation UI section"
```

---

### Task 2.4: Background scheduler (`after()` loop + auto-download)

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (replace the `_reschedule_auto_check` stub; add helpers; wire startup)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write a failing test** for the interval parser (pure function, easy to unit-test).

```python
import importlib.util, os, sys, pytest

def _mod():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m); return m

def test_auto_check_hours_to_seconds():
    m = _mod()
    f = m.auto_check_hours_to_seconds
    assert f("Off") is None
    assert f("6 hours") == 6 * 3600
    assert f("24 hours") == 24 * 3600
    assert f("nonsense") is None
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError`)

Run: `pytest tests/test_scheduler.py -q`
Expected: FAIL.

- [ ] **Step 3: Add the pure parser** as a module-level function (top of main file, near other helpers):

```python
def auto_check_hours_to_seconds(value):
    """Map an interval dropdown label to seconds, or None for 'Off'/unknown."""
    try:
        if not value or value.strip().lower() == "off":
            return None
        hours = int(value.strip().split()[0])
        return hours * 3600
    except (ValueError, AttributeError, IndexError):
        return None
```

- [ ] **Step 4: Replace the `_reschedule_auto_check` stub** with the real scheduler methods:

```python
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
        self._auto_check_pending = True
        self._watchlist_scan_all()
        # Poll for scan completion, then download + notify.
        self.after(2000, self._auto_check_after_scan)

    def _auto_check_after_scan(self):
        """Once scans settle, download any new tracks and notify."""
        if self._wl_scan_active > 0:
            self.after(2000, self._auto_check_after_scan)
            return
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
```

> `_notify_tray` is added in Task 2.6 (tray). Add a temporary stub now so this compiles:
> `def _notify_tray(self, title, msg): self._watchlist_log(f"🔔 {title}: {msg}", "info")`
> Task 2.6 replaces it with the real pystray notification (keeping the log fallback).

- [ ] **Step 5: Wire startup** — in `__init__`, after the existing `self.after(1200, self._watchlist_populate_from_folders)`, chain the scheduler so discovery runs first:

```python
        self.after(1200, self._watchlist_populate_from_folders)
        self.after(1600, self._reschedule_auto_check)
```

- [ ] **Step 6: Run test + compile + headless smoke**

Run: `pytest tests/test_scheduler.py -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean compile.

- [ ] **Step 7: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py tests/test_scheduler.py
git commit -m "feat: background auto-check scheduler with auto-download"
```

---

### Task 2.5: Run-at-Windows-startup (`cratebuilder/startup.py`)

**Files:**
- Create: `cratebuilder/startup.py`
- Modify: `DJ-CrateBuilder_v1.3.py` (replace `_on_run_at_startup_toggle` stub; init checkbox from registry)
- Test: `tests/test_startup.py`

- [ ] **Step 1: Write tests** that patch a fake registry (so they run on any OS).

```python
import sys, types, pytest
from cratebuilder import startup

class _FakeReg:
    """Minimal in-memory stand-in for winreg."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1; KEY_SET_VALUE = 2; REG_SZ = 1
    def __init__(self): self.store = {}
    def OpenKey(self, root, path, res=0, access=0): return ("k", path)
    def QueryValueEx(self, key, name):
        if name in self.store: return (self.store[name], self.REG_SZ)
        raise FileNotFoundError(name)
    def SetValueEx(self, key, name, r, t, val): self.store[name] = val
    def DeleteValue(self, key, name): self.store.pop(name, None)
    def CloseKey(self, key): pass

def test_set_and_check_startup(monkeypatch):
    fake = _FakeReg()
    monkeypatch.setattr(startup, "winreg", fake, raising=False)
    monkeypatch.setattr(startup, "_startup_command", lambda: '"C:/app.exe"')
    assert startup.startup_is_enabled() is False
    startup.set_startup(True)
    assert startup.startup_is_enabled() is True
    startup.set_startup(False)
    assert startup.startup_is_enabled() is False
```

- [ ] **Step 2: Run, expect FAIL** (module doesn't exist)

Run: `pytest tests/test_startup.py -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Create `cratebuilder/startup.py`**

```python
"""Windows 'run at login' via the per-user Run registry key.

All functions degrade gracefully (return False / no-op) off-Windows or on error.
"""
import os
import sys

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "DJ-CrateBuilder"


def _startup_command():
    """Quoted command Windows should run at login."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Running from source: prefer pythonw.exe (no console window).
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else exe
    script = os.path.abspath(sys.argv[0])
    return f'"{runner}" "{script}"'


def startup_is_enabled():
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup(enabled):
    """Add or remove the Run entry. Returns True on success."""
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ,
                                  _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest tests/test_startup.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into the App.** Add import and replace the `_on_run_at_startup_toggle` stub:

```python
from cratebuilder import startup as cb_startup
```
```python
    def _on_run_at_startup_toggle(self):
        """Add/remove the Windows Run entry to match the checkbox."""
        want = self._run_at_startup.get()
        ok = cb_startup.set_startup(want)
        if not ok and want:
            self._run_at_startup.set(False)  # revert if the write failed
            messagebox.showwarning(
                "Startup", "Could not register the app to run at startup.")
        cfg = load_config()
        cfg["run_at_startup"] = self._run_at_startup.get()
        save_config(cfg)
```

- [ ] **Step 6: Initialise the checkbox from the real registry** so config and registry can't drift. In `__init__`, right after creating `self._run_at_startup`, on Windows override its value:

```python
        if sys.platform == "win32":
            self._run_at_startup.set(cb_startup.startup_is_enabled())
```

- [ ] **Step 7: Compile + full suite**

Run: `pytest -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean.

- [ ] **Step 8: Manual check (Windows)** — toggle the box, then:

Run: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v DJ-CrateBuilder`
Expected: value present when checked, absent when unchecked.

- [ ] **Step 9: Commit**

```bash
git add cratebuilder/startup.py DJ-CrateBuilder_v1.3.py tests/test_startup.py
git commit -m "feat: run-at-Windows-startup via registry (cratebuilder.startup)"
```

---

### Task 2.6: System tray (`cratebuilder/tray.py`) + window lifecycle

**Files:**
- Create: `cratebuilder/tray.py`
- Modify: `DJ-CrateBuilder_v1.3.py` (window protocol, tray start/stop, `_notify_tray`)
- Modify: `requirements.txt` (add pystray, Pillow)
- Test: `tests/test_tray_import.py`

- [ ] **Step 1: Add deps to `requirements.txt`** (create if missing):

```text
yt-dlp
pystray>=0.19
Pillow>=10.0
```

Run: `pip install pystray Pillow`
Expected: installed.

- [ ] **Step 2: Create `cratebuilder/tray.py`** — a thin wrapper that runs the pystray icon on a background thread and marshals menu clicks back to the Tk thread via a supplied `schedule` callable (`app.after`).

```python
"""System-tray icon wrapper (Windows). Tk-free: all UI actions are marshalled
back to the main thread through the `schedule` callback passed in."""
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None
    Image = None


def _default_image():
    """A simple 64x64 icon drawn at runtime (no asset file needed)."""
    img = Image.new("RGB", (64, 64), (20, 20, 20))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 54, 54), fill=(220, 30, 40))
    d.ellipse((26, 26, 38, 38), fill=(20, 20, 20))
    return img


class TrayIcon:
    """Owns a pystray.Icon. on_open/on_scan/on_quit are zero-arg callables
    that must be safe to call (they will be wrapped via `schedule`)."""

    def __init__(self, schedule, on_open, on_scan, on_quit, image=None):
        self._schedule = schedule
        self._icon = None
        self._thread = None
        self._image = image or (_default_image() if Image else None)
        self._on_open = on_open
        self._on_scan = on_scan
        self._on_quit = on_quit

    @property
    def available(self):
        return pystray is not None and self._image is not None

    def start(self):
        if not self.available or self._icon is not None:
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda *_: self._schedule(self._on_open),
                             default=True),
            pystray.MenuItem("Scan Now", lambda *_: self._schedule(self._on_scan)),
            pystray.MenuItem("Quit", lambda *_: self._schedule(self._on_quit)),
        )
        self._icon = pystray.Icon("DJ-CrateBuilder", self._image,
                                  "DJ-CrateBuilder", menu)
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        return True

    def notify(self, message, title="DJ-CrateBuilder"):
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
                return True
            except Exception:
                return False
        return False

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
```

- [ ] **Step 3: Write an import/smoke test** (no real tray shown).

```python
def test_tray_module_constructs(monkeypatch):
    from cratebuilder import tray
    calls = []
    t = tray.TrayIcon(schedule=lambda fn: calls.append(fn),
                      on_open=lambda: None, on_scan=lambda: None,
                      on_quit=lambda: None)
    # `available` may be False if pystray/Pillow not installed in CI — both OK.
    assert hasattr(t, "start") and hasattr(t, "notify") and hasattr(t, "stop")
```

Run: `pytest tests/test_tray_import.py -q`
Expected: PASS.

- [ ] **Step 4: Replace the `_notify_tray` stub** in the App with the real version (keeps log fallback):

```python
    def _notify_tray(self, title, msg):
        """Show a tray notification if the tray is active; always log it."""
        self._watchlist_log(f"🔔 {title}: {msg}", "info")
        if self._tray_icon is not None:
            self._tray_icon.notify(msg, title)
```

- [ ] **Step 5: Add tray lifecycle methods + window handlers** to the App:

```python
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
```

- [ ] **Step 6: Bind the window protocol.** Replace the existing `self.protocol("WM_DELETE_WINDOW", self.destroy)` (~line 2081) with:

```python
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
```

- [ ] **Step 7: Start hidden in tray when auto-launched.** At the very end of `__init__`, after the scheduler wiring:

```python
        # If Windows auto-started us and tray mode is on, begin hidden.
        if (sys.platform == "win32" and self._minimize_to_tray.get()
                and "--startup" in sys.argv):
            self.after(1700, self._hide_to_tray)
```

And in `cratebuilder/startup.py` `_startup_command`, append the flag so launches are detectable — update both branches to end with ` --startup`:

```python
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    ...
    return f'"{runner}" "{script}" --startup'
```

- [ ] **Step 8: Run full suite + compile**

Run: `pytest -q && python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: PASS + clean.

- [ ] **Step 9: Manual check (Windows)** — enable "Minimize to system tray", close the window → app hides to a tray icon; right-click → Open restores; Scan Now runs a scan; Quit exits. With tray off, closing exits normally.

- [ ] **Step 10: Commit**

```bash
git add cratebuilder/tray.py DJ-CrateBuilder_v1.3.py requirements.txt tests/test_tray_import.py
git commit -m "feat: system tray (pystray) + hide-to-tray window lifecycle"
```

---

### Task 2.7: "Fix Link" button — rename + conditional visibility

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`_watchlist_refresh` card-button block, ~line 5923)
- Test: covered by `tests/test_sidecar.py` (predicate) + manual

- [ ] **Step 1: Locate the card button list** in `_watchlist_refresh` (the `card_buttons` construction). Currently it unconditionally includes `("🛠 Resolve", … , False)`. Replace that with conditional insertion using the predicate:

```python
        card_buttons = []
        if is_scanning or is_downloading:
            card_buttons.append(
                ("✕ Cancel", lambda c=cid: self._watchlist_cancel_card(c), True))
        if is_unresolved_channel(ch):
            card_buttons.append(
                ("🛠 Fix Link", lambda c=cid: self._watchlist_resolve_dialog(c), False))
        card_buttons += [
            ("🔍 Scan",    lambda c=cid: self._watchlist_scan_channel(c), False),
            (f"⬇ Download New ({pending})", lambda c=cid: self._watchlist_download_new(c), False),
            ("✏ Edit",     lambda c=cid: self._watchlist_edit_channel(c), False),
            ("✕ Remove",   lambda c=cid: self._watchlist_remove_channel(c), False),
        ]
```

> `is_unresolved_channel` is already imported (Task 1.2). `ch` is the current channel dict in the refresh loop — confirm the loop variable name in source and match it.

- [ ] **Step 2: Compile + headless smoke**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: clean.

- [ ] **Step 3: Manual check** — an unresolved card ("needs channel ID") shows **Fix Link**; a resolved card does not. Edit dialog's URL field still lets you re-point a resolved channel.

- [ ] **Step 4: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat: rename Resolve to Fix Link, show only when unresolved"
```

---

### Task 2.8: Startup folder/channel discovery — confirm incremental

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`_watchlist_populate_from_folders` — only if not already dedup-safe)
- Test: `tests/test_db.py` (dedup already covered)

- [ ] **Step 1: Read `_watchlist_populate_from_folders`** and confirm it inserts a row only when one does not already exist for that URL (relying on `UNIQUE(url)` or an explicit existence check). If insertions are wrapped so a duplicate is skipped (not raised), **no code change is needed** — note that and skip to Step 3.

- [ ] **Step 2 (only if needed): Guard the insert** so re-running on startup never errors or duplicates. Example pattern (adapt to the real loop/insert call):

```python
            existing_urls = {c["url"] for c in self._db.get_all_watchlist_channels()}
            ...
            if url in existing_urls:
                continue  # already tracked — incremental discovery skips it
```

- [ ] **Step 3: Manual check** — add a new channel folder with a `cratebuilder.json` under the Music library, relaunch; it appears in the Watch List exactly once. Relaunch again; still exactly once.

- [ ] **Step 4: Compile + full suite + commit**

```bash
python -m py_compile DJ-CrateBuilder_v1.3.py && pytest -q
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat: ensure startup folder discovery is incremental/idempotent"
```

---

### Task 2.9: About-tab FAQ — Watch List entries

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` (`_build_about_tab`, the `faq` list ~line 4039)

- [ ] **Step 1: Append these tuples** to the `faq` list (before the rendering loop):

```python
        ("Q: What is the Watch List?",
         "A: It tracks YouTube channels you care about and surfaces only "
         "genuinely-new uploads — tracks you haven't already downloaded — so "
         "you never re-grab your whole library."),
        ("Q: How do channels get added to the Watch List?",
         "A: Three ways: manually with 'Add Channel'; automatically after you "
         "download a channel (if 'Auto-add channels' is on in Settings); and "
         "auto-discovered from your existing download folders each time the app "
         "starts."),
        ("Q: What does 'needs channel ID' / the 'Fix Link' button mean?",
         "A: A channel whose link couldn't be resolved to YouTube's canonical "
         "channel ID (often added from a folder name). Click 'Fix Link', pick the "
         "right channel from the search results, and it's healed. Resolved "
         "channels don't show the button."),
        ("Q: How does automatic checking and downloading work?",
         "A: In Settings → Automation, set 'Check watched channels every…' "
         "(default 24 hours). On that interval the app scans every watched "
         "channel and automatically downloads any new tracks into their folders, "
         "using your bitrate/throttle/skip settings, and shows a tray "
         "notification summarising what it grabbed."),
        ("Q: What do 'Run at startup' and 'Minimize to system tray' do?",
         "A: 'Run at startup' launches DJ-CrateBuilder when Windows starts. "
         "'Minimize to system tray' keeps the app (and its scheduled checks) "
         "running in the background when you close the window — find it in the "
         "tray, right-click for Open / Scan Now / Quit."),
```

- [ ] **Step 2: Compile + headless smoke** (About tab builds)

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "docs: add Watch List automation FAQ entries to About tab"
```

---

# PHASE 3 — In-file tidy

> Behaviour-preserving cleanup. After **each** task: `pytest -q` + `py_compile` + headless smoke must stay green.

### Task 3.1: Section banners + logical grouping

**Files:** Modify `DJ-CrateBuilder_v1.3.py`

- [ ] **Step 1:** Add/normalise comment banners delineating regions in the main file, in this order where practical: module constants → module helpers (now thin, most moved) → styles → `App.__init__` → tab builders → Main-tab logic → Settings-tab logic → Watch-List logic → scheduler/tray → misc helpers → `__main__`. Do **not** move method bodies across class boundaries; only reorder within the class and add banners. Keep diffs reviewable (banners + small reorders only).
- [ ] **Step 2:** Compile + full suite + headless smoke. Expected: green.
- [ ] **Step 3:** Commit: `git commit -am "refactor: section banners and grouping in main file"`

### Task 3.2: Remove dead code + magic constants

**Files:** Modify `DJ-CrateBuilder_v1.3.py`

- [ ] **Step 1:** Remove commented-out/unreachable blocks you can prove are dead (search for large commented spans; confirm no references). 
- [ ] **Step 2:** Promote repeated literals to module constants near the top: the interval options list (`AUTO_CHECK_OPTIONS = ["Off", "6 hours", "12 hours", "24 hours", "48 hours"]` — reuse in Task 2.3's combobox), the registry value name (already in `startup.py`), and the `unresolved://` sentinel prefix if duplicated. Replace literals with the constants.
- [ ] **Step 3:** Compile + full suite + headless smoke. Expected: green.
- [ ] **Step 4:** Commit: `git commit -am "refactor: remove dead code, hoist magic constants"`

### Task 3.3: De-duplicate boilerplate + add docstrings

**Files:** Modify `DJ-CrateBuilder_v1.3.py`

- [ ] **Step 1:** Add a small helper for the repeated daemon-thread start pattern and use it where safe:

```python
    @staticmethod
    def _run_bg(target, *args):
        """Start `target(*args)` on a daemon thread (fire-and-forget)."""
        threading.Thread(target=target, args=args, daemon=True).start()
```
Replace obvious `threading.Thread(target=…, daemon=True).start()` call sites with `self._run_bg(…)` **only where the call is a straightforward fire-and-forget** (skip any that capture the thread handle).
- [ ] **Step 2:** Add one-line docstrings to public-ish methods that lack them (tab builders, Watch List actions, scheduler methods).
- [ ] **Step 3:** Compile + full suite + headless smoke. Expected: green.
- [ ] **Step 4:** Commit: `git commit -am "refactor: de-duplicate thread boilerplate, add docstrings"`

### Task 3.4: Delete leftover temp scripts

**Files:** Delete `_anjuna.py`, `_uitest2.py`

- [ ] **Step 1:** These are untracked temp scripts. The harness cannot delete files — **ask the user** to delete `_anjuna.py` and `_uitest2.py` from the repo root, or confirm they already did.
- [ ] **Step 2:** Confirm `git status` shows them gone. No commit needed (they were never tracked).

---

# PHASE 4 — Build, docs & final verification

### Task 4.1: Update build + requirements + README

**Files:** Modify `requirements.txt`, `README.md`, `Packaging_Guide.md` (if present), the `.iss` only if needed

- [ ] **Step 1:** Confirm `requirements.txt` lists `yt-dlp`, `pystray`, `Pillow`. 
- [ ] **Step 2:** Update the PyInstaller command in README/Packaging_Guide to ensure the package and tray deps are bundled:

```bash
pyinstaller --noconfirm --clean --name "DJ-CrateBuilder" --windowed --onedir ^
  --collect-submodules cratebuilder ^
  --hidden-import pystray._win32 --hidden-import PIL.ImageDraw ^
  DJ-CrateBuilder_v1.3.py
```
- [ ] **Step 3:** Update README Features/Settings/Version-History to mention auto-check, run-at-startup, tray, and "Fix Link". Note `requirements-dev.txt` + `pytest -q` for contributors.
- [ ] **Step 4:** Commit: `git commit -am "build: bundle cratebuilder package + tray deps; doc updates"`

### Task 4.2: Final full verification

- [ ] **Step 1:** `pytest -q` → all green.
- [ ] **Step 2:** `python -m py_compile DJ-CrateBuilder_v1.3.py` → clean.
- [ ] **Step 3:** Headless smoke snippet → 4 tabs in order Main, Watch List, Settings, About.
- [ ] **Step 4:** Launch the real app (`python DJ-CrateBuilder_v1.3.py`) and walk the manual checklist: tab order; interval combo persists + reschedules; run-at-startup writes/removes the registry key; tray hide/restore/Scan/Quit; Fix Link visibility; a forced short-interval tick downloads + notifies; startup folder discovery is idempotent.
- [ ] **Step 5:** Build the Windows exe/installer per Task 4.1 and smoke-test the packaged app (tray + startup work when frozen).
- [ ] **Step 6:** Final commit if anything changed; then hand off to the finishing-a-development-branch flow to push `v1.3`.

---

## Self-review (completed during planning)

- **Spec coverage:** tab swap (2.1), Automation settings UI+vars+persist (2.2/2.3), scheduler+auto-download+notify (2.4/2.6), run-at-startup (2.5), tray (2.6), Fix Link (2.7), startup discovery (2.8), FAQ (2.9), hybrid extraction (1.1–1.3 + 2.5/2.6 new modules), in-file tidy (3.1–3.3), test harness first (0.1–0.5), deps + build (2.6, 4.1). All spec sections map to tasks.
- **Sequencing safety:** stubs for `_reschedule_auto_check` (2.2→2.4), `_on_run_at_startup_toggle` (2.3→2.5), and `_notify_tray` (2.4→2.6) keep every intermediate commit compiling.
- **Naming consistency:** `auto_check_hours_to_seconds`, `_auto_check_hours`, `_reschedule_auto_check`, `_auto_check_tick`, `_auto_check_after_scan`, `is_unresolved_channel`, `set_startup`/`startup_is_enabled`, `TrayIcon`, `_notify_tray`, `_on_window_close` used consistently across tasks.
- **Known unknowns flagged for the implementer:** exact `DownloadsDatabase` insert method name (2.5 test note / Task 1.3), the `_watchlist_refresh` loop variable name (2.7), and whether `_watchlist_populate_from_folders` is already dedup-safe (2.8). Each task says to confirm against source.

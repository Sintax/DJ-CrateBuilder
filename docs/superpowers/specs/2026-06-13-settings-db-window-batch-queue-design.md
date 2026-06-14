# Design: Startup-Scan Toggle, Database Window, and Active-Channel Batch Queue

**Date:** 2026-06-13
**Status:** Approved
**Target file:** `DJ-CrateBuilder_v1.3.py` (plus a read method in `cratebuilder/db.py`)

## Summary

Three independent features:

1. **Scan-on-startup toggle** — an Automation setting (default ON, with tooltip)
   that gates the existing startup scan.
2. **Database window** — a new `Toplevel` (modeled on `LogViewerWindow`) that
   presents the downloads history as a file-explorer-style tree and the watch
   list as a sortable table, opened from a button in Settings.
3. **Active-channel Batch Queue** — during a Watch List download, the Main tab's
   Batch Queue container shows the full list of channels in the running batch,
   highlighting the one currently downloading.

---

## Feature 1 — "Scan on startup" toggle

### Behavior
On launch, `_watchlist_startup_scan()` (DJ-CrateBuilder_v1.3.py:3631, scheduled
at :1758) currently runs unconditionally. Add a persisted toggle that gates it.
Default **ON** to preserve current behavior.

### Changes
- **Config key:** `watchlist_scan_on_startup` (bool, default `True`).
- **Var:** `self._watchlist_scan_on_startup = tk.BooleanVar(value=cfg.get("watchlist_scan_on_startup", True))`,
  declared alongside the other automation vars (~:1729), traced to
  `_autosave_automation_settings`.
- **Persistence:** add the key to `_autosave_automation_settings` (:3618) and to
  `_save_settings` (:3376).
- **Gate:** at the top of `_watchlist_startup_scan()`, return early if the toggle
  is off (before the "Startup check: scanning…" log line).
- **UI:** a `ttk.Checkbutton` in the Automation section (:2819), placed directly
  under the "Check watched channels every" row, cross-platform (NOT inside the
  `if sys.platform == "win32"` block), styled `S.Bold.TCheckbutton`, text
  "Scan Watch List for new uploads when the app starts".
- **Tooltip:** "When enabled, scans every watched channel for new uploads the
  moment the app starts, so cards show current new-track counts. Turn off to
  skip the startup scan and check manually."

---

## Feature 2 — Database window (`DatabaseViewerWindow`)

A new `tk.Toplevel` subclass, defined near the other viewer windows (after
`DebugLogViewerWindow`, ~:794). Dark-themed, centered over parent, same
construction idiom as `LogViewerWindow` (DJ-CrateBuilder_v1.3.py:416).

### Entry point
In the Downloads Database settings section (:3317), add a button
**"🗂  Open Database"** (style `Save.TButton`) in `db_row`, before the existing
"🔄 Rebuild Database from Log" button. It calls a new app method
`_open_database_viewer()` which instantiates `DatabaseViewerWindow(self, self._db)`
(guarding against opening duplicates is optional; matching the log viewers'
behavior — a fresh window each click — is acceptable).

### Data access
Add `DownloadsDatabase.get_all_downloads()` to `cratebuilder/db.py`:
returns `list[dict]` of all `downloads` rows ordered by `download_timestamp DESC`.
Reuse the existing `get_all_watchlist_channels()` for tab 2.

The window loads both result sets into memory once on open; all grouping,
filtering, and sorting happen in Python. A **Refresh** button reloads from the DB.
(Rationale: simpler and snappier than re-querying per interaction; dataset size
is fine for in-memory handling on a desktop app.)

### Layout
A `ttk.Notebook` with two tabs.

#### Tab 1 — Downloads (file-explorer tree)
- A `ttk.Treeview` where `#0` is the expandable tree column and detail columns
  are: **Title, Channel, Genre, Platform, Upload, Downloaded, Bitrate**.
  (Leaf rows put the track title in `#0`; group rows put the group label + count
  in `#0`.)
- **Toolbar controls:**
  - **Group by** combobox with preset hierarchies:
    `Platform › Genre › Channel` (default), `Genre › Channel`, `Channel`,
    `Platform › Channel`. Parent nodes show counts, e.g. `House (124)`.
  - **Platform** filter combobox (All + distinct platforms).
  - **Genre** filter combobox (All + distinct genres).
  - **Search** entry — case-insensitive substring match over title + channel,
    live (`trace_add`).
  - **Expand All / Collapse All / Refresh / Export CSV** buttons.
- **Sorting:** clicking a column header sorts leaves within each parent group and
  sorts group nodes among themselves; clicking again reverses. The `#0`/Title
  header sorts by display name.
- **Open file / folder:**
  - Double-click a leaf → open its containing folder.
  - Right-click a leaf → context menu: **Open File**, **Open Containing Folder**,
    **Copy Path**. Reuse the app's existing OS-open approach
    (`os.startfile` on Windows; `subprocess` open elsewhere — match
    `_open_log_external`). Rows whose `file_path` is missing on disk degrade
    gracefully (folder-open falls back to the nearest existing parent; a missing
    path shows an info dialog rather than erroring).
- **Export CSV:** writes the *current filtered* leaf rows (respecting filters +
  search, ignoring grouping) to a user-chosen file via
  `filedialog.asksaveasfilename`, using the `csv` module. Columns match the
  detail columns plus file path.
- **Summary stats bar** (bottom): `Showing X of Y tracks • N channels •
  G genres • P platforms`, recomputed whenever filters/search change.

#### Tab 2 — Watch List
- A flat, sortable `ttk.Treeview` (no tree column) of watched channels:
  **Channel, Platform, Genre, Cutoff, Last scan, Pending new, Total downloaded,
  Status**. Column-header click sorts. Uses the same relative/readable timestamp
  formatters already in the file (`format_timestamp_relative`,
  `format_yyyymmdd_readable`).

### Styling
Reuse module color constants (`BG`, `SURFACE`, `SURFACE2`, `BORDER`, `TEXT`,
`TEXT_DIM`, `YT_RED`, etc.). Configure a `ttk.Treeview` style for the dark theme
within the window (matching how the app themes ttk widgets). Toolbar built like
`LogViewerWindow._build_ui` (a `SURFACE2` frame with flat buttons).

---

## Feature 3 — Active-channel Batch Queue

During a Watch List download the Batch Queue container (DJ-CrateBuilder_v1.3.py:2150)
is idle (manual downloads are disabled). Temporarily repurpose it to show the
running batch's channels with the active one highlighted, then restore the
user's manual batch view when the run ends.

### State
- `self._wl_batch_channels = []` — ordered channel display names for the running
  watchlist batch (set in `_watchlist_download_new` and
  `_watchlist_download_all_new`).
- `self._wl_batch_active_idx = -1` — index of the channel currently downloading.

Both initialized in `__init__` near the other batch state (:1726).

### Rendering
- `_batch_rebuild_rows()` (:2247) gains a branch: when
  `self._wl_download_active` is true **and** `self._wl_batch_channels` is
  non-empty, render one row per channel instead of the manual URL rows:
  - `✓` (done, dim) for index `< active_idx`,
  - `⬇` (active, highlighted background `YT_DARK`/accent) for `== active_idx`,
  - `○` (pending, dim) for `> active_idx`.
  - Header label `self._batch_count_lbl` →
    `⬇ Watch List — downloading {active_idx+1} of {n} channels`.
- When not in watchlist mode, behavior is unchanged (manual `self._batch_urls`).

### Driving the active index
- `_batch_worker` (:4584) already calls `_batch_highlight(url_idx)` per item
  (:4618). Add: when a watchlist batch is active, also set
  `self._wl_batch_active_idx = url_idx` and call `_batch_rebuild_rows()` (via
  `self.after(0, …)`). For watchlist batches `run_batch` order matches
  `_wl_batch_channels` order, so the index lines up.
- On batch completion (the `finally` block / the existing
  `self.after(0, self._batch_rebuild_rows)` at :4648), clear
  `self._wl_batch_channels = []` and `self._wl_batch_active_idx = -1` so the
  panel re-renders the user's manual batch.

### Notes
- Single-channel "Download New" (`_watchlist_download_new`) sets a 1-element
  `_wl_batch_channels`; the same rendering path applies.
- Per-card cancel on the Watch List tab is unchanged; this feature is display-only.

---

## Testing

Following the existing module-test convention (Tkinter UI not unit-tested):

- `tests/test_db.py`: `get_all_downloads()` returns inserted rows as dicts in
  `download_timestamp DESC` order; empty DB returns `[]`.
- Config/settings-vars tests: `watchlist_scan_on_startup` defaults to `True`
  when absent and round-trips through config save/load.

Manual verification (documented, not automated):
- Toggle off → relaunch → no startup scan; toggle on → startup scan runs.
- Open Database → both tabs populate; group-by/filter/search/sort/export/
  open-folder all work.
- Start a multi-channel Watch List download → Main tab Batch Queue lists the
  channels and advances the highlight; restores manual batch when done.

## Out of scope
- Editing/deleting DB rows from the window (read-only viewer).
- Live SQL filtering (in-memory is sufficient).
- Changes to the auto-check timer or tray behavior.

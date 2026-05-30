# DJ-CrateBuilder v1.3 — Watch List Automation & Settings (Final)

**Date:** 2026-05-30
**Branch:** `v1.3` (final release; no version bump)
**File:** `DJ-CrateBuilder_v1.3.py` (single-file Tkinter app, ~7181 lines)

## Goal

Add the final batch of automation features to the Watch List and Settings tabs so the
app can run unattended — periodically checking watched channels for new uploads and
auto-downloading them — with the option to launch at Windows startup and live in the
system tray.

## Scope (7 changes)

1. Swap the **Watch List** and **Settings** tab positions.
2. Add a **Settings → Automation** section with an auto-check interval dropdown,
   a run-at-Windows-startup checkbox, and a minimize-to-system-tray checkbox.
3. A **background scheduler** that scans all watched channels on the chosen interval
   and auto-downloads new tracks, then fires a tray notification.
4. **Startup discovery** of new channel folders/channels on the local machine.
5. Rename the per-card **Resolve** button to **Fix Link**, shown only when the channel
   link is unresolved.
6. Update the **About-tab FAQ** with Watch List usage entries.
7. Persist the new settings.

Out of scope: per-folder (staggered) intervals, cloud sync, non-Windows tray support.

---

## 1. Tab reorder

In `_build_ui()` (~line 2887), reorder the `_notebook.add()` calls so tab order becomes:

**Main → Watch List → Settings → About**

Move the Watch List `add()` block above the Settings `add()` block. No logic change;
the build methods (`_build_settings_tab`, `_build_watchlist_tab`) are untouched.

---

## 2. Settings → "Automation" section

New section inserted near the top of `_build_settings_tab` (after the title/divider,
before "Time / Length Limiter"). Uses the existing section-label + row + Checkbutton /
Combobox patterns already in the file.

### 2a. Auto-check interval dropdown
- Label: **"Check watched channels every:"**
- `ttk.Combobox`, `state="readonly"`, values: **`Off, 6 hours, 12 hours, 24 hours, 48 hours`**
- Bound to `self._auto_check_hours` (StringVar). **Default: `24 hours`** (auto-check ON
  out of the box).
- Changing it reschedules the background timer immediately (Section 3).

### 2b. Run at Windows startup
- `ttk.Checkbutton`: **"Run DJ-CrateBuilder when Windows starts"**
- Bound to `self._run_at_startup` (BooleanVar, default **off**).
- Toggling writes/removes a value in
  `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` via `winreg`
  (per-user, no admin). Value name: `DJ-CrateBuilder`.
- Command string targets the current executable:
  - Frozen (PyInstaller): `"<sys.executable>"` (the bundled `.exe`).
  - From source: `"<pythonw.exe>" "<abs path to DJ-CrateBuilder_v1.3.py>"` (prefer
    `pythonw.exe` to avoid a console window; fall back to `python.exe`).
- Helper functions: `startup_is_enabled()`, `set_startup(enabled: bool)` — both
  wrapped in try/except so registry failures degrade gracefully (log, uncheck box).
- On startup the box is initialised from the *actual* registry state, not just config,
  so they can't drift.

### 2c. Minimize to system tray
- `ttk.Checkbutton`: **"Minimize to system tray (keep Watch List running in background)"**
- Bound to `self._minimize_to_tray` (BooleanVar, default **off**).
- When **on**: the window-close (`WM_DELETE_WINDOW`) and minimize handlers **hide** the
  window to a tray icon instead of calling `.destroy()`. Tray icon right-click menu:
  **Open**, **Scan Now**, **Quit**.
- When **off**: behaviour is unchanged — closing the window exits the app.

---

## 3. Background scheduler

- A single `self.after()`-based rescheduling timer on the Tk main thread (no extra
  thread; it only *kicks off* the existing daemon-thread scan/download machinery).
- State: `self._auto_check_after_id` (the pending `after` handle) and persisted
  `watchlist_last_check` (epoch seconds).
- **Tick behaviour:**
  1. If interval is `Off`, the timer is cancelled and not rescheduled.
  2. On tick: call the existing `_watchlist_scan_all()`. When scans complete and report
     new tracks, auto-trigger `_watchlist_download_all_new()` (the same code the
     "Download All New" button uses) so downloads honour bitrate/throttle/skip settings.
  3. After the batch, fire a tray notification summarising results
     (e.g. *"Watch List: 5 new tracks downloaded across 2 channels"*). If nothing new,
     no notification (silent).
  4. Record `watchlist_last_check = now`, persist, and schedule the next tick.
- **Catch-up on launch:** at startup, if `now - watchlist_last_check >= interval`, run a
  tick shortly after the UI settles (reuse the existing `after(1200, …)` startup slot,
  sequenced *after* folder discovery in Section 4). Otherwise schedule the first tick for
  the remaining time.
- **Runs whenever the app is open** (foreground or tray).
- **Notifications** use pystray's built-in `icon.notify(message, title)` — no extra
  dependency beyond pystray + Pillow. If the tray icon isn't running (tray disabled),
  fall back to writing the summary to the scan log only.
- **Reentrancy guard:** if a scan/download batch is already active
  (`self._downloading` or `self._wl_download_active`), the tick skips this round and
  reschedules, so a manual operation is never interrupted.

---

## 4. Startup folder/channel discovery

- The app already calls `_watchlist_populate_from_folders()` on launch
  (`after(1200, …)`). Confirm/ensure it is **incremental and idempotent**:
  - Walks `base_dir/YouTube/<Genre>/<Channel>` (and SoundCloud where applicable).
  - For each channel folder, reads `cratebuilder.json`; adds a Watch List row only if
    one does not already exist (deduped by the DB's `UNIQUE(url)` constraint — inserts
    that would collide are skipped, not errored).
  - Folders with a sidecar `channel_id` are added `status="idle"`; folders without are
    added `status="needs_resolve"` with the `unresolved://…` sentinel URL.
- No auto-download is triggered by discovery itself; new channels simply become eligible
  for the next scheduler tick.
- Ordering at startup: **discovery → then scheduler catch-up tick** (so newly discovered
  channels are included in the first auto-check).

---

## 5. "Fix Link" button (renamed Resolve)

- In the per-card button list (`_watchlist_refresh`, ~line 5923), rename the
  `🛠 Resolve` entry to **`🛠 Fix Link`** (same command `_watchlist_resolve_dialog`).
- Show it **only** when `_is_unresolved_channel(ch)` is true (status `needs_resolve` /
  `error`, URL starting with `unresolved://`, or a space in the URL). Build the button
  list conditionally rather than unconditionally appending it.
- Resolved channels show no Fix Link button. Manual re-fixing of a resolved channel
  remains possible through the existing **Edit** dialog's Channel/Playlist URL field.

---

## 6. About-tab FAQ additions

Append Q&A tuples to the `faq` list in `_build_about_tab` (~line 4039), covering:

- **What is the Watch List?** — tracks channels and surfaces only genuinely-new uploads.
- **How do channels get added?** — manually via Add Channel, auto-added after downloading
  (if enabled), and auto-discovered from your existing folders on startup.
- **What does "needs channel ID" / the Fix Link button mean?** — a channel whose link
  couldn't be resolved to a canonical ID; click Fix Link to pick the right channel.
- **Automatic checking & downloading** — the "Check watched channels every…" setting;
  what the interval does and that new tracks auto-download to their folders with a tray
  notification.
- **Run at startup / system tray** — what the two checkboxes do and how to quit from the
  tray.

---

## 7. New persisted settings

Added to the existing `load_config()` / autosave pattern in `App.__init__`:

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `auto_check_hours` | string | `"24 hours"` | dropdown value; `"Off"` disables |
| `run_at_startup` | bool | `false` | mirrored to registry; registry is source of truth on load |
| `minimize_to_tray` | bool | `false` | |
| `watchlist_last_check` | int (epoch s) | `0` | drives catch-up tick |

Each gets a `tk.Variable` with `.trace_add()` to the autosave handler, following the
established pattern.

---

## Dependencies

- **New:** `pystray`, `Pillow` (tray icon + notifications). Add to `requirements`,
  the PyInstaller build (`--hidden-import` if needed), and the Inno Setup bundle.
- **Stdlib:** `winreg` (Windows-only; guarded by `sys.platform == "win32"`).
- Tray + startup features are Windows-only; on other platforms the checkboxes are hidden
  or disabled (the app currently targets Windows + Linux — Linux simply won't show tray /
  startup options).

## Testing

No automated suite exists. Verify by:
- `python -m py_compile DJ-CrateBuilder_v1.3.py`.
- Headless UI build via `importlib` (the `__main__` guard stops mainloop) to confirm tab
  order and that the Settings/Watch List/About tabs construct without error.
- Manual: tab order; interval dropdown reschedules; startup checkbox writes/removes the
  registry value (verify with `reg query`); tray hide/restore/Quit; Fix Link visibility
  on a resolved vs unresolved card; a forced short-interval tick downloads + notifies.
- Use a **copy** of `cratebuilder.db` and stub `_resolve_save_dir` for any DB tests so the
  real Music library is never written to.

## Risks / gotchas

- pystray runs its own loop; the icon must be created/run on a background thread and all
  Tk calls from menu actions marshalled back via `self.after(0, …)`.
- Default-on 24h auto-check means the app downloads unattended out of the box — intended,
  but called out so it's a deliberate choice.
- `tk.PanedWindow` rejects `highlightthickness` (existing gotcha — unrelated but noted).

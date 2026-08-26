# Web UI v2 — implementation plan

Spec: `UI-design/HANDOFF.md` (binding authority), `UI-design/ui-contract.json`
(machine-readable contract), `UI-design/CrateBuilder Remote v3.dc.html` (visual
reference). Scaffold already landed on `feat/webui-v2` (commit 948947a and the
follow-up settings-drift fixes): shell, Overview, Downloads (idle), Watch List
(read-only), Settings (partial), `cratebuilder/service.py`,
`cratebuilder/ui_strings.py`, `web/` bundle, `web_window.py`.

This plan finishes HANDOFF §9 phases 1–5. Phase 6 (retire tkinter) is out of
scope. The tkinter app must keep working unchanged throughout.

## Global Constraints

These bind every task. Copy them into every reviewer dispatch.

- **No tkinter imports anywhere in `cratebuilder/`.** The package stays
  headless-testable.
- **`cratebuilder/db.py` schema untouched**: `SCHEMA_VERSION` stays 7, no
  `_init_schema` changes, no new tables/columns. Additive *query helpers* are
  allowed (HANDOFF §11 explicitly permits "paged/grouped query helpers;
  schema untouched").
- **Never touch the developer's live data in tests.** Every test uses
  `tmp_path` for config, DB, logs, crate root. A test that opens
  `~/.dj_cratebuilder_config.json` or the repo's `cratebuilder.db` is a
  defect. (`Settings(path=...)` and `DownloadsDatabase(path)` accept explicit
  paths; `CrateBuilderService(settings=..., db_path=...)` accepts both.)
- **`web/theme.css` is a drop-in from the design — do not modify it.** Layout
  and new component CSS go in `web/app.css`.
- **Tooltips come from the shared registry** (`cratebuilder/ui_strings.py`
  via the `ui_strings` RPC), keyed by contract ids, never hardcoded in JS —
  the sole exception is the "not wired yet" reason pattern already in app.js.
  Never disable a control without a tooltip carrying the reason (contract
  rule).
- **No invented actions**: if it is not reachable from the tkinter UI today
  and not marked remote-only in the design, it does not exist in the web UI.
- **Transport gating is server-side**: `update.*` and `fs.*` refused by
  `CrateBuilderService.call` on the remote transport (already implemented —
  do not weaken). The remote transport additionally enforces read-only mode
  and the control lock (Task 11).
- **Event names are exactly** the eleven in `ui-contract.json` `events`:
  `state.patch, progress.current, progress.overall, queue.row,
  batch.finished, watchlist.card, scan.line, log.append, notification,
  host.status, control.holder`. Envelope on the wire: `{type, payload}`.
- **Progress is coalesced to ≤4 events/sec per event type** (HANDOFF §2) at
  the emitter side. Never push per-chunk.
- **JS never polls for state the host can push.** Local mount: host pushes via
  `window.cbApi._push(type, payload)` through pywebview `evaluate_js`.
- **Code style**: `cratebuilder/` modules use one-line module docstrings and
  the existing terse style; no new comments unless the *why* is non-obvious.
  Conventional Commits, subject ~70 chars, imperative: `feat(webui): …`,
  `fix(webui): …`, `test(webui): …`.
- **Do not bump `APP_VERSION` or `APP_BUILD`.** Do not touch
  `scripts/release.py` beyond what Task 12 says. Never run `scripts/release.py`.
- **Verification floor per task**: `python -m pytest -q -m "not gui"` passes
  (~10s, currently 1007 tests). New pure-logic tests accompany every service
  change. UI-only tasks state what was visually verified and how.
- **Windows dev box**: PowerShell is the shell; paths under
  `c:\Users\djsin\Documents\GitHub\DJ-CrateBuilder`. The live library DB in
  the repo root is real user data (31,999 rows) — read-only smoke checks are
  fine; never write to it.

## Shared design decisions (rulings already made)

- **Event bus** lives in `cratebuilder/events.py`; the service owns one
  instance; transports subscribe. Thread-safe, lossy-coalescing per type.
- **Jobs**: long actions return `{"job": "<id>"}` immediately and report via
  events (HANDOFF §3 rule 1). One job may run per category
  (batch | watchlist | maintenance); a second start is refused with a CBError
  message (HANDOFF §3 rule 2 — refused, never queued client-side).
- **Settings bindings**: the app's `Settings` schema keys and stored value
  formats are the source of truth. `service.py` owns a `SETTINGS_BINDINGS`
  table mapping contract key ↔ schema key with display↔stored value
  converters. The web UI only ever sees display values.
- **Remote state** (device tokens, pairing, read-only flag, remote enable)
  lives in its own JSON file `cratebuilder_remote.json` in the app dir,
  owned by `cratebuilder/server.py` — NOT in the Settings schema (the tkinter
  app never reads it). Add the filename to `.gitignore`.
- **Deferred with disabled-plus-reason controls** (do not implement, do not
  remove the controls): updater actions in the web UI (`about.check_updates`
  etc. — touching the updater needs its own ask per house rules), Folders
  Cleanup dialog (`db.folders_cleanup` — the one destructive flow, deserves
  its own ask), TLS termination (documented as "front with
  Tailscale/Cloudflare"), QR pairing image (pairing code + URL as text
  instead).

## Event payload shapes (all tasks conform)

```
progress.current  {title, percent: 0-100|null, speed_text, bitrate_text}
progress.overall  {done, total, downloaded, skipped, errors, percent, eta_text}
queue.row         {id, index, state, title, detail}   state ∈ contract enums.queue_row_states
batch.finished    {downloaded, skipped, errors, cancelled: bool}
scan.line         {ts: "HH:MM:SS", level: "default"|"downloaded"|"skipped"|"error", text}
watchlist.card    one normalized watch-list row (same shape watchlist_list returns)
log.append        {name: "activity"|"debug", lines: [str], offset: int}
notification      {title, body, level: "info"|"success"|"error"}
host.status       {online: bool, transport}
control.holder    {holder: str|null, you: bool}
state.patch       partial snapshot, e.g. {counts: {...}} — receiver merges
```

---

## Task 1 — Event bus, job registry, and the local push bridge

**Files**: new `cratebuilder/events.py`; edit `cratebuilder/service.py`,
`web_window.py`, `web/api.js` (minor); new `tests/test_events.py`.

`cratebuilder/events.py` — one-line docstring module with:

- `class EventBus`: `subscribe(fn) -> unsubscribe_fn` (fn receives
  `(type, payload)`), `emit(type, payload)`. Thread-safe (lock around the
  subscriber list; emit calls subscribers outside the lock; a subscriber
  raising never breaks other subscribers or the emitter).
- `class Coalescer`: wraps a bus for high-rate types. `emit(type, payload)`
  forwards immediately if ≥ `interval` (default 0.25s) has passed for that
  type, else stores the latest payload; a daemon timer flushes pending
  payloads. Non-coalesced types pass straight through. `flush()` forces
  pending out (call on terminal events so the last progress frame is never
  stale). Inject a `now` callable for tests — no sleeps in tests.

`service.py`:

- `CrateBuilderService.__init__` gains `self.events = EventBus()` and
  `self._emit = Coalescer(self.events)` (coalesced types:
  `progress.current`, `progress.overall`).
- New method `emit(type, payload)` used by all later tasks.
- Job registry: `self._jobs = {}` guarded by the existing lock;
  `_start_job(category, target, *args) -> job_id` runs `target` on a daemon
  thread, refuses (CBError, user-facing message) if that category already has
  a live job; `_job_running(category)`; jobs remove themselves on exit.
  A `snapshot()` gains `"running": {"batch": bool, "watchlist": bool,
  "maintenance": bool}`.

`web_window.py` — the push bridge:

- After `webview.start` is given a callable (`func=` arg) or via
  `window.events.loaded`, subscribe to `service.events`: each event is
  pushed with
  `window.evaluate_js(f"window.cbApi && cbApi._push({type_json}, {payload_json})")`.
  JSON-encode with `json.dumps` (handles quoting); guard every push in
  try/except so a closed window never kills the emitter thread. Unsubscribe
  on window closing.

`web/api.js`: no structural change needed (`_push` exists); verify local
transport path and remove nothing.

**Tests** (`tests/test_events.py`, pure logic): subscriber receives emit;
unsubscribe works; raising subscriber doesn't break others; coalescer passes
first event immediately, swallows intermediate, delivers last on flush;
non-coalesced types bypass; job registry: start returns id, double-start same
category raises CBError with a user-facing message, different categories run
concurrently, finished job frees the category.

**Commit**: `feat(webui): event bus, job registry and local push bridge`

## Task 2 — BatchRunner: headless download orchestration

**Files**: new `cratebuilder/batchrun.py`; edit `cratebuilder/service.py`;
new `tests/test_batchrun.py`.

Read first: `cratebuilder/download.py` (TrackPlan / Outcome / Sink /
SkipOrCancel / TrackDownloader), `cratebuilder/crate.py` (CrateLayout,
ChannelCrate, skip_decision, SkipMode, classify_scan_entries,
is_unreleased_entry), `cratebuilder/ydl.py` (YdlSession), and the monolith's
`_batch_worker` (`DJ-CrateBuilder_v1.3.py` around line 10061) to mirror its
per-entry loop semantics — but implement headlessly in the package, do NOT
move or call monolith code.

`cratebuilder/batchrun.py`:

- `class BatchRunner` constructed with injected callables/factories so tests
  never touch the network:
  `BatchRunner(settings, db, emit, *, session_factory=YdlSession,
  downloader_factory=TrackDownloader, ffmpeg_dir=None)`.
- `run(rows)` executes one batch of the service's queue rows
  (`{id, url, genre, platform, state}`) top to bottom:
  - Rows with `state == "skipped"` are logged skipped and passed over.
  - Per row: probe the URL via a `YdlSession` built from the current
    settings snapshot. A single track downloads directly; a
    playlist/channel/set expands to entries (`list_channel`) and each entry
    becomes a track. Duration limiter from `settings.download_policy()`
    (limit_enabled/limit_minutes): over-limit entries are skipped with the
    reason string pattern the activity log uses (`over time limit — H:MM:SS`).
    Unreleased/premiere entries (`is_unreleased_entry`) are deferred, not
    failures.
  - Skip-existing per `download_policy` (skip_existing + skip_mode →
    `crate.skip_decision` with `ChannelCrate` ownership + DB
    `is_video_downloaded`).
  - Save dir: `CrateLayout` naming under
    `settings.get("base_dir")/<platform>/<genre dir>/<channel>` mirroring the
    monolith's layout for channel/playlist rows vs one-off tracks (read
    `_platform_dir`/save-preview logic in the monolith for the exact naming;
    match it).
  - Each track: build a `TrackPlan` (resolve sleep range, session UA via
    `util` helpers as `download_policy`/`cookie_config` dictate, cover-art
    setting, target kbps) and run `TrackDownloader` with a Sink that maps
    `started/progress/bitrate_detected` → `progress.current` events and
    outcome → `queue.row` + counters.
  - Controls: `pause()`/`resume()` (gate between tracks — finish the current
    track, hold before the next), `cancel()` (stop after current track;
    already-downloaded kept), `skip_row(id)` (if that row is now downloading:
    interrupt via the canceller `SkipOrCancel` extra event and move on;
    else mark for pass-over). These mirror the tkinter tooltips' promises
    exactly.
  - Events: `queue.row` on every row state change; `progress.current` per
    Sink; `progress.overall` after each track with ETA text (`~N min left`
    style, simple rolling average); `batch.finished` at the end (cancelled
    flag when cancelled); `scan.line`-style entries are NOT this task
    (watch-list only); `state.patch` with refreshed counts at the end.
  - Activity-log lines: append the same DOWNLOADED/SKIPPED/ERROR line format
    the monolith writes to `activity.log` (read `_log_download`/`_log_error`
    call sites for the format; write via a small injected `log_line(text)`
    callable the service provides that appends to the activity log file in
    the app dir with the same timestamp format).
- `service.py`: `download.start` → `_start_job("batch", ...)` snapshotting
  the current queue; `download.pause/resume/cancel`, `batch.skip` route to
  the runner; starting with an empty (or all-skipped) queue is a CBError.
  While a batch runs, `batch.add` appends live (runner picks it up),
  `batch.remove/move/clear` raise CBError (mirror the tkinter UI, where the
  queue is locked during a run except skip/add).

**Tests** (`tests/test_batchrun.py`): fake session (returns canned
probe/list results) + fake downloader (records plans, returns scripted
Outcomes, honours the canceller) + recording emit. Cover: single track
success path emits started/row/overall/finished; playlist expansion; skip
decisions (already-in-folder skip when policy says so); duration limiter
skip; deferred premiere neither error nor downloaded; pause gates between
tracks; cancel stops after current; skip_row interrupts the running row and
continues; row marked "skipped" in the queue is passed over; empty queue
start raises; counters in batch.finished are right; DB row path — assert
the fake downloader was constructed with the real tmp DB. No network, no
sleeps > 0.05s.

**Commit**: `feat(webui): headless batch download runner with progress events`

## Task 3 — Downloads screen goes live

**Files**: `web/app.js`, `web/index.html`, `web/app.css` (minor),
`web_window.py` only if the bridge needs a fix.

Wire the Downloads screen (design 3b/3c) and the shell quick actions to the
now-real backend:

- Enable Start Downloads / Cancel / Pause / per-row ⏭ with their contract
  tooltips; remove those controls from the "not wired" disabled list.
- Subscribe: `progress.current` fills the Current bar/label/speed/bitrate;
  `progress.overall` fills the Overall bar + counts + ETA; `queue.row`
  updates row states (design 3c row treatments: ✓ done dimmed, ▶ running
  highlighted with `⏭ Skip` warn button, · pending); `batch.finished`
  restores idle state, shows a toast, refreshes the snapshot.
- Header state tag flips Idle ↔ Batch running (`cb-tag--grey` ↔
  `cb-tag--fill`), header Pause/Cancel appear while running (3c).
- The bottom Queue log renders the rolling track list in the 3c format
  (mono, ✓/⊘/✗/▶ markers with the design's colour classes).
- The left-panel footer shows the mini overall bar + `Batch N / M` while
  running (3c panel footer).
- Pause toggles its label (⏸ Pause ↔ ▶ Resume). While running, quick-action
  "Scan all channels" is disabled with the 3c reason tooltip (scan and batch
  can't share the session).
- Reflect `state.patch` counts into the nav badge and Overview numbers.

**Verification**: fast lane still green. Then a REAL end-to-end check the
implementer must run and report: launch `python web_window.py`, add one
short, safe URL (use a Creative Commons/NASA short video URL), start the
batch against a TEMPORARY base_dir (set `base_dir` via the UI to a temp
folder first, restore afterwards — do NOT download into the user's real
crate), watch progress render, confirm the MP3 lands in the temp folder,
then delete the temp folder. Report what was seen. If the machine is
offline, say so explicitly and verify with the runner's fake instead.

**Commit**: `feat(webui): live Downloads screen driving the batch runner`

## Task 4 — Settings for real: bindings + full wiring

**Files**: `cratebuilder/service.py` (SETTINGS_BINDINGS), `web/app.js`,
`web/index.html`, new `tests/test_settings_bindings.py`.

- `SETTINGS_BINDINGS` in service.py: contract key → `(schema_key,
  to_display, from_display)`. Cover at minimum: `bitrate_quality`
  (`"192"` ↔ `"192 kbps"`), `auto_dl_interval` → `auto_download_interval`,
  `log_limit` → `log_max_mb` (int MB ↔ `"N MB"`, unlimited sentinel — read
  the monolith for which value means unlimited), `sleep_preset` (stored
  `"Light  (1–5 s)"` style ↔ contract `"Light"` — read `_resolve_sleep_range`
  for the real stored strings), `cover_art_mode` (stored
  `DEFAULT_COVER_ART_MODE` format vs contract options — read
  `cratebuilder/util.py`), `skip_mode` (verify stored strings match contract
  options; they should), plus identity bindings for every remaining schema
  key the Settings screen renders. `settings.get/set` and `settings_all`
  translate through bindings; unknown contract keys still error cleanly.
- Wire the full Settings screen to the design's 3j card layout: the limiter
  row (checkbox + −/+ buttons + slider + `N min` mono readout), throttle
  Mode/Preset + manual Min/Max seconds (dimmed unless Manual, per 3j),
  cookies card (method/browser/profile/file rows with the file row dimmed
  unless Cookie File method), save-directory card (path field; `Browse…`
  calls `fs.pick_folder` on the local transport and fills the field; text
  edit + validation server-side via settings.set on `base_dir` — service
  canonicalizes and requires the directory to exist or be creatable),
  log-size card (dropdown + Activity/Debug buttons navigating to Task 5's
  screens), database card (Open Database → Task 7 screen; maintenance
  buttons stay disabled until Task 10), `run_at_startup` via
  `cratebuilder/startup.py` on the local transport only (disabled+reason on
  remote — it edits the host registry).
- Checkbox enable/disable dependencies follow the tkinter Settings tab
  (e.g. limiter row inert when disabled; throttle rows inert when Throttle
  off). Read `_build_settings_tab` for the dependency truth.
- Autosave semantics stay: change → `settings.set` → echo → toast.

**Tests**: bindings round-trip every non-identity mapping both directions;
`settings_all` returns display values; setting a display value stores the
stored form (assert via a `Settings(path=tmp)` instance); base_dir
validation rejects a file path and a nonexistent drive cleanly.

**Commit**: `feat(webui): settings bindings and full Settings screen wiring`

## Task 5 — Log viewers (design 3e/3f)

**Files**: `cratebuilder/service.py` (logs.* methods + tail thread),
`web/app.js`, `web/index.html`, `web/app.css`, new `tests/test_service_logs.py`.

Service:

- `logs.tail {name, offset|null, limit}` → `{lines, offset, size, path}`
  reading `activity.log`/`debug.log` from the app dir (errors-replace
  encoding; missing file → empty result, never an exception). `offset` is a
  byte offset; null means "the last `limit` lines".
- `logs.search {name, query, regex: bool}` → `{matches: [{offset, line_no}],
  total}` grepping server-side.
- `logs.download {name}` → local transport: returns `{path}` and the JS
  triggers a browser download of the file contents (pywebview
  ALLOW_DOWNLOADS is already on; use a `blob:` link built from a
  `logs.tail` full read, or `window.create_file_dialog` save — implementer
  picks the simpler that works and reports which). Remote: Task 11 exposes
  `/logs/<name>` as a download route; for now return the path.
- A tail watcher: while at least one client has the log screen open
  (`logs.watch {name, on}`), a daemon thread polls file size at 1s and emits
  `log.append` deltas. Stops when off. (Polling the file is fine — the
  "don't poll" rule is about JS polling the host.)

UI (one shared screen component, two instances — 3e Activity, 3f Debug):

- Breadcrumb bar `‹ Settings · Activity Log` per HANDOFF §1 (nav keeps
  Settings active while open).
- Toolbar exactly per design: filter dropdown (contract enums
  `activity_log_filters` / `debug_log_filters`), Wrap toggle, search box
  with ▲ ▼ ✕ and fixed-width match counter (`2 of 6` / `no match`),
  jump-to-top/bottom, ⟳ Refresh, ⤓ Download. Tooltips from the registry
  (`log.*` ids).
- Log surface uses the `.cb-log` classes; activity lines colour by
  DOWNLOADED/SKIPPED/ERROR/timestamp prefix (parse the `HH:MM:SS |` prefix,
  dim it via the `ts` class); debug lines colour by level. Search hits use
  `mark`/`mark.is-current`.
- Windowed rendering: keep at most ~2000 rendered lines in the DOM;
  jump/scroll loads adjacent windows via logs.tail offsets. `debug.log` at
  the 5 MB cap must open in under a second (acceptance criterion).
- Filter and search operate on the loaded window client-side; full-file
  search calls `logs.search` and navigates by offsets.
- Stats bar + path bar pinned at the bottom (`.cb-statbar`, `.cb-pathbar`)
  with line/size stats like the tkinter viewer.
- `log.append` events append live when tailing (and the Watch List's pinned
  scan log — Task 9 — reuses this event stream).
- Settings' "Activity Log"/"Debug Log"/"Download both" buttons route here /
  trigger downloads.

**Tests**: tail windows/offsets on a synthetic multi-KB file; last-N-lines
mode; search offsets; missing file; watcher emits `log.append` on growth
(inject poll interval; no real sleeps beyond 0.05s).

**Verification**: screenshot both viewers against the real logs (read-only)
and report line-colouring and toolbar states.

**Commit**: `feat(webui): activity and debug log viewers with live tail`

## Task 6 — Paged/grouped query helpers in db.py

**Files**: `cratebuilder/db.py` (additive only), `tests/test_db_paging.py`.

Add to `DownloadsDatabase` (schema untouched):

- `count_downloads(filters)` and
  `query_downloads(filters, *, order_by, descending, limit, offset)` →
  plain dict rows with the columns the contract's `columns.downloads` list
  needs. `filters`: platform, genre, text search (title/channel LIKE),
  group key/value.
- `group_downloads(preset, filters)` → `[{key, label, count}]` for the
  contract's `group_presets` (read `GROUP_PRESETS` in the monolith for the
  preset → SQL mapping; genre/channel/platform/date-style groupings).
- `query_watchlist_rows()` → the `_WL_COLS` shape incl. per-channel
  download counts (reuse `get_channel_download_count` or a single JOIN).
- `query_artwork_rows(filter_name, *, limit, offset)` +
  `count_artwork_rows(filter_name)` for the contract's `artwork_filters`
  (read `_ART_COLS` / `_ART_FILTERS` in the monolith for definitions —
  embedded/sidecar/on-disk distinctions come from the downloads table's
  artwork columns).
- All read-only, pooled-connection, ORDER BY whitelisted column names only
  (no string-interpolated user input; parameterized filters).

**Tests**: seed a tmp DB via `add_download`/`add_watchlist_channel`; cover
paging boundaries, each group preset returns sane counts, search filter,
order-by whitelist rejects unknown columns, artwork filters partition
correctly.

**Commit**: `feat(db): paged and grouped read helpers for the web viewer`

## Task 7 — Database viewer (design 3g/3h/3i)

**Files**: `cratebuilder/service.py` (db.groups/db.query/db.export_csv +
artwork preview), `web/app.js`, `web/index.html`, `web/app.css`.

- One route, three tabs (Downloads / Watch List / Artwork), breadcrumb
  `‹ Settings · Database`, tab help `❔` buttons carrying the long per-tab
  tooltips from the registry (`db.help_*`).
- **Downloads tab**: group-by preset dropdown (contract
  `enums.group_presets`), platform/genre filters, live search box,
  expand/collapse all, Export CSV. Group header rows (`.cb-table tr.is-group`)
  show counts; leaves load on expand via `db.query` (page size 200,
  "load more" row past that). 20k rows must not arrive in one payload
  (acceptance criterion). Sortable headers (click), draggable column order,
  column widths/order persisted in `localStorage`
  (`db_dl_col_widths`/`db_dl_col_order` keys per HANDOFF §2 — client-side,
  not the config file). Zebra rows per theme.css.
- **Watch List tab**: `_WL_COLS` columns; leading checkbox column present
  but the `🧹 Folders Cleanup ‹Smart›` action stays disabled with a
  deferred-reason tooltip (ruling above); ineligible rows show the disabled
  checkbox + explainer tooltip (`db.cleanup_ineligible_*` registry ids;
  service supplies per-row eligibility + reason via the `_wl_eligible`
  logic — read it in the monolith and port the *reason strings* only).
- **Artwork tab**: `_ART_COLS` columns, the five `artwork_filters`, preview
  pane. Preview: `db.artwork_preview {path}` returns `{data_url}` (base64,
  single selected row only — never in row payloads) from the artwork file
  on disk; missing art → empty-state text in the pane.
- Right-click context menu (`.cb-menu`): local transport gets Open File /
  Open Containing Folder (service `fs.reveal {path}` — local-only,
  `os.startfile` / explorer select) + Copy Path; remote hides the first
  two, keeps Copy Path (HANDOFF §6).
- `db.export_csv {tab, filters}` streams the current filtered set to a CSV
  the browser downloads (same mechanism as Task 5 downloads).

**Verification**: run against the real 31,999-row library READ-ONLY;
screenshot each tab; report open time for the Downloads tab (must render
groups without visible stall) and that widths/order survive a reload.

**Commit**: `feat(webui): three-tab database viewer over paged queries`

## Task 8 — Watch-list service actions

**Files**: `cratebuilder/service.py` (or a new `cratebuilder/watchrun.py` if
service.py would exceed ~800 lines), `tests/test_watchrun.py`.

Read first: monolith `_watchlist_scan_*`, `_wl_download_*`,
`_watchlist_edit_channel`, `_resolve_channel_via_search` call paths;
`cratebuilder/sidecar.py`; `cratebuilder/links.py`;
`crate.classify_scan_entries`; db watchlist helpers
(`update_watchlist_scan_result`, `set_watchlist_download_started`,
`update_watchlist_status`, `get_suppressed_reasons`,
`update_watchlist_channel_fields`, `move_channel_downloads`).

Implement, headless, reusing those modules (port orchestration, never
policy):

- `watchlist.scan {id}` / `watchlist.scan_all` → job (category
  "watchlist"): per channel, build the scan URL (`sidecar.watch_scan_url`),
  `list_channel` via YdlSession, classify entries with
  `classify_scan_entries` (is_downloaded via DB, folder keys via
  ChannelCrate, limit from policy, suppressed reasons from DB), write the
  sidecar, `update_watchlist_scan_result` with pending entries JSON in the
  same format the monolith stores (read it — the tkinter app must still be
  able to read what we write), emit `watchlist.card` per channel and
  `scan.line` progress lines in the design's format
  (`SCAN <name> — N entries, M new since last scan`, `ERROR …`, final
  `DONE Scan complete — …`).
- `watchlist.download_new {id}` / `download_all_new` /
  `force_download {id}` → job reusing Task 2's BatchRunner mechanics per
  channel (pending entries → TrackPlans; force = ignore skip-existing for
  that channel); `set_watchlist_download_started`, clear pending on
  success, `watchlist.card` updates with per-channel progress fields
  (design 3d's downloading card: bar %, current title, done count).
- `watchlist.cancel {id}` / `cancel_all` cancel the running job's current
  channel / whole run.
- `watchlist.add {url, genre}`: probe identity (`probe_identity`), refuse
  duplicates (by canonical URL/channel id), insert via
  `add_watchlist_channel`, write the links file via `links.py`.
- `watchlist.edit {id, url?, genre?}`: URL change re-probes identity;
  genre change = `move_channel_downloads` + folder move + retag (mirror
  the monolith's ordering: move folder, rewrite rows, retag; roll the
  folder move back if the DB write fails — read `_watchlist_edit_channel`
  for the exact sequence). `forget_unavailable {id}` maps to
  `forget_unavailable_for_channel`.
- `watchlist.remove {id}` removes the row only (files untouched).
- `watchlist.resolve_candidates {id}` → `search_channels` by folder name,
  return candidates with handle/channel id/subscriber count and a
  `duplicate_of` marker when a candidate matches an existing entry
  (design 3m Fix Link). `resolve_apply {id, channel_id}` writes sidecar +
  links + db fields.

**Tests**: fake YdlSession/downloader throughout; scan classifies and
persists pending JSON the same shape the monolith writes (fixture-check the
JSON structure against a sample created via the db helper); download_new
consumes pending and updates counts; force ignores skip; add rejects
duplicate; edit-genre calls move_channel_downloads and rolls back on a
forced DB failure; resolve marks duplicates. Events recorded and asserted.

**Commit**: `feat(webui): watch-list scan, download and channel management`

## Task 9 — Watch List screen + dialogs (design 3d/3m)

**Files**: `web/app.js`, `web/index.html`, `web/app.css`.

- Modal shell: `.cb-dim` overlay + `.cb-modal` card (add these two classes
  to app.css following theme.css's tokens — theme.css itself untouched;
  match the design HTML's modal look from artboard 3m). Escape / overlay
  click = safe close. Focus is trapped inside while open.
- Wire the toolbar: + Add Channel (modal: URL + genre picker → watchlist.add),
  🛠 Check Links (runs resolve flow over unresolved channels sequentially),
  ⬇ Download All New (count in label, live), 🔍 Scan for new, ✕ Cancel
  (enabled while a watchlist job runs).
- Cards go live per 3d: unresolved cards get the attn tag + orange Fix Link
  button; the downloading card shows the 2px accent border, progress bar,
  current-track line, disabled row actions + red ✕ Cancel; idle cards get
  Scan / Force Download / Download New (n) / Edit / Remove wired. Remove
  confirms via a plain yes/no modal quoting the registry tooltip's promise
  (files untouched).
- `watchlist.card` events update cards in place; `scan.line` streams into
  the pinned scan log (reuse Task 5's log-line renderer; Clear button
  clears the view only; "Open Activity Log ↗" navigates to 3e).
- **Edit Channel modal** per 3m spec: URL field + hint, 📂 Open Folder
  (local-only) / 🌐 Open Link / 🛠 Smart-Edit Link buttons, genre combo with
  `+ New` and `− Remove`, `Forget unavailable tracks (n)`. There is NO
  display-name field. Smart-Edit closes this modal before opening Fix Link
  (the design's modal-grab rule). Remote: Open Folder replaced by a
  copyable path.
- **Fix Link modal** per 3m: radio candidate list (name, handle, channel
  id, subscriber count), duplicates disabled with inline reason, Apply /
  Cancel.
- Quick-action "Scan all channels" in the panel goes live (disabled during
  a batch, with the 3c reason).

**Verification**: screenshot the Watch List with the real channels
(read-only — do NOT run a real scan against 22 live channels; verify scan
via a temporary fake channel entry in a tmp DB launched with
`CrateBuilderService(db_path=tmp)` if needed, or verify visually that
buttons arm and the modals open/close correctly and report exactly what was
exercised).

**Commit**: `feat(webui): live Watch List screen with channel dialogs`

## Task 10 — Maintenance jobs + long-job progress modal

**Files**: `cratebuilder/service.py`, `web/app.js`, `web/index.html`,
`tests/test_maintenance_jobs.py`.

- Jobs (category "maintenance", one at a time, each emitting
  `progress.overall` {done,total,percent} + a `notification` on completion):
  - `db.rebuild` — port the monolith's rebuild orchestration over
    `cratebuilder/rebuild.py` (audio discovery + artwork reuse) into a
    headless job (read the monolith's rebuild action for the sequence;
    clears and rebuilds the downloads table — the documented-safe
    operation).
  - `db.dedupe` — `dedupe_downloads_by_path` + unique-index enablement
    (mirror the monolith's Remove Duplicates action), preceded by a count
    (`count_duplicate_downloads`) surfaced in the confirm modal.
  - `db.repair_tags` — port the monolith's `_repair_track_tags` loop over
    `tagging.py`/`genrefix.py`; cancel-safe (tracks already repaired keep
    their tags — the tooltip's promise).
  - `db.fetch_artwork` — loop `get_downloads_missing_artwork` through
    `artwork.py` ingest/embed with per-track Skip support.
- Long-job modal (design 3m shell): title, current item line, determinate
  bar, counts, Cancel (always safe, wording from the design) and per-track
  Skip for fetch-artwork. Opens from the Settings database card buttons;
  buttons disabled while any maintenance job runs. Match
  `tests/test_dialog_progressbars.py`'s expectations for the dialog
  semantics where applicable (read it first — HANDOFF flags it).
- Confirm modals for rebuild/dedupe quoting the registry tooltips.

**Tests**: with tmp DB + tmp crate fixtures: rebuild ingests a seeded disk
tree; dedupe collapses seeded duplicate rows and reports counts; repair
tags respects cancellation midway (already-processed kept); fetch artwork
skips a track on request. Fake the slow parts (no real mp3 encoding needed —
seed tiny files and stub `mutagen`-touching calls where the real modules
require valid audio; if `tagging.py` needs real MP3 headers, generate a
minimal valid MP3 via the smallest fixture that keeps the real code path).

**Commit**: `feat(webui): database maintenance jobs with progress dialogs`

## Task 11 — Remote transport: FastAPI, WebSocket, pairing, control lock

**Files**: new `cratebuilder/server.py`, new `web_server.py` (entry point:
`python web_server.py [--port]`), `web/api.js` (remote path), `web/app.js`
(pairing screen + offline shell + Remote Access card wiring), `.gitignore`
(+`cratebuilder_remote.json`), `tests/test_server.py`.

`cratebuilder/server.py` (no tkinter):

- FastAPI app factory `create_app(service, remote_state)` serving:
  `web/` static at `/`; `POST /rpc {method, params}` → service.call with
  the same `{ok, result|error}` envelope api.js already unwraps;
  `WS /ws` → subscribes the connection to `service.events`, sends
  `{type, payload}` frames; `GET /logs/{name}` download route (Task 5's
  remote path).
- Auth: every route except the pairing endpoints requires a device token
  (`X-CB-Token` header or `?token=` on the WS). Tokens live in
  `cratebuilder_remote.json` (app dir): `{devices: [{token_hash, name,
  paired_at}], read_only, enabled, require_pairing}` — store SHA-256 of
  tokens, never plaintext. Constant-time compare.
- Pairing: `remote.pair_begin` (LOCAL transport only — the desktop window
  calls it) → 6-digit code, 5-minute TTL, shown in the local UI;
  `POST /pair {code, device_name}` from the browser exchanges it for a
  long-lived token (one use per code). Rate limit: max 5 attempts per
   5 minutes per IP, then refuse with a clear message (HANDOFF §8.5).
  `remote.devices` lists, `remote.revoke {token_hash|all}` revokes.
- Read-only mode: when on, remote `service.call` allows only read methods
  (state.snapshot, ui_strings, settings.get, logs.*, db.query/groups,
  watchlist.list, batch.list) and refuses the rest with the design's
  message. Control lock: first writable client claims control
  (`remote.claim_control`); others get read-only + `control.holder` events
  naming the holder; the local window always holds precedence (HANDOFF §2
  single-writer rule).
- The remote service instance is `CrateBuilderService(transport="remote")`
  sharing the SAME Settings/DB/event bus as the local one — refactor
  `web_window.py`/service construction so one process can host both mounts
  (the desktop window + the server thread) per the HANDOFF diagram: one
  `CrateBuilderService` core object with a per-call transport wrapper, not
  two separate service states. (Implementation note: split transport out of
  the constructor — `service.call(method, params, transport=...)` — keeping
  the existing constructor arg as the default; update `web_window.py` and
  existing tests' expectations minimally.)
- `web_server.py`: builds Settings/service/state, runs uvicorn
  (`uvicorn.Server` in the main thread here — it's the entry point), binds
  `127.0.0.1` by default; `--lan` binds `0.0.0.0` only when
  `remote enabled` is on in `cratebuilder_remote.json`.

UI:

- api.js remote path: attach the token from `localStorage` to /rpc and /ws;
  on 401 → show the pairing screen (design 3k): code entry + device name →
  `POST /pair` → store token, reload state. Host-offline state per 3k: WS
  close → `.cb-offline` shell (controls inert, last state visible, offline
  bar with retry) — the acceptance criterion demands no blank page.
- Settings Remote Access card (3j) goes live on the LOCAL mount: enable
  toggle, require-pairing toggle, read-only toggle, paired-devices list
  with revoke-all (writes `cratebuilder_remote.json` via service methods),
  plus a "pair a device" row showing the active code + URL when begun.
  On remote mounts the card is read-only-with-reason.
- Updater/fs gating: verify `update.*`/`fs.*` refusal still holds through
  /rpc (test it).

**Tests** (httpx/starlette TestClient, no real sockets needed for most):
unauth /rpc → 401; pairing happy path issues token, code single-use, TTL
expiry (inject clock), rate limit trips; token auth passes; revoked token →
401; read-only refuses download.start but allows state.snapshot; control
lock: second client refused writes, holder events emitted; update.check via
/rpc refused; WS receives an emitted event frame (starlette TestClient
supports WS).

**Commit**: `feat(webui): remote transport with pairing, read-only and control lock`

## Task 12 — Overview live, notifications, About, polish, docs

**Files**: `web/app.js`, `web/index.html`, `cratebuilder/service.py`
(minor), `docs/specs/webui-v2-scaffold.md` (update), `README.md` (short
"Web UI (preview)" section).

- Overview (3a) live: Now-running card driven by `progress.*` events with
  Pause/Cancel working from there; Watch List card count + "Open Watch
  List"; Refresh re-snapshots. Notifications panel (3n): a bell in the
  header area collecting `notification` events (scan found tracks, batch
  complete, errors) with mark-read; no OS integration.
- About content (3n): author block from the contract's `about` section
  (avatar asset already in `web/assets/`), GitHub/Submit Issues links
  (`webbrowser.open` via a local-only `fs.open_url` — refuse remotely with
  the copyable URL instead), FAQ accordion with expand/collapse-all, build
  number from `version_info`. Updater controls rendered disabled with the
  deferred-reason tooltip (ruling).
- Polish sweep: nav badge live everywhere; host footer dot/label reflects
  `host.status`; every remaining "not wired" placeholder either wired by
  now or carrying an accurate reason; tooltip pass — every control with a
  registry id has it bound.
- Docs: update `docs/specs/webui-v2-scaffold.md` status section (what
  shipped, what stayed deferred + why), README section on running
  `python web_window.py` / `python web_server.py`, and a PyInstaller note
  (web/ as data files, `--collect-submodules uvicorn` — note only, spec
  files are local-only).
- Full suite `python -m pytest -q` (both lanes) must pass; report the count.

**Commit**: `feat(webui): live overview, notifications, about and docs`

---

## Acceptance criteria (from HANDOFF §10, adjusted for rulings)

- Every control in ui-contract.json exists wired or disabled-with-reason,
  with its tooltip.
- A batch started from the web UI shows current + overall progress, pauses,
  cancels, and skips a URL mid-flight.
- Watch List scan streams into the pinned scan log line by line.
- debug.log at the 5 MB cap opens in under a second and scrolls smoothly.
- The database viewer opens the 31,999-row library without a visible stall;
  column widths and order survive a reload.
- Killing the host leaves the last state on screen, controls disabled, with
  a retry affordance.
- An unpaired browser reaches nothing but the pairing screen.
- The tkinter app still launches and its full test suite passes.
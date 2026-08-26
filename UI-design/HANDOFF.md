# DJ-CrateBuilder — Web UI handoff

Target: route the existing tkinter UI to a web-based control surface, usable from a
browser on the LAN or over the internet, without rewriting the app's logic.

Design source of truth: `CrateBuilder Remote v3.dc.html` (open it in a browser; hover
any control to see its tooltip). Machine-readable contract: `ui-contract.json`.
Drop-in stylesheet: `theme.css`.

---

## 1. What was designed, and what it maps to

The desktop app has four tabs plus three `Toplevel` windows. The web UI keeps every
control, and the three `Toplevel` windows stay where they are today: reached from
Settings, not from the nav. The left panel lists Overview, Downloads, Watch List and
Settings only — Activity Log, Debug Log and the Database viewer are opened by the
buttons in the Settings "Log Size Limit" and "Downloads Database" cards, and show
Settings as the active nav item while they are open. Each of those five screens carries a
breadcrumb bar above its toolbar — a `‹ Settings` button plus the screen's own name —
so the way back is a control, not an inference from the highlighted nav item.

| Web screen | Design id | Replaces | Built from |
|---|---|---|---|
| Overview | `3a` | *(new)* — remote needs a "what is happening" landing | aggregates of the below |
| Downloads (idle) | `3b` | Main tab | `_build_main_tab`, `_batch_*` |
| Downloads (running) | `3c` | Main tab, batch in flight | `_start`, `_cancel`, `_toggle_pause`, `_batch_skip` |
| Watch List | `3d` | Watch List tab | `_build_watchlist_tab`, `_wl_*`, `_watchlist_*` |
| Activity Log | `3e` | `LogViewerWindow` | `activity.log` |
| Debug Log | `3f` | `DebugLogViewerWindow` | `debug.log` |
| Database → Downloads | `3g` | `DatabaseViewerWindow` tab 1 | `DownloadsDatabase`, `_DL_COLS` |
| Database → Watch List | `3h` | `DatabaseViewerWindow` tab 2 | `_WL_COLS`, `cleanup.py` |
| Database → Artwork | `3i` | `DatabaseViewerWindow` tab 3 | `_ART_COLS`, `artwork.py` |
| Settings | `3j` | Settings tab | `_build_settings_tab` |
| Pairing / host offline | `3k` | *(new)* | remote transport |
| Tooltip pattern | `3l` | reference panel | `Tooltip`, `_settings_help`, `_wl_celltip` |
| Dialogs | `3m` | `Toplevel` modals | `_resolve_channel_via_search`, `_watchlist_edit_channel`, `_CleanupReviewWindow`, `_repair_track_tags` |
| Notifications + About | `3n` | tray notifications, About tab | `tray.py`, `_build_about_tab` |

**The updater is local-session-only, not undesigned.** `Check for updates`,
`Update Now` and the auto-check interval are fully enabled in the window running on
the host machine itself, and render disabled (with the reason in their tooltips) in
every remote session — a browser somewhere else must not be able to replace the
binary it is talking to. Gate this on the transport, not on a config flag: the local
pywebview mount may call `update.*`; the FastAPI/WebSocket transport must reject those
calls server-side even if a client asks. `3n` shows the About content read-only — the author credit block from
`ABOUT_FIELDS` (avatar, name, mailto, "Built with"), the GitHub and Submit Issues
buttons, the bug/suggestion note, and the FAQ accordion — plus the build number so you
can tell whether the host is current. `assets/about_avatar.png` is the repo's own
`about_avatar.png`, copied in; it is 44×44, the size the tkinter label uses.

### Dialog convention
The desktop app uses separate `Toplevel` windows. On the web they are **centred modals
over a dimmed page** (`.cb-dim` + `.cb-modal` in `theme.css`), not slide-overs: each one
is a decision that blocks, and a modal says so. Four are designed on `3m` and they
cover every pattern the app needs:

- **Fix Link** — radio list of search candidates, each with handle, channel id and
  subscriber count; a candidate that would duplicate an existing entry is disabled with
  the reason inline. Backed by `watchlist.resolve_candidates` / `resolve_apply`.
- **Edit Channel** (`_watchlist_edit_channel`, :12270; 460×576) — "Channel / Playlist
  URL" with its hint, a row of `📂 Open Folder` / `🌐 Open Link` / `🛠 Smart-Edit Link`,
  the Genre combo with `+ New` and `− Remove`, and `Forget unavailable tracks (n)`.
  **There is no display-name field** — the name comes from the resolved channel. On open
  the dialog verifies the folder location and can silently re-link the database, showing
  a "moved out of band" or "no folder yet" note. Saving a changed genre moves the folder,
  rewrites the downloads rows, and retags each file; a failed database write rolls the
  move back. `Smart-Edit Link` closes this dialog before opening Fix Link so two modal
  grabs cannot fight over focus — keep that ordering.
  Remote: `Open Folder` is host-local; expose the path for copy instead.
- **Folders Cleanup review** (`_CleanupReviewWindow`, :2131; 900×520) — the only
  destructive dialog, and one modal **per channel**, titled `Folders Cleanup — {name}
  ({i} of {total})`. Columns: tick, File, Size, Modified, Reason (Reason is the
  stretching column). **Confidence drives the initial state**: strong matches start
  ticked, weak ones start unticked and render dimmed — do not tick everything by
  default. `Select All` / `Deselect All` sit in the header. Footer: `Confirm Deletions`,
  `Skip Channel` (present only when more than one channel is queued), and
  `Cancel Scans` / `Cancel Scan` (the label is singular for a single channel). Closing
  the window is the safe no-delete exit — skip when multi, cancel when single. Files go
  to the Recycle Bin via `send2trash`.
- **Long-job progress** — one shell for Rebuild Database, Repair Track Tags and Fetch
  Missing Artwork: current item, determinate bar, counts. `Cancel` is always safe
  ("Stop after the current track. Tags already written are kept."); Fetch Artwork also
  has a per-track `Skip`. The internals of the real progress dialog were not read —
  see `tests/test_dialog_progressbars.py` and match it before building.

Dialogs that are a plain yes/no (Remove Duplicates prompt, new-genre confirmation) need
no mockup; use the same `.cb-modal` shell with a single line of body copy.

### The shell
A fixed 236px left panel: brand, a three-control quick-action block (URL field, Add
to batch, Scan all channels), the nav list, and a host-status footer. Content area is
a single scroll region. Every screen uses the same shell — the panel never changes
width or order between screens, and the active item is the only thing that moves.

The nav carries one live count (Watch List pending-new). Do not add more badges; the
Overview screen exists so counts have somewhere to live.

---

## 2. Stack decision

**Use pywebview for the local window, and a FastAPI/uvicorn ASGI app for everything
else. One HTML bundle, two transports, one JS facade.**

```
                    ┌──────────────────────────────────────┐
                    │  cratebuilder/  (unchanged logic)    │
                    │  db · download · scanproc · ydl · …  │
                    └───────────────┬──────────────────────┘
                                    │  CrateBuilderService (new, thin)
                     ┌──────────────┴───────────────┐
                     │                              │
        pywebview js_api                    FastAPI + WebSocket
        (local window)                      (LAN / internet)
                     │                              │
                     └──────────► web/ ◄────────────┘
                          one static bundle
                          cbApi.call() / cbApi.on()
```

Why: `pywebview` gives a native window with no browser chrome, native file dialogs,
and a synchronous-feeling Python bridge, and it packages under the PyInstaller setup
already in `DJ-CrateBuilder.spec`. It does **not** serve HTTP, so remote reach needs a
server anyway — and once you have the server, the same bundle serves both. Writing the
UI once against a transport-agnostic facade is the whole trick.

Rejected, with reasons:

- **NiceGUI** — owns the render layer (server-side Vue/Quasar) and wants to be the
  process's main loop. This design would have to be re-expressed in its component
  vocabulary, and the tkinter app would become a plugin to a web framework. Wrong
  direction for an app whose value is its Python core.
- **Eel** — genuinely the fastest path to a prototype (it is already Bottle +
  websocket, so remote is nearly free) and a legitimate choice if you want something
  running this week. Costs: no native window control, it opens Chrome/Edge in app
  mode rather than owning a window, and its single global function namespace makes
  per-client auth and read-only mode awkward once more than one browser connects. If
  you start here, keep the `cbApi` facade so the swap is one file.

### Advanced features worth using

**pywebview**
- `js_api` class → `window.pywebview.api.method()` returns a Promise. This is the
  local transport; keep every method's signature identical to the WebSocket RPC.
- `window.evaluate_js()` from worker threads to push progress — this is what replaces
  the tkinter `after()` polling. Do not poll from JS.
- `webview.create_file_dialog(webview.FOLDER_DIALOG)` for the two `Browse…` buttons
  (save directory, cookie file). Remote clients get a validated text field instead;
  the host is the only thing that can see the host's filesystem.
- `webview.start(private_mode=False)` so `localStorage` survives restarts — the
  database viewer's column widths and order (`db_dl_col_widths`, `db_dl_col_order`,
  and the `_WL_`/`_ART_` equivalents) can then live client-side instead of round-
  tripping to the config file.
- `webview.create_window()` a second time for the log and database viewers if you
  want them as real OS windows locally, matching today's `Toplevel` behaviour. The
  same route renders in-page for remote clients — one component, two mounts.
- `window.events.closing` to keep the minimize-to-tray contract from `tray.py`.
- Frameless + `easy_drag` if you want the custom titlebar; optional, and it costs you
  the OS window-snapping users expect. Recommend keeping the native frame.
- `webview.settings['ALLOW_DOWNLOADS'] = True` — needed for Export CSV and the log
  download buttons.
- Windows only: `window.native` reaches the HWND, so the batch can drive a taskbar
  progress bar via `ITaskbarList3`. Small touch, reads as native.

**Server side**
- One WebSocket per client for all pushes. JSON envelopes, `{type, payload}`.
- Progress at ~4 Hz, coalesced. The tkinter UI can afford per-chunk updates; a
  remote socket cannot.
- Log tail as append deltas, never whole-file resends (`debug.log` is 4.6 MB).
- `EventSource` fallback for restrictive networks — pushes are one-directional, so
  SSE covers everything except RPC, which can go over `fetch`.
- Single-writer lock: exactly one client may hold "control" at a time; others are
  read-only and see who holds it. This is what stops two phones starting two batches
  against one yt-dlp session.

---

## 3. API surface

Full signatures in `ui-contract.json`. Shape:

```js
// web/api.js — the only file that knows which transport is live
export const cbApi = {
  call(method, params) {},   // → Promise<result>
  on(event, handler) {},     // → unsubscribe
};
```

```python
# cratebuilder/service.py  (new — the only new logic file)
class CrateBuilderService:
    """Every UI action, transport-agnostic. Raises CBError with a user-facing
    message; never returns a tkinter widget or a Tk variable."""
```

Method groups:

- `state.snapshot()` — one call that fills the whole shell on connect. Everything
  else is a delta.
- `batch.add / remove / move / clear / skip / list`
- `download.start / pause / resume / cancel`
- `watchlist.list / add / edit / remove / scan / scan_all / download_new /
  download_all_new / force_download / cancel / cancel_all / resolve_candidates /
  resolve_apply`
- `settings.get / set` — `set` takes one key, validates, autosaves, echoes the
  stored value back. Mirrors today's per-widget autosave exactly.
- `logs.tail / search / download`
- `db.query / groups / export_csv / rebuild / dedupe / fetch_artwork / repair_tags /
  cleanup_scan / cleanup_apply`
- `fs.pick_folder` — local transport only; returns `null` remotely.

Events (host → client):

`state.patch`, `progress.current`, `progress.overall`, `queue.row`, `batch.finished`,
`watchlist.card`, `scan.line`, `log.append`, `notification`, `host.status`,
`control.holder`

### Rules that matter
1. **Every long action is already threaded** in the app. Keep it that way — the
   service returns a job id immediately and reports through events.
2. **The host is the only source of truth.** A remote click that arrives while the
   host is busy is refused with a message, never queued client-side.
3. **No action is invented.** If it is not reachable from the tkinter UI today, it is
   not in the web UI either — except the Remote Access block on `3j`, which is marked
   as remote-only in the design.

---

## 4. Tooltips

Every button, checkbox and dropdown in the design has one. The copy is lifted verbatim
from the `Tooltip(...)` and `_settings_help(...)` calls in `DJ-CrateBuilder_v1.3.py` —
`ui-contract.json` carries the registry with the source line for each.

**Do this: make one registry, shared.** Move the strings into
`cratebuilder/ui_strings.py` as `TOOLTIPS: dict[str, str]`, keyed by the control ids in
`ui-contract.json`. Have tkinter read from it (`Tooltip(w, TOOLTIPS["wl.scan_all"])`)
and expose it to JS via `api.ui_strings()`. Two hand-maintained copies of 60 tooltip
strings will drift within a release.

Three affordances, all in the design:
- **Control tooltip** — wraps the control itself. Delay ~350 ms on hover, instant on
  focus, dismissed on Escape. Touch: long-press.
- **`?` help icon** — a 14px outlined box after a Settings row, matching
  `_settings_help`. Used where the label needs more than a sentence.
- **Disabled-cell explainer** — on a disabled checkbox or button, the tooltip explains
  *why*, matching `_wl_celltip`. Shown on `3h`. Never disable a control without one.

Accessibility: `aria-describedby` on the control, `role="tooltip"` on the popover, and
the popover must be reachable by keyboard focus. The design's hover-only CSS is a
mockup convenience — implement with JS so focus and Escape work.

---

## 5. Log viewers (`3e`, `3f`)

Same shell for both; only the filter values and the line colouring differ.

- Toolbar: filter, wrap toggle, search with ▲ ▼ ✕ and a fixed-width match count
  (`"2 of 6"` / `"no match"` — keep the fixed width so the toolbar never reflows),
  jump-to-top/bottom, Refresh, Download.
- Path bar and stats bar pinned at the bottom, exactly as today.
- Colours: activity log tags on `DOWNLOADED` / `SKIPPED` / `ERROR` / separator lines,
  timestamps dimmed up to the first `|`. Debug log colours by level. Values in
  `theme.css` as `--cb-log-*`.
- **`System Viewer` has no remote equivalent** — a browser cannot open the host's text
  editor. It becomes `⤓ Download`, which streams the file to the client. Keep the
  original button on the local window.

Implementation: `logs.tail(name, offset, limit)` returns a window of lines plus the
new offset; `log.append` events carry deltas. Filter and search operate on the loaded
window client-side; a full-file search calls `logs.search`, which greps server-side and
returns match offsets. Virtualize the list — `debug.log` at the 5 MB cap is ~40k lines.

---

## 6. Database viewer (`3g`, `3h`, `3i`)

Three tabs, one route. Keep `DatabaseViewerWindow`'s behaviours: click a header to
sort, drag a header to reorder, widths and order persisted, zebra rows, right-click
context menu, `❔ Help` carrying the long per-tab tooltip (already in the design,
verbatim from `_HELP_TOOLTIP`).

**Downloads** — group-by presets from `GROUP_PRESETS`, platform/genre filters, live
search, expand/collapse all, Export CSV. Group rows show counts; leaves load on
expand. `db.groups()` returns the tree skeleton with counts, `db.query()` fills a
group — a 20k-row library must not arrive in one payload.

**Watch List** — the `_WL_COLS` set including the leading checkbox column that drives
`🧹 Folders Cleanup ‹Smart›`. Ineligible channels keep their disabled checkbox *and*
the explainer tooltip (`_wl_eligible` already carries the reason string — send it).
Cleanup is two-phase: `cleanup_scan` reports candidates per channel, `cleanup_apply`
deletes only what the user confirmed. Deletions go to the Recycle Bin via
`send2trash`, unchanged.

**Artwork** — `_ART_COLS` and the five `_ART_FILTERS`. The Embedded / Sidecar / On Disk
distinction is the point of this tab; keep all three columns and the preview pane.
Serve previews from a host route (`/artwork/<id>.jpg`), not as base64 in the row
payload.

Right-click actions that touch the host filesystem (`Open File`, `Open Containing
Folder`) are local-only. Remotely they are hidden, and `Copy Path` stays.

---

## 7. Theme

`theme.css` carries it. Near-white ground, YouTube red as the line that separates
everything, IBM Plex Sans with IBM Plex Mono on every numeral, path, timestamp and log
line.

```
--cb-bg        #F4F5F7   page ground
--cb-surface   #FFFFFF   cards, panels, toolbars
--cb-line      #CC0000   1px structural outlines, dividers, table rules
--cb-line-soft rgba(204,0,0,.22)   inner row rules
--cb-accent    #E00000   fills, active states, progress, badges
--cb-text      #24262B
--cb-muted     #6B7280
--cb-quiet     #D6D9E0   non-red control borders
radius 8px · shadow 0 1px 2px rgba(20,22,28,.05)
```

Two rules that carry the look: **structural outlines are `#CC0000` at 1px, never
`#E00000`** (pure red hairlines vibrate on near-white and fail small-text contrast),
and **`#E00000` is reserved for fills, the active nav border, progress bars and
badges** (it was `#FF0000`; darkened so white text on a solid fill clears 4.5:1). Selected segmented-control options use the solid fill; if that reads too hot
at scale, drop them to a red outline and leave everything else alone.

Status colours are the app's own: `#127A3E` success, `#A66300` skipped/warning,
`#CC0000` error, `#FF8C00` unresolved-link orange.

Minimum control height 32px, 44px on touch. Log and table type never below 12px.

---

## 8. Security

Non-negotiable once this is reachable over the internet:

1. **Pairing, not passwords.** Host generates a 6-digit code with a 5-minute TTL,
   shown in the desktop app and as a QR. The client exchanges it once for a long-lived
   device token stored in `localStorage`. Tokens are listed and revocable in Settings.
2. **TLS always.** Do not roll your own — put it behind a Cloudflare Tunnel or Caddy.
   Document that plain HTTP is LAN-only.
3. **Read-only mode** as a host setting, plus the single-writer control lock.
4. **No path traversal.** `settings.set` on a directory key validates and canonicalizes
   server-side. `fs.pick_folder` never accepts a client-supplied path.
5. **Rate-limit** pairing attempts. Six digits is 10^6 and a phone can try fast.
6. **`singleton.py` stays.** One host process, one database writer.

---

## 9. Suggested phases

1. `cratebuilder/service.py` + `ui-contract.json` frozen as the contract. No UI yet.
   Prove it by driving a batch from a Python REPL.
2. pywebview window serving `web/`, local transport only. Ship Downloads + Watch List.
   The tkinter app keeps running as-is beside it.
3. Log and Database viewers. These are the highest-value screens for remote use and
   the ones with real payload-size constraints — do them before the network layer so
   you find the constraints locally.
4. FastAPI + WebSocket, pairing, read-only mode, control lock. LAN first.
5. Settings, notifications, TLS, internet reach.
6. Retire the tkinter UI only once every control has a web equivalent — the tooltip
   registry doubles as the checklist.

## 10. Acceptance criteria

- Every control in `ui-contract.json` exists, is wired, and has its tooltip.
- A batch started from the web UI shows current-track and overall progress, pauses,
  cancels, and skips a single URL mid-flight.
- Watch List scan streams into the pinned scan log line by line.
- `debug.log` at the 5 MB cap opens in under a second and scrolls smoothly.
- The database viewer opens a 20k-row library without a visible stall; column widths
  and order survive a reload.
- Killing the host process leaves the last state on screen with every control disabled
  and a retry affordance — no blank page, no silent failure.
- An unpaired browser can reach nothing but the pairing screen.

## 11. Repo files this touches

| File | Change |
|---|---|
| `cratebuilder/service.py` | **new** — transport-agnostic action surface |
| `cratebuilder/ui_strings.py` | **new** — the shared tooltip/label registry |
| `cratebuilder/server.py` | **new** — FastAPI app, WebSocket, pairing, tokens |
| `web/` | **new** — the bundle: `index.html`, `theme.css`, `api.js`, screens |
| `DJ-CrateBuilder_v1.3.py` | tooltips read from `ui_strings`; long actions delegate to `service`; no other change in phases 1–3 |
| `cratebuilder/db.py` | add paged/grouped query helpers; schema untouched |
| `cratebuilder/settings.py` | expose a validated `set(key, value)` used by both UIs |
| `DJ-CrateBuilder.spec` | bundle `web/`, add pywebview + uvicorn hidden imports |
| `requirements.txt` | `pywebview`, `fastapi`, `uvicorn[standard]` |
| `tests/` | service-level tests; `test_settings_tooltips.py` extends to cover the registry |

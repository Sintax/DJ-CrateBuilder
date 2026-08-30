# Web UI (v2) — scaffold

Status: **feature-complete on `feat/webui-v2`**, phases 1–5 of the build order in
`UI-design/HANDOFF.md` §9. Every screen in the design is built and driven by the
host; the remote transport (pairing, read-only, control lock) is in. The tkinter
app is untouched and remains the shipping UI — phase 6 (retiring it) has not
been opened.

See **[Status](#status--what-shipped)** at the foot for what shipped, what is
deliberately still deferred, and why.

## What this is

`UI-design/` (produced in Claude Design) is the source of truth for the v2
frontend: `README.md` for scope, `HANDOFF.md` for the engineering brief,
`ui-contract.json` for the machine-readable contract, `theme.css` for tokens, and
`CrateBuilder Remote v3.dc.html` as the visual reference. This note records what
was built against it and the decisions that are not obvious from the diff.

## Stack

Per HANDOFF §2, and against an earlier working assumption that was wrong:

- **pywebview** hosts the local window over a static `web/` bundle.
- **FastAPI + WebSocket** will serve the same bundle remotely (not yet built).
- **NiceGUI was evaluated and rejected** — it owns the render layer and wants to
  be the process's main loop, which would make the Python core a plugin to a web
  framework and make this design impossible to reproduce precisely. It is no
  longer a dependency of the frontend.

One bundle, two transports, one JS facade (`web/api.js`). No screen knows which
transport is live.

## What exists

| Path | Role |
|---|---|
| `cratebuilder/service.py` | `CrateBuilderService` — transport-agnostic action surface; raises `CBError` with user-facing text |
| `cratebuilder/ui_strings.py` | Shared registry: 76 tooltips + 42 settings keys, generated verbatim from `ui-contract.json` |
| `web/` | `index.html`, `theme.css` (drop-in, unmodified), `app.css` (layout only), `api.js`, `app.js`, `assets/` |
| `web_window.py` | Local pywebview host; `--screen <name>` opens straight to a screen |
| `web_server.py` | Remote mount: uvicorn over `cratebuilder/server.py` |
| `cratebuilder/server.py` | FastAPI app, WebSocket push, pairing, token auth |
| `cratebuilder/remoteauth.py` | Device tokens, pairing codes, read-only flags, the control lock |

Screens built: the shell (fixed 236px panel, four-item nav, host footer),
Overview (`3a`), Downloads (`3b`/`3c`), Watch List (`3d`), Activity and Debug
log viewers (`3e`/`3f`), the three-tab Database viewer (`3g`/`3h`/`3i`),
Settings (`3j`), pairing and host-offline (`3k`), the modal dialogs (`3m`), and
Notifications + About (`3n`).

## Decisions worth keeping

**Transport gating is server-side.** `LOCAL_ONLY = ("update.", "fs.")` is checked
in `CrateBuilderService.call`, so a remote client cannot reach the updater or the
filesystem even if it asks — HANDOFF's rule that a browser elsewhere must never
replace the binary it is talking to. Covered by tests.

**Row shapes are normalised in the service, not in JS.** The watch-list columns
(`pending_new_count`, `total_downloaded`, `last_scanned_timestamp`) are mapped to
`new_count` / `downloaded` / `last_scan` in Python so no screen knows the schema
and a column rename is one edit. `unresolved` is computed as *YouTube without a
canonical channel id* — a null there is normal for SoundCloud and is not a fault.

**Probing never creates the database.** Opening `DownloadsDatabase` runs the
schema migrations, so `library_stats` checks for the file first; otherwise merely
opening the frontend would write a database into the user's install.

**Version constants are parsed, not imported.** Importing the monolith builds a Tk
window, which would drag the whole service into the gui test lane for two values.

**Controls that are not wired yet render disabled with the reason in their
tooltip**, never as dead controls. This honours the contract's "never disable a
control without a tooltip giving the reason" and keeps the gap honest on screen.

**Settings enums show the host's real value.** The contract's option strings do
not always match what the app stores (`"192"` vs `"192 kbps"`,
`auto_dl_interval` vs `auto_download_interval`, `log_limit` vs `log_max_mb`). The
select prepends the stored value when it is not among the options rather than
rendering blank, and contract keys with no matching schema key render disabled
with an explanation plus a count at the foot of the screen. **This drift is real
and unresolved** — reconciling the contract's key names and option spellings with
`cratebuilder/settings.py` is a prerequisite for wiring Settings for real.

## Gotcha: window size is in device pixels, not CSS pixels

On a 125%-scaled display a 1240px window is only ~995 CSS px of viewport, below
the 1100px the layouts are designed against. `WINDOW_SIZE` is therefore 1560×980.
The layout collapses to one column below 1100px so it degrades instead of
clipping — this is graceful degradation, *not* the mobile breakpoint the design
explicitly excludes.

The same trap applies to screenshots: a DPI-unaware capture process silently
crops a DPI-aware window. Verify with `SetProcessDpiAwarenessContext(-4)`, or by
measuring `document.body.scrollWidth` against `window.innerWidth` in the page.

## Release impact

`requirements.txt` gains `pywebview`, `fastapi`, `uvicorn[standard]`, and
`scripts/release.py`'s `BUNDLED_DEPS` tracks them. `normalize_pkg_name` now
strips extras, since `uvicorn[standard]` is a requirement specifier and would
never match the plain `uvicorn==…` that pip freeze reports. Packaging the bundle
into the PyInstaller spec is **not** done — `web/` must be added as data and
uvicorn needs `--collect-submodules`.

## Status — what shipped

Everything below is on `feat/webui-v2`. `APP_VERSION` stays `"1.3"` and the
tkinter app is byte-identical to `main`'s: nothing here changes the shipping app.

**Screens.** All fourteen design ids. Downloads runs a real batch with current
and overall progress, pause/resume, cancel and per-row skip; the Watch List
streams its scan log line by line and drives per-channel scans, downloads,
force-downloads, Fix Link and Edit Channel; the log viewers window a 5 MB file
without loading it whole; the database viewer pages a 31,999-row library with
persisted column widths and order; Settings autosaves per control; the Overview
aggregates all of it and drives whichever job is running.

**Transport.** One bundle, two mounts. `web_window.py` is the local pywebview
window; `web_server.py` serves the same files to browsers. Pairing is a 6-digit
code exchanged once for a device token, rate-limited host-side; read-only mode
and a single-writer control lock gate every write; `update.*` and `fs.*` are
refused server-side on the remote transport, not left to the client.

**Notifications.** `notification` events from the host — scan finished, batch
finished, maintenance finished, and any job that crashed — collect in a bell on
the Overview with mark-all-read, and feed the Recent activity card. Client-side
and per-browser (`localStorage`); there is no server-side inbox and no OS
integration, which is what the design asks for.

**Tooltips.** One registry (`cratebuilder/ui_strings.py`, generated) read by
both frontends. Descriptive text comes from a registry key; a disabled control
adds why it is off underneath. Both render on disabled controls — the hover half
of the engine is one document-level pointer tracker asking what is under the
cursor, because a disabled form control dispatches no mouse events at all and a
per-element `mouseenter` on one can never fire.

## Status — deliberately deferred

Each of these renders visibly and disabled with the reason in its tooltip,
rather than being hidden — the gap stays honest on screen.

- **Updater controls** (`Check for updates`, `Update Now`, the auto-check
  interval). The in-app updater is its own effort with its own risk profile
  (it replaces the running binary), and `update.*` is LOCAL_ONLY besides. The
  build number beside them is live, so About still answers "is this host
  current".
- **Folders Cleanup dialog** (`3m`'s per-channel review). The only destructive
  flow in the design; `db.cleanup_scan` / `cleanup_apply` are unimplemented and
  the Watch List tab's 🧹 button is disabled with that reason. Deleting a user's
  files needs its own ask.
- **TLS.** HANDOFF §8.2 says do not roll your own: plain HTTP is LAN-only, and
  anything wider goes behind Caddy or a Cloudflare Tunnel. `--host-allow` exists
  so a proxied deployment can name itself.
- **QR pairing** (the contract's `qr_svg`). Needs a `qrcode` dependency nobody
  authorised; the 6-digit code plus the printed URL cover the same ground.
- **EventSource fallback** for networks that block WebSockets. The socket path
  works everywhere tested; the fallback is a second push implementation to
  maintain and nothing has needed it yet.
- **Creating and removing genre folders** from the web UI. No service method;
  the desktop app still does it, and a genre appears here as soon as something
  is downloaded into it.

## Next

Phase 6 in HANDOFF §9 order — retiring the tkinter UI — is the only phase left,
and it is not opened. The tooltip registry is the checklist: every key it holds
is either bound in the bundle or deliberately unbound (a local-only control, or
one of the deferrals above).

# Web UI (v2) — scaffold

Status: **scaffold landed** on `feat/webui-v2`. Phase 1–2 of the build order in
`UI-design/HANDOFF.md` §9. The tkinter app is untouched and still the shipping UI.

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

Screens built: the shell (fixed 236px panel, four-item nav, host footer),
Overview (`3a`), Downloads (`3b` idle), Watch List (`3d`), Settings (`3j`).
Not built: log viewers, database viewer, dialogs, pairing, the remote transport.

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

## Next

Phase 3 in HANDOFF §9 order: log viewers, then the database viewer (payload-size
constraints are easier to find locally), then the FastAPI transport with pairing.
`APP_VERSION` stays `"1.3"`; nothing here changes the shipping app.

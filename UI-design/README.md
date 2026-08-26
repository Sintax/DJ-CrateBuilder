# DJ-CrateBuilder — Web / Remote UI redesign

Everything needed to implement the web control surface for DJ-CrateBuilder v1.3.
Read this file first, then `HANDOFF.md` for the engineering detail and
`ui-contract.json` for the exact API surface, settings keys and tooltip strings.

## What this is

DJ-CrateBuilder is a Python/tkinter desktop app (`DJ-CrateBuilder_v1.3.py`) that
downloads audio from YouTube and SoundCloud into a genre/channel folder tree. This
redesign gives it a **browser-based control surface** with the same capabilities:
add URLs, run and cancel batches, manage the Watch List, read both logs, browse the
downloads database, and change every setting — served both as a local desktop window
and to a paired remote browser (phone, laptop, another room).

**Desktop and remote desktop only.** No mobile breakpoint has been designed or
smoke-tested. Do not assume the layouts reflow below ~1100px.

## About the design files

`CrateBuilder Remote v3.dc.html` is a **design reference written in HTML** — a
prototype showing intended look, copy and behaviour. It is not production code to
lift. The job is to recreate these screens in the app's real environment:

- **Local window** — `pywebview` pointing at a bundled `web/` directory.
- **Remote** — FastAPI + a WebSocket for live progress, same pages served over the
  network, behind pairing.
- **One façade.** Both mounts call the same `cbApi` object (`HANDOFF.md` §2 and the
  `api` block in `ui-contract.json`). The local mount binds it to Python directly;
  the remote mount binds it to HTTP/WS. No screen knows which it is talking to.

Pick the front-end framework you prefer for `web/` — nothing in the design depends
on a particular one. The prototype's own runtime (`support.js`) exists only so the
HTML file opens in a browser for reference; do not ship it.

## Fidelity

**High fidelity.** Final colours, type, spacing, copy and tooltip text. Recreate it
closely; every value is in `theme.css` and every tooltip string is in
`ui-contract.json` with the Python line it came from. Where the desktop app already
has the wording, the design uses it verbatim — keep it.

## Screens

Open `CrateBuilder Remote v3.dc.html` in a browser; each screen carries its `3x` id
as a visible badge, and `HANDOFF.md` §1 maps every id to the Python class or method
it comes from.

| id | Screen |
| --- | --- |
| `3a` | Overview — landing screen after pairing |
| `3b` / `3c` | Downloads — idle, and batch running |
| `3d` | Watch List — channel cards over the pinned scan log |
| `3e` / `3f` | Activity Log, Debug Log viewers |
| `3g` / `3h` / `3i` | Database viewer — Downloads, Watch List, Artwork tabs |
| `3j` | Settings — full mirror of the desktop tab |
| `3k` | Pairing, and the host-offline fallback |
| `3l` | Tooltip pattern reference |
| `3m` | Dialogs — Add / Edit Channel, Fix Link, Confirm Delete, long-job progress |
| `3n` | Notifications, and About |

### Shell

Fixed 236px left panel on every screen: brand, three quick actions (URL field, Add to
batch, Scan all channels), the nav list, host-status footer. The panel never changes
width or order — only the active item moves.

**Nav is exactly four items:** Overview, Downloads, Watch List, Settings. The nav
carries one live count (Watch List pending-new); do not add badges.

**The three former `Toplevel` windows are not in the nav.** Activity Log, Debug Log
and the Database viewer open from Settings — the log buttons in the "Log Size Limit"
card and "Open Database" in the "Downloads Database" card. While one is open,
Settings stays the active nav item, and the screen opens with a breadcrumb bar above
its toolbar: a `‹ Settings` back button plus the screen's own name.

## Two hard rules

1. **The updater is local-session-only.** `Check for updates`, `Update Now` and the
   auto-check interval are fully enabled in the window running on the host machine
   itself, and render disabled — with the reason in their tooltips — in every remote
   session. Gate this on the **transport**, not a config flag: the remote transport
   must reject `update.*` server-side even if a client asks. A browser elsewhere must
   never be able to replace the binary it is talking to.
2. **The host is the only thing that touches the filesystem.** Folder pickers,
   "Open Folder", "Open in text editor" are local-window-only; remote gets a
   validated path field or a file download instead. `fs.pick_folder` never accepts a
   client-supplied path.

## Interaction conventions

- **Tooltips.** Every control has one, and the strings are non-negotiable — they are
  the app's real help text. Three affordances: hover on a control, a `?` help icon
  next to a label, and a disabled control whose tooltip explains *why* it is
  disabled. Pattern documented on `3l`; all 119 strings in `ui-contract.json`.
- **Never disable a control without a tooltip giving the reason.**
- **Disabled treatment** is one rule: 45% opacity, grey border and text,
  `cursor: not-allowed`. It is used on 24 controls across the design.
- **Dialogs** are centred modals over a dimmed page, mirroring the desktop app's
  separate windows. Conventions and per-dialog behaviour in `HANDOFF.md` §4 —
  including the ordering rule that `Smart-Edit Link` closes its dialog *before*
  opening Fix Link so two modal grabs cannot fight over focus.
- **Host offline** (`3k`) disables every control and leaves the last received state
  on screen. A remote surface that empties itself on a dropped socket is useless.
- **Live progress** arrives over the WebSocket: per-track percentage, batch counts,
  and the Watch List scan log streaming line by line.

## Design tokens

All in `theme.css` as `--cb-*` custom properties. The three that carry the look:

- **`#CC0000`** — every structural outline, at 1px. Never a brighter red for a
  hairline: pure red vibrates on a near-white ground.
- **`#E00000`** — fills, the active nav border, progress bars and badges. (Was
  `#FF0000`; darkened so white text on a solid fill clears 4.5:1 contrast.)
- **`#F4F5F7`** ground, `#FFFFFF` surfaces, `#24262B` text.

Type: **IBM Plex Sans** for interface, **IBM Plex Mono** for every numeral, path,
timestamp and log line. Minimum control height 32px; log and table type never below
12px. Dim text is darker than the desktop app's `TEXT_DIM` — the same dimming that
works on a near-black ground fails AA on near-white; column headers, section kickers
and log timestamps use `#6E7480` or darker.

## Assets

`assets/logo.png` and `assets/about_avatar.png` come from the app itself and are the
real thing. `assets/01-main.png` … `04-about.png` are screenshots of the existing
tkinter app, included as reference for what each screen replaces.

## Files in this folder

- `README.md` — this file
- `HANDOFF.md` — engineering brief: stack rationale, screen-to-code map, the `cbApi`
  façade, dialog conventions, log and database viewer requirements, security rules,
  build order, and the acceptance checks
- `ui-contract.json` — machine-readable: API methods and payloads, every settings key
  with type and default, all 119 tooltip strings with source line numbers, shell and
  nav rules, theme rules
- `theme.css` — the design tokens as CSS custom properties
- `CrateBuilder Remote v3.dc.html` — the design itself; open it in a browser
- `support.js` — runtime the prototype needs to render locally. Reference only.
- `assets/` — images the design loads

# Update page: the components table

*2026-09-13 — shipped with the build after 82.*

## What the user sees

Under the Update page's check / install controls, a table with one row per
bundled component: Python, FFmpeg, yt-dlp and its JS helper, the certificate
bundle, the window engine (pywebview), the bundle server (bottle), the remote
API and server (FastAPI, uvicorn), Pillow, mutagen, pystray and send2trash.

Three columns: **Component · You have · In build N.** The table is always
there. Before any check the third column is blank and the note says to check
for updates. After a check it compares against the live build — newer or not
— and a row the build would change is highlighted, its new version in bold
orange with a *will update* tag. A count badge beside the heading says how
many rows that is, or *All current*.

Build 82 and earlier were published without the block, so against those the
third column reads *not listed* and the note says why.

## How it works

- `cratebuilder/components.py` owns the list (`COMPONENTS`: key, label, pip
  name), reads what the process is running (`installed_versions`: Python from
  the interpreter, FFmpeg from the updater's `ffmpeg.version` marker or the
  binary's own report, packages via `importlib.metadata`), normalises the
  manifest's block (`offered_versions`) and compares (`compare` → rows with
  one of four states: `newer`, `same`, `unknown`, `missing`).
- The nightly manifest (`update.json` on the `nightly` branch) gains a
  `"components": {name: version}` block. `scripts/release.py` writes it from
  the package map it already records per build, plus the build machine's
  Python and the FFmpeg the channel offers. It composes the block through
  `components.freeze_versions`, so the key spelling (PEP 503 normalised —
  `pillow`, `yt-dlp`, `uvicorn`) is decided in one place.
- `update.check` carries the manifest's block into its result; `update.status`
  (local only) returns the comparison (`components: {build, available,
  rows}`) so the page just draws. Installed versions are read once per
  service — they don't change under a running app, and the FFmpeg read may
  spawn the binary.
- The page refreshes that status after every silent check (`update.checked`
  and `update.available`), so the launch check fills the third column
  without a reload.

## The activity.log record

The table itself logs nothing — it only describes. The one line that is
written lands in `activity.log` at the moment the app hands off to the
updater (after download, verify and stage have all succeeded, so a failed
attempt leaves no trace):

    UPDATED     | Build: 82 -> 83 | Components: yt-dlp 2026.8.19 -> 2026.9.2, …

Only the rows the build changes are named. A build published without the
block reads `Components: not listed`; a build that changes no bundled
component reads `Components: none`. `activitylog.updated` formats it; the
verb was chosen so the Activity Log viewer's Downloaded / Skipped / Errors
filters never match it.

## Not in scope

- Nothing here installs anything: the table describes; the existing Update
  Now flow applies. A delta nightly carries any package that changed, so the
  comparison holds for deltas and full builds alike.
- The remote transport never sees the table (`update.` is local-only).

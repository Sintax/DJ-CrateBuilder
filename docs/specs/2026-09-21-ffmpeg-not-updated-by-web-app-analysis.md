# Why build 95's FFmpeg update never installed (analysis, 2026-09-21)

Status: analysis only, no fix yet. Written to seed a fix plan.

## What happened

- The nightly manifest for build 95 offers FFmpeg `9.0.2-essentials_build-www.gyan.dev+3256173f`
  as a separate zip (the `ffmpeg` block in `update.json` on the `nightly` branch).
- The installed app (`C:\Program Files\DJ-CrateBuilder`) still has FFmpeg 9.0.1
  (`ffmpeg.version` marker from 2026-08-29, binaries dated 2026-08-23).
- `activity.log` says the build 94 -> 95 update changed
  "FFmpeg 9.0.1... -> 9.0.2...". That line is written from the *manifest's* component
  list, before anything is installed. It describes what the new build carries, not what
  this install received. So the log claims an FFmpeg change that never happened.
- Nothing was "running in the background". There is no FFmpeg download job in the
  shipped app at all. Waiting could not have finished it.

## Root cause

The FFmpeg self-update feature was built into the retired tkinter app and was never
ported to the web app that now ships.

1. The original feature (commits `b70cd8b`, `287b004`, July 2026) put the decision +
   install core in `cratebuilder/updater_core.py` (`ffmpeg_update_action`,
   `install_ffmpeg_from_zip`) and the trigger in the tkinter monolith
   (`MP3DownloaderApp._maybe_update_ffmpeg`, called from `_on_check_result`).
2. The "fix" the maintainer remembers (`c48257f` trust-the-binary, `dc4e6ca` idle
   guard + retry, 2026-08-23) also landed only in the monolith.
3. On 2026-08-29 the web updater design (`docs/specs/2026-08-29-webui-local-updater-design.md`,
   section "FFmpeg piggyback stays tkinter-only") explicitly scoped FFmpeg out of the
   service, on the reasoning that "the installer and desktop app already keep FFmpeg
   current". That assumption was true only while the tkinter app was the desktop app.
   Once `web_window.py` became the shipped exe, no process was left that runs the
   FFmpeg swap.
4. Today `ffmpeg_update_action` and `install_ffmpeg_from_zip` have zero callers
   outside the monolith and the tests (`graft grep` confirms). `CrateBuilderService`
   mentions FFmpeg only to locate the binary for yt-dlp and to *report* its version in
   the components table (`update_components`, `_installed_components`).
5. The release side is fine: `scripts/release.py` deliberately excludes
   `ffmpeg.exe`/`ffprobe.exe`/`ffmpeg.version` from every app payload (full and delta)
   and publishes FFmpeg as its own zip referenced by the manifest. That is by design,
   so the app-side swap is the *only* way FFmpeg reaches an existing install.

## What the user sees today

- Update page components table: FFmpeg row tagged "newer" before the update, and
  still "newer" after it, forever, because the install never changes.
- activity.log: an UPDATED line that lists FFmpeg as a component change.
- The FAQ (commit `a5d37c2`) tells users packaged builds self-update FFmpeg.

## Pieces that already exist and can be reused in a fix

- `updater_core.validate_ffmpeg_block`, `read_ffmpeg_version`, `write_ffmpeg_version`,
  `probe_ffmpeg_build`, `ffmpeg_update_action`, `install_ffmpeg_from_zip` — all
  tested (`tests/test_ffmpeg_update.py`, `tests/test_ffmpeg_marker.py`).
- The monolith's `_maybe_update_ffmpeg` / `_start_ffmpeg_update` / `_arm_ffmpeg_retry`
  (DJ-CrateBuilder_v2.0.py ~L8056-L8160) are a working reference for the trigger:
  frozen-Windows only, decide off the marker + binary probe, defer while any
  download/scan is live, retry once a minute until idle, swap, rewrite marker.
- The service already has the idle guard (`_require_idle_for_update`), a job registry
  (`_start_job`), the event bus, and `bundled_ffmpeg_dir()`.

## Open questions for the plan

1. When should the web app run the swap: piggybacked on every successful update
   check (as the monolith did), or as part of `update_apply` before the handoff, or
   both? The monolith approach makes it skip-proof (independent of app build).
2. Should the swap be a visible job (progress, cancel) or silent with a notification?
   The FFmpeg zip is ~100 MB, so silent-with-log may surprise users on metered links.
3. The components table and `activitylog.updated` should stop implying FFmpeg was
   installed by an app update. Either log FFmpeg separately when the swap actually
   lands, or word the UPDATED line as "build carries".
4. Remote transport: the swap must be LOCAL-only (`LOCAL_ONLY` already covers
   `update.` methods, so a new `update.ffmpeg` method inherits that).
5. Update `docs/specs/2026-08-29-webui-local-updater-design.md` to retract the
   "stays tkinter-only" decision, and the FAQ if wording changes.

## Suggested shape (for the plan author, not decided)

- New service method, e.g. `update_ffmpeg_check(manifest)` called from the end of
  `update_check` when the manifest is valid, plus a one-minute retry timer while busy,
  mirroring the monolith. Worker downloads to the update workspace, verifies SHA-256,
  calls `install_ffmpeg_from_zip`, emits a notification and refreshes
  `_installed_components_cache` so the table flips to "same".
- Model floor for implementation: opus (touches service + event wiring + updater).

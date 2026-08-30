# Web UI: the local-session self-updater goes live

## Problem

The About screen's Updates card (`UI-design/HANDOFF.md`, `about.local_session_only`)
was built visible-and-disabled behind `ABOUT_UPDATER_DEFERRED` — a standing ruling
that the in-app updater was its own effort. `cratebuilder/updater_core.py` already
carries every moving part (manifest fetch, checksum, extract, the swap-process
handoff); nothing in `cratebuilder/service.py` called into it. This wires the three
`update.*` methods the contract already reserves and turns the card on for the local
pywebview window, leaving the remote transport exactly as designed: readable, never
writable.

## Design

### Server-side manifest trust

`update.apply` takes no trusted parameters from the client. It re-fetches and
re-validates the manifest itself (`ucore.fetch_manifest` / `validate_manifest`), and
the build/url/sha256 the download worker acts on come only from that fetch — a caller
cannot name a build, a URL, or a checksum. `update.check` is a separate, side-effect-
light fetch (it still persists `last_update_check`, matching the monolith's
`_check_updates_worker`) so the About screen can show a result without committing to
a download.

Both URLs (`UPDATE_MANIFEST_URL` / `UPDATE_MANIFEST_URL_LINUX`) are read out of the
monolith's own source with `ast`, the same "one copy, no drift" approach
`version_info()`/`about_info()` already use — necessary here because each constant is
built from adjacent string literals rather than one short literal a regex can anchor
on.

### can_self_update gate, and Linux is not ported here

`update_apply` refuses with the monolith's exact wording
("Build N is available, but you're running from source...") when
`ucore.can_self_update()` is false. On Linux it refuses unconditionally with a link to
the `linux-v1.3` release, regardless of `can_self_update()`: the tkinter Linux path
installs the `.deb` via `pkexec apt-get`, a privileged GUI prompt that has no meaning
in a browser tab, and the `.deb` payload doesn't ship the web UI in the first place.
Porting that flow was explicitly out of scope for this pass.

### Cross-job exclusion, both directions

An update swaps every file under the app and restarts it, so a batch/watchlist
download, a maintenance run, or a genre-move tag rewrite mid-flight is the same
failure the monolith's own `_launch_updater_and_quit` polls around before quitting.
Rather than port the poll, `update.apply` simply refuses to start unless batch,
watchlist, and maintenance are ALL idle and no genre-move retag is sweeping
(`_require_idle_for_update`, checked both as a synchronous pre-flight and as
`_start_job`'s atomic guard — the same two-checks pattern `maintenance_start` already
uses).

The reverse direction touches every entry point that claims one of those slots, not
only the two obvious ones:

- `_require_idle_for_download` (batch and Watch List downloads) and
  `_require_idle_library` (maintenance — including `db.repair_tags`, which is
  otherwise deliberately excluded from the download-table-collision set
  `_MAINTENANCE_NEEDS_IDLE` names, but not from this one: it saves ID3 frames in
  place with mutagen the same way a retag does) now refuse while `UPDATE_JOB` holds
  the slot.
- `claim_tag_writes` — the Watch List genre-move retag's own claim, which holds no
  job-registry slot at all (`_retags` is a plain counter) — refuses the same way,
  under the same lock as the check.
- `watchlist_scan`/`watchlist_scan_all` gained a guard they never had before this
  pass: a scan shares `WATCHLIST_JOB` with a download, which only excludes another
  same-category claim, not a different category like `UPDATE_JOB`.

So a download, a maintenance run, a tag-repair sweep, a genre-move retag, or a
Watch List scan — every path that writes into the app's files, the downloads table,
or a track's ID3 frames — is refused for as long as `UPDATE_JOB` holds its slot.

### FFmpeg piggyback stays tkinter-only

The monolith's automatic-check path also decides an independent FFmpeg swap off the
same manifest (`_maybe_update_ffmpeg`). That stays entirely in the monolith; the web
service's `update_check`/`update_apply` don't read or act on the manifest's `ffmpeg`
block. The installer and desktop app already keep FFmpeg current for every user this
web frontend also serves (LOCAL transport implies one install, one FFmpeg), so there
is nothing for the web path to duplicate.

### The auto-check timer is LOCAL-only, explicitly armed, and never injects a clock

A `threading.Timer`, armed from `interval_label_to_seconds(update_check_interval)` and
re-armed after every fire and every `update.set_interval`. Arming is NOT a constructor
side effect — `CrateBuilderService.__init__` never starts it. `start_update_timer()` is
a separate call `web_window.py` makes right after building the service, the same way
`start_remote_mount()` already is: a service built for one snapshot or one test (which
is most services this codebase constructs) never pays for a background Timer just from
being built. `start_update_timer()`/`_arm_update_timer()` are also no-ops once
`_default_transport` is `REMOTE` — a browser elsewhere must not make the host poll
GitHub on its behalf, matching `update.*`'s `LOCAL_ONLY` gate — and once `close()` has
run (`_closed`, checked inside the same locked section `_arm_update_timer` uses, so a
fire already past its own lock hold when `close()` lands can't resurrect a timer
afterwards).

Per ADR 0001, no clock abstraction is injected; a test wanting a fast fire uses a short
interval (or calls `_update_timer_fire()` directly, as most of this pass's tests do). A
fire that lands while any job is running is skipped but still re-arms — installing
mid-scan is exactly the failure this whole design avoids, and the next tick offers the
update again once things are quiet. A fire never downloads on its own; it only emits
`update.available` (and an info-level `notification`) for the About screen to act on,
exactly like the monolith's silent check with the window hidden. The timer is
cancelled in `CrateBuilderService.close()`, called from `web_window.py`'s
`window.events.closing`.

### AV warning first

The confirm modal that gates the actual download shows the antivirus/SmartScreen
false-positive warning before any bytes move, ahead of and alongside the build's
release notes — the ordering the desktop app's own FAQ entry
("Windows says the installer is unrecognised...") already commits to in writing. No
dedicated `_show_av_warning` dialog exists in the monolith to port verbatim; the web
modal's copy is original text carrying the same two facts that entry does (unsigned
build, SHA-256 verified against the manifest before anything is written to disk).

### Restart handoff

`CrateBuilderService.on_update_restart` defaults to `None`; the `update.apply` worker
calls it, if set, right after handing the built command to `subprocess.Popen` and
before returning. `web_window.py` sets it to `window.destroy`, which makes
`webview.start()` return and the process exit — `updater.exe` waits up to 30s on this
PID before swapping files, same as the monolith's `_launch_updater_and_quit`. The
worker runs on its own thread, so `on_update_restart` is invoked off the UI thread;
`window.destroy()` is safe to call there because pywebview marshals it.

The worker's own failure-purge only covers the part before that handoff. Once
`Popen` has returned successfully, `<workspace>/staged` belongs to the separate
updater process — which is already waiting on this PID to exit — not to this
process's error handling; nothing after the `Popen` call runs inside the
purge-guarded `try`. `on_update_restart` is called outside it and any exception
from it is swallowed (the app is exiting either way, and the update itself already
succeeded), so `job.finished` still reports `ok=True` for a handoff that landed.

### Two smaller hardenings

`update.check`/`update.apply` treat an unparseable or missing monolith source (no
`UPDATE_MANIFEST_URL` to read, so nothing to fetch) as data, not a crash: the check
reports its normal "unreachable" shape and apply raises a `CBError` — never a bare
`TypeError` out of the RPC. `update.progress` is coalesced (`cratebuilder/events.py`'s
`DEFAULT_COALESCED_TYPES`) alongside `progress.current`/`progress.overall`, matching
what "emit freely from the progress callback" always assumed: `download()` calls its
callback once per 64 KB block, and a nightly-sized payload is hundreds of those.

## What did not change

- `cratebuilder/updater_core.py` gained one new pure function
  (`launch_updater_command`) and nothing else; the monolith's own inline copy of the
  same command construction (`_launch_updater_and_quit`) is untouched and stays
  byte-identical.
- `web/theme.css`, `UI-design/ui-contract.json`, `cratebuilder/ui_strings.py`: not
  touched.

# Domain glossary

Terms with a specific meaning in DJ-CrateBuilder. Architecture reviews and
design conversations use these words exactly.

- **Crate** — the on-disk library: `~/Music/DJ-CrateBuilder/<Platform>/<Genre>/<Channel>/`.
- **Platform** — YouTube or SoundCloud; the top-level crate split and the key
  into the `PLATFORMS` config table.
- **Channel** — one artist/uploader folder inside a genre, stamped with a
  `cratebuilder.json` sidecar carrying its canonical identity.
- **Watch List** — the set of channels scanned for new uploads; rows live in
  the DB, resolved links are mirrored to `cratebuilder_links.json`.
- **Pending** — tracks a scan surfaced as new and downloadable; cleared only
  after the channel's batch item runs clean.
- **YdlSession** — the single module through which the app asks yt-dlp
  anything read-only (probe metadata, list a channel, search). Built once per
  operation from a settings snapshot; one auth policy per session, applied to
  every intent. The JS-runtime rule (only intents that extract real formats
  need it) and the captive-portal rule (never trust a permanent-looking error
  while the network is unreachable) live inside it. Raises typed errors;
  `list_channel` deliberately returns raw yt-dlp entry dicts, so the yt-dlp
  schema is part of this interface — the name says so on purpose.
- **Intent** — one named question a `YdlSession` answers (`probe_metadata`,
  `probe_formats`, `list_channel`, `search_channels`), as opposed to a
  hand-built yt-dlp options dict.
- **Scan worker** — a child process answering one `list_channel` intent for
  the Watch List scan (`cratebuilder/scanproc.py`), because a flat-extraction
  is pure-Python work that holds the GIL and would starve the UI thread run
  anywhere in-process. The child builds a real `YdlSession` from the
  request's `CookieConfig`, so the session stays the single yt-dlp boundary;
  its typed errors cross the pipe as the same types, and the captive-portal
  rule runs in the child, whose network view is the one the failure happened
  in. Cancellation kills the child mid-listing. Reached via
  `python -m cratebuilder.scanproc` from source and `--scan-worker` on the
  frozen exe (intercepted before the single-instance guard).
- **CrateLayout** — the single answer to "where does this track live?": the
  channel folder path and the track's file name. Pure naming — it never
  creates a folder (callers still do that), and its only disk access is
  `find_existing`, which resolves which of the two exact filename spellings a
  download actually wrote. The 40-char prefix tier that used to sit beside
  those two is retired: for any title under 40 characters the "prefix" was the
  whole title, so an original was claimed as owned by any remix extending it.
  A padded genre is kept verbatim (it names a real folder and is stored that
  way); only a whitespace-only genre normalises to the no-genre folder. The
  only place allowed to translate between the in-app no-genre value
  `"(none)"` and the on-disk folder name `_No Genre`; the third form (blank,
  from `genrefix.genre_from_dir_name`) is retired. Lives in
  `cratebuilder/crate.py` beside `ChannelCrate`, which must recognise
  exactly the names it produces.
- **ChannelCrate** — a per-channel view of the crate: the folder's normalized
  track-key index (snapshotted at construction) plus live DB and suppression
  oracles, answering `owns(entry) -> Ownership`. The single interface both
  the scan and the download paths cross — the "Mirror the scan EXACTLY"
  invariant, held as a return type instead of a comment. Scan classification
  (`classify(entries)`) lives here too; `sidecar.py` is sidecar-file I/O only.
  Matching is by exact normalized track key — the 40-char prefix fallback is
  retired everywhere.
- **Ownership** — the fact a `ChannelCrate` states about one entry:
  owned-by-id · owned-on-disk(path) · new · too-long · upcoming ·
  unavailable(reason). A pure `skip_decision(ownership, mode)` maps it to
  skip · download · confirm-redownload; the Tk prompt stays at the caller.
- **TrackDownloader** — owns one track end-to-end: build options, run the
  attempt ladder, tag, harvest cover art, write the downloads row, classify
  and record failures. Constructed once per **track**, with its dependencies
  (ydl runner, db, `DownloadPolicy` and `CookieConfig` snapshots, canceller,
  ffmpeg locator, format probe, and callables for tagging, artwork, the
  crate's `remember`, and the two activity-log writers); `run(plan, sink) ->
  Outcome`. Per track and not per batch because the two snapshots are read at
  construction: reading them per track is what keeps a setting the user
  changes mid-batch — "Keep original format", geo-bypass, cookies — taking
  effect on the next track rather than the next URL. Options exist only via
  one builder taking an `authenticated` flag, so the age-gate attempt can
  differ from the first by exactly that flag and the bitrate it implies, and
  by nothing else. `run` never raises: the batch driver has no per-track
  guard, so a raise would abandon every remaining track. The per-entry
  loop, pause gate and counters stay in the monolith for now.
- **Attempt ladder** — the declared retry policy inside a `TrackDownloader`,
  two rungs deep: authenticated, then unauthenticated. Each rung is the same
  transient-retry loop (3 attempts, interruptible exponential backoff), so an
  age-gate retry is no longer a one-shot that a dropped connection can kill.
  The second rung is climbed only when the first authenticated *and* its
  failure reads like an age gate *and* that failure was not transient. The age
  test matches the phrases YouTube actually uses, never the bare word "age" —
  that matched inside "webpage", which both mislabelled network trouble as an
  age restriction and doubled every failing track's give-up time. The
  non-transient condition is kept independently of it, so a real age gate
  wrapped in a transient error stays classified as the network problem it also
  is. The whole policy is the module's, not the caller's.
- **Sink** — the seam a `TrackDownloader` reports progress through, as
  semantic events (`started`, `progress`, `bitrate_detected`,
  `title_corrected`, `finished`) rather than raw yt-dlp hook dicts. Tk
  adapter in the app, recording adapter in tests. ANSI stripping and
  percent/speed parsing live behind it.
- **Canceller** — anything with `wait(timeout) -> bool` and `is_set()`
  (`threading.Event` satisfies both natively): waits out a backoff and
  reports True if cancelled meanwhile, so cancellation interrupts a retry
  instead of waiting it out. `is_set` is what makes cancellation *immediate*:
  the progress hook checks it per chunk and aborts the in-flight transfer by
  raising, which `_attempts` reports as cancelled, never as an error.
  `SkipOrCancel` composes the batch's cancel Event with a per-row Skip
  predicate, polling the predicate in short slices so either source
  interrupts a backoff.
- **Settings** — the single in-memory owner of the user config
  (`~/.cratebuilder/config.json`; the names it went by in the home directory
  itself are tidied into `~/.cratebuilder/legacy/` on launch). One declared
  schema (key → default →
  legacy migration) behind a generic `get`/`set`; every write persists the
  whole store atomically, so no writer can drop another writer's keys.
  Unknown keys found in the file are preserved on write but rejected by
  `get`/`set`. `run_at_startup` is stored like any key; the Windows registry
  remains its source of truth at the UI layer.
- **Snapshot** — a frozen, Tk-free, per-concern record handed to worker
  threads instead of live Tk variables: `CookieConfig` (what a `YdlSession`
  authenticates with), `DownloadPolicy` (skip / limiter / bitrate and its
  opt-in auto-upgrade probe / sleep / geo / UA / cover art),
  `AutomationConfig` (intervals, startup-scan, tray).
  Each consumer receives only the fields it reads. **Workers are handed the
  app's `_cookie_config()` / `_download_policy()` / `_automation_config()`,
  built from the live Tk vars** — that is what the user currently sees, and
  it preserves today's read-at-point-of-use semantics for the settings that
  autosave late (the sleep spinboxes, the cookie profile/file entries).
  `Settings.cookie_config()` and its siblings build the same records from the
  stored values, for headless callers and tests; they are not the
  worker-facing source.

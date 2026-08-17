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
- **CrateLayout** — the single answer to "where does this track live?": the
  channel folder path and the track's file name. Pure naming — it never
  reads the disk and never creates a folder (callers still do that). The
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
  and record failures. Constructed once per batch with its dependencies
  (ydl runner, db, tagging, artwork, canceller, `DownloadPolicy`, ffmpeg
  locator); `run(plan, sink) -> Outcome` per track. Options exist only via
  one builder taking an `authenticated` flag, so the age-gate attempt can
  differ from the first by exactly that flag and nothing else. The per-entry
  loop, pause gate and counters stay in the monolith for now.
- **Attempt ladder** — the declared retry policy inside a `TrackDownloader`:
  transient network errors retry with interruptible exponential backoff;
  an age-gate failure retries once with authentication dropped. Both are
  the module's, not the caller's.
- **Sink** — the seam a `TrackDownloader` reports progress through, as
  semantic events (`started`, `progress`, `bitrate_detected`,
  `title_corrected`, `finished`) rather than raw yt-dlp hook dicts. Tk
  adapter in the app, recording adapter in tests. ANSI stripping and
  percent/speed parsing live behind it.
- **Canceller** — anything with `wait(timeout) -> bool` (`threading.Event`
  satisfies it natively): waits out a backoff and reports True if cancelled
  meanwhile, so cancellation interrupts a retry instead of waiting it out.
- **Settings** — the single in-memory owner of the user config
  (`~/.dj_cratebuilder_config.json`). One declared schema (key → default →
  legacy migration) behind a generic `get`/`set`; every write persists the
  whole store atomically, so no writer can drop another writer's keys.
  Unknown keys found in the file are preserved on write but rejected by
  `get`/`set`. `run_at_startup` is stored like any key; the Windows registry
  remains its source of truth at the UI layer.
- **Snapshot** — a frozen, Tk-free, per-concern record handed to worker
  threads instead of live Tk variables: `CookieConfig` (what a `YdlSession`
  authenticates with), `DownloadPolicy` (skip / limiter / bitrate / sleep /
  geo / UA), `AutomationConfig` (intervals, startup-scan, tray). Each
  consumer receives only the fields it reads.

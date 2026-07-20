# Multi-Format Cover Art, Rebuild Artwork Preservation, and UI Pass — Design

**Date:** 2026-07-20
**Status:** Approved, ready for planning
**Branch:** `feat/artwork-and-ui-pass`
**Scope:** Five independent tasks — (1) make cover art work when "Keep original format" is on, (2) preserve and reassociate artwork during Rebuild Database from Files, (3) Watch List card button colour + tooltips, (4) About tab layout cleanup, (5) Settings/Main tab layout cleanup.

---

## Task 1 — Cover art with "Keep original format"

### Problem

With **Keep original format** checked, downloaded files show no cover art. With conversion on (MP3) it works.

### Root cause

The embed is MP3-only by hard guard, and nothing implements the other containers.

`cratebuilder/artwork.py:149` — `embed_cover()` returns immediately:

```python
if not audio_path.lower().endswith(".mp3"):
    return False
```

The same guard exists in `cratebuilder/tagging.py:39` for `write_track_tags()`.

The chain, in order:

1. `DJ-CrateBuilder_v1.3.py:8858` — with **Keep original format** on, the `FFmpegExtractAudio` postprocessor is skipped. The file stays in whatever container the platform served: `.webm` (Opus) or `.m4a` (AAC) from YouTube, `.mp3` or `.webm` from SoundCloud.
2. `DJ-CrateBuilder_v1.3.py:8852` — `writethumbnail` is still set, so the raw image **is** downloaded.
3. `DJ-CrateBuilder_v1.3.py:4569` — `ingest_thumbnail()` still runs, so `.artwork/<video_id>.jpg` **is** written.
4. `DJ-CrateBuilder_v1.3.py:4574` — `embed_cover()` returns `False`. Logged as `COVER SIDECAR | ... saved, not embedded`.

The art is not missing. It is in the hidden `.artwork/` folder, never written into the file.

### Why MP3 "just works"

MP3 uses ID3v2, where cover art is a self-contained `APIC` frame prepended to the file — one mutagen call. Every other container uses a different, incompatible mechanism:

| Container | Cover art mechanism | mutagen support |
|---|---|---|
| **MP3** | ID3v2 `APIC` frame | Full — what is used today |
| **M4A/MP4** (AAC) | `covr` atom in the iTunes `ilst` box | `MP4Cover` — straightforward |
| **WebM** (Opus in Matroska) | Cover is an **attachment track**, not a tag | None — mutagen cannot write Matroska |
| **Ogg/Opus** | base64 `METADATA_BLOCK_PICTURE` in Vorbis comments | `OggOpus` + `flac.Picture` |

WebM is the blocker, and it is the most common YouTube "original format". Its cover art lives in a Matroska attachment element — a container-structure edit, not a metadata write. mutagen has no Matroska writer.

Verified locally: mutagen 1.48.0, with `mutagen.mp4`, `mutagen.oggopus`, and `mutagen.flac.Picture` all importable. No new dependency is required.

### Decision

Embed natively where mutagen can, and **losslessly remux WebM to Opus** so it becomes a taggable container.

Rejected alternative: `ffmpeg -attach` to write a Matroska attachment. It rewrites the whole file for a format most DJ software reads poorly, and yt-dlp's own `EmbedThumbnail` postprocessor does the same remux-to-Opus for this reason.

### Implementation

New functions in `cratebuilder/artwork.py` — Tk-free, unit-testable:

| Function | Responsibility |
|---|---|
| `embed_cover_mp4(path, jpg)` | mutagen `MP4` → `covr` atom via `MP4Cover(data, FORMAT_JPEG)` |
| `embed_cover_ogg(path, jpg)` | mutagen `OggOpus`/`OggVorbis` → base64 `METADATA_BLOCK_PICTURE` built from `flac.Picture` |
| `remux_webm_to_opus(path, ffmpeg_dir)` | `ffmpeg -i in.webm -c:a copy -vn out.opus`. Stream copy — zero re-encode, audio bit-identical. Returns the new path. |
| `embed_cover_any(path, jpg, ffmpeg_dir=None)` | Dispatcher on extension: `.mp3`→existing APIC, `.m4a`/`.mp4`→mp4, `.opus`/`.ogg`→ogg, `.webm`→remux then ogg, else `False` |

`embed_cover()` keeps its exact current signature and MP3-only behaviour. Existing callers and `tests/test_artwork.py` are untouched.

`_harvest_cover_art()` (`DJ-CrateBuilder_v1.3.py:4539-4584`) calls `embed_cover_any()` instead of `embed_cover()`, and returns the possibly-changed audio path so `add_download()` records the real `.opus` filename rather than the stale `.webm` one.

Vorbis-comment and MP4 text tagging is added to `cratebuilder/tagging.py` alongside the ID3 path, so `write_track_tags` is not silently a no-op for these files. This matters for Task 2, which recovers `video_id` from the source-URL tag.

### Safety rules

- Remux writes to a temp path first. The source `.webm` is deleted only after the output exists and is non-trivial in size.
- If FFmpeg is unavailable (running from source without it on PATH), remux is skipped and behaviour falls back to today's sidecar-only. Logged, not raised.
- Every new function follows the module convention: never raises, returns `False`/`None` on failure. An artwork failure must not fail a download.

### Known behaviour change

`.webm` downloads land as `.opus` files. Same audio, same bitrate, different extension. This must be called out in the release notes.

---

## Task 2 — Rebuild Database from Files: preserve artwork

### Problem

Rebuild loses artwork association and causes duplicate JPEGs on disk.

### Root cause

`_rebuild_db_from_files()` (`DJ-CrateBuilder_v1.3.py:11539-11617`) has three defects:

1. **`video_id` is always `None`** (`:11593`). Rebuild cannot recover ids from filenames, so it writes `None` for every row. Sidecars are named `<video_id>.jpg`, so a later artwork backfill re-keys the track by filename stem via `artwork_key()`'s fallback, fails to find `dQw4w9WgXcQ.jpg`, re-fetches, and writes a **second byte-identical JPEG** as `<filename-stem>.jpg` in the same folder. **This is the duplication source.**
2. **Only `.mp3` files are indexed** (`:11580`). A "Keep original format" library rebuilds to an empty database. Directly collides with Task 1.
3. **Artwork is carried only by exact `file_path` string match** (`:11554`, `:11590`). `get_artwork_by_path()` snapshots the three artwork columns before the wipe; any path that differs by case, separator, or that was moved or renamed since silently loses its artwork bookkeeping. The `.artwork/` folder on disk is never consulted.

### Decision

**Rebuild never writes and never deletes an artwork file.** It only reads what already exists and re-points DB rows at it. Duplication becomes structurally impossible rather than merely avoided. No network access — a track with no local art is left blank for the existing **Fetch Missing Artwork** button to handle on demand.

Orphaned duplicate JPEGs created by past rebuilds are left on disk. They are harmless and small; the real fix is that recovering `video_id` stops new ones being created. A "clear artwork cache" button was considered and **rejected**: for legacy non-MP3 tracks the sidecar is the only copy of the art, so such a button is a one-click path to permanent artwork loss in exchange for a few MB.

### Implementation

New module `cratebuilder/rebuild.py` — pure logic, Tk-free, so the walk and the resolution rules are testable without a Tk root.

| Function | Responsibility |
|---|---|
| `AUDIO_EXTS` | `(".mp3", ".m4a", ".webm", ".opus", ".ogg", ".flac", ".wav")` — replaces the `.mp3`-only filter at `:11580` |
| `recover_video_id(path)` | Reads the `WOAS`/`COMM` source URL that `_tag_track` already stamps on every file (MP3 ID3, or the MP4/Vorbis equivalent added in Task 1) and parses the id out of it. Returns the id, or `None` when the file carries no source-URL tag. Artwork association for the `None` case is handled by `resolve_artwork` step 2, not here. |
| `resolve_artwork(path, video_id, art_index)` | Applies the resolution order below. Returns `(artwork_path, artwork_embedded)`. |

`resolve_artwork` resolution order, all local:

1. `.artwork/<video_id>.jpg` exists → reuse as-is
2. `.artwork/<filename-stem>.jpg` exists → reuse as-is (picks up art from a previous rebuild)
3. Art embedded in the file (`has_cover`) → record `artwork_embedded=1`, no file written
4. The pre-wipe `art_snapshot` by exact path → today's behaviour, now a fallback rather than the only path
5. Nothing → leave blank

### Performance

`.artwork/` is listed **once per channel directory** and cached in `art_index`, not once per track. A 5,000-track rebuild does one `listdir` per channel folder.

`recover_video_id` opens each audio file to read its tags, which the current rebuild does not do. On a large library this makes an already-synchronous main-thread operation slower. The rebuild worker is moved onto a background thread with the existing progress-dialog pattern (see `_fetch_missing_artwork` at `:11481` for the shape).

---

## Task 3 — Watch List entry cards

### 3a. Cancel button colour

The per-card Cancel button (`DJ-CrateBuilder_v1.3.py:9645-9657`) uses a function-local literal `WL_CARD_CANCEL = "#78350f"` (dark orange) and is hardcoded `state="disabled"` — `_watchlist_cancel_card()` at `:9227` exists but is unreachable from the card.

Three different colour languages exist for Cancel across the app:

| Location | Idle | Active |
|---|---|---|
| Card Cancel (`:9648`) | `#78350f` dark orange, local literal | never active |
| Watch List toolbar (`:9336`) | `WL_CANCEL_IDLE` `#5e1414` | `YT_DARK` `#cc2222` |
| Main tab (`:5446`, ttk styles) | `SURFACE2` `#242424` | `YT_DARK` `#cc2222` |

**Change:** wire the button up — enabled when that card `is_scanning or is_downloading`, bound to the existing `_watchlist_cancel_card()`. Adopt the toolbar's colour language: `YT_DARK` when active, `WL_CANCEL_IDLE` when idle. Promote to a module-level constant beside `WL_CANCEL_IDLE` at `:207` rather than leaving a local literal.

### 3b. Tooltips on Scan / Force Download / Edit

The card is the only button cluster in the Watch List tab with no hover help.

Use the existing `Tooltip` class (`:404-529`) attached **directly to the buttons** — `Tooltip(btn, "…")` — not the separate question-mark-in-a-box helper. This is explicitly what was asked for. `_settings_help` (`:5535`) would be wrong here anyway: it hardcodes `bg=BG` and the cards are `SURFACE`.

Buttons are created in the loop at `:9650-9673`; the tooltip text is attached there, keyed off the label already in the `card_buttons` tuples at `:9622-9644`.

| Button | Tooltip |
|---|---|
| 🔍 Scan | Check this channel for new uploads without downloading anything. |
| ⚡ Force Download | Re-download every track from this channel, including ones already in your library. |
| ✏ Edit | Change this channel's genre, platform, or download settings. |

---

## Task 4 — About tab

### 4a. Remove the redundant Application entry

`ABOUT_FIELDS` (`:80-84`) line 81 is `("Application", f"{APP_NAME}  v{APP_VERSION_FULL}")` — the identical string already rendered by the tab's own title label at `:7337`, roughly 40px above it.

Delete line 81. The render loop at `:7409` is data-driven; nothing else changes.

### 4b. Move View on GitHub above Submit Issues

`top_sec` (`:7346-7429`) is a pure `pack` two-column layout. **`btn_col` is packed before `info_col`** (`:7362` vs `:7407`) because with `pack` the right-side child must be declared first to claim the right edge — the comment at `:7358-7360` states this. Do not reorder those two `pack()` calls.

Move `self._github_btn` (`:7364-7370`) from `btn_col` to `info_col`, packed above `self._issues_btn` (`:7417`), changing `anchor="e"` → `anchor="w"`. Its tooltip moves with it.

The right column then contains only the update box (`:7372-7402`), which is what was asked for. The `pady=(22,4)` on `self._update_status_var` (`:7376`) was spacing it below the GitHub button; with that button gone it becomes the top widget and the padding is retuned.

---

## Task 5 — Settings and Main tab

| # | Change | Location |
|---|---|---|
| 5a | Rename section title `"Audio Output"` → `"File Output"` | `:5715-5716` |
| 5b | Swap pack order so `cover_row` comes before `skip_row` | `cover_row` `:5771-5790`, `skip_row` `:5756-5768` |
| 5c | Unbold the Skip checkbutton: style `S.Bold.TCheckbutton` → `S.Opt.TCheckbutton` | `:5758` |
| 5d | Move Open Folder off `skip_row`; add two right-aligned buttons to `genre_row` | from `:5767-5768` to after `:5429` |

**On 5c:** every other Settings option row uses `S.Opt.TCheckbutton` (`:5036`, 10pt `TEXT_MED`). The Skip row is the only one using `S.Bold.TCheckbutton` (`:5029`, 11pt white), which makes it visually outrank its neighbours and read almost like a section header. The file's own comment at `:5033-5035` says `S.Bold` is for the Main and Watch List tabs and Settings should use `S.Opt` — so this fixes a documented inconsistency.

**On 5d:** `genre_row` (`:5415-5416`) is `fill="x"` with all three existing widgets `side="left"`, so there is slack at the right edge. Two buttons are added, both `style="MainBrowse.TButton"`:

- **📂 Genre** — opens `~/Music/DJ-CrateBuilder/<Platform>/<Genre>/` for the currently selected genre
- **📂 Root** — opens the platform root, the existing `_open_download_dir` behaviour (`:7692`)

With `pack(side="right")` the **first declared ends up furthest right** (the pattern already used at `:4797-4801`). So **Root is declared first**, giving a left-to-right reading order of Genre, Root.

The genre-folder handler is new: it resolves the current `self._genre_var` value, maps `"(none)"` back to the `_No Genre` directory, `makedirs(exist_ok=True)`, then platform-dispatches exactly as `_open_download_dir` does.

Existing comments at `:5436-5437` and `:5755` note that Skip / Open Folder were previously relocated *out* of the Main tab into Settings. These are now stale and get updated.

---

## Testing

### Automated — `python -m pytest -q`

| File | Coverage |
|---|---|
| `tests/test_artwork.py` (extended) | Cover round-trip per container using generated silent fixtures (`ffmpeg -f lavfi -i anullsrc`) for MP3/M4A/Opus. `embed_cover_any` dispatch table. `embed_cover()` unchanged for MP3 and still `False` for non-MP3. Remux is skipped with a clear skip reason when FFmpeg is absent. |
| `tests/test_rebuild.py` (new) | `recover_video_id` from a tagged file and from an `.artwork` filename match. `resolve_artwork` returns each of the five resolution branches. **Asserts no file is written or deleted** — the core guarantee. `AUDIO_EXTS` filtering. |
| `tests/test_tagging.py` (extended) | MP4 and Vorbis text tags round-trip; MP3 path unchanged. |

### Manual — `python DJ-CrateBuilder_v1.3.py`

Tasks 3, 4 and 5 are tkinter layout and hover behaviour. They are verified by launching the app and confirming visually. Two things **cannot** be asserted headlessly and will not be claimed as tested: the tooltip hover popups, and the card Cancel button's enabled state during a live scan. These are reported as manually verified or not verified — never as passing tests.

An end-to-end check for Task 1 requires a real download with **Keep original format** on, confirming the resulting `.opus` file carries art in Windows Explorer.

---

## Out of scope

- Any change to `APP_VERSION` (stays `"1.3"`) or `APP_BUILD` (owned by `scripts/release.py`).
- Any `cratebuilder.db` schema change. All three artwork columns already exist from the v4 migration (`cratebuilder/db.py:67-69`); `SCHEMA_VERSION` is not bumped.
- Content-hash deduplication of artwork across different `video_id`s. Reuse-by-key is sufficient once `video_id` recovery lands.
- A "clear artwork cache" button — considered and rejected above.
- Extracting further logic out of the monolith beyond the two new `cratebuilder/` modules, which are new pure-logic units needing tests.

# Cover Art from Source Thumbnails — Design

**Date:** 2026-07-14
**Status:** Approved, phase 1 implementation
**Scope:** Embed the YouTube/SoundCloud thumbnail as cover art on downloaded tracks, keep a sidecar copy on disk, and record both in the database.

---

## Problem

Downloaded MP3s have no cover art. In Windows Explorer they show a generic audio icon; on Android media players and in DJ software they show a blank tile. The source platforms already serve a thumbnail for every track — it should end up on the file.

## Key constraint (drives the whole design)

**ID3 has no usable "reference to an external image file."**

The `APIC` frame either carries the image *bytes* embedded inside the MP3, or (with mime type `-->`) a URL string. Effectively nothing reads the URL variant. Windows Explorer, Windows Media Player, and every Android player read **embedded APIC bytes**. A JPEG sitting in a sibling subfolder is invisible to them — Explorer only honours a `folder.jpg` in the *same* folder, and only as the folder's icon, never as per-track art.

So "art shows up in Explorer/Android" and "image lives in a subfolder" are two separate jobs. We do both:

- **Embedded APIC bytes** — this is what makes art appear.
- **Sidecar JPEG on disk** — archival copy and re-embed source, so art survives a re-encode or a stripped tag without re-fetching from the network.

## Existing building blocks

Everything needed is already a dependency:

- `mutagen` — runtime dep; `cratebuilder/tagging.py` already writes ID3 v2.3.
- `Pillow` — runtime dep (tray icon). YouTube serves `.webp` thumbnails, which need converting to JPEG regardless.
- `_tag_track()` in the monolith is already the single choke point for post-download tagging.

---

## Architecture

### New module: `cratebuilder/artwork.py`

Pure logic, no tkinter, headless-testable — matching the existing package boundary. `tagging.py` is left alone: cover art is a separate concern with a separate dependency (Pillow), and image handling should not be dragged into the text-tag module.

| Function | Responsibility |
|---|---|
| `thumbnail_dir(track_dir)` | Return `<track_dir>/.artwork/`, creating it and setting the Windows hidden attribute (`ctypes.SetFileAttributesW`) on first create. |
| `ingest_thumbnail(raw_path, art_dir, video_id, mode)` | Take whatever yt-dlp wrote (`.webp` from YouTube, `.jpg` from SoundCloud), convert to RGB JPEG q90 via Pillow, apply the crop/pad mode, write `.artwork/<video_id>.jpg`, delete the raw file. Returns the final path or `None`. |
| `embed_cover(mp3_path, jpg_path)` | Write the ID3 `APIC` frame (type 3 "front cover", mime `image/jpeg`), saved as ID3 v2.3 to match `tagging.py`. Returns `True` if the file changed. |
| `has_cover(mp3_path)` | Predicate: does this track already carry cover art. The phase-2 backfill's entry point. |

### Sidecar layout

`~/Music/DJ-CrateBuilder/<Platform>/<Genre>/<Channel>/.artwork/<video_id>.jpg`

- **Dot-prefixed folder** — hidden on Linux, marked hidden on Windows. Stays out of rekordbox/Serato folder scans and out of the user's way in the crate browser.
- **Named by `video_id`** — stable, collision-free, filesystem-safe with no sanitising needed. Matching art to track is exact via the `video_id` already stored in the `downloads` table. Survives the track being renamed.

---

## Data

**Schema v3 → v4.** Three new columns on `downloads`, added with the same idempotent `ALTER TABLE`-inside-a-`try` pattern `_init_schema` already uses for the v2 and v3 migrations.

| Column | Type | Purpose |
|---|---|---|
| `artwork_path` | `TEXT` | Absolute path to the sidecar JPEG |
| `artwork_embedded` | `INTEGER DEFAULT 0` | `0`/`1` — did the APIC write actually land |
| `thumbnail_url` | `TEXT` | Source thumbnail URL from yt-dlp's metadata |

`thumbnail_url` is carried now, not later, because it is the **phase-2 backfill enabler**: for YouTube a backfill could reconstruct the URL from `video_id`, but for **SoundCloud it cannot** — the artwork URL is not derivable from the track ID. One column now, or a full library re-download later.

`add_download()` and `backfill_downloads()` take the three new fields as keyword args defaulting to `None`/`0`, so every existing call site keeps working untouched.

---

## Download flow

`writethumbnail: True` is added to `ydl_opts`.

This deliberately does **not** use yt-dlp's `EmbedThumbnail` postprocessor. We do the embed ourselves so that we control the crop, keep the sidecar, and can reuse the identical code path in the phase-2 backfill — where no yt-dlp download is happening at all.

Per track, after a successful download:

1. Locate the thumbnail yt-dlp wrote next to the audio file.
2. `ingest_thumbnail(...)` → converts, crops, writes `.artwork/<video_id>.jpg`, removes the raw file.
3. `embed_cover(...)` → writes the APIC frame onto the MP3.
4. `db.add_download(..., artwork_path=..., artwork_embedded=..., thumbnail_url=...)`.

`_tag_track(path, title, url)` is already the single choke point for post-download tagging — three call sites (fresh download, age-gate retry, already-on-disk backfill). It grows `video_id` and `thumb_dir` params and gains the artwork step after the text tags.

### Edge cases

All resolve as no-ops, never as failures.

- **"Keep original format" is on** — the file is `.webm`/`.m4a`, not MP3. The sidecar JPEG is still saved and recorded; the APIC embed is skipped and `artwork_embedded` stays `0`. (Opus and MP4 cover art use entirely different frame formats — out of scope for phase 1.)
- **Already-on-disk skip path** — no download happened, so there is no thumbnail. Text tags backfill as they do today; artwork is left for phase 2.
- **No thumbnail available / Pillow fails / mutagen fails** — logged to `debug.log`, the download still counts as a success. *An artwork failure must never fail a track* — the same contract `write_track_tags` already honours.

---

## Settings

One dropdown in the Settings tab, persisted to config as `cover_art_mode`:

| Value | Behaviour |
|---|---|
| `crop` (**default**) | 1280×720 → centred 720×720 square. Fills the art box in every player with no bars — looks like real album art. |
| `original` | 16:9 embedded as-is. Nothing is cropped away; players letterbox it. |
| `off` | No thumbnail download, no embed, no sidecar. |

Folding the off-switch into the aspect dropdown avoids a separate boolean and a second config key.

---

## Testing

**`tests/test_artwork.py`** — headless, synthesising a 1280×720 test image with Pillow and a minimal generated MP3:

- Crop geometry — a 1280×720 source yields a 720×720 centred square.
- `original` mode preserves the source aspect ratio.
- `webp` → `jpeg` conversion.
- APIC round-trip: `embed_cover` then `has_cover` returns `True`.
- Non-MP3 input is a no-op returning `False`, not a raise.
- Corrupt/truncated image input returns `None`, not a raise.

**`tests/test_db.py`** — a v3 database migrates to v4 without data loss, and the new columns default correctly on existing rows.

---

## Out of scope (phase 2)

- Bulk "Fetch missing artwork" job over the existing library. The schema, `has_cover()`, and `thumbnail_url` are designed to make this a small additive change rather than a redesign.
- Cover art for non-MP3 containers (Opus `METADATA_BLOCK_PICTURE`, MP4 `covr`).

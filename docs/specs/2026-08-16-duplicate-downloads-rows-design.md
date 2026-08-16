# Duplicate `downloads` Rows — Design

**Date:** 2026-08-16
**Status:** Implemented
**Branch:** `main` (routine work per project convention)
**Scope:** Stop the `downloads` table growing a second row for a file it already records, and give the user a deliberate way to clean up the rows already there. Schema v6 → v7.

---

## Problem

The live database held **59,632 rows for 31,955 distinct `file_path` values** — 27,677 redundant rows. Some tracks appeared four or five times.

The table had no uniqueness on `file_path`, and every write path was INSERT-only. Three mechanisms followed from that, measured on a copy of the live database:

**1. Rebuild followed by a scan — 27,182 of the 27,500 duplicate groups.**

`_rebuild_db_from_files` clears the table and re-derives every row from disk. `recover_video_id` reads the id back off the file, and for most of the library it cannot: 27,542 rebuilt rows carried `video_id = NULL`. Those rows are otherwise complete — an `upload_date` synthesised from the file's mtime, cover art re-resolved from disk, no bitrate.

`classify_scan_entries` then decides a track is already owned only via `is_downloaded(video_id)`. With the ids gone, every one of a channel's videos looked unknown — but the file *was* on disk, so each landed in the `on_disk` bucket, whose whole purpose is to be backfilled "so future dedup is exact and instant". `backfill_downloads` inserted every one of them as a new row.

The fingerprints are unambiguous. The rebuild ran 2026-07-14; a full Watch List scan ran 2026-07-20 01:23–01:26, inserting 27,635 rows across 32 batch timestamps for 21 channels — all id-bearing, none with a bitrate, 27,620 with no upload date. One rebuild plus one scan doubled the library. (The cover art those rows now carry arrived later still: `set_download_artwork` updates `WHERE file_path = ?`, so a subsequent Fetch Missing Artwork stamped both rows of each pair — 27,401 pairs share an identical `artwork_path`.)

**2. One file, several videos — 168 groups of 3 to 5 rows.**

`folder_keys` maps a *normalised title* to a path, so several videos sharing a recurring title ("Neurofunk Drum and Bass 2025") all match the one file on disk. Each backfilled the same `file_path` under its own video id.

**3. Re-downloads — 30 groups.**

`add_download` always inserted, so a Force Download or a repeat download of a track already recorded added a second row.

Every one of these is the same missing invariant: **one file on disk is one row**.

## Requirements

1. No write path may add a second row for a file already recorded.
2. A database that already holds duplicates must keep working, unchanged, until the user chooses to clean it — schema init must never delete rows on its own.
3. The cleanup keeps everything the duplicates knew and never touches audio files, cover art or the Watch List.
4. `get_artwork_by_path` (keyed by `file_path`) and `move_channel_downloads` (rewrites by `file_path` prefix) must keep working.

## Design

### Uniqueness (schema v7)

A **partial** unique index:

```sql
CREATE UNIQUE INDEX idx_dl_file_path_unique
    ON downloads(file_path) WHERE file_path IS NOT NULL AND file_path != ''
```

Partial because `file_path` may be empty — a download whose real path was never resolved keys on nothing, and two such rows are not evidence of the same track.

The index cannot be built over existing duplicates. Rebuilding it at startup would mean deleting the user's rows behind their back during a routine app start, so `_try_unique_path_index` treats the `IntegrityError` as expected: it logs the count, records `has_unique_path_index = False`, and lets schema init succeed. Those databases behave exactly as they did at v6 until the user runs the cleanup, which retries the index. (Same spirit as the v6 `scan_cutoff_date` drop: a migration that cannot take must not stop the app starting.)

### Upserts, with the right column winning

Both write paths take `ON CONFLICT(file_path)` when the index exists, and fall back to plain INSERT when it does not.

`add_download` describes what is on disk *now*, so its values win — except where it has nothing to say, which never blanks out what the row already knew (`COALESCE(NULLIF(excluded.x, ''), x)`).

`backfill_downloads` is the opposite. What a scan or a rebuild genuinely knows is **where a track lives**, so title, channel and genre follow the backfill. What a track *is* — bitrate, upload date, cover art, video id, download timestamp — is only filled in where the existing row is empty, never overwritten. That is what makes the scan path idempotent, and it turns the scan's backfill into the repair it was always trying to be: the row missing its id gets the id, instead of a second row carrying it.

### Path rewrites

`move_channel_downloads` and `update_download_path` become `UPDATE OR REPLACE`. A genre move can land a track on a path that already has a row; under a bare `UPDATE` that collision would roll the transaction back and abandon the entire move. The rows being moved describe the files that are actually there, so they replace the stale ones.

### Cleanup — `dedupe_downloads_by_path`

Deliberate only. Nothing calls it during startup, a scan, a download or a rebuild; it runs from **Settings ▸ Downloads Database ▸ 🧹 Remove Duplicate Rows**, which shows the count first and requires an explicit OK.

Within each group the richest row survives — a real bitrate first (only `add_download` writes one), then a real upload date, embedded art, a sidecar, a video id, newest timestamp — and every column it left empty is filled from the other rows in the group, richest first. So the id a scan recovered and the art a rebuild resolved end up on the same surviving row, and the group loses nothing. Rows with no `file_path` are left alone. The index is created afterwards, which is what makes the cleanup a one-time action rather than a recurring chore.

## Verification

Against a copy of the live database (the live file is never touched):

| | before | after |
|---|---|---|
| rows | 59,632 | 31,955 |
| distinct paths | 31,955 | 31,955 |
| duplicate groups | 27,500 | 0 |
| rows with cover art | 31,894 | 31,894 (0 lost) |
| rows with an upload date | 31,955* | 31,955 |

\* across the duplicate pairs, merged onto the survivors. Cleanup took 0.9 s.

Replaying the original sequence on the cleaned copy — rebuild (ids lost), then a full Watch List scan — leaves the table at 31,955 rows and re-attaches 31,883 video ids, where before it would have inserted ~27,000 rows.

23 tests in `tests/test_db_dedupe.py`, including the rebuild-then-scan regression, the merge, the collision cases, and a legacy database whose duplicates block the index.

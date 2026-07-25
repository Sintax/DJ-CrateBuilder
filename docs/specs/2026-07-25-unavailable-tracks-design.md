# Permanently-Unavailable Track Memory — Design

**Date:** 2026-07-25
**Status:** Approved, ready for planning
**Branch:** `main` (routine work per project convention; the schema change is additive and migration-free)
**Scope:** Stop the Watch List re-reporting tracks that can never be downloaded. Permanent download failures (DRM-protected, removed, geo-blocked) are recorded in a new `unavailable_tracks` table and excluded from a channel card's "new" count. Platform-agnostic — YouTube and SoundCloud both benefit.

---

## Problem

The UKG 2025 SoundCloud channel reports the same 47 new tracks on every scan. Those 47 are DRM-protected: SoundCloud serves only encrypted formats for them, so they fail on every download attempt and always will.

The detection loop has no memory of failure. `classify_scan_entries` ([`cratebuilder/sidecar.py`](../../cratebuilder/sidecar.py)) drops an entry from the "new" bucket only when `is_downloaded(video_id)` is true — that is, when the track has a row in `downloads`. And `add_download` is called **only on success**. A track that fails permanently is therefore indistinguishable, forever, from a track that was never attempted.

The information already exists at download time and is thrown away. In the batch worker's error handler, `classify_permanent_failure(clean)` ([`cratebuilder/util.py`](../../cratebuilder/util.py)) already returns `"DRM-protected"`, `"Removed"` or `"Geo-blocked"`; the run summary already prints `∅ 47 unavailable (DRM/removed)`. Nothing persists it.

Consequences on every cycle:

- The card badge overstates what is actually obtainable ("+47 new" when the true answer is 0).
- Every "Download New" run re-attempts all 47 and re-fails all 47, wasting time and requests.
- The overstated count propagates to the Watch List header total, the tray menu label, and auto-download decisions.

## Requirements

1. A track that fails for a permanent reason must stop being counted as new.
2. The memory must survive **Rebuild Database from Files**, which clears the `downloads` table.
3. Download-history semantics must not be polluted: a failed track is not a download and must never appear in the Database window, `total_downloaded`, or the artwork-backfill worklist.
4. A geo-blocked track must be able to come back on its own if the user's location or VPN changes.
5. A brief outage must not permanently bury a real track.
6. The user must be able to wipe the memory for a channel and force a fresh check.

---

## Decisions

Three policy questions were resolved with the maintainer before design:

| # | Question | Decision |
|---|----------|----------|
| 1 | Are geo-blocked tracks condemned permanently? | **No.** DRM and Removed are permanent. Geo-blocked is suppressed but automatically re-checked every **7 days**. |
| 2 | Does the channel card show a note about hidden tracks? | **No card note.** The card simply shows the honest new count. A **Forget unavailable tracks** button in the Edit modal clears the memory. |
| 3 | How many failures condemn a track? | **DRM: one failure.** DRM is deterministic proof. **Removed and Geo-blocked: two failures**, so a transient outage or 404 blip cannot bury a live track. |

---

## Architecture

Four moving parts:

1. **`unavailable_tracks` table** in `cratebuilder.db` (schema v5) — the memory.
2. **Write point** in the batch worker's permanent-failure branch — records the failure.
3. **Suppression predicate** injected into `classify_scan_entries` — a pure function decides whether a remembered track is currently suppressed.
4. **Forget action** in the Watch List Edit modal — clears the memory for one channel.

### 1. Schema (v4 → v5)

```sql
CREATE TABLE IF NOT EXISTS unavailable_tracks (
    platform     TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    channel_url  TEXT,
    title        TEXT,
    reason       TEXT NOT NULL,   -- 'DRM-protected' | 'Removed' | 'Geo-blocked'
    attempts     INTEGER NOT NULL DEFAULT 1,
    first_failed INTEGER NOT NULL,
    last_failed  INTEGER NOT NULL,
    PRIMARY KEY (platform, video_id)
);
CREATE INDEX IF NOT EXISTS idx_unavail_channel_url
    ON unavailable_tracks(channel_url);
```

`SCHEMA_VERSION` goes 4 → 5. The migration is the `CREATE TABLE IF NOT EXISTS` itself — a new table needs no `ALTER`, so it is naturally idempotent and safe on every existing database. No user data is touched.

**Key choice.** `(platform, video_id)` rather than `video_id` alone. `downloads` dedups on `video_id`, but YouTube ids (11-char base64) and SoundCloud ids (numeric) live in the same namespace here; including the platform costs nothing and removes the collision class entirely.

**Entries with no id are never recorded.** A track with no `video_id` cannot be keyed, so it cannot be remembered — matching the existing behaviour of `is_downloaded`, which also no-ops on an empty id. This is accepted: in practice both platforms' flat listings supply ids.

### 2. Write point

In the batch worker's error handler, inside the existing `if _perm:` branch (alongside `unavail += 1`):

```python
self._db.record_unavailable(
    platform=platform,
    video_id=entry.get("id") or "",
    channel_url=url if is_collection else "",
    title=item_title,
    reason=_perm)
```

`record_unavailable` is an upsert: it inserts with `attempts = 1` and both timestamps set, or on conflict bumps `attempts = attempts + 1`, refreshes `last_failed`, and overwrites `reason` with the latest classification (a track that goes DRM → Removed should be judged on its current reason). Like every other DB method it swallows and logs exceptions — recording a failure must never break a download run.

`title` and `channel_url` are stored for diagnostics only; nothing keys on them except the per-channel forget.

### 3. Suppression predicate

The decision of whether a remembered track is *currently* suppressed is pure logic, unit-tested, and lives in `cratebuilder/db.py` beside the query that feeds it:

```python
GEO_RECHECK_SECONDS = 7 * 24 * 3600

def is_suppressed(reason, attempts, last_failed, now):
    """True if a remembered failure should hide the track from 'new'."""
    if reason == "DRM-protected":
        return attempts >= 1
    if reason == "Removed":
        return attempts >= 2
    if reason == "Geo-blocked":
        return attempts >= 2 and (now - last_failed) < GEO_RECHECK_SECONDS
    return False
```

An unrecognised reason returns False — the memory can only ever hide tracks it positively understands, so a future reason string added to `classify_permanent_failure` degrades to today's behaviour rather than hiding things silently.

**Geo re-check behaviour.** After 7 days the track stops being suppressed and reappears as new exactly once. If it fails again, `last_failed` is refreshed and it hides for another 7 days. If it succeeds, it lands in `downloads` and `is_downloaded` takes over permanently — the stale `unavailable_tracks` row is then harmless (see *Precedence*).

**Precedence.** `is_downloaded` is checked first and wins. A track that was once unavailable but has since downloaded is a download, full stop.

### 4. Scan integration

`DownloadsDatabase` gains `get_suppressed_reasons(platform, now=None)` returning a `{video_id: reason}` dict of everything currently suppressed for that platform. The scan fetches it **once** per channel — not one query per entry — and hands `dict.get` to the classifier as the predicate. Returning the reason rather than a bare bool is what lets the classifier report *why* a track was skipped.

`classify_scan_entries` gains one keyword-only parameter:

```python
def classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec,
                          platform, is_unavailable=None):
```

`is_unavailable(video_id)` returns a **reason string** when the track is suppressed, or a falsy value when it is not. It defaults to `None`, treated as "never suppressed", so existing callers are unaffected. The check sits immediately after the `is_downloaded` check, before the duration filter and the folder-key match.

The return value gains a third bucket:

```python
{"new": [...], "on_disk": [...], "unavailable": [...]}
```

`"unavailable"` items carry `{id, title, reason}`. They are **not** written to the card and **not** counted anywhere — the bucket exists so the scan can emit an honest debug/scan-log line and so the behaviour is directly assertable in tests. Callers that only read `["new"]` and `["on_disk"]` keep working.

The scan logs one line when the bucket is non-empty:

```
UKG 2025: 0 new tracks found (47 permanently unavailable, skipped)
```

This is the *only* user-visible trace, per decision 2 — it appears in the Watch List scan log, not on the card.

### 5. Forget action

The Watch List Edit modal gains a **Forget unavailable tracks** button. It is:

- **Disabled with a zero count** when the channel has no remembered failures; labelled `Forget unavailable tracks (47)` when it does. The count comes from `count_unavailable_for_channel(channel_url)`.
- **Confirmed** via `messagebox.askyesno`, consistent with the Remove-genre button added in build 43.
- Wired to `forget_unavailable_for_channel(channel_url)`, which deletes the rows and returns the count removed.

Because the memory is keyed on `channel_url`, forgetting is exactly scoped to one card. Nothing else is touched — no files, no download history.

---

## Interaction with existing features

**Rebuild Database from Files** clears only `downloads` (`clear_all_downloads`). `unavailable_tracks` is untouched, so a rebuild does not resurrect the 47. This is requirement 2 and is satisfied for free by the separate-table choice — it is the decisive argument against storing sentinel rows in `downloads`.

**Folders Cleanup** deletes `downloads` rows by path (`delete_downloads_by_paths`). Unavailable tracks have no file on disk, so cleanup cannot reach them and needs no change.

**Watch List totals / tray label / auto-download.** These all read `pending_new_count`, which now reflects the honest number. No changes needed at those call sites — they get correct data automatically.

**Main-tab batch downloads** write to the same table through the same code path (the error handler is shared), so pasting a channel URL into the Main tab also teaches the app. Suppression only *reads* the table during a Watch List scan; a Main-tab run still attempts everything the listing returns, which is the correct behaviour for an explicit user-initiated download.

## Rejected alternatives

**Sentinel rows in `downloads`** (empty `file_path` plus a status column). No new table and dedup works for free, but it inflates `total_downloaded`, pollutes the Database window and `refresh_watchlist_totals`, and — fatally — is erased by Rebuild Database from Files.

**Per-channel sidecar (`cratebuilder.json`) list.** Travels with the folder and needs no schema bump, but sidecars are user data with a stricter change bar, and it does nothing for Main-tab downloads outside a watched channel folder. Viable as a future complement, not as the primary store.

**Detect DRM at scan time** by un-flattening extraction. Would need a full per-track extraction — hundreds of extra network round-trips per scan. Rejected on cost.

---

## Testing

Pure-logic only; no tkinter. Run with `python -m pytest -q`.

**`tests/test_db.py`** (or a new `tests/test_unavailable.py`)
- `record_unavailable` inserts a row with `attempts == 1` and equal timestamps.
- A second call for the same `(platform, video_id)` bumps `attempts` to 2 and moves `last_failed` only.
- A reason change on re-failure overwrites `reason`.
- The same `video_id` on two platforms yields two independent rows.
- An empty `video_id` records nothing.
- `get_suppressed_reasons` returns DRM after one failure, and Removed only after two.
- `get_suppressed_reasons` omits a geo-blocked row older than 7 days and includes one newer.
- `get_suppressed_reasons` maps each suppressed id to its reason string.
- `count_unavailable_for_channel` / `forget_unavailable_for_channel` are scoped to one `channel_url`.
- Schema v5 initialises cleanly on a fresh DB **and** on a DB created at v4.

**`tests/test_sidecar.py`**
- `classify_scan_entries` with no `is_unavailable` behaves exactly as before (regression guard).
- A suppressed id lands in `"unavailable"`, not `"new"`.
- A track that is both downloaded and suppressed is dropped by the `is_downloaded` check (precedence).
- The `"unavailable"` bucket carries `{id, title, reason}`.

**Manual verification** (per CLAUDE.md, tkinter changes must be seen): scan UKG 2025 before and after a download run and confirm the card drops from 47 to 0 and the scan log names the skip; open Edit and confirm the Forget button shows `(47)`, clears on confirm, and that a re-scan then reports 47 again.

## Out of scope

- No per-track UI listing of what is unavailable (decision 2 — no card note).
- No global "forget everything" button; per-channel only.
- No retry policy for non-permanent errors — transient failures keep today's behaviour.
- No `APP_VERSION` change. Ships as a normal nightly build.

# Permanently-Unavailable Track Memory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Watch List re-reporting tracks that can never be downloaded, by remembering permanent download failures (DRM-protected, removed, geo-blocked) and excluding them from a channel's "new" count.

**Architecture:** A new `unavailable_tracks` table in `cratebuilder.db` (schema v5) records permanent failures at the moment the download loop already classifies them. A pure predicate decides whether a remembered failure is *currently* suppressed (DRM forever after 1 failure; Removed forever after 2; Geo-blocked after 2 but re-checked every 7 days). The Watch List scan loads the suppression map once per channel and hands it to `classify_scan_entries`, which grows a third output bucket.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, pytest, tkinter (GUI wiring only).

**Spec:** [`docs/specs/2026-07-25-unavailable-tracks-design.md`](../2026-07-25-unavailable-tracks-design.md)

## Global Constraints

- **No tkinter imports in `cratebuilder/`** — the package is a pure-logic boundary.
- `SCHEMA_VERSION` goes **4 → 5**. The migration is a `CREATE TABLE IF NOT EXISTS` — additive, idempotent, no `ALTER`, no user data rewritten.
- Every new `DownloadsDatabase` method **swallows and logs** exceptions via `self._log("error", …)` and returns a safe empty value, matching every existing method. Recording or reading a failure must never break a download run.
- Reason strings are exactly the three `classify_permanent_failure` returns: `"DRM-protected"`, `"Removed"`, `"Geo-blocked"`. An unrecognised reason is **never** suppressed.
- Geo re-check window: `GEO_RECHECK_SECONDS = 7 * 24 * 3600`.
- Suppression thresholds: DRM `attempts >= 1`; Removed `attempts >= 2`; Geo-blocked `attempts >= 2` **and** `now - last_failed < GEO_RECHECK_SECONDS`.
- `is_downloaded` takes precedence over suppression — a track that has since downloaded is a download.
- **No card note** — the only user-visible trace of a skip is a line in the Watch List scan log. Per-channel Forget button lives in the Edit modal.
- `cratebuilder/` modules use **one-line module docstrings**; the monolith uses multi-line docstrings and `# ══…` Unicode dividers. Match the file you are editing.
- **Do not bump `APP_BUILD` or `APP_VERSION`** — `scripts/release.py` owns `APP_BUILD`.
- Commit messages: Conventional Commits, ending with the trailer
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Full test command: `python -m pytest -q` from the repo root.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `cratebuilder/db.py` | Modify | New table in `_init_schema`; `is_suppressed` pure predicate + module constant; `record_unavailable`, `get_suppressed_reasons`, `count_unavailable_for_channel`, `forget_unavailable_for_channel` |
| `cratebuilder/sidecar.py` | Modify | `classify_scan_entries` gains `is_unavailable` and a third `"unavailable"` bucket |
| `tests/test_unavailable.py` | Create | All DB-side tests for the new table and predicate |
| `tests/test_scan_classifier.py` | Modify | New bucket coverage + fix the now-stale exact-dict assertion |
| `DJ-CrateBuilder_v1.3.py` | Modify | Write point in `_process_one_url`; scan integration in `_watchlist_scan_channel`; Forget button in `_watchlist_edit_channel` |

---

### Task 1: Schema v5 + `record_unavailable`

**Files:**
- Modify: `cratebuilder/db.py` (`SCHEMA_VERSION`, `_init_schema`, new method after `is_video_downloaded`)
- Modify: `tests/test_db.py:273-279` (the existing `test_schema_version_is_4`)
- Test: `tests/test_unavailable.py` (create)

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `DownloadsDatabase.SCHEMA_VERSION == 5`
  - table `unavailable_tracks(platform, video_id, channel_url, title, reason, attempts, first_failed, last_failed)` with `PRIMARY KEY (platform, video_id)`
  - `DownloadsDatabase.record_unavailable(*, platform, video_id, channel_url, title, reason, now=None) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_unavailable.py`:

```python
"""Tests for the permanently-unavailable track memory (unavailable_tracks)."""
from cratebuilder.db import DownloadsDatabase


def _new_db(tmp_path, name="test.db"):
    return DownloadsDatabase(str(tmp_path / name))


def _rows(db):
    with db._conn() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM unavailable_tracks")]


def test_unavailable_table_exists_and_reinit_is_idempotent(tmp_path):
    db = _new_db(tmp_path)
    assert _rows(db) == []
    # Re-opening the same file must not raise and must keep the table usable.
    db2 = DownloadsDatabase(str(tmp_path / "test.db"))
    assert _rows(db2) == []


def test_record_unavailable_inserts_first_failure(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(
        platform="SoundCloud", video_id="123", channel_url="https://sc/ukg",
        title="Some Track", reason="DRM-protected", now=1000)
    assert ok is True
    row = _rows(db)[0]
    assert row["attempts"] == 1
    assert row["first_failed"] == 1000
    assert row["last_failed"] == 1000
    assert row["reason"] == "DRM-protected"
    assert row["title"] == "Some Track"
    assert row["channel_url"] == "https://sc/ukg"


def test_record_unavailable_second_failure_bumps_attempts(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="https://sc/ukg", title="T",
                          reason="Removed", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="https://sc/ukg", title="T",
                          reason="Removed", now=2000)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["first_failed"] == 1000   # unchanged
    assert rows[0]["last_failed"] == 2000    # refreshed


def test_record_unavailable_updates_reason_on_refailure(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="", title="T",
                          reason="Geo-blocked", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="123",
                          channel_url="", title="T",
                          reason="Removed", now=2000)
    assert _rows(db)[0]["reason"] == "Removed"


def test_same_id_on_two_platforms_is_two_rows(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="abc",
                          channel_url="", title="A",
                          reason="DRM-protected", now=1000)
    db.record_unavailable(platform="YouTube", video_id="abc",
                          channel_url="", title="A",
                          reason="Removed", now=1000)
    assert len(_rows(db)) == 2


def test_record_unavailable_ignores_empty_video_id(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(platform="YouTube", video_id="",
                               channel_url="", title="T",
                               reason="Removed", now=1000)
    assert ok is False
    assert _rows(db) == []


def test_record_unavailable_ignores_empty_reason(tmp_path):
    db = _new_db(tmp_path)
    ok = db.record_unavailable(platform="YouTube", video_id="v1",
                               channel_url="", title="T",
                               reason="", now=1000)
    assert ok is False
    assert _rows(db) == []
```

- [ ] **Step 2: Update the existing schema-version test**

`tests/test_db.py:273-279` pins the old version. Replace that whole test with:

```python
def test_schema_version_is_5(tmp_path):
    db = _new_db(tmp_path)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'").fetchone()
    assert row["value"] == "5"
    assert DownloadsDatabase.SCHEMA_VERSION == 5
```

Leave `test_v3_database_migrates_to_v4_without_data_loss` (immediately below it)
alone — it asserts a real historical migration path and still holds.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_unavailable.py tests/test_db.py -q`
Expected: FAIL — `sqlite3.OperationalError: no such table: unavailable_tracks`,
`AttributeError: 'DownloadsDatabase' object has no attribute 'record_unavailable'`,
and `assert '4' == '5'` in `test_schema_version_is_5`.

- [ ] **Step 4: Bump the schema version**

In `cratebuilder/db.py`, change:

```python
    SCHEMA_VERSION = 4
```

to:

```python
    SCHEMA_VERSION = 5
```

- [ ] **Step 5: Add the table to `_init_schema`**

In the `conn.executescript("""…""")` block in `_init_schema`, append after the `watchlist` table definition (still inside the triple-quoted script, before the closing `"""`):

```sql
                    CREATE TABLE IF NOT EXISTS unavailable_tracks (
                        platform     TEXT NOT NULL,
                        video_id     TEXT NOT NULL,
                        channel_url  TEXT,
                        title        TEXT,
                        reason       TEXT NOT NULL,
                        attempts     INTEGER NOT NULL DEFAULT 1,
                        first_failed INTEGER NOT NULL,
                        last_failed  INTEGER NOT NULL,
                        PRIMARY KEY (platform, video_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_unavail_channel_url
                        ON unavailable_tracks(channel_url);
```

No `ALTER` migration is needed — a brand-new table is created identically on fresh and pre-existing databases.

- [ ] **Step 6: Add `record_unavailable`**

In `cratebuilder/db.py`, immediately after the `is_video_downloaded` method, add:

```python
    def record_unavailable(self, *, platform, video_id, channel_url, title,
                           reason, now=None):
        """Remember that a track failed for a permanent reason.

        Upsert keyed on (platform, video_id): a first failure inserts with
        attempts=1, a repeat bumps attempts and refreshes last_failed and
        reason. Returns True when a row was written. Tracks with no id or no
        reason cannot be keyed or judged, so they are ignored (False)."""
        if not video_id or not reason:
            return False
        ts = int(now if now is not None else time.time())
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO unavailable_tracks
                      (platform, video_id, channel_url, title, reason,
                       attempts, first_failed, last_failed)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(platform, video_id) DO UPDATE SET
                        attempts    = attempts + 1,
                        last_failed = excluded.last_failed,
                        reason      = excluded.reason,
                        title       = COALESCE(NULLIF(excluded.title, ''),
                                               title),
                        channel_url = COALESCE(NULLIF(excluded.channel_url, ''),
                                               channel_url)
                """, (platform or "", video_id, channel_url or "",
                      title or "", reason, ts, ts))
            return True
        except Exception as e:
            self._log("error", f"record_unavailable failed for "
                               f"{video_id!r}: {e}")
            return False
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_unavailable.py tests/test_db.py -q`
Expected: PASS — 7 new tests in `test_unavailable.py`, plus the whole of
`test_db.py` including the retargeted `test_schema_version_is_5`.

- [ ] **Step 8: Run the full suite for regressions**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add cratebuilder/db.py tests/test_unavailable.py tests/test_db.py
git commit -m "feat(db): add unavailable_tracks table and record_unavailable (schema v5)"
```

---

### Task 2: Suppression predicate + `get_suppressed_reasons`

**Files:**
- Modify: `cratebuilder/db.py` (module-level constant + function above the class; new method after `record_unavailable`)
- Test: `tests/test_unavailable.py` (append)

**Interfaces:**
- Consumes: `record_unavailable` and the `unavailable_tracks` table from Task 1
- Produces:
  - `cratebuilder.db.GEO_RECHECK_SECONDS = 604800`
  - `cratebuilder.db.is_suppressed(reason, attempts, last_failed, now) -> bool`
  - `DownloadsDatabase.get_suppressed_reasons(platform, now=None) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_unavailable.py` (and extend the import line at the top of the file to
`from cratebuilder.db import DownloadsDatabase, GEO_RECHECK_SECONDS, is_suppressed`):

```python
DAY = 24 * 3600


def test_geo_recheck_window_is_seven_days():
    assert GEO_RECHECK_SECONDS == 7 * DAY


def test_is_suppressed_drm_after_one_failure():
    assert is_suppressed("DRM-protected", 1, 1000, 1000) is True


def test_is_suppressed_removed_needs_two_failures():
    assert is_suppressed("Removed", 1, 1000, 1000) is False
    assert is_suppressed("Removed", 2, 1000, 1000) is True


def test_is_suppressed_removed_never_expires():
    assert is_suppressed("Removed", 2, 1000, 1000 + 999 * DAY) is True


def test_is_suppressed_geo_needs_two_failures_and_expires():
    # One failure is never enough.
    assert is_suppressed("Geo-blocked", 1, 1000, 1000) is False
    # Two failures, fresh -> suppressed.
    assert is_suppressed("Geo-blocked", 2, 1000, 1000 + 6 * DAY) is True
    # Two failures, older than the window -> eligible again.
    assert is_suppressed("Geo-blocked", 2, 1000, 1000 + 8 * DAY) is False


def test_is_suppressed_unknown_reason_is_never_suppressed():
    assert is_suppressed("Something New", 99, 1000, 1000) is False


def test_get_suppressed_reasons_maps_id_to_reason(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="drm",
                          channel_url="", title="D",
                          reason="DRM-protected", now=1000)
    assert db.get_suppressed_reasons("SoundCloud", now=1000) == {
        "drm": "DRM-protected"}


def test_get_suppressed_reasons_honours_the_two_strike_rule(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="YouTube", video_id="gone",
                          channel_url="", title="G",
                          reason="Removed", now=1000)
    assert db.get_suppressed_reasons("YouTube", now=1000) == {}
    db.record_unavailable(platform="YouTube", video_id="gone",
                          channel_url="", title="G",
                          reason="Removed", now=2000)
    assert db.get_suppressed_reasons("YouTube", now=2000) == {
        "gone": "Removed"}


def test_get_suppressed_reasons_expires_geo_after_the_window(tmp_path):
    db = _new_db(tmp_path)
    for ts in (1000, 2000):
        db.record_unavailable(platform="YouTube", video_id="geo",
                              channel_url="", title="G",
                              reason="Geo-blocked", now=ts)
    assert db.get_suppressed_reasons("YouTube", now=2000 + 6 * DAY) == {
        "geo": "Geo-blocked"}
    assert db.get_suppressed_reasons("YouTube", now=2000 + 8 * DAY) == {}


def test_get_suppressed_reasons_is_scoped_to_one_platform(tmp_path):
    db = _new_db(tmp_path)
    db.record_unavailable(platform="SoundCloud", video_id="x",
                          channel_url="", title="X",
                          reason="DRM-protected", now=1000)
    assert db.get_suppressed_reasons("YouTube", now=1000) == {}


def test_get_suppressed_reasons_empty_on_fresh_db(tmp_path):
    assert _new_db(tmp_path).get_suppressed_reasons("YouTube") == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_unavailable.py -q`
Expected: FAIL — `ImportError: cannot import name 'GEO_RECHECK_SECONDS'`.

- [ ] **Step 3: Add the constant and the pure predicate**

In `cratebuilder/db.py`, after the imports and before `class DownloadsDatabase:`, add:

```python
# How long a geo-blocked track stays suppressed before the app gives it
# another chance (the user's location or VPN may have changed since).
GEO_RECHECK_SECONDS = 7 * 24 * 3600


def is_suppressed(reason, attempts, last_failed, now):
    """True if a remembered permanent failure should hide a track from 'new'.

    DRM is deterministic proof on the first failure. Removed and Geo-blocked
    need two, so a transient outage or 404 blip can't bury a live track.
    Geo-blocked additionally expires after GEO_RECHECK_SECONDS. An unrecognised
    reason is never suppressed, so a future reason string added to
    classify_permanent_failure degrades to today's behaviour rather than
    hiding tracks silently."""
    if reason == "DRM-protected":
        return attempts >= 1
    if reason == "Removed":
        return attempts >= 2
    if reason == "Geo-blocked":
        return attempts >= 2 and (now - last_failed) < GEO_RECHECK_SECONDS
    return False
```

- [ ] **Step 4: Add `get_suppressed_reasons`**

In `cratebuilder/db.py`, immediately after `record_unavailable`, add:

```python
    def get_suppressed_reasons(self, platform, now=None):
        """Return {video_id: reason} for every track on *platform* currently
        hidden by the unavailable-track memory.

        Fetched once per scan and handed to classify_scan_entries as its
        is_unavailable predicate (dict.get), so a scan costs one query rather
        than one per entry. Returns {} on failure."""
        ts = int(now if now is not None else time.time())
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT video_id, reason, attempts, last_failed
                    FROM unavailable_tracks WHERE platform = ?
                """, (platform or "",)).fetchall()
            return {r["video_id"]: r["reason"] for r in rows
                    if is_suppressed(r["reason"], r["attempts"],
                                     r["last_failed"], ts)}
        except Exception as e:
            self._log("error", f"get_suppressed_reasons failed: {e}")
            return {}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_unavailable.py -q`
Expected: PASS (18 tests: 7 from Task 1 + 11 new)

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/db.py tests/test_unavailable.py
git commit -m "feat(db): add suppression predicate and get_suppressed_reasons"
```

---

### Task 3: Per-channel count + forget

**Files:**
- Modify: `cratebuilder/db.py` (two methods after `get_suppressed_reasons`)
- Test: `tests/test_unavailable.py` (append)

**Interfaces:**
- Consumes: the table and `record_unavailable` from Task 1
- Produces:
  - `DownloadsDatabase.count_unavailable_for_channel(channel_url) -> int`
  - `DownloadsDatabase.forget_unavailable_for_channel(channel_url) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_unavailable.py`:

```python
def _seed_two_channels(db):
    db.record_unavailable(platform="SoundCloud", video_id="a",
                          channel_url="https://sc/ukg", title="A",
                          reason="DRM-protected", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="b",
                          channel_url="https://sc/ukg", title="B",
                          reason="DRM-protected", now=1000)
    db.record_unavailable(platform="SoundCloud", video_id="c",
                          channel_url="https://sc/other", title="C",
                          reason="Removed", now=1000)


def test_count_unavailable_for_channel(tmp_path):
    db = _new_db(tmp_path)
    _seed_two_channels(db)
    assert db.count_unavailable_for_channel("https://sc/ukg") == 2
    assert db.count_unavailable_for_channel("https://sc/other") == 1
    assert db.count_unavailable_for_channel("https://sc/nope") == 0


def test_count_unavailable_counts_all_rows_not_just_suppressed(tmp_path):
    # A single Removed failure is recorded but not yet suppressed; the button
    # count reports what is remembered, so forgetting clears everything.
    db = _new_db(tmp_path)
    db.record_unavailable(platform="YouTube", video_id="one",
                          channel_url="https://yt/c", title="O",
                          reason="Removed", now=1000)
    assert db.get_suppressed_reasons("YouTube", now=1000) == {}
    assert db.count_unavailable_for_channel("https://yt/c") == 1


def test_forget_unavailable_for_channel_is_scoped(tmp_path):
    db = _new_db(tmp_path)
    _seed_two_channels(db)
    assert db.forget_unavailable_for_channel("https://sc/ukg") == 2
    assert db.count_unavailable_for_channel("https://sc/ukg") == 0
    assert db.count_unavailable_for_channel("https://sc/other") == 1


def test_forget_unavailable_on_empty_url_is_a_noop(tmp_path):
    db = _new_db(tmp_path)
    _seed_two_channels(db)
    assert db.forget_unavailable_for_channel("") == 0
    assert db.count_unavailable_for_channel("https://sc/ukg") == 2


def test_count_unavailable_on_empty_url_is_zero(tmp_path):
    db = _new_db(tmp_path)
    _seed_two_channels(db)
    assert db.count_unavailable_for_channel("") == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_unavailable.py -q`
Expected: FAIL — `AttributeError: … has no attribute 'count_unavailable_for_channel'`.

- [ ] **Step 3: Add both methods**

In `cratebuilder/db.py`, immediately after `get_suppressed_reasons`, add:

```python
    def count_unavailable_for_channel(self, channel_url):
        """How many permanent failures are remembered for *channel_url*.

        Counts every remembered row, not just the currently-suppressed ones,
        because this is the number behind the Watch List "Forget unavailable
        tracks" button and forgetting clears the lot. Returns 0 on failure or
        an empty url."""
        if not channel_url:
            return 0
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM unavailable_tracks "
                    "WHERE channel_url = ?", (channel_url,)).fetchone()
                return int(row["n"]) if row else 0
        except Exception as e:
            self._log("error", f"count_unavailable_for_channel failed: {e}")
            return 0

    def forget_unavailable_for_channel(self, channel_url):
        """Drop the unavailable-track memory for one channel and return the
        number of rows removed. Scoped strictly to *channel_url* — no files
        and no download history are touched. Returns 0 on failure or an empty
        url."""
        if not channel_url:
            return 0
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM unavailable_tracks WHERE channel_url = ?",
                    (channel_url,))
                return cur.rowcount or 0
        except Exception as e:
            self._log("error", f"forget_unavailable_for_channel failed: {e}")
            return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_unavailable.py -q`
Expected: PASS (23 tests: 18 from Tasks 1–2 + 5 new)

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/db.py tests/test_unavailable.py
git commit -m "feat(db): add per-channel unavailable count and forget"
```

---

### Task 4: Classifier third bucket

**Files:**
- Modify: `cratebuilder/sidecar.py:110-158` (`classify_scan_entries`)
- Test: `tests/test_scan_classifier.py` (modify + append)

**Interfaces:**
- Consumes: nothing at runtime — the predicate is injected. In production it is
  `DownloadsDatabase.get_suppressed_reasons(...).get` from Task 2.
- Produces:
  - `classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec, platform, is_unavailable=None) -> {"new": [...], "on_disk": [...], "unavailable": [...]}`
  - `"unavailable"` items are `{"id": str, "title": str, "reason": str}`

- [ ] **Step 1: Fix the now-stale exact-dict assertion**

`tests/test_scan_classifier.py::test_empty_entries` asserts the return value is
exactly `{"new": [], "on_disk": []}`, which the third bucket breaks. Replace that
test body with:

```python
def test_empty_entries():
    out = sidecar.classify_scan_entries(
        [], is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube")
    assert out == {"new": [], "on_disk": [], "unavailable": []}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_scan_classifier.py`:

```python
def test_suppressed_entry_goes_to_unavailable_not_new():
    out = sidecar.classify_scan_entries(
        [{"id": "drm1", "title": "Locked Track"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud",
        is_unavailable=lambda vid: "DRM-protected" if vid == "drm1" else None)
    assert out["new"] == []
    assert out["unavailable"] == [{"id": "drm1", "title": "Locked Track",
                                   "reason": "DRM-protected"}]


def test_unsuppressed_entry_still_passes_through():
    out = sidecar.classify_scan_entries(
        [{"id": "ok1", "title": "Fine"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="SoundCloud", is_unavailable=lambda _vid: None)
    assert [e["id"] for e in out["new"]] == ["ok1"]
    assert out["unavailable"] == []


def test_downloaded_takes_precedence_over_suppressed():
    # Once a track has downloaded it is a download, full stop — it must not
    # reappear in the unavailable bucket.
    out = sidecar.classify_scan_entries(
        [{"id": "v1", "title": "Owned"}],
        is_downloaded=lambda vid: vid == "v1", folder_keys={}, limit_sec=None,
        platform="YouTube", is_unavailable=lambda _vid: "Removed")
    assert out["new"] == []
    assert out["on_disk"] == []
    assert out["unavailable"] == []


def test_suppression_is_skipped_for_entries_without_an_id():
    # No id means nothing could have been recorded against it.
    out = sidecar.classify_scan_entries(
        [{"title": "No ID"}],
        is_downloaded=_never_downloaded, folder_keys={}, limit_sec=None,
        platform="YouTube", is_unavailable=lambda _vid: "Removed")
    assert [e["title"] for e in out["new"]] == ["No ID"]
    assert out["unavailable"] == []


def test_suppression_beats_the_duration_filter_and_folder_match():
    # A suppressed track is reported as unavailable regardless of how it
    # would otherwise have been bucketed.
    key = sidecar.normalize_track_key("On Disk")
    out = sidecar.classify_scan_entries(
        [{"id": "a", "title": "Too Long", "duration": 99999},
         {"id": "b", "title": "On Disk"}],
        is_downloaded=_never_downloaded,
        folder_keys={key: r"C:\Music\On Disk.mp3"}, limit_sec=60,
        platform="YouTube", is_unavailable=lambda _vid: "DRM-protected")
    assert out["new"] == []
    assert out["on_disk"] == []
    assert {e["id"] for e in out["unavailable"]} == {"a", "b"}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_scan_classifier.py -q`
Expected: FAIL — `TypeError: classify_scan_entries() got an unexpected keyword argument 'is_unavailable'`, plus a `KeyError: 'unavailable'` on `test_empty_entries`.

- [ ] **Step 4: Update `classify_scan_entries`**

In `cratebuilder/sidecar.py`, change the signature to:

```python
def classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec,
                          platform, is_unavailable=None):
```

Append this paragraph to the existing docstring, immediately before the
`Returns {"new": …}` paragraph:

```
    *is_unavailable(video_id)* is the permanently-unavailable memory: it returns
    a reason string ("DRM-protected", "Removed", "Geo-blocked") for a track that
    can never be downloaded, or a falsy value otherwise. None (the default)
    means "never suppressed". It is consulted after the DB check — a track that
    has since downloaded is a download — and before every other rule, so a
    suppressed track is reported as unavailable no matter how it would
    otherwise have been bucketed.
```

Update the `Returns` paragraph to mention the third bucket:

```
    Returns {"new": [...], "on_disk": [...], "unavailable": [...]} where each new
    item is {id, title, url, upload_date}, each on_disk item is
    {id, title, upload_date, file_path}, and each unavailable item is
    {id, title, reason}. The id is "" when the entry has none."""
```

Then change the body: add `unavailable = []` beside the other two accumulators,
insert the suppression check right after the `is_downloaded` check, and add the
bucket to the return value.

```python
    new_entries = []
    on_disk = []
    unavailable = []
    for e in entries:
        vid_id = e.get("id")
        if vid_id and is_downloaded(vid_id):
            continue
        if vid_id and is_unavailable is not None:
            reason = is_unavailable(vid_id)
            if reason:
                unavailable.append({
                    "id":     vid_id,
                    "title":  e.get("title") or "",
                    "reason": reason,
                })
                continue
        if limit_sec is not None:
```

…leaving the rest of the loop untouched, and finally:

```python
    return {"new": new_entries, "on_disk": on_disk,
            "unavailable": unavailable}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_scan_classifier.py -q`
Expected: PASS (16 tests: the 11 that were already there + 5 new)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS. The monolith's `_watchlist_scan_channel` reads only `["new"]` and
`["on_disk"]`, so the extra key is inert until Task 6.

- [ ] **Step 7: Commit**

```bash
git add cratebuilder/sidecar.py tests/test_scan_classifier.py
git commit -m "feat(sidecar): add unavailable bucket to classify_scan_entries"
```

---

### Task 5: Record permanent failures during downloads

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:9822-9825` (inside `_process_one_url`)

**Interfaces:**
- Consumes: `DownloadsDatabase.record_unavailable` from Task 1
- Produces: rows in `unavailable_tracks` for every permanent failure, from both
  Main-tab batches and Watch List "Download New" runs (they share this code path)

- [ ] **Step 1: Add the write**

In `DJ-CrateBuilder_v1.3.py`, find this block in `_process_one_url` (immediately
after the `_perm = classify_permanent_failure(clean)` cascade, around line 9822):

```python
                    if _perm:
                        unavail += 1
                    else:
                        errors += 1
```

Replace it with:

```python
                    if _perm:
                        unavail += 1
                        # Remember it so the Watch List stops reporting a track
                        # that can never be downloaded as "new" on every scan.
                        self._db.record_unavailable(
                            platform=platform,
                            video_id=entry.get("id") or "",
                            channel_url=url if is_collection else "",
                            title=item_title,
                            reason=_perm)
                    else:
                        errors += 1
```

All four values are already in scope: `platform` and `url` are `_process_one_url`
parameters, `is_collection` is set at line 9175, and `entry` / `item_title` come
from the per-entry loop at lines 9257 / 9274.

- [ ] **Step 2: Verify the file still compiles**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: no output (success)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(watchlist): record permanently-failed tracks during downloads"
```

---

### Task 6: Suppress remembered tracks during a scan

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:11948-11997` (inside `_watchlist_scan_channel._do_scan`)

**Interfaces:**
- Consumes: `DownloadsDatabase.get_suppressed_reasons` (Task 2) and the
  `is_unavailable` parameter / `"unavailable"` bucket (Task 4)
- Produces: honest `pending_new_count`, plus a scan-log line naming the skips

- [ ] **Step 1: Pass the suppression map into the classifier**

Find the `classify_scan_entries(...)` call and the two lines above it:

```python
                limit_on  = bool(self._limit_enabled.get())
                limit_sec = self._limit_minutes.get() * 60 if limit_on else None
                classified = classify_scan_entries(
                    entries,
                    is_downloaded=self._db.is_video_downloaded,
                    folder_keys=folder_keys,
                    limit_sec=limit_sec,
                    platform=platform)
                new_entries = classified["new"]
```

Replace with:

```python
                limit_on  = bool(self._limit_enabled.get())
                limit_sec = self._limit_minutes.get() * 60 if limit_on else None
                # One query per scan, not one per entry: every track this
                # platform has already proven undownloadable (DRM, removed,
                # geo-blocked) so it is not offered as "new" again.
                suppressed = self._db.get_suppressed_reasons(platform)
                classified = classify_scan_entries(
                    entries,
                    is_downloaded=self._db.is_video_downloaded,
                    folder_keys=folder_keys,
                    limit_sec=limit_sec,
                    platform=platform,
                    is_unavailable=suppressed.get)
                new_entries = classified["new"]
                n_unavail = len(classified.get("unavailable") or [])
```

- [ ] **Step 2: Report the skips in the scan log and debug log**

Find the block that logs the scan result:

```python
                tag = "ok" if count > 0 else "info"
                self.after(0, lambda: self._watchlist_log(
                    f"{ch['display_name']}: {count} new track{'s' if count != 1 else ''} found",
                    tag))
```

Replace with:

```python
                tag = "ok" if count > 0 else "info"
                scan_msg = (f"{ch['display_name']}: {count} new "
                            f"track{'s' if count != 1 else ''} found")
                if n_unavail:
                    scan_msg += (f" ({n_unavail} permanently unavailable, "
                                 f"skipped)")
                    self._dbg.info(
                        f"WL SCAN SUPPRESSED | {ch['display_name']}  "
                        f"{n_unavail} track(s) hidden by the "
                        f"unavailable-track memory")
                self.after(0, lambda m=scan_msg: self._watchlist_log(m, tag))
```

Note the `m=scan_msg` default-argument binding — the surrounding code uses
late-binding closures elsewhere, but this one must capture the value.

- [ ] **Step 3: Verify the file still compiles**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: no output (success)

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(watchlist): exclude permanently-unavailable tracks from scans"
```

---

### Task 7: "Forget unavailable tracks" button

> **AMENDED DURING EXECUTION.** As originally written this task counted and
> cleared the memory with `ch["url"]`, but Task 5 records rows under the URL
> `_process_one_url` actually received — which for a Watch List download is
> `watch_fetch_url(platform, ch["url"])`, i.e. the base URL plus `/tracks`
> (SoundCloud) or `/videos` (YouTube), percent-encoded. The DB methods match
> `channel_url` exactly, so the button would have read `(0)` forever and cleared
> nothing. The task was amended to add a pure `canonical_channel_url(url)` helper
> in `cratebuilder/sidecar.py` — it strips the listing-tab suffix and
> percent-decodes — applied at both the write site and the button, so the two
> sides converge on one key. The as-built brief is
> `.superpowers/sdd/2026-07-25-unavailable-tracks/task-7-brief.md`; the steps
> below describe the original, unfixed shape and are kept only for the record.

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:11471-11480` (dialog height) and `:11651` (new row before the verify-state notice, inside `_watchlist_edit_channel`)
- (amended) Modify: `cratebuilder/sidecar.py` + `tests/test_sidecar.py` (the `canonical_channel_url` helper), and the Task 5 write site's `channel_url=` argument

**Interfaces:**
- Consumes: `count_unavailable_for_channel` / `forget_unavailable_for_channel` from Task 3
- Produces: (amended) `cratebuilder.sidecar.canonical_channel_url(url) -> str`

- [ ] **Step 1: Grow the dialog to fit the new row**

The Edit dialog is a fixed 460x540 and is already full. Change both the geometry
call and the centring maths (they must stay in sync or the dialog is off-centre).

```python
        dlg.geometry("460x540")
```
becomes
```python
        dlg.geometry("460x596")
```

and
```python
        py = self.winfo_y() + (self.winfo_height() - 540) // 2
```
becomes
```python
        py = self.winfo_y() + (self.winfo_height() - 596) // 2
```

- [ ] **Step 2: Add the button row**

Insert immediately **before** the `if verify_state == "missing":` block (after
`add_hover(remove_btn)` at the end of the genre row):

```python
        # ── Unavailable-track memory ──────────────────────────────────────
        # Tracks that failed permanently (DRM, removed, geo-blocked) stop being
        # reported as new. This is the escape hatch: forgetting them makes the
        # next scan offer them again and the next download re-attempt them.
        unavail_row = tk.Frame(outer, bg=BG)
        unavail_row.pack(fill="x", pady=(8, 0))
        _ch_url = ch.get("url") or ""
        _unavail_n = self._db.count_unavailable_for_channel(_ch_url)

        def _forget_unavailable():
            n = self._db.count_unavailable_for_channel(_ch_url)
            if not n:
                return
            if not messagebox.askyesno(
                    "Forget Unavailable Tracks",
                    f"Forget {n} track(s) recorded as permanently unavailable "
                    f"for '{ch['display_name']}'?\n\n"
                    f"They will count as new again on the next scan and be "
                    f"re-attempted on the next download.",
                    parent=dlg):
                return
            removed = self._db.forget_unavailable_for_channel(_ch_url)
            forget_btn.config(text="  Forget unavailable tracks (0)  ",
                              state="disabled")
            self._watchlist_log(
                f"{ch['display_name']}: forgot {removed} unavailable track(s)",
                "info")

        forget_btn = tk.Button(
            unavail_row,
            text=f"  Forget unavailable tracks ({_unavail_n})  ",
            font=("Segoe UI", 9), bg=SURFACE2, fg=TEXT_DIM,
            activebackground=BORDER, activeforeground=TEXT,
            relief="flat", bd=0, padx=10, pady=4,
            cursor=("hand2" if _unavail_n else "arrow"),
            state=("normal" if _unavail_n else "disabled"),
            command=_forget_unavailable)
        forget_btn.pack(side="left")
        if _unavail_n:
            add_hover(forget_btn)
```

`add_hover` is applied only when the button is live — a disabled `tk.Button`
should not repaint on mouse-over.

- [ ] **Step 3: Verify the file still compiles**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py`
Expected: no output (success)

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Visually verify the dialog**

Run: `python DJ-CrateBuilder_v1.3.py`
Open the Watch List, click **Edit** on any channel, and confirm:
- the dialog is not clipped — Save and Cancel are still fully visible at the bottom;
- the new button reads `Forget unavailable tracks (0)` and is greyed/unclickable
  on a channel with no recorded failures.

Per CLAUDE.md, a tkinter change must be seen before it is called done. If it
cannot be verified visually, say so explicitly rather than asserting it works.

- [ ] **Step 6: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(watchlist): add Forget unavailable tracks to the Edit dialog"
```

---

### Task 8: End-to-end verification on the real channel

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1–7
- Produces: evidence that the 47 UKG 2025 tracks stop being reported

- [ ] **Step 1: Run the full suite one more time**

Run: `python -m pytest -q`
Expected: PASS. Report the exact count.

- [ ] **Step 2: Confirm the live schema migrated**

The app opens the user's real `cratebuilder.db` on launch and runs `_init_schema`.
Launch the app once, then check the table exists without touching any user data:

Write this to `check_schema.py` in the scratchpad (not the repo) and run it — a
one-liner with nested quotes is a portability trap on Windows shells:

```python
import sqlite3
conn = sqlite3.connect("cratebuilder.db")
print(conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='unavailable_tracks'").fetchall())
print(conn.execute(
    "SELECT value FROM schema_info WHERE key='version'").fetchall())
conn.close()
```

Expected: `[('unavailable_tracks',)]` then `[('5',)]`. This is read-only — never
write to the live database by hand.

- [ ] **Step 3: Baseline the bug**

In the Watch List, scan **UKG 2025** and record the reported new count (expected:
47, the pre-fix behaviour — nothing has been recorded yet).

- [ ] **Step 4: Teach the app**

Click **Download New** on that card and let it finish. The run summary should
report `∅ 47 unavailable (DRM/removed)`.

- [ ] **Step 5: Confirm the fix**

Scan **UKG 2025** again. Expected: the card shows **0 new**, and the Watch List
scan log reads:

```
UKG 2025: 0 new tracks found (47 permanently unavailable, skipped)
```

- [ ] **Step 6: Confirm the escape hatch**

Open **Edit** on that card. The button should read `Forget unavailable tracks (47)`
and be enabled. Click it, confirm the prompt, then re-scan — the card should
report 47 new again, proving the memory is exactly reversible.

- [ ] **Step 7: Confirm rebuild-resistance**

With the memory repopulated (re-run steps 4–5 if you cleared it), use Settings →
**Rebuild Database from Files**, then scan UKG 2025 again. Expected: still 0 new
— the rebuild clears `downloads` but must not touch `unavailable_tracks`. This is
the requirement that ruled out storing the memory as sentinel download rows.

- [ ] **Step 8: Report**

State plainly which steps passed, with the observed numbers. Do not claim the
feature works on any step that was not actually run.

---

## Notes for the implementer

- **Do not run `scripts/release.py`.** Shipping is a separate, explicitly-requested step via the `/build-update` skill.
- `cratebuilder.db`, `activity.log` and `debug.log` are gitignored runtime user data — never commit them, and never hand-edit the live database.
- `tests/test_db.py::test_schema_version_is_4` is the only test pinning the old version; Task 1 Step 2 retargets it. Nothing else in the suite references `SCHEMA_VERSION`.
- Tasks 5–7 touch the monolith and have no unit tests — that is deliberate, not an omission. They are terminal UI/threading wiring; all the logic they call is already covered by Tasks 1–4, and Task 8 verifies them by hand against the real channel.

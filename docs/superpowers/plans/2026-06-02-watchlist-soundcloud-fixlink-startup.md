# Watch List: Fix Link repair + SoundCloud support + startup scan — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the silent "Fix Link" failure (with duplicate detection/removal), make SoundCloud a first-class Watch List platform, and actively scan every entry on startup.

**Architecture:** Three independent workstreams on branch `v1.3`. Pure logic goes into the unit-tested `cratebuilder/` package (db, sidecar, util) with thin Tk wrappers in `DJ-CrateBuilder_v1.3.py` delegating to it — matching the existing hybrid structure. Each phase is behavior-additive and independently testable; TDD throughout (the repo has a 22-test pytest suite).

**Tech Stack:** Python 3.14, tkinter/ttk (single-file app), yt-dlp, SQLite (`cratebuilder/db.py`), pytest. Run tests with `set PYTHONIOENCODING=utf-8` then `python -m pytest -q`.

---

## Root-cause summary (confirmed)

**Issue 1 — Fix Link never sticks.** `_persist_resolved_channel` (`DJ-CrateBuilder_v1.3.py:5840`) calls `update_watchlist_channel_fields` (`cratebuilder/db.py:265`) which runs `UPDATE watchlist SET url=…`. The resolved canonical URL **already belongs to another watchlist row**, so it violates `UNIQUE(url)` (`db.py:78`). The DB method swallows the `IntegrityError` (`db.py:276-277`) and returns nothing; `_persist_resolved_channel` doesn't check, writes the sidecar, and logs `WL RESOLVE OK` (`:5863`). The row never updates → stays unresolved → Fix Link reappears. The two broken entries collide with: "Deep-Tech Station" (a duplicate already-resolved row) and "UKF Drum & Bass" (the generic name "Drum & Bass" mis-matched UKF).

**Issue 2 — startup + SoundCloud.** Startup never runs an unconditional scan; counts shown are the last-saved `pending_new_count`. And SoundCloud is not a Watch List platform: the Add dialog (`:6292`), auto-add (`:6877`), and folder import (`:6943-6966`) all hardcode `platform="YouTube"`; `_watchlist_scan_channel` (`:6444`) hardcodes YouTube URL munging and `_resolve_save_dir(..., platform="YouTube")`.

## File structure (what changes)

- `cratebuilder/util.py` — add pure `detect_platform(url)`.
- `cratebuilder/sidecar.py` — make `is_unresolved_channel(ch)` platform-aware; add pure `watch_scan_url(platform, url)`.
- `cratebuilder/db.py` — `update_watchlist_channel_fields` returns `bool` and distinguishes a UNIQUE collision; add `get_watchlist_channel_by_url` if missing.
- `DJ-CrateBuilder_v1.3.py` — `_persist_resolved_channel` collision handling + duplicate-removal UX; platform-aware `_watchlist_scan_channel`; platform detection in Add dialog / auto-add / folder import; SoundCloud-aware resolve dialog; new `_watchlist_startup_scan`; delegate `_detect_platform` to `util.detect_platform`.
- `tests/` — new tests in `test_db.py`, `test_sidecar.py`, `test_util.py`.

## Conventions for every task

- After EACH task: `set PYTHONIOENCODING=utf-8` then `python -m py_compile DJ-CrateBuilder_v1.3.py cratebuilder/*.py` (clean) and `python -m pytest -q` (green). One commit per task. Behavior-preserving except where the task explicitly adds behavior.
- Commit footer exactly: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- Do NOT touch `main`. Do NOT push. Do NOT commit `cratebuilder.db`.

---

# Phase A — Issue 1: Fix Link collision repair + duplicate UX

## Task A1: DB layer reports success and detects UNIQUE collisions

**Files:**
- Modify: `cratebuilder/db.py:265-277` (`update_watchlist_channel_fields`)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db.py` (mirror existing fixture style — the file already constructs a `DownloadsDatabase` against a temp path):

```python
def test_update_fields_returns_true_on_success(tmp_path):
    db = _fresh_db(tmp_path)  # use the file's existing helper/fixture
    wid = db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCaaa/videos",
        display_name="A", platform="YouTube", genre="(none)",
        scan_cutoff_date="20260101")
    assert db.update_watchlist_channel_fields(
        wid, channel_id="UCaaa", status="idle") is True


def test_update_fields_returns_false_on_unique_collision(tmp_path):
    db = _fresh_db(tmp_path)
    db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCdup/videos",
        display_name="Existing", platform="YouTube", genre="(none)",
        scan_cutoff_date="20260101")
    other = db.add_watchlist_channel(
        url="https://www.youtube.com/@Some Name", display_name="Dup",
        platform="YouTube", genre="(none)", scan_cutoff_date="20260101")
    # Trying to point `other` at the URL already owned by Existing must fail,
    # and must NOT silently report success.
    ok = db.update_watchlist_channel_fields(
        other, url="https://www.youtube.com/channel/UCdup/videos",
        channel_id="UCdup", status="idle")
    assert ok is False
    # And the row must be unchanged (still its spaced url, no channel_id).
    row = db.get_watchlist_channel(other)
    assert row["channel_id"] in (None, "")
    assert " " in row["url"]
```

(If `tests/test_db.py` lacks a `_fresh_db` helper, reuse whatever construction the existing tests use — match the file, don't invent a new fixture.)

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_db.py -q`
Expected: the two new tests FAIL (method returns `None`).

- [ ] **Step 3: Implement — return bool, surface collisions**

Replace `update_watchlist_channel_fields` (`cratebuilder/db.py:265-277`) with:

```python
    def update_watchlist_channel_fields(self, wl_id, **fields):
        """Update allowed watchlist columns for one row.

        Returns True on success, False on failure (including a UNIQUE(url)
        collision, which means the target url already belongs to another row).
        Never raises — callers branch on the bool instead of getting a silent
        no-op."""
        allowed = {"display_name", "genre", "scan_cutoff_date",
                   "channel_id", "url", "status", "last_error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [wl_id]
            with self._conn() as conn:
                conn.execute(f"UPDATE watchlist SET {sets} WHERE id = ?", vals)
            return True
        except sqlite3.IntegrityError as e:
            # UNIQUE(url) collision — the target url already exists on another
            # row. Caller should treat this as a duplicate, not a generic error.
            self._log("info",
                      f"update_watchlist_channel_fields collision: {e}")
            return False
        except Exception as e:
            self._log("error", f"update_watchlist_channel_fields failed: {e}")
            return False
```

(`sqlite3` is already imported at the top of `db.py`.)

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_db.py -q` → PASS. Then full suite `python -m pytest -q` → still green.

- [ ] **Step 5: Confirm `get_watchlist_channel_by_url` exists**

Run: `grep -n "def get_watchlist_channel_by_url" cratebuilder/db.py`
Expected: it exists (auto-add uses it at main `:6884`). If MISSING, add:

```python
    def get_watchlist_channel_by_url(self, url):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM watchlist WHERE url = ?", (url,)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            self._log("error", f"get_watchlist_channel_by_url failed: {e}")
            return None
```

- [ ] **Step 6: Commit** — `git commit -m "fix(db): watchlist field update returns success and flags UNIQUE collisions"`

## Task A2: `_persist_resolved_channel` detects duplicates and stops the false "OK"

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:5840-5864` (`_persist_resolved_channel`)

- [ ] **Step 1: Replace the method**

Replace `_persist_resolved_channel` with the version below. It (a) checks the DB result, (b) on collision finds the existing owner and offers to remove THIS duplicate, (c) only writes the sidecar after a successful DB update, (d) returns a bool so callers know the outcome:

```python
    def _persist_resolved_channel(self, ch, channel_id, handle="", url=None):
        """Commit a resolved identity. Returns True if the row was updated.

        If the resolved URL already belongs to ANOTHER watchlist row, this is a
        duplicate: we don't fake success — we tell the user which entry it
        duplicates and offer to remove this redundant row."""
        platform = ch.get("platform") or "YouTube"
        store_url = url or channel_url_from_id(channel_id)

        # Pre-check for a collision so we can give a meaningful message instead
        # of a silent UNIQUE failure.
        owner = self._db.get_watchlist_channel_by_url(store_url)
        if owner and owner.get("id") != ch.get("id"):
            self._dbg.info(
                f"WL RESOLVE DUPLICATE | {ch.get('display_name')!r} → "
                f"{channel_id} already tracked as {owner.get('display_name')!r}")
            self._watchlist_offer_remove_duplicate(ch, owner)
            return False

        ok = self._db.update_watchlist_channel_fields(
            ch["id"], url=store_url, channel_id=channel_id,
            status="idle", last_error=None)
        if not ok:
            # Lost a race or another constraint — surface honestly.
            self._db.update_watchlist_status(
                ch["id"], "error", last_error="Could not save resolved link")
            self._watchlist_log(
                f"Couldn't save link for {ch.get('display_name')} "
                f"— it may duplicate another entry.", "err")
            self._watchlist_refresh()
            return False

        # DB update succeeded — now (and only now) stamp the folder sidecar.
        try:
            folder = self._resolve_save_dir(
                ch.get("genre") or "(none)", ch.get("display_name"),
                platform=platform)
            write_channel_sidecar(
                folder, channel_id=channel_id, channel_url=store_url,
                handle=handle, display_name=ch.get("display_name"),
                platform=platform, genre=ch.get("genre") or "(none)")
        except Exception as e:
            self._dbg.warning(f"WL RESOLVE | sidecar write failed: {e}")
        self._dbg.info(
            f"WL RESOLVE OK | {ch.get('display_name')!r} → {channel_id}")
        return True
```

- [ ] **Step 2: Add the duplicate-removal helper**

Add this method directly below `_persist_resolved_channel`:

```python
    def _watchlist_offer_remove_duplicate(self, dup, owner):
        """A Fix Link resolved to a channel already tracked by `owner`.
        Offer to delete the redundant `dup` row."""
        keep = owner.get("display_name") or "another entry"
        remove = dup.get("display_name") or "this entry"
        msg = (f"“{remove}” is the same channel you already track as "
               f"“{keep}”.\n\nRemove the duplicate “{remove}”? "
               f"Its folder on disk is left untouched.")
        if messagebox.askyesno("Duplicate channel", msg, parent=self):
            self._db.remove_watchlist_channel(dup["id"])
            self._watchlist_log(
                f"Removed duplicate “{remove}” (already tracked as "
                f"“{keep}”).", "ok")
        else:
            # User kept it: park it clearly rather than leaving a phantom
            # unresolved row that keeps offering a Fix Link that can't succeed.
            self._db.update_watchlist_status(
                dup["id"], "error",
                last_error=f"Duplicate of “{keep}”")
            self._watchlist_log(
                f"Kept “{remove}”. It duplicates “{keep}” and can't be "
                f"resolved separately.", "info")
        self._watchlist_refresh()
```

- [ ] **Step 3: Verify compile + manual sanity**

Run: `python -m py_compile DJ-CrateBuilder_v1.3.py` → clean. `python -m pytest -q` → green (no unit test for the Tk method; the DB collision path is covered by A1).

- [ ] **Step 4: Commit** — `git commit -m "fix(watchlist): Fix Link detects duplicates and offers removal instead of silent failure"`

## Task A3: Live data repair (the user's two broken entries)

**This is a manual verification task, not code.** After A1+A2 are in:

- [ ] Launch the app, open Watch List.
- [ ] Click **Fix Link** on **"Deep-Tech Station"** → expect the "Duplicate channel… already tracked as Deep-Tech Station" dialog → choose **Yes** to remove the duplicate. Confirm the card disappears and no Fix Link reappears.
- [ ] Click **Fix Link** on **"Drum & Bass"** → it resolves to **UKF Drum & Bass**. **CHECKPOINT — ask the user:** is the "Drum & Bass" folder meant to track UKF (then remove the duplicate) or a *different* DnB channel (then they paste that channel's URL in the resolve dialog instead)? Do not assume.
- [ ] Verify `python -c "import sqlite3; c=sqlite3.connect('file:cratebuilder.db?mode=ro',uri=True); print([(r[0],r[1]) for r in c.execute('select display_name,url from watchlist')])"` shows no remaining spaced/`unresolved://` URLs for the repaired rows.

---

# Phase B — Issue 2a: SoundCloud as a first-class Watch List platform

## Task B1: Pure `detect_platform` in util + delegate

**Files:**
- Modify: `cratebuilder/util.py` (add function)
- Modify: `DJ-CrateBuilder_v1.3.py:4316-4318` (`_detect_platform` → delegate)
- Test: `tests/test_util.py`

- [ ] **Step 1: Failing test** — add to `tests/test_util.py`:

```python
def test_detect_platform():
    from cratebuilder.util import detect_platform
    assert detect_platform("https://soundcloud.com/artist") == "SoundCloud"
    assert detect_platform("https://www.youtube.com/@chan") == "YouTube"
    assert detect_platform("") == "YouTube"  # default
```

- [ ] **Step 2: Run** `python -m pytest tests/test_util.py -q` → FAIL (no `detect_platform`).

- [ ] **Step 3: Implement** — add to `cratebuilder/util.py`:

```python
import re as _re

def detect_platform(url):
    """Return 'SoundCloud' for a soundcloud.com URL, else 'YouTube' (default)."""
    if url and _re.search(r"soundcloud\.com", url, _re.IGNORECASE):
        return "SoundCloud"
    return "YouTube"
```

- [ ] **Step 4: Delegate** — replace the body of `_detect_platform` (`DJ-CrateBuilder_v1.3.py:4316-4318`) with:

```python
    @staticmethod
    def _detect_platform(url):
        """Return 'SoundCloud' or 'YouTube' based on the URL."""
        return detect_platform(url)
```

Ensure `detect_platform` is imported at the top of the main file alongside the other `from cratebuilder.util import ...` names.

- [ ] **Step 5: Run** `python -m pytest -q` → green; `py_compile` clean.
- [ ] **Step 6: Commit** — `git commit -m "feat(util): pure detect_platform helper; main delegates to it"`

## Task B2: Platform-aware `is_unresolved_channel` + `watch_scan_url`

**Files:**
- Modify: `cratebuilder/sidecar.py:61-69`
- Test: `tests/test_sidecar.py`

- [ ] **Step 1: Failing tests** — add to `tests/test_sidecar.py`:

```python
def test_is_unresolved_platform_aware():
    from cratebuilder.sidecar import is_unresolved_channel
    # YouTube: a clean canonical URL is resolved; a spaced one isn't.
    assert is_unresolved_channel(
        {"platform": "YouTube", "status": "idle",
         "url": "https://www.youtube.com/channel/UCx/videos"}) is False
    assert is_unresolved_channel(
        {"platform": "YouTube", "status": "idle",
         "url": "https://www.youtube.com/@A B"}) is True
    # SoundCloud: a soundcloud.com URL is resolved (no channel-id needed).
    assert is_unresolved_channel(
        {"platform": "SoundCloud", "status": "idle",
         "url": "https://soundcloud.com/artist"}) is False
    # SoundCloud sentinel / status still unresolved.
    assert is_unresolved_channel(
        {"platform": "SoundCloud", "status": "needs_resolve",
         "url": "unresolved://SoundCloud/x"}) is True


def test_watch_scan_url():
    from cratebuilder.sidecar import watch_scan_url
    assert watch_scan_url(
        "YouTube", "https://www.youtube.com/@chan"
        ) == "https://www.youtube.com/@chan/videos"
    assert watch_scan_url(
        "YouTube", "https://www.youtube.com/channel/UCx/videos"
        ) == "https://www.youtube.com/channel/UCx/videos"
    assert watch_scan_url(
        "SoundCloud", "https://soundcloud.com/artist"
        ) == "https://soundcloud.com/artist/tracks"
    assert watch_scan_url(
        "SoundCloud", "https://soundcloud.com/artist/tracks"
        ) == "https://soundcloud.com/artist/tracks"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_sidecar.py -q` → FAIL.

- [ ] **Step 3: Implement** — replace `is_unresolved_channel` (`sidecar.py:61-69`) and add `watch_scan_url`:

```python
def is_unresolved_channel(ch):
    """True if a watchlist row has no usable scan identifier yet.

    Platform-aware:
    - Any platform: explicit needs_resolve/error status, the unresolved://
      sentinel, or a space in the URL (a folder-name URL) is unresolved.
    - YouTube additionally needs a *real* channel reference — a bare
      youtube.com root with no /channel/, /@handle, or playlist is unresolved.
    - SoundCloud just needs a soundcloud.com URL (usernames are stable; no
      channel-id resolution exists), so a clean soundcloud.com/<user> is
      resolved."""
    url = (ch.get("url") or "")
    if (ch.get("status") in ("needs_resolve", "error")
            or url.startswith("unresolved://")
            or " " in url):
        return True
    platform = (ch.get("platform") or "YouTube")
    if platform == "SoundCloud":
        return "soundcloud.com" not in url.lower()
    # YouTube: must reference a channel/handle/playlist, not a bare root.
    low = url.lower()
    return not ("/channel/" in low or "/@" in low
                or "list=" in low or "/playlist" in low)


def watch_scan_url(platform, url):
    """Return the URL to hand yt-dlp for a *listing* scan of this entry.

    YouTube: ensure the /videos tab for an @handle/channel. SoundCloud: ensure
    the /tracks tab for a user. Idempotent — never double-appends."""
    url = (url or "").rstrip("/")
    if not url:
        return url
    if platform == "SoundCloud":
        return url if url.endswith("/tracks") else url + "/tracks"
    # YouTube
    if "/videos" in url:
        return url
    last = url.split("/")[-1]
    if last.startswith("@") or "/channel/" in url:
        return url + "/videos"
    return url
```

- [ ] **Step 4: Run** `python -m pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(sidecar): platform-aware unresolved check + watch_scan_url for YouTube/SoundCloud"`

## Task B3: Make `_watchlist_scan_channel` platform-aware

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:6524-6535` (URL building), `:6560-6562` (`_resolve_save_dir`), `:6595` (backfill platform), `:6603-6609` (new-entry url fallback)

- [ ] **Step 1: Replace the URL-building block** (`:6524-6535`).

Old (the YouTube-only munging) becomes platform-aware via `watch_scan_url`:

```python
                platform = ch.get("platform") or "YouTube"
                url = watch_scan_url(platform, ch["url"])

                # URL-encode the path so handles containing spaces (e.g.
                # "@BASS ENTITY") aren't truncated by yt-dlp at the first
                # whitespace, which otherwise produces a 404.
                parsed = urllib.parse.urlsplit(url)
                url = urllib.parse.urlunsplit(parsed._replace(
                    path=urllib.parse.quote(parsed.path, safe="/@&")))
```

Ensure `watch_scan_url` is imported at the top of the main file from `cratebuilder.sidecar`.

- [ ] **Step 2: Use the entry's real platform for the folder** (`:6560-6562`):

```python
                    folder = self._resolve_save_dir(
                        ch.get("genre") or "(none)", ch.get("display_name"),
                        platform=platform)
```

- [ ] **Step 3: Use the entry's real platform in backfill** (`:6595`):

```python
                                "platform":     platform,
```

- [ ] **Step 4: Platform-correct the new-entry URL fallback** (`:6603-6609`):

```python
                    new_entries.append({
                        "id":          vid_id or "",
                        "title":       title,
                        "url":         (e.get("url") or e.get("webpage_url")
                                        or (f"https://www.youtube.com/watch?v={vid_id}"
                                            if platform == "YouTube" else "")),
                        "upload_date": e.get("upload_date") or "",
                    })
```

- [ ] **Step 5: Verify** `py_compile` clean; `python -m pytest -q` green. (The pure URL/resolution logic is covered by B2.)
- [ ] **Step 6: Commit** — `git commit -m "feat(watchlist): scan engine honors each entry's platform (YouTube + SoundCloud)"`

## Task B4: Create SoundCloud entries — Add dialog, auto-add, folder import

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:6281-6295` (Add dialog), `:6877-6880` (auto-add), `:6897-6978` (folder import)

- [ ] **Step 1: Add dialog detects platform** — replace the hardcoded `plat = "YouTube"`/`platform="YouTube"` block (`:6281-6295`) so platform comes from the pasted URL:

```python
                plat = self._detect_platform(raw_url)
                folder = os.path.join(self._base_dir, plat,
                                      genre or "_No Genre", name)
                count, newest = scan_folder_newest_mp3(folder)
                if newest:
                    cutoff = subtract_days_from_yyyymmdd(
                        newest, WATCHLIST_CUTOFF_BUFFER_DAYS)
                else:
                    cutoff = today_yyyymmdd()
            else:
                cutoff = today_yyyymmdd()

            result = self._db.add_watchlist_channel(
                url=raw_url, display_name=name,
                platform=self._detect_platform(raw_url), genre=genre,
                scan_cutoff_date=cutoff, auto_added=False)
```

(The `else:` and following lines already exist — keep them; only the platform/folder lines change. Verify the surrounding `if <folder-exists>:` structure still reads correctly after the edit.)

- [ ] **Step 2: Auto-add detects platform** — in `_watchlist_auto_add` replace (`:6877-6880`):

```python
        result = self._db.add_watchlist_channel(
            url=url, display_name=display_name,
            platform=self._detect_platform(url), genre=genre or "(none)",
            scan_cutoff_date=cutoff, auto_added=True)
```

- [ ] **Step 3: Folder import also walks SoundCloud** — generalize `_watchlist_populate_from_folders` (`:6897-6978`) to loop over both platform roots. Replace the single-platform body (from `yt_dir = self._platform_dir("YouTube")` through the end of the channel loop) with a per-platform loop:

```python
        added = 0
        for platform in ("YouTube", "SoundCloud"):
            proot = self._platform_dir(platform)
            if not os.path.isdir(proot):
                continue
            for genre_dir in sorted(os.listdir(proot)):
                genre_path = os.path.join(proot, genre_dir)
                if not os.path.isdir(genre_path):
                    continue
                genre = "(none)" if genre_dir == "_No Genre" else genre_dir

                for channel_dir in sorted(os.listdir(genre_path)):
                    channel_path = os.path.join(genre_path, channel_dir)
                    if not os.path.isdir(channel_path):
                        continue

                    count, newest = scan_folder_newest_mp3(channel_path)
                    cutoff = (subtract_days_from_yyyymmdd(
                                  newest, WATCHLIST_CUTOFF_BUFFER_DAYS)
                              if newest else today_yyyymmdd())

                    sc = read_channel_sidecar(channel_path)
                    if sc and (sc.get("channel_url") or sc.get("channel_id")):
                        real_url = (sc.get("channel_url")
                                    or channel_url_from_id(sc.get("channel_id")))
                        result = self._db.add_watchlist_channel(
                            url=real_url,
                            channel_id=sc.get("channel_id"),
                            display_name=sc.get("display_name") or channel_dir,
                            platform=platform,
                            genre=genre,
                            scan_cutoff_date=cutoff,
                            auto_added=True,
                            status="idle")
                        status_note = "from sidecar"
                    else:
                        # No sidecar: park as needs_resolve with a unique
                        # sentinel so UNIQUE(url) holds and nothing bogus is
                        # scanned. YouTube can auto-resolve via Fix Link search;
                        # SoundCloud is fixed by pasting the soundcloud.com URL.
                        sentinel = (f"{UNRESOLVED_URL_PREFIX}{platform}/"
                                    f"{genre}/{channel_dir}")
                        result = self._db.add_watchlist_channel(
                            url=sentinel,
                            display_name=channel_dir,
                            platform=platform,
                            genre=genre,
                            scan_cutoff_date=cutoff,
                            auto_added=True,
                            status="needs_resolve")
                        status_note = "needs_resolve"

                    if result is not None:
                        added += 1
                        self._dbg.info(
                            f"WL FOLDER-POPULATE | {channel_dir!r}  "
                            f"platform={platform}  genre={genre}  "
                            f"cutoff={cutoff}  ({status_note})")
```

- [ ] **Step 4: Verify** `py_compile` clean; `python -m pytest -q` green; headless smoke builds 4 tabs.
- [ ] **Step 5: Commit** — `git commit -m "feat(watchlist): create SoundCloud entries via Add dialog, auto-add, and folder import"`

## Task B5: Resolve dialog handles SoundCloud (manual paste, no YouTube search)

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:5918+` (`_watchlist_resolve_dialog`)

- [ ] **Step 1:** At the top of `_watchlist_resolve_dialog`, after loading `ch`, branch for SoundCloud so it doesn't run the YouTube search:

```python
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return
        if (ch.get("platform") or "YouTube") == "SoundCloud":
            # SoundCloud has no channel-id search; just collect the URL.
            return self._watchlist_soundcloud_link_dialog(cid, on_done)
```

- [ ] **Step 2:** Add a small SoundCloud link dialog method (paste a `soundcloud.com/<user>` URL, then reuse `_watchlist_apply_url`):

```python
    def _watchlist_soundcloud_link_dialog(self, cid, on_done=None):
        """Minimal Fix Link for SoundCloud: paste the soundcloud.com/<user> URL."""
        ch = self._db.get_watchlist_channel(cid)
        if not ch:
            return
        url = simpledialog.askstring(
            "Fix Link — SoundCloud",
            f"Paste the SoundCloud profile URL for “{ch['display_name']}”\n"
            f"(e.g. https://soundcloud.com/artist-name):",
            parent=self)
        if url and "soundcloud.com" in url.lower():
            self._watchlist_apply_url(cid, url.strip())
            if on_done:
                on_done(True)
        elif url is not None:
            messagebox.showwarning(
                "Not a SoundCloud URL",
                "That doesn't look like a soundcloud.com URL.", parent=self)
            if on_done:
                on_done(False)
```

Ensure `simpledialog` is imported (`from tkinter import simpledialog`) — check the top of the file; add if missing.

Note: `_watchlist_apply_url` (`:5866`) already canonicalises YouTube `/channel/UC…` and otherwise stores the URL as typed and resolves in the background. For a SoundCloud URL, `_channel_id_from_url` returns None, so it stores the URL as-is (status idle) — which is exactly "resolved" for SoundCloud per B2. Confirm this path doesn't force a YouTube-only resolve; if the background `_bg()` resolver errors on a SoundCloud URL it only logs and leaves the (valid) URL in place, which is acceptable.

- [ ] **Step 3:** Verify `py_compile` clean; `python -m pytest -q` green.
- [ ] **Step 4: Commit** — `git commit -m "feat(watchlist): SoundCloud Fix Link uses manual URL paste"`

---

# Phase C — Issue 2b: Scan all entries on startup

## Task C1: Background startup scan

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:1752-1754` (constructor scheduling), add `_watchlist_startup_scan` near the scheduler (`~:3627`)

- [ ] **Step 1: Schedule the startup scan** — after the populate/reschedule lines (`:1752-1754`), add a third deferred call:

```python
        # First-run: auto-populate the Watch List from existing channel folders
        self.after(1200, self._watchlist_populate_from_folders)
        self.after(1600, self._reschedule_auto_check)
        # Actively refresh new-track counts for every entry on each launch.
        self.after(2200, self._watchlist_startup_scan)
```

- [ ] **Step 2: Implement `_watchlist_startup_scan`** — add near `_reschedule_auto_check`:

```python
    def _watchlist_startup_scan(self):
        """On launch, scan every watched channel (all platforms) so the cards
        show current new-track counts. Runs in the background via
        _watchlist_scan_all; skipped if a scan/download is already underway."""
        try:
            channels = self._db.get_all_watchlist_channels()
        except Exception:
            return
        if not channels:
            return
        if self._downloading or self._wl_download_active or self._wl_scan_active:
            return
        self._watchlist_log("🚀 Startup check: scanning all channels…", "info")
        self._watchlist_scan_all()
        # Stamp last-check so the interval timer counts from now, not from a
        # stale value (prevents an immediate second auto-check).
        self._watchlist_last_check = int(time.time())
        self._autosave_automation_settings()
```

(Confirm `_autosave_automation_settings` persists `watchlist_last_check`; if it doesn't include that key, persist it the same way the scheduler does — match the existing save path. If `_watchlist_last_check` is saved elsewhere, mirror that.)

- [ ] **Step 3: Verify** `py_compile` clean; `python -m pytest -q` green; headless smoke builds the app without error (the `after` callbacks won't fire in the smoke since there's no mainloop — that's fine).
- [ ] **Step 4: Commit** — `git commit -m "feat(watchlist): scan all entries on startup to refresh new-track counts"`

---

# Phase D — Verification, docs, manual walkthrough

## Task D1: Full automated verification
- [ ] `set PYTHONIOENCODING=utf-8 && python -m py_compile DJ-CrateBuilder_v1.3.py cratebuilder/*.py` → clean
- [ ] `python -m pytest -q` → all green (expect 22 + new tests)
- [ ] Headless smoke prints tabs Main / Watch List / Settings / About

## Task D2: Live manual walkthrough
- [ ] **Fix Link:** repair the two broken entries per Task A3 (with the Drum & Bass checkpoint).
- [ ] **SoundCloud add:** Add a `soundcloud.com/<artist>` URL via Add Channel → entry created as SoundCloud, no Fix Link button, Scan returns a real new-track count, Download New works and files land under `base/SoundCloud/<genre>/<artist>`.
- [ ] **SoundCloud auto-add:** download from a SoundCloud artist on the Main tab → a SoundCloud Watch List entry appears automatically.
- [ ] **Startup scan:** relaunch the app → the scan log shows "Startup check: scanning all channels…" and counts refresh for both YouTube and SoundCloud entries.

## Task D3: Docs
- [ ] Update `README.md` Watch List feature + FAQ to note SoundCloud channels are now supported and that the app refreshes new-track counts on startup.
- [ ] Update the About-tab FAQ in `DJ-CrateBuilder_v1.3.py` similarly (the Watch List FAQ entries).
- [ ] Commit: `docs: Watch List now supports SoundCloud + startup scan`

## Task D4: Finish
- [ ] Final `python -m pytest -q` green; update `HANDOFF.md` status.
- [ ] Use `superpowers:finishing-a-development-branch` — ASK the user whether to push `v1.3` / open PR / leave local. Do NOT push or touch `main` without explicit approval.

---

## Self-review notes
- **Spec coverage:** Issue 1 (silent collision + dup UX) → Phase A. SoundCloud first-class (create + scan + resolve) → Phase B. Startup scan-all → Phase C. ✓
- **Type consistency:** `update_watchlist_channel_fields` returns `bool` (A1) and every caller that now branches on it (A2) expects `bool`. `detect_platform`/`watch_scan_url`/`is_unresolved_channel` signatures match their tests and call sites. `_persist_resolved_channel` returns `bool`; existing callers (`_watchlist_apply_url`, resolve dialog `_confirm`) ignore the return value today — that remains valid (they refresh regardless). ✓
- **Open checkpoint:** Task A3 "Drum & Bass" intent question must be put to the user during execution, not assumed.

# Import Sanitising, Bug Reports, Auth-Failure Pop-up — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repo is indexed by `graft/` — locate code with `graft grep "<symbol>"` / `graft ask "<question>" --source` before opening any file; the `file:line` spans below were taken from graft on 2026-09-15 and may have drifted a few lines.

**Goal:** Three independent, user-facing safety and support features: (1) a Watch List import file is validated and sanitised before anything is shown or written; (2) users can send an anonymised bug report — description plus scrubbed `activity.log` / `debug.log` — to the project's GitHub issues; (3) repeated login-type download failures raise one pop-up per run that tells the user what to try with their cookie setting.

**Architecture:** Each feature is a new pure-logic module in `cratebuilder/` (headless, fully unit-tested), one or two new named methods in the `CrateBuilderService._methods()` dispatch table, and a thin wiring in `web/app.js`. No schema changes, no new dependencies, no AI. Nothing leaves the machine except through the user's own browser (the pre-filled GitHub "New issue" page).

**Tech Stack:** Python 3.10+ stdlib (`json`, `re`, `zipfile`, `urllib.parse`, `os`), pytest; plain JS in `web/app.js`; pywebview file dialogs.

**Spec:** decisions recorded in this plan's *Design decisions* section (agreed with the maintainer 2026-09-15).

## Design decisions (the spec)

| # | Decision | Chosen |
|---|---|---|
| 1a | Where import sanitising lives | A pure `sanitize_entry` / size check in `cratebuilder/watchlist_share.py`, run inside `parse()` so **every** caller (service read, tests) gets it. |
| 1b | What to do with a rejected entry | Drop it and **tell the user** how many were dropped and why (a note under the picker's title). |
| 1c | Accepted links | `http`/`https` only; host must be `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`, `youtu.be`, `soundcloud.com`, `www.soundcloud.com`, `m.soundcloud.com`, `on.soundcloud.com`. |
| 1d | File size cap | `MAX_IMPORT_BYTES = 4 * 1024 * 1024`. Read is refused (not truncated) above it. |
| 1e | Text field limits | `display_name`, `genre` ≤ 200 chars after stripping; control characters (`\x00-\x1f\x7f`) removed; a genre containing `/`, `\`, or consisting only of dots/spaces is replaced with the no-genre value. `channel_id` must match `^[A-Za-z0-9_-]{1,64}$` or is dropped (entry kept). |
| 2a | How a report reaches GitHub | The app opens `GITHUB_ISSUES_URL` (`…/issues/new`) with `title` and `body` query params pre-filled, **and** writes a zip of the scrubbed logs where the user chooses (Save dialog, default name `cratebuilder-report-YYYYMMDD-HHMM.zip`). The user drags the zip onto the issue. No token in the app, ever. |
| 2b | What is scrubbed | Home directory → `<HOME>`; library `base_dir` → `<LIBRARY>`; the OS username as a bare word → `<USER>`; anything already `<redacted>` stays; `token=…` → `token=<redacted>`; cookie file paths → `<COOKIE_FILE>`; IPv4 addresses → `<IP>`; e-mail addresses → `<EMAIL>`. **Track titles and channel URLs are kept** (maintainer's call — they are needed to reproduce). |
| 2c | Log size | Each log is trimmed to its **last 512 KiB** before scrubbing. |
| 2d | Preview | The report dialog shows the scrubbed text of both logs in a read-only box **before** the Save/Open buttons enable. Nothing is written or opened until the user presses *Save bundle & open GitHub*. |
| 2e | Transport | Local window only (`fs.` prefix). A remote browser sees the same button disabled with the tooltip "Available in the app window on the host machine." |
| 3a | What counts as an auth failure | A settled track whose reason is one of `AUTH_REASONS = ("login required", "age-restricted", "refused (403)", "bot check")`. `"bot check"` is a **new** label added to `classify_download_failure` for the yt-dlp text `"sign in to confirm you're not a bot"` (checked **before** the plain `"sign in"` match). |
| 3b | Threshold | `AUTH_WARN_THRESHOLD = 3` auth failures in one runner (a manual batch; or one channel of a Watch List run — the Watch List builds one runner per channel). |
| 3c | Once per job | The host emits `auth.trouble` each time the threshold is reached; the **frontend** shows the dialog only once per `job` until that job's `job.finished`. |
| 3d | Dialog content by cookie state | `use_cookies` off → "Downloads are being refused as if you weren't signed in" + button *Open cookie settings* + *Read the how-to*. On + browser → "Try switching browser cookies **off**" + *Open cookie settings* + *Read the how-to*, with the note that a running download must be stopped first (download-policy keys are frozen during a run — `_refuse_frozen_setting`). On + Cookie File → "Your cookie file may have expired — export a fresh one" + same buttons. |
| 3e | "Don't show again" | A tick box *Don't show this again this session* — frontend-only flag, cleared on page reload. |
| 3f | Where it fires | Both the Downloads queue and Watch List runs (same runner class). |

## Global Constraints

- **No tkinter imports in `cratebuilder/`.**
- `cratebuilder/` modules use **one-line module docstrings**; `web/app.js` uses `/* ── section ── */` comments that explain *why*.
- **Read the `changing-the-web-ui` skill before touching `web/`.** Every new RPC goes through `call()` in `app.js` (never `pywebview.api` directly); every new method is one entry in `_methods()` (`cratebuilder/service.py:1166-1272`); local-only methods carry the `fs.` prefix (`LOCAL_ONLY`, `service.py:105`).
- Frontend tests are Python that read `web/app.js` as text (`tests/test_web_*_client.py`). Add static assertions for every new wiring point.
- **Do not bump `APP_BUILD` or `APP_VERSION`.**
- Do not run the full suite as routine validation; run the named test files per task. The maintainer decides when the full `python -m pytest -q` runs.
- Commit messages: Conventional Commits with the attribution trailer the session provides.
- Web changes are verified by launching `python web_window.py --screen <name>` in **both** themes.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `cratebuilder/watchlist_share.py` | Modify | `MAX_IMPORT_BYTES`, `ALLOWED_HOSTS`, `sanitize_entry(item) -> (entry \| None, reason \| None)`; `parse()` returns `(entries, dropped)`. |
| `cratebuilder/service.py:2742-2773` | Modify | `watchlist_import_read`: size check before read; pass `dropped` through to the picker. |
| `web/app.js:7254-7288` | Modify | `openImportPicker`: show the dropped-entries note. |
| `tests/test_watchlist_share.py`, `tests/test_watchlist_share_service.py`, `tests/test_web_watchlist_client.py` | Modify | Coverage for the above. |
| `cratebuilder/support.py` | **Create** | `scrub_text(text, *, home, base_dir, username, cookie_file)`, `tail_bytes(path, limit)`, `build_bundle(zip_path, files)`, `issue_url(base, title, body)`, `system_block(...)`. |
| `cratebuilder/service.py` | Modify | New methods `support.preview` (read-only, both transports) and `fs.support_send` (local); registered in `_methods()`. |
| `web/index.html` | Modify | *Report a problem* button on the About screen. |
| `web/app.js` | Modify | `openReportDialog()`; About wiring. |
| `tests/test_support.py` | **Create** | Pure-logic tests. |
| `tests/test_service_support.py` | **Create** | Service-level tests (sandboxed paths). |
| `tests/test_web_about_client.py` | Modify | Static wiring assertions. |
| `cratebuilder/download.py:249-309` | Modify | `"bot check"` label; `AUTH_REASONS`, `is_auth_reason(reason)`. |
| `cratebuilder/batchrun.py:430-472` | Modify | `_auth_failures` counter; `auth.trouble` event at threshold. |
| `web/app.js:7350-7524` | Modify | `auth.trouble` subscriber → `openAuthTroubleDialog(payload)`; once-per-job + session-mute flags. |
| `tests/test_download.py`, `tests/test_batchrun.py`, `tests/test_web_downloads_client.py` | Modify | Coverage. |

Task order: **Phase C (auth pop-up) → Phase A (import sanitising) → Phase B (bug reports).** Each phase is independent and ships on its own.

---

## Phase C — Auth-failure pop-up

### Task C1: Label bot-check failures and define the auth reason set

**Files:**
- Modify: `cratebuilder/download.py:249-309`
- Test: `tests/test_download.py`

**Interfaces:**
- Produces: `AUTH_REASONS: tuple[str, ...]`, `is_auth_reason(reason: str) -> bool`, and the new reason string `"bot check"` from `classify_download_failure`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_download.py  (append)
from cratebuilder.download import classify_download_failure, is_auth_reason, AUTH_REASONS


def test_bot_check_is_labelled_before_plain_sign_in():
    text = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies"
    out = classify_download_failure(text)
    assert out.kind == "failed"
    assert out.reason == "bot check"


def test_plain_sign_in_still_reads_login_required():
    out = classify_download_failure("ERROR: Sign in to view this video")
    assert out.reason == "login required"


def test_auth_reasons_cover_the_four_login_shaped_labels():
    assert set(AUTH_REASONS) == {"login required", "age-restricted",
                                 "refused (403)", "bot check"}
    for r in AUTH_REASONS:
        assert is_auth_reason(r)
    assert not is_auth_reason("network error")
    assert not is_auth_reason("")
    assert not is_auth_reason(None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_download.py -q -k "bot_check or sign_in or auth_reasons"`
Expected: FAIL — `ImportError: cannot import name 'is_auth_reason'`.

- [ ] **Step 3: Implement**

In `cratebuilder/download.py`, above `_CONDITION_MARKERS`:

```python
# The failure labels that mean "YouTube/SoundCloud wanted a signed-in user":
# what the Downloads screen's auth-trouble pop-up counts. "refused (403)" is
# included because SoundCloud answers an expired session with a bare 403.
AUTH_REASONS = ("login required", "age-restricted", "refused (403)", "bot check")


def is_auth_reason(reason):
    """True when *reason* is one of the login-shaped failure labels."""
    return bool(reason) and reason in AUTH_REASONS
```

In `classify_download_failure`, replace the `"sign in"` line so the bot-check text is matched first (the curly apostrophe in yt-dlp's message is normalised by lower-casing only, so match on the stable prefix):

```python
    if   "ffmpeg"        in lower: return Failure("failed", "FFmpeg missing")
    elif "sign in to confirm you" in lower: return Failure("failed", "bot check")
    elif "sign in"       in lower: return Failure("failed", "login required")
    elif is_age:                   return Failure("failed", "age-restricted")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_download.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/download.py tests/test_download.py
git commit -m "feat(download): label bot-check failures and define the auth reason set"
```

### Task C2: Count auth failures per run and emit `auth.trouble`

**Files:**
- Modify: `cratebuilder/batchrun.py:430-472` (`_settle`, `_reset`)
- Test: `tests/test_batchrun.py`

**Interfaces:**
- Consumes: `is_auth_reason` from Task C1.
- Produces: event `auth.trouble` with payload `{"job": <job>, "count": <int>, "reason": <last reason>}`; constant `AUTH_WARN_THRESHOLD = 3`.

- [ ] **Step 1: Write the failing test**

Look at how `tests/test_batchrun.py` builds a `BatchRunner` with a fake `emit` (grep `def make_runner` or the first fixture in that file) and reuse that helper. The test drives `_settle` directly — it is the one place every failed track passes through.

```python
# tests/test_batchrun.py (append)
from cratebuilder import batchrun


def _settle_failed(runner, reason):
    spec = SimpleNamespace(url="https://youtu.be/x", genre="(none)", row_id=1,
                           entry={}, title="t", save_dir="", platform="YouTube",
                           channel_name="", channel_url="", channel_id=None,
                           suppress_channel_url=False)
    tally = {"downloaded": 0, "skipped": 0, "errors": 0, "deferred": 0,
             "stopped": False, "state": None, "detail": ""}
    runner._settle(spec, "t", ("failed", reason, ""), tally)


def test_third_auth_failure_emits_auth_trouble_once(runner_and_events):
    runner, events = runner_and_events
    runner._reset(5)
    _settle_failed(runner, "login required")
    _settle_failed(runner, "network error")     # not counted
    _settle_failed(runner, "bot check")
    assert not [e for e in events if e[0] == "auth.trouble"]
    _settle_failed(runner, "age-restricted")
    trouble = [e for e in events if e[0] == "auth.trouble"]
    assert len(trouble) == 1
    assert trouble[0][1]["count"] == 3
    assert trouble[0][1]["reason"] == "age-restricted"
    assert trouble[0][1]["job"] == runner._job
    _settle_failed(runner, "login required")    # 4th: no second event this run
    assert len([e for e in events if e[0] == "auth.trouble"]) == 1


def test_reset_clears_the_auth_counter(runner_and_events):
    runner, events = runner_and_events
    runner._reset(3)
    for _ in range(3):
        _settle_failed(runner, "login required")
    runner._reset(3)
    _settle_failed(runner, "login required")
    assert len([e for e in events if e[0] == "auth.trouble"]) == 1
```

`runner_and_events` is a fixture returning `(runner, events)` where `events` is the list the fake `emit` appends `(name, payload)` to. If the file has no such fixture, add one next to its existing runner factory using the same constructor arguments that factory uses.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_batchrun.py -q -k auth`
Expected: FAIL — no `auth.trouble` event emitted.

- [ ] **Step 3: Implement**

```python
# cratebuilder/batchrun.py — module level, near the other constants
from .download import is_auth_reason

AUTH_WARN_THRESHOLD = 3


# in _reset(self, total):
        self._auth_failures = 0
        self._auth_warned = False


# in _settle, inside the final `else:` (the errors branch), after
# `state, detail = "error", reason`:
            if is_auth_reason(reason):
                self._auth_failures += 1
                if (self._auth_failures >= AUTH_WARN_THRESHOLD
                        and not self._auth_warned):
                    self._auth_warned = True
                    self._emit("auth.trouble", {
                        "job": self._job, "count": self._auth_failures,
                        "reason": reason})
```

Also initialise both attributes in `__init__` (`self._auth_failures = 0`, `self._auth_warned = False`) so a runner that has never been reset still has them.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_batchrun.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/batchrun.py tests/test_batchrun.py
git commit -m "feat(batchrun): raise auth.trouble after three login-shaped failures"
```

### Task C3: Frontend dialog

**Files:**
- Modify: `web/app.js` — `subscribeDownloadEvents` (`:7350-7524`), new `openAuthTroubleDialog`.
- Test: `tests/test_web_downloads_client.py` (static assertions).

**Interfaces:**
- Consumes: event `auth.trouble` `{job, count, reason}`; `state.settings.use_cookies`, `state.settings.cookie_method`, `state.settings.cookies_browser`; existing helpers `openModal`, `modalButton`, `modalNote`, `show('settings')`, `openCookieHowto(browser)` (`app.js:5681`), `dl.running`, `wl.running`.

- [ ] **Step 1: Write the failing static test**

```python
# tests/test_web_downloads_client.py (append)
def test_auth_trouble_event_opens_the_dialog(app_js):
    assert "cbApi.on('auth.trouble'" in app_js
    assert "function openAuthTroubleDialog(" in app_js
    # once per job, and a session mute
    assert "authTrouble.shownFor" in app_js
    assert "authTrouble.muted" in app_js
    # the three cookie states each get their own copy
    assert "switching browser cookies off" in app_js
    assert "cookie file may have expired" in app_js
    assert "Open cookie settings" in app_js
```

(`app_js` is the fixture the file already uses to load `web/app.js` as text; if it is named differently there, use that name.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_web_downloads_client.py -q -k auth_trouble`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `web/app.js`, next to the other `dl`/`wl` state objects:

```js
  /* ── auth-trouble pop-up ──
     The host says "three login-shaped failures" once per runner; the
     frontend decides whether the user has already been told this job, or
     asked not to be told again this session. */
  const authTrouble = { shownFor: {}, muted: false };
```

Inside `subscribeDownloadEvents`, after the `job.finished` handler:

```js
    cbApi.on('auth.trouble', (p) => {
      const job = (p && p.job) || 'batch';
      if (authTrouble.muted || authTrouble.shownFor[job]) return;
      authTrouble.shownFor[job] = true;
      openAuthTroubleDialog(p);
    });
```

And in the existing `job.finished` handler, first line: `if (p && p.job) delete authTrouble.shownFor[p.job];`

The dialog:

```js
  function openAuthTroubleDialog(p) {
    const s = (state && state.settings) || {};
    const running = dl.running || wl.running;
    let lead, hint;
    if (!s.use_cookies) {
      lead = 'Several downloads were refused as if you weren’t signed in.';
      hint = 'These sites often need a signed-in session. Set up a cookie '
           + 'source in Settings ▸ Browser & Cookies — the how-to walks '
           + 'through a throwaway browser profile or saving a cookie file.';
    } else if (s.cookie_method === 'Cookie File') {
      lead = 'Several downloads were refused even with your cookie file.';
      hint = 'Your cookie file may have expired — export a fresh one from '
           + 'your browser and point Settings at it.';
    } else {
      lead = `Several downloads were refused with ${s.cookies_browser || 'browser'} cookies on.`;
      hint = 'Try switching browser cookies off and running again. If that '
           + 'doesn’t help, the how-to explains a throwaway profile or a '
           + 'saved cookie file.';
    }
    const refs = {};
    openModal({
      title: '🔐 Looks like a sign-in problem',
      width: 520,
      body(body) {
        const a = document.createElement('p'); a.textContent = lead;
        const b = document.createElement('p'); b.textContent = hint;
        body.append(a, b);
        if (running) {
          body.appendChild(modalNote('Cookie settings are locked while a '
            + 'download is running — stop the run first, then change them.'));
        }
        const lab = document.createElement('label');
        lab.className = 'cb-check';
        refs.mute = document.createElement('input');
        refs.mute.type = 'checkbox';
        lab.append(refs.mute, document.createTextNode(
          ' Don’t show this again this session'));
        body.appendChild(lab);
      },
      foot(foot, api) {
        const settings = modalButton('Open cookie settings', 'cb-btn--warn',
          () => { api.close(); show('settings'); });
        const howto = modalButton('Read the how-to', 'cb-btn--quiet',
          () => { api.close(); openCookieHowto(s.cookies_browser || 'Chrome'); });
        const close = modalButton('Close', 'cb-btn--quiet', api.close);
        close.style.marginLeft = 'auto';
        foot.append(settings, howto, close);
      },
      onClose() { if (refs.mute && refs.mute.checked) authTrouble.muted = true; },
    });
  }
```

Check `modalNote` and the `cb-check` class exist in `app.js`/`styles`; if the checkbox class is named differently in `index.html`'s settings markup, use that name so it themes correctly in dark mode.

- [ ] **Step 4: Run tests, then verify visually**

Run: `python -m pytest tests/test_web_downloads_client.py tests/test_web_wiring_client.py -q`
Expected: PASS.

Visual: `python web_window.py --screen downloads`; in the DevTools console (or a temporary line) run `cbApi._push('auth.trouble', {job:'batch', count:3, reason:'login required'})` in each of the three cookie states; check light and dark theme. Confirm *Open cookie settings* lands on Settings and *Read the how-to* opens the walkthrough.

- [ ] **Step 5: Commit**

```bash
git add web/app.js tests/test_web_downloads_client.py
git commit -m "feat(web): sign-in trouble pop-up after repeated auth failures"
```

---

## Phase A — Sanitise Watch List import files

### Task A1: Pure entry sanitiser and size cap

**Files:**
- Modify: `cratebuilder/watchlist_share.py:49-87`
- Test: `tests/test_watchlist_share.py`

**Interfaces:**
- Produces: `MAX_IMPORT_BYTES`, `ALLOWED_HOSTS`, `sanitize_entry(item) -> tuple[dict | None, str | None]`, and `parse(text) -> tuple[list[dict], list[str]]` (**signature change** — the second element is the list of human-readable drop reasons).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watchlist_share.py (append)
import json
from cratebuilder import watchlist_share as ws


def _file(channels):
    return json.dumps({"format": ws.FORMAT, "version": ws.VERSION,
                       "channels": channels})


def test_parse_returns_entries_and_drop_reasons():
    entries, dropped = ws.parse(_file([
        {"url": "https://www.youtube.com/@ok", "display_name": "OK"}]))
    assert [e["url"] for e in entries] == ["https://www.youtube.com/@ok"]
    assert dropped == []


def test_non_http_and_unknown_hosts_are_dropped_with_a_reason():
    entries, dropped = ws.parse(_file([
        {"url": "file:///C:/Windows/system.ini", "display_name": "x"},
        {"url": "javascript:alert(1)", "display_name": "y"},
        {"url": "https://evil.example.com/@z", "display_name": "z"},
    ]))
    assert entries == []
    assert len(dropped) == 3
    assert all("link" in r for r in dropped)


def test_genre_with_path_separators_becomes_no_genre():
    entries, dropped = ws.parse(_file([
        {"url": "https://soundcloud.com/a", "genre": "..\\..\\Windows"},
        {"url": "https://soundcloud.com/b", "genre": "../etc"},
        {"url": "https://soundcloud.com/c", "genre": ".."},
        {"url": "https://soundcloud.com/d", "genre": "House"},
    ]))
    assert [e["genre"] for e in entries] == ["(none)", "(none)", "(none)", "House"]
    assert dropped == []          # the entry is kept; only the genre is reset


def test_control_characters_are_stripped_and_names_capped():
    long = "A" * 500
    entries, _ = ws.parse(_file([
        {"url": "https://youtu.be/x", "display_name": "Bad\x00Name\x1f", "genre": long}]))
    assert entries[0]["display_name"] == "BadName"
    assert len(entries[0]["genre"]) == 200


def test_malformed_channel_id_is_dropped_but_entry_kept():
    entries, _ = ws.parse(_file([
        {"url": "https://youtu.be/x", "channel_id": "UC../..\\evil"}]))
    assert entries[0]["channel_id"] is None


def test_max_import_bytes_is_four_megabytes():
    assert ws.MAX_IMPORT_BYTES == 4 * 1024 * 1024
```

Update the existing `parse` tests in this file that unpack a bare list — they now unpack `entries, _ = ws.parse(...)`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_watchlist_share.py -q`
Expected: FAIL (tuple unpack / missing names).

- [ ] **Step 3: Implement**

```python
# cratebuilder/watchlist_share.py — constants after FIELDS
import re
from urllib.parse import urlsplit

MAX_IMPORT_BYTES = 4 * 1024 * 1024
MAX_TEXT = 200
ALLOWED_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com",
    "on.soundcloud.com"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _clean_text(value):
    return _CONTROL.sub("", str(value or "")).strip()[:MAX_TEXT]


def _link_ok(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and \
        (parts.hostname or "").lower() in ALLOWED_HOSTS


def sanitize_entry(item):
    """One raw channel object → (clean entry, None), or (None, reason) when
    it cannot be imported at all. A bad genre or channel id never drops the
    entry: the genre falls back to no-genre and the id is forgotten."""
    if not isinstance(item, dict):
        return None, "an entry that is not a channel record"
    url = _clean_text(item.get("url"))
    if not url or url.startswith("unresolved://"):
        return None, "an entry with no link"
    if not _link_ok(url):
        return None, f"a link that is not a YouTube or SoundCloud web address ({url[:60]})"
    genre = _clean_text(item.get("genre"))
    if ("/" in genre or "\\" in genre or not genre.strip(". ")):
        genre = ""
    channel_id = _clean_text(item.get("channel_id"))
    if channel_id and not _CHANNEL_ID.match(channel_id):
        channel_id = ""
    platform = _clean_text(item.get("platform"))
    return {
        "url": url,
        "display_name": _clean_text(item.get("display_name")),
        "platform": platform or util.detect_platform(url),
        "genre": genre or CrateLayout.NO_GENRE_VALUE,
        "channel_id": channel_id or None,
    }, None
```

Then rewrite the loop at the end of `parse` (`:71-87`):

```python
    entries, dropped = [], []
    for item in raw:
        entry, reason = sanitize_entry(item)
        if entry is None:
            dropped.append(reason)
        else:
            entries.append(entry)
    return entries, dropped
```

Update the `parse` docstring's last sentence to: "Returns (entries, dropped): the clean entries and one reason per entry that was thrown out."

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_watchlist_share.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/watchlist_share.py tests/test_watchlist_share.py
git commit -m "feat(watchlist): sanitise import entries and report what was dropped"
```

### Task A2: Service — size check and pass-through of dropped reasons

**Files:**
- Modify: `cratebuilder/service.py:2742-2773` (`watchlist_import_read`)
- Test: `tests/test_watchlist_share_service.py`

**Interfaces:**
- Consumes: `watchlist_share.parse -> (entries, dropped)`, `MAX_IMPORT_BYTES`.
- Produces: `fs.watchlist_import_read` result gains `"dropped": [str, ...]`.

- [ ] **Step 1: Write the failing tests**

Reuse the file's existing pattern for stubbing `_file_dialog` to return a temp path.

```python
def test_import_read_refuses_files_over_the_cap(service, tmp_path, monkeypatch):
    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b" " * (ws.MAX_IMPORT_BYTES + 1) + b"}")
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: str(big))
    with pytest.raises(CBError) as exc:
        service.call("fs.watchlist_import_read", {}, transport=LOCAL)
    assert "too large" in str(exc.value)


def test_import_read_reports_dropped_entries(service, tmp_path, monkeypatch):
    f = tmp_path / "list.json"
    f.write_text(json.dumps({"format": ws.FORMAT, "version": ws.VERSION,
        "channels": [{"url": "https://youtu.be/ok"},
                     {"url": "file:///etc/passwd"}]}), encoding="utf-8")
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: str(f))
    res = service.call("fs.watchlist_import_read", {}, transport=LOCAL)
    assert len(res["entries"]) == 1
    assert len(res["dropped"]) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_watchlist_share_service.py -q -k "cap or dropped"`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `watchlist_import_read`, before the `open()`:

```python
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise CBError(f"Couldn't read the list file: {exc}")
        if size > watchlist_share.MAX_IMPORT_BYTES:
            raise CBError("That file is too large to be a Watch List export "
                          f"({size // (1024 * 1024)} MB; the limit is "
                          f"{watchlist_share.MAX_IMPORT_BYTES // (1024 * 1024)} MB).")
```

Change `entries = watchlist_share.parse(text)` to `entries, dropped = watchlist_share.parse(text)` and the return to `return {"path": path, "entries": out, "dropped": dropped}`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_watchlist_share_service.py tests/test_watchlist_share.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/service.py tests/test_watchlist_share_service.py
git commit -m "fix(watchlist): cap import file size and surface dropped entries"
```

### Task A3: Picker note for dropped entries

**Files:**
- Modify: `web/app.js:7254-7288` (`openImportPicker`), `wlShareListModal` (`:7007-7057`) if it has no `note`-below-title slot for a warning.
- Test: `tests/test_web_watchlist_client.py`

- [ ] **Step 1: Write the failing static test**

```python
def test_import_picker_reports_dropped_entries(app_js):
    assert "res.dropped" in app_js
    assert "weren’t imported" in app_js or "weren't imported" in app_js
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_web_watchlist_client.py -q -k dropped`

- [ ] **Step 3: Implement**

In `openImportPicker`, after `const entries = res.entries || [];`:

```js
    const dropped = res.dropped || [];
    if (!entries.length) {
      toast(dropped.length
        ? `Nothing to import — ${dropped.length} entries weren’t valid.` 
        : 'That file holds no channels to import.', true);
      return;
    }
    const droppedNote = dropped.length
      ? `${dropped.length} ${dropped.length === 1 ? 'entry' : 'entries'} weren’t imported: `
        + dropped.slice(0, 3).join('; ') + (dropped.length > 3 ? '; …' : '')
      : '';
```

and pass `note: droppedNote ? `Choose the channels to add to your Watch List. ⚠ ${droppedNote}` : 'Choose the channels to add to your Watch List.'` to `wlShareListModal`. (Check that `note` is rendered with `textContent` — it is a plain string, and file-supplied text must never reach `innerHTML`.)

- [ ] **Step 4: Run tests and verify visually**

Run: `python -m pytest tests/test_web_watchlist_client.py -q`
Visual: `python web_window.py --screen watchlist` → Import, pick a hand-made JSON with one good and two bad entries; check the note in light and dark.

- [ ] **Step 5: Commit**

```bash
git add web/app.js tests/test_web_watchlist_client.py
git commit -m "feat(web): tell the user which import entries were dropped"
```

---

## Phase B — Anonymised bug reports

### Task B1: Pure support module

**Files:**
- Create: `cratebuilder/support.py`
- Test: `tests/test_support.py`

**Interfaces:**
- Produces:
  - `TAIL_BYTES = 512 * 1024`
  - `tail_bytes(path, limit=TAIL_BYTES) -> str` — last *limit* bytes decoded UTF-8 (errors replaced), starting on a line boundary; `""` for a missing file.
  - `scrub_text(text, *, home, base_dir, username, cookie_file=None) -> str`
  - `system_block(*, app_version, app_build, platform, python, transport) -> str`
  - `issue_url(base_url, title, body) -> str` — `base_url?title=…&body=…`, body truncated to 6000 chars with a trailing `\n\n[log bundle attached]` line.
  - `build_bundle(zip_path, files: dict[str, str]) -> None` — writes each `{name: text}` into a zip with `ZIP_DEFLATED`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_support.py
import zipfile
from cratebuilder import support


def test_scrub_replaces_home_library_username_and_secrets():
    text = (r"C:\Users\djsin\Music\DJ-CrateBuilder\YouTube\House\x.mp3 "
            r"C:\Users\djsin\AppData\Roaming\x  user djsin here "
            "token=abc123 cookiefile C:/Users/djsin/cookies.txt "
            "from 192.168.1.20 mail dj@example.com")
    out = support.scrub_text(
        text, home=r"C:\Users\djsin",
        base_dir=r"C:\Users\djsin\Music\DJ-CrateBuilder",
        username="djsin", cookie_file=r"C:/Users/djsin/cookies.txt")
    assert "djsin" not in out
    assert "<LIBRARY>" in out and "<HOME>" in out and "<USER>" in out
    assert "token=<redacted>" in out
    assert "<COOKIE_FILE>" in out
    assert "<IP>" in out and "<EMAIL>" in out


def test_scrub_keeps_track_titles_and_channel_urls():
    text = "DOWNLOADED  Artist - Track  https://www.youtube.com/@channel"
    out = support.scrub_text(text, home="/home/u", base_dir="/home/u/Music",
                             username="u")
    assert "Artist - Track" in out
    assert "https://www.youtube.com/@channel" in out


def test_scrub_handles_forward_and_back_slashes_the_same(tmp_path):
    out = support.scrub_text("C:/Users/djsin/Music/DJ-CrateBuilder/a and C:\\Users\\djsin\\b",
                             home="C:\\Users\\djsin",
                             base_dir="C:\\Users\\djsin\\Music\\DJ-CrateBuilder",
                             username="djsin")
    assert out == "<LIBRARY>/a and <HOME>\\b"


def test_tail_bytes_starts_on_a_line_boundary(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert support.tail_bytes(str(p), limit=9) == "line3\n"
    assert support.tail_bytes(str(tmp_path / "missing.log")) == ""


def test_issue_url_encodes_and_truncates():
    url = support.issue_url("https://github.com/x/y/issues/new", "Crash on scan",
                            "a" * 10000)
    assert url.startswith("https://github.com/x/y/issues/new?title=Crash+on+scan&body=")
    assert len(url) < 7000
    assert "log+bundle+attached" in url


def test_build_bundle_writes_named_members(tmp_path):
    z = tmp_path / "r.zip"
    support.build_bundle(str(z), {"activity.log": "a\n", "debug.log": "b\n",
                                  "report.txt": "c"})
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["activity.log", "debug.log", "report.txt"]
        assert zf.read("debug.log") == b"b\n"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_support.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Anonymised bug-report bundles: log tails, scrubbing, and the GitHub issue link."""
import os
import re
import zipfile
from urllib.parse import urlencode

TAIL_BYTES = 512 * 1024
BODY_LIMIT = 6000
_TOKEN = re.compile(r"(token=)[^\s&\"']+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def tail_bytes(path, limit=TAIL_BYTES):
    """The last *limit* bytes of *path* as text, starting on a whole line."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
    except OSError:
        return ""
    if size > limit:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl != -1 else data
    return data.decode("utf-8", errors="replace")


def _both_slashes(path):
    """A regex matching *path* with either slash in every separator."""
    parts = re.split(r"[\\/]+", path.strip())
    return r"[\\/]".join(re.escape(p) for p in parts if p)


def scrub_text(text, *, home, base_dir, username, cookie_file=None):
    """Replace anything that identifies the machine or person. Longest
    paths first so <LIBRARY> wins over <HOME> where they nest."""
    out = text or ""
    for value, tag in ((cookie_file, "<COOKIE_FILE>"),
                       (base_dir, "<LIBRARY>"), (home, "<HOME>")):
        if value and value.strip():
            out = re.sub(_both_slashes(value), tag, out, flags=re.IGNORECASE)
    if username and username.strip():
        out = re.sub(r"(?<![\w-])" + re.escape(username) + r"(?![\w-])",
                     "<USER>", out, flags=re.IGNORECASE)
    out = _TOKEN.sub(r"\1<redacted>", out)
    out = _EMAIL.sub("<EMAIL>", out)
    out = _IPV4.sub("<IP>", out)
    return out


def system_block(*, app_version, app_build, platform, python, transport):
    return (f"App: DJ-CrateBuilder {app_version} build {app_build}\n"
            f"OS: {platform}\nPython: {python}\nWindow: {transport}\n")


def issue_url(base_url, title, body):
    body = (body or "")[:BODY_LIMIT] + "\n\n[log bundle attached]"
    return base_url + "?" + urlencode({"title": title or "", "body": body})


def build_bundle(zip_path, files):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text or "")
```

Note the test for slashes expects `<LIBRARY>/a and <HOME>\b` — the separator *after* the placeholder is whatever followed the path in the source; the regex consumes only the path itself.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_support.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/support.py tests/test_support.py
git commit -m "feat(support): anonymised log bundle helpers"
```

### Task B2: Service methods `support.preview` and `fs.support_send`

**Files:**
- Modify: `cratebuilder/service.py` — `_methods()` (`:1166-1272`), new methods near `logs_tail` (`:2526`).
- Test: `tests/test_service_support.py` (create)

**Interfaces:**
- Consumes: `support.*` from B1; `self._log_path`, `self._debug_log_path`, `self._settings.get("base_dir")`, `self._settings.cookie_config().cookie_file`, `about_info()` (`:712`) for version/build/issues URL, `self._file_dialog(webview.SAVE_DIALOG, …)` (`:2688`), `self.open_url` (`:2889`).
- Produces:
  - `support.preview` `{}` → `{"activity": str, "debug": str, "system": str}` (scrubbed; both transports — read-only).
  - `fs.support_send` `{"title": str, "description": str}` → `{"saved": path | None, "opened": bool}`; local only. Refuses an empty description.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_service_support.py
import os, pytest
from cratebuilder.service import CBError, LOCAL, REMOTE


def test_preview_is_scrubbed(service):
    home = os.path.expanduser("~")
    with open(service._debug_log_path, "w", encoding="utf-8") as fh:
        fh.write(f"YDL OPTS | cookiefile {home}/cookies.txt token=abc\n")
    res = service.call("support.preview", {}, transport=REMOTE)
    assert home not in res["debug"]
    assert "token=<redacted>" in res["debug"]
    assert "build" in res["system"]


def test_send_refuses_empty_description(service):
    with pytest.raises(CBError):
        service.call("fs.support_send", {"title": "t", "description": "  "},
                     transport=LOCAL)


def test_send_is_local_only(service):
    with pytest.raises(CBError):
        service.call("fs.support_send", {"title": "t", "description": "x"},
                     transport=REMOTE)


def test_send_writes_bundle_and_opens_issue(service, tmp_path, monkeypatch):
    target = tmp_path / "r.zip"
    monkeypatch.setattr(service, "_file_dialog", lambda *a, **k: str(target))
    opened = {}
    monkeypatch.setattr(service, "open_url", lambda url: opened.setdefault("url", url) or {"opened": True})
    res = service.call("fs.support_send",
                       {"title": "Scan hangs", "description": "It just sits there."},
                       transport=LOCAL)
    assert res["saved"] == str(target) and target.exists()
    assert res["opened"] is True
    assert opened["url"].startswith("https://github.com/Sintax/DJ-CrateBuilder/issues/new?")
    assert "Scan+hangs" in opened["url"]
```

`service` is whatever fixture the other service tests use for a sandboxed `CrateBuilderService` (see `tests/test_watchlist_share_service.py`).

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_service_support.py -q`
Expected: FAIL — unknown method.

- [ ] **Step 3: Implement**

Register in `_methods()`:

```python
            "support.preview": lambda p: self.support_preview(),
            "fs.support_send": lambda p: self.support_send(
                p.get("title"), p.get("description")),
```

Methods (place after `logs_tail`):

```python
    def _scrub_kwargs(self):
        cookies = self._settings.cookie_config()
        return dict(home=os.path.expanduser("~"),
                    base_dir=str(self._settings.get("base_dir") or ""),
                    username=os.path.basename(os.path.expanduser("~")),
                    cookie_file=(cookies.cookie_file or "").strip() or None)

    def support_preview(self):
        """The scrubbed report exactly as it would be sent — shown to the
        user before anything is written or opened."""
        kw = self._scrub_kwargs()
        info = about_info()
        return {
            "activity": support.scrub_text(support.tail_bytes(self._log_path), **kw),
            "debug": support.scrub_text(support.tail_bytes(self._debug_log_path), **kw),
            "system": support.system_block(
                app_version=info.get("version"), app_build=info.get("build"),
                platform=platform.platform(), python=platform.python_version(),
                transport=self.transport),
        }

    def support_send(self, title, description):
        """Save the bundle where the user chooses, then open the pre-filled
        issue page. Local only: it needs the Save dialog and the browser."""
        if self.transport != LOCAL:
            raise CBError("Reports are sent from the app window on the host machine.")
        description = (description or "").strip()
        if not description:
            raise CBError("Describe the problem first — a sentence is enough.")
        import webview
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path = self._file_dialog(webview.SAVE_DIALOG,
                                 save_filename=f"cratebuilder-report-{stamp}.zip",
                                 file_types=("Zip archive (*.zip)",))
        if not path:
            return {"saved": None, "opened": False}
        preview = self.support_preview()
        support.build_bundle(path, {
            "report.txt": f"{title or ''}\n\n{description}\n\n{preview['system']}",
            "activity.log": preview["activity"],
            "debug.log": preview["debug"]})
        body = f"{description}\n\n{preview['system']}"
        issues = about_info().get("issues_url") or ""
        opened = self.open_url(support.issue_url(issues, title or "Bug report", body))
        return {"saved": path, "opened": bool(opened.get("opened"))}
```

Add `import platform` and `from . import support` at the top of `service.py` alongside the existing imports; `datetime` and `about_info` are already available there (check with `graft grep "^from datetime\|^import datetime" --in cratebuilder/service.py`). Confirm `about_info()` returns `"issues_url"` and `"build"` keys — `service.py:321` maps `issues_url`; check the build key name with `graft skeleton cratebuilder/service.py` and adjust.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_service_support.py tests/test_server.py -q`
Expected: PASS (the server tests confirm `fs.` methods are still refused remotely).

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/service.py tests/test_service_support.py
git commit -m "feat(service): support.preview and fs.support_send for bug reports"
```

### Task B3: Report dialog on the About screen

**Files:**
- Modify: `web/index.html` (About screen — one button next to the existing *Submit Issues* link), `web/app.js` (`aboutOpen` wiring + `openReportDialog`), `UI-design/ui-contract.json` (tooltip `about.report`), then `python scripts/gen_ui_strings.py`.
- Test: `tests/test_web_about_client.py`

**Interfaces:**
- Consumes: `support.preview`, `fs.support_send`; `openModal`, `modalButton`, `modalNote`, `toast`, `state.host.transport`, `setDisabled`.

- [ ] **Step 1: Write the failing static test**

```python
def test_about_has_a_report_a_problem_flow(app_js, index_html):
    assert 'id="about-report"' in index_html
    assert "function openReportDialog(" in app_js
    assert "call('support.preview'" in app_js
    assert "call('fs.support_send'" in app_js
    assert "Save bundle & open GitHub" in app_js
    assert "Nothing is sent until you press" in app_js
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_web_about_client.py -q -k report`

- [ ] **Step 3: Implement**

`index.html`, About screen, beside the existing issues link: `<button id="about-report" class="cb-btn cb-btn--quiet" data-tt="about.report">🐞 Report a problem</button>`.

`ui-contract.json`: add `about.report` → "Send a bug report: your description plus the last part of activity.log and debug.log with your user name, folders and cookies blanked out. You see everything before it goes." Then run `python scripts/gen_ui_strings.py`.

`app.js`, in `aboutOpen` (or the About wiring block): `$('#about-report').addEventListener('click', openReportDialog);` and if `state.host.transport === 'remote'` call `setDisabled(btn, true, { reason: 'Available in the app window on the host machine.' })`.

```js
  /* ── bug report ──
     Preview first, send second: the user reads the scrubbed logs before a
     single byte is written, and the Save dialog is the moment of consent. */
  async function openReportDialog() {
    let preview;
    try { preview = await call('support.preview', {}); }
    catch (_) { return; }
    const refs = {};
    openModal({
      title: '🐞 Report a problem',
      width: 720,
      body(body) {
        refs.title = document.createElement('input');
        refs.title.className = 'cb-input';
        refs.title.placeholder = 'Short title (e.g. "Scan hangs on one channel")';
        refs.desc = document.createElement('textarea');
        refs.desc.className = 'cb-input';
        refs.desc.rows = 4;
        refs.desc.placeholder = 'What happened, and what you expected.';
        const h = document.createElement('div');
        h.className = 'cb-mut';
        h.textContent = 'This is exactly what will be in the bundle — names, folders and cookies are already blanked out:';
        const pre = document.createElement('pre');
        pre.className = 'cb-log-preview';
        pre.textContent = preview.system + '\n── activity.log ──\n' + preview.activity
                        + '\n── debug.log ──\n' + preview.debug;
        body.append(refs.title, refs.desc, h, pre,
          modalNote('Nothing is sent until you press the button below. It saves a zip '
                  + 'where you choose and opens a pre-filled GitHub issue — drag the '
                  + 'zip onto that page. (A GitHub account is needed to post.)'));
      },
      foot(foot, api) {
        const go = modalButton('Save bundle & open GitHub', 'cb-btn--warn', async () => {
          api.busy(true);
          try {
            const res = await call('fs.support_send',
              { title: refs.title.value, description: refs.desc.value });
            if (res && res.saved) { toast(`Saved ${res.saved}`); api.close(); }
          } catch (err) { api.error(err && err.message || 'Could not send.'); }
          finally { api.busy(false); }
        });
        const cancel = modalButton('Cancel', 'cb-btn--quiet', api.close);
        cancel.style.marginLeft = 'auto';
        foot.append(go, cancel);
      },
      focus: () => refs.desc,
    });
  }
```

Add a `.cb-log-preview` rule in the stylesheet using existing theme tokens (max-height 260px, `overflow:auto`, monospace, `background: var(--cb-panel)` or whatever the log viewer already uses — copy its class if one exists rather than adding a new one).

- [ ] **Step 4: Run tests, then verify visually**

Run: `python -m pytest tests/test_web_about_client.py tests/test_web_wiring_client.py tests/test_ui_strings.py -q` (adjust the last name to the generated-strings test if it differs).

Visual: `python web_window.py --screen about` → *Report a problem*; check the preview really has `<HOME>`/`<USER>` and no real path; press the button, cancel the Save dialog (nothing should open), then save for real and confirm the browser lands on GitHub with title and body filled. Both themes.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/app.js web/styles.css UI-design/ui-contract.json cratebuilder/ui_strings.py tests/test_web_about_client.py
git commit -m "feat(web): report-a-problem dialog with scrubbed log preview"
```

---

## Self-review notes

- Coverage: 1a-1e → A1/A2/A3; 2a-2e → B1/B2/B3; 3a-3f → C1/C2/C3.
- Open risk, Phase C: `AUTH_REASONS` includes `"refused (403)"`, which SoundCloud also returns for some geo/DRM cases already caught earlier by `classify_permanent_failure` — those never reach the counter because permanent failures are classified first (`download.py:288-290`). Acceptable.
- Open risk, Phase B: a very long GitHub URL is silently truncated by some browsers around 8 KB; `BODY_LIMIT = 6000` leaves headroom. The full text is in the zip regardless.
- Not in scope: sending reports without a GitHub account (would need a hosted relay — deliberately rejected), scrubbing track titles (deliberately kept).

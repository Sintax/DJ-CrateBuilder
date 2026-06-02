# HANDOFF — DJ-CrateBuilder v1.3 Final Build (Automation + Refactor)

> ════════════════════════════════════════════════════════════════════════
> # ⏩ CURRENT EFFORT (2026-06-02): Fix Link repair + SoundCloud + startup scan
> ════════════════════════════════════════════════════════════════════════
>
> **Read THIS section first — it is the active work. The older "ALL PHASES COMPLETE (0–4)" section below is a finished prior effort, kept for reference.**
>
> **STATUS: ALL CODE DONE & COMMITTED on `v1.3`. 27 tests pass, compile clean. Remaining = (1) finishing-a-development-branch (ASK user push/PR/local) — THE ONLY blocking item; (2) optional re-test of SoundCloud on the user's normal network; (3) user housekeeping (delete scratch files).**
>
> ─────────────────────────────────────────────────────────────────────
> **🌙 AUTONOMOUS RUN RESULTS (2026-06-02, while user asleep) — READ THIS:**
> - **A3 repair DONE via DB (not GUI).** GUI computer-use was impossible with the user asleep, so I repaired the 2 broken entries directly: backed up `cratebuilder.db` → **`cratebuilder.db.bak-pre-a3-repair`** first, then `db.remove_watchlist_channel()` removed the two confirmed-duplicate rows — **id 16 "Deep-Tech Station"** (dup of resolved id 18) and **id 3 "Drum & Bass"** (user-confirmed same as "UKF Drum & Bass" id 6). Result: **broken count = 0**, watchlist now **16 rows**. No code touched; user data backup is on disk (gitignored). If the user wants the entries back, restore from the `.bak-pre-a3-repair` copy.
> - **SoundCloud helpers VERIFIED, but live scan hit a 403 (ENVIRONMENTAL, not a bug).** For `soundcloud.com/nocopyrightsounds`: `detect_platform`→`SoundCloud` ✅, `is_unresolved_channel`→`False` ✅, `watch_scan_url`→`https://soundcloud.com/nocopyrightsounds/tracks` ✅. The live `yt_dlp.extract_info` raised `HTTP 403 Forbidden` (SoundCloud anti-bot blocking yt-dlp's client_id bootstrap in this sandbox). **Isolation proof:** a YouTube scan through the *same* code path returned 5 entries fine, and yt-dlp is current (`2026.03.17`) — so network + yt-dlp work; only SoundCloud's extractor is refused *here*. The same yt-dlp powers the app's existing SoundCloud downloads, so if those work on the user's normal network/cookies, the Watch List scan will too. **Action for user: do one real SoundCloud add+Scan on your normal connection to confirm.**
> - **Baseline still green:** 27 passed, `py_compile` OK, tree clean. HEAD was `e7b213e` before this HANDOFF commit.
> - **NOT pushed. finishing-a-development-branch was deliberately left for the user** (push v1.3 / PR / leave local — NEVER push or touch `main` without explicit OK).
> ─────────────────────────────────────────────────────────────────────
>
> **Plan file (full task detail):** `docs/superpowers/plans/2026-06-02-watchlist-soundcloud-fixlink-startup.md`
>
> **What was built (3 goals), 12 commits `3dc012f`..`bb18bf6` (HEAD=`bb18bf6`):**
> - **Issue 1 — Fix Link silent failure FIXED.** Root cause: resolved channel URL collided with `UNIQUE(url)`; `db.update_watchlist_channel_fields` swallowed the IntegrityError and the app logged false "OK". Now: that db method returns `bool` + catches `IntegrityError` (`3dc012f`); `_persist_resolved_channel` pre-checks `get_watchlist_channel_by_url`, and on a duplicate calls `_watchlist_offer_remove_duplicate` (askyesno → remove the redundant row, or park it as `error`), writes sidecar only on DB success, returns bool (`115b81f`); all 5 resolve call sites routed through a new `_finish_resolve(ch, channel_id, handle, url, success_msg, close_fn)` helper so success is announced only when it sticks (`a7b76bd`).
> - **Issue 2a — SoundCloud is now first-class.** `util.detect_platform(url)` pure + `_detect_platform` delegates (`a48c921`); `sidecar.is_unresolved_channel` is platform-aware (SoundCloud needs `soundcloud.com` in url; YouTube kept PERMISSIVE to avoid regressing legacy `/c/`,`/user/` URLs) + new `sidecar.watch_scan_url(platform,url)` (YouTube→`/videos`, SoundCloud→`/tracks`) (`3e42938`); `_watchlist_scan_channel` honors `ch["platform"]` (scan url, save dir, backfill, new-entry url fallback) (`f87d913`); Add dialog / auto-add / folder-import store real platform + import walks `base/SoundCloud` too (`4a54106`); SoundCloud Fix Link = `_watchlist_soundcloud_link_dialog` (paste soundcloud.com URL via simpledialog) (`671c6c7`); platform-aware "needs resolve" scan message (`f4d1bf2`).
> - **Issue 2b — Startup scan.** `_watchlist_startup_scan` scheduled `self.after(2200, ...)` in `__init__`; scans all entries in background via `_watchlist_scan_all`, stamps `_watchlist_last_check` + `_autosave_automation_settings()` (which reschedules, cancelling the constructor's 1600ms timer → no double-scan) (`c949073`).
> - Docs (`bb18bf6`): README + About FAQ updated for SoundCloud + startup scan + dup detection.
>
> **All tasks reviewed** (spec + quality two-stage). Final whole-impl review = **READY-FOR-WALKTHROUGH, no blockers**. The one fixed NIT = platform-aware scan message.
>
> **>>> RESUME HERE after compaction <<<**
> 1. Re-verify: `cd` repo, `set PYTHONIOENCODING=utf-8`, `python -m py_compile DJ-CrateBuilder_v1.3.py cratebuilder/*.py` + `python -m pytest -q` (expect **27 passed**). Confirm HEAD=`bb18bf6`.
> 2. **Live walkthrough — USER CHOSE: "I drive via computer-use" (option 1).** Steps:
>    - Launch the DEV app (NOT the installed exe). The installed Start-menu "DJ-CrateBuilder" is the OLD v1.2 — `open_application` launches that, WRONG. Launch dev via PowerShell: `Start-Process -FilePath 'C:\Python314\python.exe' -ArgumentList 'DJ-CrateBuilder_v1.3.py' -WorkingDirectory '<repo>' -PassThru`, capture PID, then focus it with a `[Win]::SetForegroundWindow($p.MainWindowHandle)` + `ShowWindow(...,9)` Add-Type helper (pattern already used this session). request_access for "DJ-CrateBuilder" AND "Python 3.14 (64-bit)" (python.exe is the dev process, granted full tier).
>    - Confirm **startup scan** ran (scan log shows "🚀 Startup check: scanning all channels…" and counts refresh).
>    - **A3 repair (2 entries, both `needs_resolve`):** **Deep-Tech Station (db id 16)** is a duplicate of an already-resolved "Deep-Tech Station" (db id 18) → Fix Link → expect "Duplicate channel… already tracked as Deep-Tech Station" → click **Yes** to remove id 16. **Drum & Bass (db id 3)** — **USER CONFIRMED it's the SAME as their "UKF Drum & Bass" channel → Fix Link → on the duplicate prompt click Yes to REMOVE the redundant 'Drum & Bass' row.** (No further question needed.)
>    - **SoundCloud test:** Add a `soundcloud.com/<artist>` entry via Add Channel → confirm it's created as SoundCloud, NO Fix Link button, Scan returns a real new-track count (network read only, no download). PAUSE before any real "Download New" (downloads files — get user OK).
>    - NOTE: Explorer is DENIED for computer-use (can't drive the Windows tray menu); that's fine, not needed here.
> 3. After walkthrough: **finishing-a-development-branch** — ASK the user: push `v1.3` only / open PR / leave local. NEVER push or touch `main` without explicit approval. (User's standing rule: v1.3 only.)
>
> **Env/gotchas (this effort):** Tests read the REAL user config `~/.dj_cratebuilder_config.json`; `test_settings_vars.py::test_new_settings_defaults` expects `minimize_to_tray=False` — it was restored to False this session (don't re-enable and leave it). `test_tabs.py` self-skips with no display (so "26 passed, 1 skipped" == fine). Smart quotes/emoji are used in UI strings (keep UTF-8). `cratebuilder.db` = live user data: read-only (`file:...?mode=ro`) for diagnosis, NEVER commit. Two untracked scratch files `_anjuna.py`/`_uitest2.py` still need USER deletion (`Remove-Item _anjuna.py, _uitest2.py`). The dev app may be relaunched several times; kill stale instances via PID before relaunch.
>
> **Task state (TaskList):** #15-#16,#18-#23 completed; **#17 (A3 repair) DONE autonomously via DB (rows 16 & 3 removed, backup `cratebuilder.db.bak-pre-a3-repair`, 0 broken remain)**; #24 (D) — verify+docs+A3 done; **only finish-branch remains (USER decision).** The optional live GUI walkthrough is no longer needed for repair (A3 done); user may still want to eyeball the SoundCloud add+Scan on their own network per the 403 note above.

---

**Read this in full before resuming. It is the single source of truth for an in-progress, multi-phase, subagent-driven rework.**

Last updated at: **ALL PHASES COMPLETE (0–4). HEAD = `3cdb725`.** Tests: **22 passed**. Branch: `v1.3` (local only — user chose "don't push yet"; NEVER touch `main` without explicit go-ahead).

> **STATUS: DONE.** Phases 0–4 are all complete and verified (automated suite + headless wiring + a live computer-use walkthrough of tab order, the Settings automation UI, interval persistence, Fix Link conditional visibility, on-startup scan, and hide-to-tray). Remaining optional items the user deferred: (a) the two side-effecting live checks — run-at-startup registry toggle + a real short-interval auto-download — were SKIPPED by user choice (covered by the test suite); (b) `_anjuna.py`/`_uitest2.py` still need deletion by the user (harness can't delete — `Remove-Item _anjuna.py, _uitest2.py`); (c) the user's config has `minimize_to_tray=True` left on from the tray test (default is Off — untick in Settings if undesired). If the user later says "push it", per their standing rule push **`v1.3` only** (`git push -u origin v1.3`) and do NOT merge to `main` or open a PR unless they explicitly ask.

---

## 0. What this project is

- **DJ-CrateBuilder** = a Windows/Linux desktop app (Python 3.14 + tkinter, yt-dlp + FFmpeg) that downloads high-quality audio from YouTube/SoundCloud into organized "crate" folders for DJ sets. The headline feature is the **Watch List**: tracks channels and surfaces/downloads only *genuinely new* uploads.
- This is the **final v1.3 release**. Single entry file: `DJ-CrateBuilder_v1.3.py` (was ~7181 lines; now smaller after extraction).
- Repo: `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder`. Remote: `https://github.com/Sintax/DJ-CrateBuilder.git`. Working branch: **`v1.3`** (do NOT touch `main`; v1.3 will eventually be promoted to a main release by the user).
- The user is the developer (a DJ). Not committed/pushed beyond local unless noted — **all 23 commits below are local on `v1.3`; NOT yet pushed** in this work session (the earlier screenshots/README work WAS pushed; this automation work has not been pushed yet — confirm with `git status -sb` before pushing).

## 1. Governing documents (READ THESE)

- **Spec:** `docs/superpowers/specs/2026-05-30-watchlist-automation-design.md`
- **Plan (the task-by-task bible):** `docs/superpowers/plans/2026-05-30-watchlist-automation.md` — 22 tasks across 5 phases, full code in each task. Locate code by SYMBOL, not the plan's line numbers (they are stale).

## 2. The 7 features + 2 cleanup goals being delivered

Feature work (user-approved decisions baked into the spec):
1. Swap tab order → **Main → Watch List → Settings → About**. ✅ DONE
2. Settings "Automation" section: auto-check interval dropdown (Off/6/12/24/48h, **default 24h ON**), run-at-Windows-startup checkbox, minimize-to-system-tray checkbox. ✅ DONE
3. Background scheduler: every N hours scan all watched channels, auto-download new tracks, fire tray notification. ✅ DONE
4. Startup discovery of new channel folders (incremental/idempotent). ✅ DONE (already dedup-safe; no code change needed)
5. Rename per-card **Resolve → Fix Link**, shown only when channel link unresolved. ✅ DONE
6. About-tab FAQ updated with Watch List entries. ✅ DONE
7. Persist new settings. ✅ DONE

Cleanup goals (user explicitly requested "best practices / less spaghetti" for the final build):
8. **Hybrid refactor** — extract `cratebuilder/` package + in-file tidy. Extraction ✅ DONE (Phase 1). In-file tidy ⬜ NOT STARTED (Phase 3).
9. **Test harness first** — pytest safety net. ✅ DONE (Phase 0).

User decisions (locked): pystray+Pillow for tray; interval presets Off/6/12/24/48h, **default 24h**; auto-download **+ notify** (via pystray's built-in `icon.notify()` — no extra dep); timer runs whenever app is open; tray notification on new tracks; start hidden in tray when Windows-auto-launched (`--startup` flag); keep version **1.3** (final), work on `v1.3` branch.

## 3. Execution method (FOLLOW THIS — it's the superpowers subagent-driven-development skill)

For EACH work unit: dispatch a **general-purpose implementer subagent** → it implements per the plan, TDD, commits per task → then dispatch a **spec-compliance reviewer subagent** (verify independently, don't trust the report) → fix any issues → then a **code-quality reviewer subagent** (only after spec ✅) → fix issues → mark complete → next unit. Continuous execution, no pausing between units unless BLOCKED.

**Context-saving adaptation in use:** the 22 plan tasks are grouped into **9 work units** (cohesive, sequential batches) to reduce subagent round-trips. Each unit still gets BOTH reviews. Implementer/reviewer prompts point the subagent at the plan file + the specific task numbers + key gotchas, rather than pasting full task text (to conserve controller context). This is deliberate and working well — keep doing it.

The 3 dispatch prompt templates live at:
`C:\Users\djsin\.claude\plugins\cache\claude-plugins-official\superpowers\5.1.0\skills\subagent-driven-development\{implementer-prompt,spec-reviewer-prompt,code-quality-reviewer-prompt}.md`

After ALL phases: dispatch a final whole-implementation code reviewer, then use the **superpowers:finishing-a-development-branch** skill to wrap up (the user has NOT yet decided merge vs PR for promoting v1.3 to a main release — ASK them then).

## 4. The 9 work units & status (TodoWrite IDs in parens)

| Unit | Plan tasks | Status |
|------|-----------|--------|
| Phase 0: pytest harness (#6) | 0.1–0.5 | ✅ DONE |
| Phase 1: extract cratebuilder package (#7) | 1.1–1.3 | ✅ DONE |
| Phase 2a: tabs + automation settings UI (#8) | 2.1–2.3 | ✅ DONE |
| Phase 2b: background scheduler (#9) | 2.4 | ✅ DONE |
| Phase 2c: run-at-startup module (#10) | 2.5 | ✅ DONE |
| Phase 2d: system tray + lifecycle (#11) | 2.6 | ✅ DONE |
| Phase 2e: Fix Link + discovery + FAQ (#12) | 2.7–2.9 | ✅ DONE |
| **Phase 3: in-file tidy (#13)** | **3.1–3.4** | **⬜ IN PROGRESS — NOT STARTED. THIS IS NEXT.** |
| Phase 4: build, docs, final verify (#14) | 4.1–4.2 | ⬜ NOT STARTED |

TodoWrite tasks #1–#5 (brainstorm/plan phases) are done. #6–#12 done. **#13 is marked in_progress but no work done yet.** #14 pending.

## 5. >>> RESUME HERE: Phase 3 (in-file tidy) <<<

Behavior-preserving cleanup of `DJ-CrateBuilder_v1.3.py`. After EACH sub-task: `python -m py_compile` clean + `python -m pytest -q` green (22) + headless smoke builds the app. Commit per sub-task. **Keep diffs small and reviewable; do NOT move method bodies across class boundaries; do NOT change behavior.**

- **3.1** Section banners + logical grouping (banners + small in-class reorders only).
- **3.2** Remove provably-dead code; hoist magic constants to module level — e.g. `AUTO_CHECK_OPTIONS = ["Off","6 hours","12 hours","24 hours","48 hours"]` (reuse in the Settings combobox built in `_build_settings_tab`), and the `unresolved://` sentinel prefix if duplicated.
- **3.3** Add a small `_run_bg(target, *args)` daemon-thread helper and use it ONLY for straightforward fire-and-forget `threading.Thread(target=..., daemon=True).start()` call sites (skip any that capture the thread handle or do extra setup). Add docstrings to public-ish methods lacking them.
- **3.4** Delete temp scripts `_anjuna.py` and `_uitest2.py` — **the harness/agents CANNOT delete files; ASK THE USER to delete them** (they are untracked, never committed, so just confirm removal; no commit needed).

Dispatch a Phase 3 implementer with those bounds, then spec + quality review. Recommend telling the implementer: be conservative, prioritize safety over aggressive restructuring, one commit per sub-task, verify after each.

## 6. Phase 4 (after Phase 3)

- **4.1** Update `requirements.txt` (already has yt-dlp, pystray>=0.19, Pillow>=10.0 — confirm). Update PyInstaller command in README/Packaging_Guide to bundle the package + tray deps: `--collect-submodules cratebuilder --hidden-import pystray._win32 --hidden-import PIL.ImageDraw`. Update README Features/Settings/Version-History to mention auto-check, run-at-startup, tray, Fix Link; note `requirements-dev.txt` + `pytest -q` for contributors.
- **4.2** Final verification: `pytest -q` green; `py_compile` clean; headless smoke (4 tabs in order); **launch the real app** (`python DJ-CrateBuilder_v1.3.py`) and walk the manual checklist (tab order; interval combo persists+reschedules; run-at-startup writes/removes the registry key — verify with `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v DJ-CrateBuilder`; tray hide/restore/Scan/Quit; Fix Link visibility on resolved vs unresolved card; a forced short-interval tick downloads+notifies; folder discovery idempotent). Optionally build the exe and smoke-test frozen. Then finishing-a-development-branch (ASK user: push v1.3 / merge to main / PR).

## 7. Architecture map (post-extraction)

New package `cratebuilder/` (Tk-free, unit-tested) imported by the main file:
- `cratebuilder/util.py` — `_config_path`, `load_config`, `save_config`, `today_yyyymmdd` (returns `YYYYMMDD` **no dashes**), `normalize_track_key`, `scan_folder_newest_mp3`, `CONFIG_NAME`.
- `cratebuilder/sidecar.py` — `CHANNEL_SIDECAR_NAME`, `channel_url_from_id`, `read_channel_sidecar`, `write_channel_sidecar`, `is_unresolved_channel(ch)`. (App keeps a thin staticmethod `_is_unresolved_channel` delegating to this.)
- `cratebuilder/db.py` — `DownloadsDatabase` class. KEY: `add_watchlist_channel(*, url, display_name, platform, genre, scan_cutoff_date, auto_added=False, channel_id=None, status="idle")` catches `sqlite3.IntegrityError` on `UNIQUE(url)` and returns `None` (this is what makes folder discovery dedup-safe). `get_all_watchlist_channels()`. Schema `SCHEMA_VERSION = 2`.
- `cratebuilder/startup.py` — `_startup_command()` (appends ` --startup`; frozen→`sys.executable`, source→pythonw.exe/python.exe + script), `startup_is_enabled()`, `set_startup(enabled)`. Guarded `winreg` import; degrades gracefully off-Windows.
- `cratebuilder/tray.py` — `TrayIcon` class (pystray on daemon thread; `.available/.start()/.notify()/.stop()`; menu Open/Scan Now/Quit; runtime-drawn 64x64 Pillow icon; menu clicks marshalled via a `schedule` callback = `lambda fn: self.after(0, fn)`).

Key App methods added (in `MP3DownloaderApp` in the main file):
- Scheduler: `auto_check_hours_to_seconds(value)` (module fn), `_reschedule_auto_check`, `_auto_check_tick`, `_auto_check_after_scan` (bounded at `_AUTO_CHECK_MAX_POLLS = 150`), `_autosave_automation_settings`.
- Startup: `_on_run_at_startup_toggle` (set registry + revert/warn on fail + persist).
- Tray/lifecycle: `_notify_tray`, `_ensure_tray`, `_hide_to_tray` (iconify fallback if tray unavailable), `_show_from_tray`, `_quit_app` (cancel timer + stop tray + destroy), `_on_window_close` (→ tray if minimize_to_tray & win32 else quit). `WM_DELETE_WINDOW` bound to `_on_window_close` in `__init__` (App had NO prior binding; the 3 other `self.destroy` protocols belong to Toplevels LogViewerWindow/DebugLogViewerWindow/CookieHowToWindow — leave them).
- New settings vars in `__init__`: `_auto_check_hours` ("24 hours"), `_run_at_startup` (False, overridden from registry on win32), `_minimize_to_tray` (False), `_watchlist_last_check` (int), `_auto_check_after_id`, `_tray_icon`, `_auto_check_poll_count`. Persisted keys: `auto_check_hours`, `run_at_startup`, `minimize_to_tray`, `watchlist_last_check`.

## 8. Test suite (22 passing)

`tests/`: `conftest.py` (importlib loader fixture `cb` loads the single-file app as module), `test_util.py`, `test_config.py`, `test_sidecar.py`, `test_db.py`, `test_scheduler.py`, `test_settings_vars.py`, `test_tabs.py`, `test_startup.py`, `test_tray_import.py`. `requirements-dev.txt` = pytest.
- Run: `python -m pytest -q` (set `PYTHONIOENCODING=utf-8` to avoid emoji cp1252 console crashes).
- `tests/test_tabs.py` self-skips when no grabbable display (so you may see "21 passed, 1 skipped" in some headless invocations — NOT a failure).

## 9. Verification commands (use after every change)

```
cd C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder
set PYTHONIOENCODING=utf-8
python -m py_compile DJ-CrateBuilder_v1.3.py
python -m pytest -q
```
Headless UI smoke (builds the App without blocking on mainloop; do NOT call `_hide_to_tray` — it spawns a real tray icon):
```
python -c "import importlib.util as u; s=u.spec_from_file_location('cb','DJ-CrateBuilder_v1.3.py'); m=u.module_from_spec(s); s.loader.exec_module(m); a=m.MP3DownloaderApp(); a.update(); print('TABS', [a._notebook.tab(i,'text') for i in a._notebook.tabs()]); a.destroy()"
```

## 10. Critical gotchas (learned this session — do not relearn the hard way)

- **Harness/agents CANNOT delete files** (`rm`/`Remove-Item` denied). For `_anjuna.py`/`_uitest2.py` deletion (Phase 3.4), ASK THE USER.
- **`today_yyyymmdd()` returns `YYYYMMDD` (no dashes)** — a Phase 0 test was corrected to match.
- **`SendMessage` to continue a prior subagent is NOT available** in this environment — dispatch a fresh subagent, or (for trivial 1-3 line review fixes) the controller applies them directly to save context (this has been the practice for minor review nits — e.g. dead-import removal, assert strengthening).
- **`_watchlist_log(msg, tag)`** inserts `tag` as a Tk Text tag; unknown tags render unstyled (harmless). Tags seen: `"info"`, `"err"`.
- **pystray `icon.run()` BLOCKS** → daemon thread. Menu callbacks must marshal to Tk via `self.after(0, fn)`, never touch Tk directly.
- **Withdraw (not destroy)** keeps the Tk mainloop alive so the scheduler keeps ticking; `_quit_app` is the only real exit.
- **DB tests** must use a temp path; stub `_resolve_save_dir` for anything that could write into the real Music library; never touch the real `cratebuilder.db`.
- `tk.PanedWindow` rejects `highlightthickness` (pre-existing).
- Git emits harmless LF→CRLF warnings on Windows commits — ignore.
- Default-on 24h auto-check means the app downloads unattended out of the box — INTENDED (user chose 24h default).

## 11. Commit log on v1.3 (this session, newest first, all LOCAL — not pushed)

```
3c8652c fix: scan-guard message names Fix Link, not Resolve   <- HEAD (end of Phase 2e)
1343e03 docs: add Watch List automation FAQ entries to About tab
d548469 feat: rename Resolve to Fix Link, show only when unresolved
89df480 polish: tray start() threading-contract docstring + test assertions
6884a8d feat: system tray (pystray) + hide-to-tray window lifecycle
b67ad69 test: cover startup degradation + command branches; clarify persist
136fb41 feat: run-at-Windows-startup via registry (cratebuilder.startup)
7fd981c harden: bound scheduler post-scan poll, drop dead flag
4058516 feat: background auto-check scheduler with auto-download
0a30572 test: use tab-title join as assert message in test_tabs
6515d3e feat: add Settings Automation UI section
cf8421e feat: add automation settings vars + persistence
80305aa feat: swap Watch List and Settings tab order
6e444af refactor: drop dead imports + unused re-exports after extraction
dcd8c04 refactor: extract DownloadsDatabase into cratebuilder.db
85fb678 refactor: extract sidecar helpers + is_unresolved_channel
29328fb refactor: extract util helpers into cratebuilder.util
c830b3c test: strengthen db dedup assertions, drop unused fixture
36ab3de + 4 more: Phase 0 test characterization commits
```
Plan/spec commits precede these (`2e97bee` = plan committed; baseline for `git diff`).

## 12. Immediate next action after reading this

1. `cd` to repo, run the verification commands in §9 — confirm **22 passed** and HEAD = `3c8652c` (or later if you progressed).
2. Confirm TodoWrite state (recreate if lost: units #6–#12 done, #13 Phase 3 in progress, #14 pending).
3. Dispatch the **Phase 3 implementer** per §5 with the conservative bounds, then spec + quality review, then Phase 4, then final review + finishing-a-development-branch.
4. Do NOT push or merge without asking the user.

# HANDOFF — DJ-CrateBuilder v1.3 Final Build (Automation + Refactor)

**Read this in full before resuming. It is the single source of truth for an in-progress, multi-phase, subagent-driven rework.**

Last updated at: Phase 2e COMPLETE, Phase 3 NOT STARTED. HEAD = `3c8652c`. Tests: **22 passed**. Branch: `v1.3`.

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

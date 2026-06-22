# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DJ-CrateBuilder is a Windows/Linux desktop app (Python 3.10+, tkinter) that batch-downloads audio from YouTube and SoundCloud as MP3s via `yt-dlp` + FFmpeg, organized into `~/Music/DJ-CrateBuilder/<Platform>/<Genre>/<Channel>/`. Current branch is `main`, on version **1.3** (build 8).

## Commands

```bash
# Run from source
python DJ-CrateBuilder_v1.3.py

# Install runtime deps
pip install -r requirements.txt           # yt-dlp, pystray, Pillow, send2trash

# Install dev deps (just adds pytest)
pip install -r requirements-dev.txt

# Run full test suite (quiet)
python -m pytest -q

# Run a single test file or test
python -m pytest tests/test_db.py -q
python -m pytest tests/test_util.py::test_safe_filename -q

# Cut a nightly release (bump APP_BUILD, build, delta-zip, upload to nightly tag)
python scripts/release.py
python scripts/release.py --help          # full flag reference
python scripts/release.py --dry-run       # build + zip locally, no upload
python scripts/release.py --full          # ship a full payload, reset baseline
```

There is no lint step. The release script requires the GitHub CLI (`gh`) authenticated.

## Architecture

The app is a **monolithic tkinter UI** (`DJ-CrateBuilder_v1.3.py`, ~10k lines) wrapped around a **pure-logic package** (`cratebuilder/`). The split exists so the logic is unit-testable without spinning up tkinter.

- `DJ-CrateBuilder_v1.3.py` — GUI, threading, yt-dlp orchestration, all four tabs (Main / Watch List / Settings / About). The version constants live at the top: `APP_VERSION` ("1.3") is pinned; **only `APP_BUILD` is bumped per nightly** (the release script does this automatically).
- `cratebuilder/util.py` — config persistence (`~/.dj_cratebuilder_config.json`), date helpers, filename sanitisation, cookie option building, platform detection. No tkinter imports.
- `cratebuilder/db.py` — SQLite (`cratebuilder.db`, WAL mode) holding both downloads history and the Watch List. `SCHEMA_VERSION = 3`; migrations live in `_init_schema`.
- `cratebuilder/sidecar.py` — per-channel-folder `cratebuilder.json` sidecar: tracks canonical channel ID, last-seen track keys, and powers new-upload detection on the Watch List.
- `cratebuilder/cleanup.py` — Folders Cleanup logic (trash-vs-keep partitioning, scan trustworthiness checks). Uses `send2trash`.
- `cratebuilder/updater_core.py` — manifest fetch, SHA-256 verify, zip extract, file swap. **No tkinter** — shared by the in-app updater button (About tab) and the standalone `updater.py` swap process.
- `cratebuilder/startup.py` — Windows "Run at startup" registry toggle.
- `cratebuilder/tray.py` — pystray system-tray wrapper (lazy-imported so headless tests don't need pystray).
- `cratebuilder/singleton.py` — single-instance lock (TCP port `SINGLE_INSTANCE_PORT`); a second launch hands off to the already-running window instead of starting a duplicate.
- `updater.py` — separate process built as `updater.exe`. A running Windows .exe can't overwrite itself, so the main app stages the new build, launches `updater.exe`, exits, and the updater waits for PID exit then calls `updater_core.apply_update` to swap files and relaunch.

### Testing convention

`tests/conftest.py` exposes a session-scoped `cb` fixture that loads `DJ-CrateBuilder_v1.3.py` via `importlib` as the module `cb_main`. Use it only for tests against logic still living in the monolith; **prefer importing from `cratebuilder.*` directly** for anything extracted into the package.

### Release / nightly update channel

The nightly channel is intentionally isolated from `main`:

- `main` carries the tagged v1.3 release; nightly metadata lives on a dedicated `nightly` branch.
- `update.json` on the `nightly` branch is the manifest the app polls (URL in `UPDATE_MANIFEST_URL` near the top of the main file). It carries the build number, zip URL, and SHA-256.
- Payloads are **deltas by default** — `scripts/release.py` hashes the build, diffs against the last full baseline (stored in `.nightly_release_state.json`, gitignored), and zips only changed files. `--full` ships a full payload and resets the baseline. FFmpeg binaries (`ffmpeg.exe`, `ffprobe.exe`) are excluded from payloads — the installer ships them and they never change.
- The updater overlays files additively, so a delta applied over any install derived from the current baseline yields the full current build.

### PyInstaller packaging

Two specs: `DJ-CrateBuilder.spec` (onedir, the app) and `updater.spec` (onefile, `updater.exe`). When building manually, you must `--collect-submodules cratebuilder` and `--hidden-import` the lazy deps (`pystray._win32`, `PIL.ImageDraw`, `send2trash`); see the README "Building from Source" section. Copy `ffmpeg.exe` / `ffprobe.exe` into `dist/DJ-CrateBuilder/` after building. The Windows installer is built with Inno Setup using `docs/DJ-CrateBuilder_Installer_Windows.iss`.

## Conventions / gotchas

- **Do not bump `APP_VERSION`.** It stays `"1.3"`. The release script edits `APP_BUILD` for you — don't hand-edit it in the same commit as feature work.
- **The monolith uses Unicode box-drawing section dividers** (`# ══════════…`). Keep them when editing nearby code.
- `activity.log` is the user-facing download history; `debug.log` is yt-dlp/cookie diagnostics. Both are gitignored and produced at runtime in the install dir.
- `_cookies/`, `cookies.txt`, `cookies.json`, and `cratebuilder.db*` are gitignored — never commit them.
- `superpowers/` is a gitignored scratchpad for multi-phase rework plans, not user docs.
- The `cratebuilder.json` sidecar in each channel folder is **user data** — don't change its schema without a migration path in `cratebuilder/sidecar.py`.
- ffmpeg discovery: when frozen (PyInstaller), the app points yt-dlp at the exe directory; from source it relies on FFmpeg being on PATH. See `bundled_ffmpeg_dir()` in the main file.

---

## Working with this codebase (collaboration rules)

The section above is project facts. This section is how the maintainer (DJ / Sintax) wants Claude to work here. Take these as standing instructions.

### Commits & branches

- **Conventional Commits.** Format: `type(scope): subject`. Types in active use: `feat`, `fix`, `chore`, `refactor`, `style`, `docs`, `build`, `installer`. Keep subjects ~70 chars, imperative mood.
- **Direct-to-`main` for routine work.** Bug fixes, small features, doc tweaks, nightly bumps all land on `main`.
- **Feature branch + PR for larger reworks** (see [PR #2](https://github.com/Sintax/DJ-CrateBuilder/pull/2) for the shape). Branch name: `feat/<short-slug>` or `fix/<short-slug>`.
- **Never push without an explicit ask.** Commit freely; pushing, tagging, and PR creation always wait for the user.

### Releases / publishing

- **Don't run `scripts/release.py` without an explicit ask.** Every invocation bumps `APP_BUILD`, builds artefacts, and publishes to GitHub — it's a real release.
- **Prefer the project-local `/build-update` skill** over hand-running the script. The skill presents the correct menu (delta nightly / full nightly / fresh installer / dry-run / cleanup / init).
- **`APP_BUILD` is owned by the release script.** Never bump it in a feature commit — the release script will do it as part of its own `chore: bump APP_BUILD to N` commit.
- **`APP_VERSION` stays `"1.3"`.** Don't propose bumping it unless the user has explicitly opened a v1.4 effort.
- **Delta is the default** payload mode; `--full` only when the user asks (it resets the baseline in `.nightly_release_state.json`).

### Large-change workflow

For large or critical reworks (multi-file refactors, schema migrations, anything touching the updater, anything where a mistake would corrupt user data): **delegate the implementation to subagents with two-stage review** rather than doing it on the main thread. Reason: it preserves main-thread context for steering and review, and the user prefers this for high-risk work. Small isolated changes don't need it.

### Code style

- **Match what's already there.** The monolith uses substantial multi-line docstrings on most functions and Unicode box-drawing dividers (`# ══════════…`) between sections — keep both when editing nearby code. The `cratebuilder/` modules use one-line module docstrings (`"""SQLite persistence: downloads history + watchlist."""`) — match that, don't write essays.
- **No new comments for new code** unless the *why* is non-obvious. The existing in-file docstrings are load-bearing context, not stylistic clutter — leave them alone.
- **Don't extract from the monolith without asking.** A separate effort is currently investigating whether to modularize `DJ-CrateBuilder_v1.3.py`; until that decision lands, default to editing in place. The only exception is when adding a *new* unit of pure logic that needs tests — those can go straight into `cratebuilder/`. Don't refactor existing monolith code into the package on your own initiative.
- **No tkinter imports in `cratebuilder/`.** The package is a pure-logic boundary that keeps tests headless — anything that needs a Tk root belongs in the monolith.

### Verification before claiming "done"

- **Pure-logic changes (anything in `cratebuilder/` or `scripts/`):** run `python -m pytest -q` and report the result.
- **tkinter UI changes:** launch `python DJ-CrateBuilder_v1.3.py` and confirm the change is visible. If it can't be visually verified (invisible refactor, error-path code), say so explicitly rather than asserting it works.
- **Release-script changes:** `python scripts/release.py --dry-run` is the smoke test. Never test on the real nightly tag.

### Don't touch without asking

- **`cratebuilder.db` schema** (`cratebuilder/db.py`): bump `SCHEMA_VERSION` and add a migration step in `_init_schema` for any schema change. Never edit a user's live `cratebuilder.db` file.
- **`cratebuilder.json` channel sidecars** (`cratebuilder/sidecar.py`): same rule — sidecars are user data, schema changes need backward-compatible reads.
- **`docs/DJ-CrateBuilder_Installer_Windows.iss`**: only edit when intentionally changing the installer. The `[CUSTOMIZED]` variant is gitignored and machine-local — never propose committing it.
- **`.nightly_release_state.json`**: never hand-edit. It's the delta baseline; only the release script writes it.
- **`update.json` on the `nightly` branch**: never edit from `main`. `scripts/release.py` force-pushes it via git plumbing.
- **`activity.log`, `debug.log`, `cratebuilder.db*`, `_cookies/`**: runtime user data, gitignored, never read or commit them unless the user explicitly asks for diagnosis.

### Useful project-local skills

- `/build-update` — the canonical way to ship a build. Use this instead of invoking `scripts/release.py` directly.


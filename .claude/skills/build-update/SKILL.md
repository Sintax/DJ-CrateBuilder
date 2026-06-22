---
name: build-update
description: Build and release DJ-CrateBuilder via scripts/release.py. Presents a menu to publish a delta nightly, a full nightly, a fresh Windows installer, or to dry-run/cleanup/init the nightly channel. Use when the user wants to ship a build, publish a nightly, cut a release, build an installer, or invokes /build-update.
---

# Build-Update

Drive `scripts/release.py` to build and release DJ-CrateBuilder. Run everything from the repo root `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder`. The agent runs the commands itself — never just hand the user a command to paste.

## Step 0 — Refuse to run outside the main parent repo

This skill **must only execute from the main parent repo** at `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder`. Running it from a git worktree would publish a release built off worktree state, bump `APP_BUILD` in the wrong checkout, and confuse the `.nightly_release_state.json` baseline.

**Before anything else,** run both checks below from the current working directory. **If either check fails, abort immediately** — do not present the menu, do not proceed to Step 1, do not offer a workaround. Tell the user where they are, where they need to be, and stop.

1. **Path check.** Resolve the current directory and compare to the canonical repo root:
   ```
   python -c "import os, sys; expected=os.path.normcase(os.path.realpath(r'C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder')); actual=os.path.normcase(os.path.realpath(os.getcwd())); sys.exit(0 if expected == actual else 1)"
   ```
   Non-zero exit ⇒ abort with: *"Refusing to run /build-update from `<cwd>` — this skill only runs from the main parent repo at `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder`. `cd` there and re-invoke."*

2. **Worktree check.** Even inside the right path, confirm git agrees this is the primary checkout — not a worktree that happens to share the path:
   ```
   git rev-parse --git-dir
   git rev-parse --git-common-dir
   ```
   The two outputs must be identical (both `.git` or both the same absolute path). If they differ, this is a worktree — abort with: *"Refusing to run /build-update from a git worktree. Switch to the main parent repo checkout and re-invoke."*

Only after both checks pass, continue to Step 1.

## Step 1 — Present the menu

Ask the user which path they want (use the clickable question UI):

| # | Path | Command | Outward-facing? |
|---|------|---------|-----------------|
| 1 | **Delta nightly** (normal) | `python scripts/release.py --notes "<notes>"` | Yes — uploads + pushes `nightly` |
| 2 | **Full nightly** (resets baseline) | `python scripts/release.py --full --notes "<notes>"` | Yes — uploads + pushes `nightly` |
| 3 | **Fresh installer** | `python scripts/release.py --build-only` → ISCC | No (local build) |
| 4 | **Dry run** | `python scripts/release.py --dry-run` | No (local only) |
| 5 | **Cleanup** | `python scripts/release.py --cleanup` | No (local only) |
| 6 | **Init nightly branch** | `python scripts/release.py --init` | Yes — creates/pushes `nightly` |

Use **Full nightly** after any change touching the bundled CPython runtime, dependencies, or `updater.exe`. Otherwise use **Delta nightly**.

## Step 2 — Gather notes (paths 1, 2, 4 only)

The script's `input()` prompt will HANG if run non-interactively, so always collect a one-line release-notes string from the user first and pass it via `--notes "<notes>"`. (`--dry-run` runs fine without notes, but ask anyway so the printed manifest is realistic.)

## Step 3 — Pre-flight (publish paths 1, 2 only)

Before any publish, verify:
- `gh auth status` succeeds. If not, stop and tell the user to run `gh auth login`.
- `origin/nightly` exists: `git ls-remote --heads origin nightly`. If empty, offer to run path 6 (`--init`) first.

## Step 4 — Confirm gate (outward-facing paths 1, 2, 6)

Show the exact command plus the resolved details — version + new build number (read `APP_VERSION`/`APP_BUILD` from the `DJ-CrateBuilder_v*.py` source), full vs delta, and the notes — then wait for explicit go-ahead. Local-only paths (3, 4, 5) run with no gate.

## Step 5 — Run it

Run the chosen command from the repo root and report the output.

### Installer path (3) specifics
1. Run `python scripts/release.py --build-only`.
2. Pick the `.iss` script to compile:
   - Prefer the user's custom script `docs/DJ-CrateBuilder_Installer_Windows [CUSTOMIZED].iss`.
   - **If that file does not exist**, ask the user whether they have their own customized `.iss` file to use (e.g. a path elsewhere on disk). If they provide one, use it. If they don't, fall back to the repo's `docs/DJ-CrateBuilder_Installer_Windows.iss` (note its install paths may be placeholders) or stop and let the user supply a script.
3. Locate `ISCC.exe` (try PATH, then `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`).
4. If found, offer to compile the chosen script:
   `& "<ISCC.exe>" "<chosen .iss>"`
5. If ISCC isn't found, hand off with manual Inno Setup steps (open the chosen `.iss` in the Inno Setup GUI and Build).

## Step 6 — Post-publish (after a successful path 1 or 2)

The script bumps `APP_BUILD` in the source file and leaves it uncommitted. **Auto-commit and push it:**
```
git add DJ-CrateBuilder_v*.py
git commit -m "chore: bump APP_BUILD to <N>"
git push origin HEAD
```
Use the new build number `<N>` from the release output. This pushes the current branch (`main`); the nightly channel itself was already updated by the script via the `nightly` branch.

## Notes
- The script never touches the working tree or `main` for the publish itself — only Step 6 commits the bump.
- `--dry-run` keeps artifacts; suggest path 5 (`--cleanup`) afterward if the user wants them gone.
- Full flag reference: `python scripts/release.py --help`.

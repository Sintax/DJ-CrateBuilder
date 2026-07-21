# Skip-Proof FFmpeg Self-Update — Design

**Date:** 2026-07-21
**Status:** Approved, ready for planning
**Branch:** `worktree-ffmpeg-self-update`
**Scope:** Let the app update its bundled `ffmpeg.exe`/`ffprobe.exe` independently of the app build, so a newer FFmpeg reaches every user regardless of which build they jump to. Delivered as a version-marked block in the existing `update.json` manifest (Option A), with the FFmpeg version derived automatically from the binary at release time.

---

## Problem

The bundled FFmpeg binaries are deliberately excluded from update payloads (`FFMPEG_BINS` in `scripts/release.py`) because they are large (~80–150 MB) and normally static. The installer ships them and the additive updater leaves them in place. There is currently **no path** for the app to receive a newer FFmpeg after install.

Naively piggybacking FFmpeg onto an app payload is *not* skip-proof. The delta system is cumulative from the last `--full` baseline, so a changed FFmpeg would ride every delta — until the next `--full`, which excludes FFmpeg and resets the baseline. Any user who had not yet caught the FFmpeg-carrying delta before that `--full` would miss it **permanently and silently**. The requirement is that a newer FFmpeg reaches a user no matter which build they land on.

## Requirement

The FFmpeg update decision must be based on **what is actually installed on disk vs what is currently offered**, never on build-number traversal. That makes it immune to skipped builds and to `--full` baseline resets.

---

## Architecture

FFmpeg becomes its own independently-versioned channel that travels inside the manifest the app already polls. Four moving parts:

1. **Manifest block** — an optional `ffmpeg` object in `update.json`.
2. **Installed-version marker** — `ffmpeg.version` on disk in the install dir.
3. **Decision + apply logic** — pure functions in `cratebuilder/updater_core.py`, unit-tested.
4. **Release-script support** — a mode that auto-derives the version, zips the binaries, uploads the asset, and merges the block into the manifest.

### 1. Manifest extension (backward compatible)

`update.json` gains an optional top-level `ffmpeg` object alongside the existing `build`/`url`/`sha256`:

```json
{
  "build": 40,
  "url": "https://github.com/.../build-40.zip",
  "sha256": "…",
  "ffmpeg": {
    "version": "7.0.2+a1b2c3d4",
    "url": "https://github.com/.../ffmpeg-7.0.2-a1b2c3d4.zip",
    "sha256": "…"
  }
}
```

- **Absent `ffmpeg` block** = the app does nothing FFmpeg-related. Every existing manifest and every older client keeps working untouched.
- `version` is an **opaque string** compared by **equality**, not ordering. "Installed ≠ offered → update." This also permits a deliberate rollback (ship an older version string and clients converge to it).
- The block is validated defensively, mirroring `validate_manifest`: it must be a dict with a non-empty `version`, a non-empty `url`, and a 64-char hex `sha256`. A malformed block is treated as "no FFmpeg info" — it can never crash the running app.

### 2. Installed-version marker

A small UTF-8 text file `ffmpeg.version` lives in the install directory next to `ffmpeg.exe`. It records the version string of the binaries currently on disk.

- **Written by the release script** into the build (and thus shipped by the installer), so fresh installs start with an accurate marker.
- **Rewritten by the app** after a successful swap.
- **Rollout safety (absent marker):** an already-installed user upgrading into the first build that understands this feature will have no marker yet. In that case the app **adopts** the manifest's currently-advertised version by writing the marker *without downloading* — it trusts the installer-shipped binary as current. This prevents the feature's debut from triggering a ~100 MB download for the entire existing user base. Only genuine future version bumps download.

### 3. Version auto-derivation (release time)

The maintainer never types a version. The release script runs the binary being packaged:

```
ffmpeg.exe -version
→ ffmpeg version 7.0.2-full_build-www.gyan.dev Copyright (c) 2000-2024 …
```

It parses the token after `ffmpeg version` and appends the first 8 hex chars of the `ffmpeg.exe` SHA-256, yielding e.g. `7.0.2+a1b2c3d4`. The version string is therefore:

- **Automatic** — derived from the actual binary.
- **Human-legible** — shows the upstream FFmpeg version.
- **Change-precise** — the hash suffix distinguishes two builds that share a version string but differ in bytes, and is identical for byte-identical binaries (so re-running with unchanged binaries is a no-op).

If parsing `-version` fails for any reason, the release script falls back to a pure `ffmpeg-<sha16>` string so the feature still functions without a legible version.

### 4. Applying the swap

Reuse the existing `download` / `verify_sha256` / `extract_zip` in `updater_core`. The FFmpeg zip contains `ffmpeg.exe` and `ffprobe.exe` at its root. Then swap them into the install dir.

The binaries are **not** locked by the main Python process — only by a *running* yt-dlp download/convert subprocess. Swap strategy:

- **Idle → swap in place.** Back up the existing binaries, copy the new ones over, rewrite `ffmpeg.version`. No app restart required (much lighter than the app updater's exit/relaunch dance). On any failure mid-swap, roll back to the backed-up originals.
- **Busy or locked → defer.** If a download is in progress, or an in-place swap hits a Windows sharing violation, abort this attempt cleanly and retry on the next update check. FFmpeg updates are rare and non-urgent; deferring is acceptable and avoids fighting a live file handle.

The `updater.exe` restart-based handoff is **not** used for FFmpeg — it is heavier than needed and the idle-swap path covers the normal case.

### 5. UI / flow (About tab)

The FFmpeg check piggybacks the existing About-tab update check (`fetch_manifest` is already called there). When a version bump is detected and the app is frozen/idle, surface it in the same update experience — an "FFmpeg update available (`<version>`)" line — and apply it in the same flow. Because idle swaps need no restart, the FFmpeg step is quieter than an app update (no relaunch prompt). Match the existing update-dialog patterns and copy.

---

## Scope boundaries

- **Windows (frozen) only for v1.** Linux `.deb` installs source FFmpeg from the system package, so the app no-ops there (guarded by `is_frozen()` / platform check). Running from source also no-ops (`bundled_ffmpeg_dir()` returns `None`; FFmpeg is on PATH and not ours to manage).
- **No new manifest, no new URL, no new poll, no manual button.** The check rides the existing update poll. A manual "Check FFmpeg now" button is a possible later addition; explicitly out of scope here.

## Release-script support (gitignored — local only)

`scripts/` is gitignored in this repo, so `scripts/release.py` changes **cannot** be committed to the feature branch/PR. They are applied directly to the maintainer's local `scripts/release.py` in the main checkout. This is expected and does not affect the committable app-side code.

New behavior (a `--ffmpeg` flag and/or `/build-update` menu item):

1. Run `<build>/ffmpeg.exe -version`, derive the version string (§3).
2. Short-circuit if the derived version already equals the manifest's current `ffmpeg.version` (safe no-op re-run).
3. Zip `ffmpeg.exe` + `ffprobe.exe` into `ffmpeg-<version>.zip`.
4. Upload it as an asset to the existing nightly GitHub release (same place app payloads live), reusing `gh_upload`/prune conventions.
5. Merge the `ffmpeg` block into `update.json` and publish it to the `nightly` branch via the existing git-plumbing path (`publish_manifest`), **preserving** the current `build`/`url`/`sha256` app fields.
6. Stamp `ffmpeg.version` into the build tree so fresh installs are marked.

The existing FFmpeg exclusion from *app* payloads stays exactly as-is. FFmpeg now has its own channel, which **permanently closes the `--full` laundering hole** because FFmpeg no longer depends on the app baseline at all.

`--dry-run` must exercise the version-derivation, zip, and manifest-merge paths without uploading or publishing.

---

## Backward compatibility & data safety

- Older clients ignore the `ffmpeg` block (they never read it). ✓
- New clients with no block present do nothing. ✓
- `ffmpeg.version` is a new app-managed file, not a schema change to `cratebuilder.db` or the `cratebuilder.json` sidecar — no migration needed. ✓
- The manifest merge preserves existing app-update fields, so publishing an FFmpeg update never disturbs the app-update channel and vice-versa. ✓

## Error handling

- Malformed/absent `ffmpeg` block → treated as "no update", never raises (mirrors `is_update_available`).
- Download/checksum failure → abort, leave existing binaries and marker untouched, retry next check.
- Swap failure mid-copy → roll back to backed-up originals; marker not advanced.
- Locked binaries (active download) → defer silently.

## Testing

- **Unit (pure, `updater_core`):** marker read/write; block validation (valid, missing fields, bad sha, non-dict); version-diff decision (equal, differ, absent-marker-adopt); swap apply + rollback with an injected copy failure (mirrors the `apply_update` `_copyfn` test pattern).
- **Release script:** `--dry-run` covers version derivation + zip + manifest merge without network. (Local/untracked — verified on the maintainer's machine, not in CI.)
- **UI:** manual `python DJ-CrateBuilder_v1.3.py` launch to confirm the About-tab messaging renders; the swap path itself is Windows-frozen-only and stated as not visually verifiable from source.

---

## File-by-file change list

**Committable (tracked, on the branch):**

- `cratebuilder/updater_core.py` — new pure functions: `validate_ffmpeg_block`, `ffmpeg_update_available`, `read_ffmpeg_version`, `write_ffmpeg_version`, `apply_ffmpeg_update` (download→verify→extract→swap→marker, with rollback).
- `DJ-CrateBuilder_v1.3.py` — wire the `ffmpeg` block into the existing About-tab update check; idle/busy guard; UI messaging; call into `updater_core`.
- `tests/test_updater_core.py` (or a new `tests/test_ffmpeg_update.py`) — unit tests for all new pure functions.

**Local only (gitignored, main checkout):**

- `scripts/release.py` — `--ffmpeg` mode: derive version, zip, upload, merge manifest block, stamp marker.
- `.claude/skills/build-update/SKILL.md` (if desired) — a menu entry for "Publish FFmpeg update". (Skill dir; confirm tracked status before editing.)

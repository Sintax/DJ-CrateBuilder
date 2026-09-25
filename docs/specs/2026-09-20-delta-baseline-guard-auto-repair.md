# Delta-baseline guard + auto-repair

**Date:** 2026-09-20
**Status:** approved for implementation

## Problem

A nightly delta payload only contains files that differ from the last **full**
build (the *baseline*, recorded as `base` in `update.json` and in
`.nightly_release_state.json`). The updater overlays additively, so a delta is
only complete when applied over an install that already has every file from the
baseline — i.e. an install whose build number is `>= base`.

Today nothing checks this. An install **older than the baseline** that applies a
delta ends up missing every file that changed *between that old build and the
baseline* and hasn't changed since — a silent, partial install. The manifest
already carries `base`, but no code reads it.

"Out of date" in the sense that matters = **`current_build < base`**. A full
publish sets `base == build`, so a full is always safe; only a *delta*
(`base < build`) landing on `current_build < base` is unsafe.

## Decisions (from the maintainer)

1. **Auto-repair** when too far behind: the app fetches the retained **full**
   build, installs it (→ reaches the baseline build), restarts, and picks up the
   newest changes on the next update check. Two hops, one restart between.
2. **Warn at ~25 builds**: the release tool warns in its pre-flight / build
   summary when the last full build is more than ~25 nightlies old and suggests
   shipping a full. It does **not** auto-force a full.

## What does NOT change

`updater.py` / `updater_core.apply_update` (the file-swap + relaunch core) is
untouched. The only change to the in-app apply path is **which payload URL + SHA
is downloaded**; download → SHA-256 verify → extract → hand-off is identical.

## Design

### 1. Pure guard functions — `cratebuilder/updater_core.py` (additive)

```python
def needs_full_reinstall(manifest, current_build):
    """True when the manifest offers a DELTA whose baseline is newer than the
    running build, so applying it would leave a partial install. Defensive:
    any missing/malformed field returns False (no guard) — a full payload
    (base == build) is always safe, so it also returns False."""
    if not isinstance(manifest, dict):
        return False
    try:
        build = int(manifest["build"])
        base = int(manifest["base"])
        current = int(current_build)
    except (KeyError, TypeError, ValueError):
        return False
    return base < build and current < base


def full_payload(manifest):
    """The retained full build's download, or None. Validates like
    validate_manifest: non-empty url, 64-hex sha256. build == base."""
    if not isinstance(manifest, dict):
        return None
    url = str(manifest.get("full_url", "")).strip()
    sha = str(manifest.get("full_sha256", "")).strip()
    if not url or len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        return None
    try:
        base = int(manifest["base"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"url": url, "sha256": sha, "build": base}
```

### 2. Service — `cratebuilder/service.py`

**`update_check`** result gains:

- `base`: `int(manifest["base"])` when parseable, else `None`.
- `needs_full`: `ucore.needs_full_reinstall(manifest, current)`. (Implies
  `available`, since it requires `current < base < build`.)
- `full_available`: `bool(needs_full and full_payload and not is_linux() and
  can_self_update())` — whether auto-repair can actually run.
- `installer_url`: `self._installer_url()` — platform release page for the
  fallback link (Windows `.../releases/tag/v2.0`, Linux `.../tag/linux-v2.0`).

Add a small `_installer_url()` helper (mirrors the URLs already hard-coded in the
`update_apply` Linux branch).

**`update_apply`** — where `dl_url`/`sha256` are currently read from the manifest,
select the payload:

```python
install_build = build
if ucore.needs_full_reinstall(manifest, current):
    fp = ucore.full_payload(manifest)          # is_linux()/source already raised above
    if not fp:
        raise CBError(
            "You're several builds behind, and the in-app updater can't safely "
            "bridge this gap. Download and run the latest installer instead:\n\n"
            + self._installer_url())
    dl_url, sha256, install_build = fp["url"], fp["sha256"], fp["build"]
else:
    dl_url, sha256 = manifest["url"], manifest["sha256"]
```

Use `install_build` (not `build`) for the staged zip name (`build-{install_build}.zip`),
the `update.restarting` event, and the activity-log line. For a full-repair do
**not** log the latest build's component diff (those components belong to the
latest build, not the baseline being installed) — log a plain "catching up to
build {install_build}; more on the next check" line. The `_require_idle_for_update`
gate and the cancel wiring are unchanged.

Keep the existing Linux and `can_self_update` refusals ahead of this block, so
`needs_full` handling is only reached on a Windows frozen install.

### 3. Release script — `scripts/release.py`

**Full-zip naming + retention.** Give full payloads a distinct infix so pruning
can tell them apart:

- Delta zip: `DJ-CrateBuilder-{version}.{build}.zip` (unchanged).
- Full zip: `DJ-CrateBuilder-full-{version}.{build}.zip`.

**Prune rules** (extend `prune_old_zip_assets` / `gh_upload` with an
`exclude_substr` param):

- Delta publish: keep the new delta AND the current full — prune old
  `DJ-CrateBuilder-*` **except** names containing `-full-`.
- Full publish: keep only the new full — prune all other `DJ-CrateBuilder-*`
  (old fulls and old deltas). `ffmpeg-*` is never touched (separate prefix).

**Manifest fields.** Add `full_url`, `full_sha256`:

- Full publish: point at the just-uploaded full zip; `base == build`; write
  `full_url`/`full_sha256` into `.nightly_release_state.json` via `save_state`.
- Delta publish: read `full_url`/`full_sha256` from state (written by the last
  full). If state lacks them, carry forward from the existing live manifest. If
  still absent, omit them (app falls back to the installer link — expected until
  the first full ships after this change).

**Baseline-age warning.** `FULL_BASELINE_WARN = 25`. In `_preflight` and in the
build summary before the confirm gate: when `state` exists, the payload is a
delta, and `current_build - base_build >= FULL_BASELINE_WARN`, print a clear
warning recommending a `--full` publish. Surfaced so `/build-update` relays it in
plain language.

### 4. Web UI — `web/app.js`

In `renderUpdateControls` / `aboutUpdateStatusLine` / the confirm modal, when
`result.needs_full`:

- **`full_available` true** — Update Now stays enabled; the confirm modal gains a
  note: "You're several builds behind. This installs build {base} first and
  restarts, then offers the rest on the next check." Status line says similar.
- **`full_available` false** — replace Update Now with a **"Get the full
  installer"** button that opens `result.installer_url`; status line explains a
  reinstall is needed.

Follow the `changing-the-web-ui` skill: colours via `--cb-*` tokens, reach the
host only via `cbApi`, tests slice functions verbatim.

## Tests

- `tests/test_updater_core.py`: `needs_full_reinstall` (full always safe;
  delta safe when `current >= base`; delta unsafe when `current < base`; missing
  `base`; malformed). `full_payload` (valid; missing; bad sha; build == base).
- `tests/test_service_update.py`: `update_check` reports `base`/`needs_full`/
  `full_available`/`installer_url`; `update_apply` downloads the full payload
  when `needs_full` + full available; raises with the installer link when
  `needs_full` + no full payload.
- `tests/test_release.py` / `tests/test_release_preflight.py`: manifest carries
  `full_url`/`full_sha256` on a full and echoes them on a delta; full-zip naming;
  prune keeps the full on a delta and removes it on a full; baseline-age warning
  fires past the threshold.
- `tests/test_web_about_client.py`: two-step confirm note when `full_available`;
  installer-link button when not.

## Migration / rollout note

`full_url`/`full_sha256` first appear in the manifest on the **next `--full`
publish** after this ships. Until then the guard still protects users (it just
uses the installer-link fallback instead of auto-repair). Recommend shipping a
`--full` nightly soon after merging so auto-repair goes live.

# Skip-Proof FFmpeg Self-Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (inline execution chosen). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the app update its bundled `ffmpeg.exe`/`ffprobe.exe` independently of the app build, driven by installed-on-disk vs offered version so it survives skipped builds and `--full` resets.

**Architecture:** An optional `ffmpeg` block in the existing `update.json` carries version/url/sha256. The app compares that against a local `ffmpeg.version` marker and, when they differ and the app is idle, downloads a small FFmpeg-only zip and swaps the binaries in place (reusing the tested `apply_update` overlay + rollback). All new decision/apply logic is pure and lives in `cratebuilder/updater_core.py`.

**Tech Stack:** Python 3.10+, stdlib only (`hashlib`, `zipfile`, `urllib`, `subprocess`), pytest, tkinter (GUI wiring only).

## Global Constraints

- No tkinter imports in `cratebuilder/` — pure logic only.
- Version comparison is by **equality** on an opaque string, never ordering.
- Absent marker ⇒ **adopt** the offered version without downloading (trust installer-shipped binary).
- Malformed/absent `ffmpeg` block ⇒ treated as "no update", never raises.
- FFmpeg self-update is **Windows-frozen only**; no-op from source and on Linux.
- `scripts/` and `CLAUDE.md` are gitignored — `scripts/release.py` changes are applied to the local main checkout, NOT committed on this branch.
- Match monolith style (multi-line docstrings, `# ══…` dividers); `cratebuilder/` uses one-line module docstrings.
- Commit messages: Conventional Commits, end with the `Co-Authored-By: Claude Opus 4.8 (1M context)` trailer.

---

### Task 1: Manifest block validation + version marker (pure)

**Files:**
- Modify: `cratebuilder/updater_core.py` (add functions near `validate_manifest`)
- Test: `tests/test_ffmpeg_update.py` (create)

**Interfaces:**
- Produces:
  - `FFMPEG_VERSION_FILE = "ffmpeg.version"`
  - `validate_ffmpeg_block(block) -> (bool, str)`
  - `read_ffmpeg_version(install_dir) -> str | None`
  - `write_ffmpeg_version(install_dir, version) -> str` (returns the path written)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ffmpeg_update.py
import os
from cratebuilder import updater_core as uc


def test_validate_ffmpeg_block_accepts_good_block():
    ok, reason = uc.validate_ffmpeg_block(
        {"version": "7.0.2+a1b2c3d4", "url": "http://x/z.zip", "sha256": "a" * 64})
    assert ok and reason == ""


def test_validate_ffmpeg_block_rejects_bad():
    assert uc.validate_ffmpeg_block(None)[0] is False
    assert uc.validate_ffmpeg_block({})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "", "url": "u", "sha256": "a" * 64})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "v", "url": "", "sha256": "a" * 64})[0] is False
    assert uc.validate_ffmpeg_block(
        {"version": "v", "url": "u", "sha256": "xyz"})[0] is False


def test_version_marker_roundtrip(tmp_path):
    assert uc.read_ffmpeg_version(str(tmp_path)) is None
    uc.write_ffmpeg_version(str(tmp_path), "7.0.2+a1b2c3d4")
    assert uc.read_ffmpeg_version(str(tmp_path)) == "7.0.2+a1b2c3d4"
    # blank file reads as None
    open(os.path.join(str(tmp_path), uc.FFMPEG_VERSION_FILE), "w").close()
    assert uc.read_ffmpeg_version(str(tmp_path)) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_ffmpeg_update.py -q`
Expected: FAIL (AttributeError: module has no attribute `validate_ffmpeg_block`)

- [ ] **Step 3: Implement in `cratebuilder/updater_core.py`**

Add after `validate_manifest` (around line 133):

```python
FFMPEG_VERSION_FILE = "ffmpeg.version"


def validate_ffmpeg_block(block):
    """Return (ok, reason) for the optional manifest ``ffmpeg`` sub-block.

    Mirrors ``validate_manifest``: any non-dict, empty version/url, or malformed
    sha256 is reported (ok=False) rather than raised, so a bad block is treated
    as "no ffmpeg update" and can never crash the running app.
    """
    if not isinstance(block, dict):
        return False, "ffmpeg block is not a JSON object"
    if not str(block.get("version", "")).strip():
        return False, "ffmpeg version is empty"
    if not str(block.get("url", "")).strip():
        return False, "ffmpeg url is empty"
    sha = str(block.get("sha256", "")).strip()
    if len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        return False, "ffmpeg sha256 is not a 64-character hex digest"
    return True, ""


def read_ffmpeg_version(install_dir):
    """Return the recorded on-disk ffmpeg version string, or None if absent/blank."""
    path = os.path.join(install_dir, FFMPEG_VERSION_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_ffmpeg_version(install_dir, version):
    """Record the installed ffmpeg version (write-then-rename for atomicity)."""
    path = os.path.join(install_dir, FFMPEG_VERSION_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(version).strip())
    os.replace(tmp, path)
    return path
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_ffmpeg_update.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/updater_core.py tests/test_ffmpeg_update.py
git commit -m "feat(updater): validate ffmpeg manifest block + version marker"
```

---

### Task 2: Update-action classifier (pure)

**Files:**
- Modify: `cratebuilder/updater_core.py`
- Test: `tests/test_ffmpeg_update.py`

**Interfaces:**
- Consumes: `validate_ffmpeg_block` (Task 1)
- Produces: `ffmpeg_update_action(manifest, installed_version) -> "none" | "adopt" | "update"`

- [ ] **Step 1: Write the failing tests**

```python
def _manifest(ver):
    return {"build": 40, "url": "u", "sha256": "a" * 64,
            "ffmpeg": {"version": ver, "url": "http://x/z.zip", "sha256": "b" * 64}}


def test_action_none_without_block():
    assert uc.ffmpeg_update_action({"build": 40}, None) == "none"
    assert uc.ffmpeg_update_action("nope", "7.0") == "none"


def test_action_adopt_when_no_marker():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+aa"), None) == "adopt"


def test_action_update_when_differs():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+bb"), "7.0.2+aa") == "update"


def test_action_none_when_matches():
    assert uc.ffmpeg_update_action(_manifest("7.0.2+aa"), "7.0.2+aa") == "none"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_ffmpeg_update.py -k action -q`
Expected: FAIL (no attribute `ffmpeg_update_action`)

- [ ] **Step 3: Implement**

Add after `write_ffmpeg_version`:

```python
def ffmpeg_update_action(manifest, installed_version):
    """Classify the ffmpeg state as 'none', 'adopt', or 'update'.

    'none'   → no valid ffmpeg block, or the installed marker already matches.
    'adopt'  → valid block but no local marker yet: the caller records the
               offered version WITHOUT downloading (the installer-shipped binary
               is trusted as current), so the feature's debut doesn't force a
               large download on every existing install.
    'update' → valid block and the installed marker differs from the offer.

    Decision is installed-vs-offered only — never build-number based — so it is
    immune to skipped builds and to --full baseline resets.
    """
    if not isinstance(manifest, dict):
        return "none"
    block = manifest.get("ffmpeg")
    ok, _reason = validate_ffmpeg_block(block)
    if not ok:
        return "none"
    offered = str(block["version"]).strip()
    if installed_version is None:
        return "adopt"
    return "update" if str(installed_version).strip() != offered else "none"
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_ffmpeg_update.py -q`
Expected: PASS (7 tests total)

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/updater_core.py tests/test_ffmpeg_update.py
git commit -m "feat(updater): classify ffmpeg update action (none/adopt/update)"
```

---

### Task 3: Install-from-zip (verify → extract → swap → mark), pure

**Files:**
- Modify: `cratebuilder/updater_core.py`
- Test: `tests/test_ffmpeg_update.py`

**Interfaces:**
- Consumes: `verify_sha256`, `extract_zip`, `apply_update`, `purge_dir`, `write_ffmpeg_version` (existing + Task 1)
- Produces: `install_ffmpeg_from_zip(zip_path, expected_sha, install_dir, staged_dir, backup_dir, version) -> True`

- [ ] **Step 1: Write the failing tests**

```python
import zipfile
from cratebuilder.updater_core import sha256_file


def _make_ffmpeg_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ffmpeg.exe", b"NEW-FFMPEG")
        zf.writestr("ffprobe.exe", b"NEW-FFPROBE")
    return path


def test_install_ffmpeg_swaps_and_marks(tmp_path):
    install = tmp_path / "app"; install.mkdir()
    (install / "ffmpeg.exe").write_bytes(b"OLD")
    (install / "ffprobe.exe").write_bytes(b"OLD")
    zip_path = _make_ffmpeg_zip(str(tmp_path / "f.zip"))
    sha = sha256_file(zip_path)

    uc.install_ffmpeg_from_zip(
        zip_path, sha, str(install),
        str(tmp_path / "staged"), str(tmp_path / "backup"), "7.0.2+aa")

    assert (install / "ffmpeg.exe").read_bytes() == b"NEW-FFMPEG"
    assert (install / "ffprobe.exe").read_bytes() == b"NEW-FFPROBE"
    assert uc.read_ffmpeg_version(str(install)) == "7.0.2+aa"


def test_install_ffmpeg_rejects_bad_checksum(tmp_path):
    install = tmp_path / "app"; install.mkdir()
    zip_path = _make_ffmpeg_zip(str(tmp_path / "f.zip"))
    import pytest
    with pytest.raises(ValueError):
        uc.install_ffmpeg_from_zip(
            zip_path, "0" * 64, str(install),
            str(tmp_path / "staged"), str(tmp_path / "backup"), "7.0.2+aa")
    # marker not advanced on failure
    assert uc.read_ffmpeg_version(str(install)) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_ffmpeg_update.py -k install_ffmpeg -q`
Expected: FAIL (no attribute `install_ffmpeg_from_zip`)

- [ ] **Step 3: Implement**

Add after `apply_update`:

```python
def install_ffmpeg_from_zip(zip_path, expected_sha, install_dir,
                            staged_dir, backup_dir, version):
    """Verify, extract, and swap a downloaded ffmpeg zip into ``install_dir``.

    The zip carries ``ffmpeg.exe``/``ffprobe.exe`` at its root. We reuse the
    tested ``apply_update`` overlay (per-file move-aside + copy, with rollback)
    so a swap that fails mid-way — e.g. a binary locked by a running yt-dlp
    subprocess raising a Windows sharing violation — rolls back cleanly. The
    version marker is only advanced after a fully successful swap.

    The caller is responsible for downloading the zip first (existing
    ``download``) and for only calling this when the app is idle. Raises on
    checksum/extract/swap failure; returns True on success.
    """
    if not verify_sha256(zip_path, expected_sha):
        raise ValueError("ffmpeg checksum mismatch — download may be corrupt")
    purge_dir(staged_dir)
    extract_zip(zip_path, staged_dir)
    apply_update(staged_dir, install_dir, backup_dir)
    write_ffmpeg_version(install_dir, version)
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_ffmpeg_update.py -q`
Expected: PASS (9 tests total)

- [ ] **Step 5: Full suite regression**

Run: `python -m pytest -q`
Expected: same baseline (only the pre-existing `test_settings_vars` env failure).

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/updater_core.py tests/test_ffmpeg_update.py
git commit -m "feat(updater): install ffmpeg from verified zip with rollback"
```

---

### Task 4: GUI wiring on the About-tab update check (manual verify)

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py` — `_on_check_result` (line ~7149) and a new helper `_maybe_update_ffmpeg`

**Interfaces:**
- Consumes: `ucore.ffmpeg_update_action`, `ucore.read_ffmpeg_version`, `ucore.write_ffmpeg_version`, `ucore.install_ffmpeg_from_zip`, `ucore.download`, `bundled_ffmpeg_dir`

**Design notes (no unit test — tkinter):**
- The ffmpeg check must run whenever the manifest is valid, **independent of the app-build branch** — a user on the latest build can still need an ffmpeg bump. So call `self._maybe_update_ffmpeg(manifest, manual)` right after `validate_manifest` succeeds (after line 7174), before the `is_update_available` early-returns.
- Guard to Windows-frozen: `if not ucore.is_frozen() or ucore.is_linux(): return`. Install dir = `bundled_ffmpeg_dir()`; if `None`, return.
- `action = ffmpeg_update_action(manifest, read_ffmpeg_version(install_dir))`.
  - `"adopt"` → `write_ffmpeg_version(install_dir, manifest["ffmpeg"]["version"])`; return (silent).
  - `"none"` → return.
  - `"update"` → if `self._downloading or self._wl_download_active or self._wl_scan_active`: defer (return, retries next check). Else run the download+swap on a background thread, then `write` marker; surface a small status line ("FFmpeg updated to `<version>`"). On any exception, log and leave state untouched (retry next check).

- [ ] **Step 1: Add the helper method** (near the update-flow methods, ~line 7190). Full method:

```python
    def _maybe_update_ffmpeg(self, manifest, manual):
        """Piggyback the update check: bring bundled FFmpeg to the offered version.

        Skip-proof by construction — the decision is the on-disk ``ffmpeg.version``
        marker vs the manifest's offered version, never the app build. Windows
        packaged builds only; a source/Linux run manages FFmpeg elsewhere and
        no-ops here. Runs the swap only while idle so it never fights a live
        yt-dlp file handle; otherwise it defers to the next check.
        """
        if not ucore.is_frozen() or ucore.is_linux():
            return
        install_dir = bundled_ffmpeg_dir()
        if not install_dir:
            return
        action = ucore.ffmpeg_update_action(
            manifest, ucore.read_ffmpeg_version(install_dir))
        block = manifest.get("ffmpeg") or {}
        version = str(block.get("version", "")).strip()
        if action == "adopt":
            try:
                ucore.write_ffmpeg_version(install_dir, version)
            except OSError:
                pass
            return
        if action != "update":
            return
        if self._downloading or self._wl_download_active or self._wl_scan_active:
            return   # busy — retry on the next scheduled check
        self._start_ffmpeg_update(install_dir, block, version)

    def _start_ffmpeg_update(self, install_dir, block, version):
        """Download + swap the FFmpeg binaries on a background thread."""
        ws = ucore.default_workspace()

        def worker():
            try:
                zip_path = os.path.join(ws, f"ffmpeg-{version}.zip")
                ucore.download(block["url"], zip_path)
                ucore.install_ffmpeg_from_zip(
                    zip_path, block["sha256"], install_dir,
                    os.path.join(ws, "ffmpeg_staged"),
                    os.path.join(ws, "ffmpeg_backup"), version)
                self.after(0, lambda: self._set_update_status(
                    f"FFmpeg updated to {version}."))
            except Exception as exc:   # noqa: BLE001 — retry next check
                log_debug(f"FFmpeg self-update deferred/failed: {exc}")
            finally:
                ucore.purge_dir(ws)

        threading.Thread(target=worker, daemon=True).start()
```

- [ ] **Step 2: Call it from `_on_check_result`** — insert after the `validate_manifest` block (after line 7174, before the `is_update_available` check at 7176):

```python
        self._maybe_update_ffmpeg(manifest, manual)
```

- [ ] **Step 3: Verify the exact log/util helper name.** Confirm `log_debug` exists (grep); if the debug logger has a different name, use it. If `self._wl_download_active` / `self._wl_scan_active` are named differently, match the real attributes (they exist per grep at lines 6786/6823).

- [ ] **Step 4: Launch smoke test**

Run: `python DJ-CrateBuilder_v1.3.py`
Expected: app starts, About tab renders, "Check for updates" still works (from source `is_frozen()` is False, so the ffmpeg path no-ops — confirms it doesn't break the existing flow). State this is the limit of source-side verification; the swap itself is frozen-only.

- [ ] **Step 5: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(updater): apply skip-proof FFmpeg updates on the update check"
```

---

### Task 5: Release-script FFmpeg publish mode (LOCAL / untracked)

**Files:**
- Modify (local main checkout only — gitignored, NOT on this branch):
  `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder\scripts\release.py`

**Design notes:**
- New `--ffmpeg` flag. Steps: derive version from `dist/DJ-CrateBuilder/ffmpeg.exe`; short-circuit if it equals the manifest's current `ffmpeg.version`; zip the two binaries; `gh_upload` to the nightly release; fetch current `update.json`, merge/replace the `ffmpeg` block (preserving `build`/`url`/`sha256`); `publish_manifest`; write `ffmpeg.version` into the build tree.
- Version derivation (add near the hash helpers):

```python
def derive_ffmpeg_version(ffmpeg_exe):
    """Auto-derive an opaque ffmpeg version: '<upstream>+<sha8>' from the binary.

    Parses `ffmpeg -version` for the upstream token and appends the first 8 hex
    of the exe's SHA-256 so byte-identical binaries map to the same version
    (re-run = no-op) and different builds always differ. Falls back to a pure
    hash string if `-version` can't be parsed.
    """
    import re
    digest = _sha256(ffmpeg_exe)
    try:
        out = subprocess.run([ffmpeg_exe, "-version"],
                             capture_output=True, text=True, timeout=15)
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        m = re.match(r"ffmpeg version (\S+)", first)
        if m:
            return f"{m.group(1)}+{digest[:8]}"
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return f"ffmpeg-{digest[:16]}"
```

- [ ] **Step 1: Implement `--ffmpeg` mode** in the local `scripts/release.py` per the design (§Release-script support). Reuse `gh_upload`, `publish_manifest`, `_sha256`.

- [ ] **Step 2: Dry-run smoke test** (requires a built `dist/DJ-CrateBuilder/ffmpeg.exe`):

Run: `python scripts/release.py --ffmpeg --dry-run`
Expected: prints derived version, builds `ffmpeg-<version>.zip` locally, prints the merged manifest JSON, uploads/publishes nothing.

- [ ] **Step 3: No commit on this branch.** This file is gitignored; the change lives on the maintainer's disk. Note it in the PR/summary so the maintainer knows the release side is applied locally.

---

### Task 6: Docs + wrap-up

- [ ] **Step 1: README note** (tracked) — one line under the update/FFmpeg section that FFmpeg now self-updates on Windows packaged builds. Confirm the relevant README section exists before editing.
- [ ] **Step 2: Full suite** `python -m pytest -q` — confirm baseline (only the pre-existing env failure).
- [ ] **Step 3: Commit** `git commit -m "docs(readme): note FFmpeg self-update on packaged builds"`.
- [ ] **Step 4: Finish** — invoke `superpowers:finishing-a-development-branch` to choose merge/PR.

---

## Self-Review

**Spec coverage:** manifest block → T1/T2; marker + adopt → T1/T2/T4; auto-version → T5; apply/swap/rollback → T3; idle/busy guard + Windows-only + UI → T4; release publish → T5; backward-compat (absent block no-ops) → T2/T4; testing → T1-T3 unit + T4/T5 manual. No gaps.

**Placeholder scan:** all code steps carry full code; the only deferred verifications (T4 Step 3, T5, T6 README) are real "confirm the exact existing name" checks, not hand-waves.

**Type consistency:** `ffmpeg_update_action` returns the same `"none"/"adopt"/"update"` strings consumed in T4; `install_ffmpeg_from_zip` signature is identical in T3 test, T3 impl, and the T4 call site; `FFMPEG_VERSION_FILE` defined once in T1, reused via `read/write_ffmpeg_version`.

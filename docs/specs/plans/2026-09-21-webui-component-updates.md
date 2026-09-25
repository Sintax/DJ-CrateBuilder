# Web-UI Component Updates (FFmpeg) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repo is indexed by `graft/` — locate code with `graft grep "<symbol>"` / `graft ask "<question>" --source` before opening any file; the `file:line` spans below were taken from graft on 2026-09-21 and may drift a few lines as earlier tasks land. **Dispatch subagents one at a time** (memory: parallel dispatch crashed the machine on 2026-09-15). Every task that touches `web/` must run **all** of `python -m pytest -q tests/test_web_*_client.py`, not just the About file. Read `.claude/skills/changing-the-web-ui/SKILL.md` before any `web/` edit.

**Goal:** The shipped web app installs the FFmpeg the nightly manifest offers — during an app update (between staging and restart, with per-component progress in the update modal) and on its own when the app is already current — and the activity log only ever records an FFmpeg change that actually happened.

**Architecture:** The decision and swap helpers already exist and are tested in `cratebuilder/updater_core.py` (`ffmpeg_update_action`, `install_ffmpeg_from_zip`). This plan adds (1) one pure helper that turns a manifest into a component plan, (2) a component phase inside `CrateBuilderService.update_apply`'s worker plus a standalone `update.components_apply` method, both emitting a new `update.component` event, and (3) component rows in the existing "Updating DJ-CrateBuilder" modal plus an "Update FFmpeg" button on the Update page. No schema changes, no new dependencies. Remote browsers never see any of it (the `update.` prefix is already `LOCAL_ONLY`).

**Tech Stack:** Python 3.10+ stdlib, pytest; plain JS in `web/app.js` tested by text-slicing under `node`.

**Spec:** `docs/specs/2026-09-21-ffmpeg-not-updated-by-web-app-analysis.md` (the root cause) plus the *Design decisions* table below (agreed with the maintainer 2026-09-21).

## Global Constraints

- `APP_VERSION` stays `"2.1"`; never touch `APP_BUILD`.
- No tkinter imports in `cratebuilder/`. The monolith (`DJ-CrateBuilder_v2.0.py`) is **not** edited — its `_maybe_update_ffmpeg` stays as the retired reference.
- `cratebuilder/ui_strings.py` is generated: edit `UI-design/ui-contract.json`, then `python scripts/gen_ui_strings.py`.
- Frontend talks to the host only through `cbApi` / `call()`; live updates only through `cbApi.on(...)`. Never poll.
- Colours via existing `--cb-*` tokens only; this plan adds no new token and no new CSS rule.
- Conventional Commits, direct to `main`, **never push**.
- Subagent model floor: `sonnet` for single-file pure-logic tasks; `opus` for anything touching `service.py`, `web/app.js`, event wiring, or the updater, and for every reviewer.
- Do not run the full `python -m pytest -q` as routine validation (standing order 2026-08-29); run the targeted files named in each task. Web tasks run all `tests/test_web_*_client.py`.

## Design decisions (the spec)

| # | Decision | Chosen |
|---|---|---|
| D1 | When FFmpeg is swapped during an app update | Inside `update_apply`'s worker, **after** the app payload is staged and **before** `update.restarting`. The app is already idle (the existing `_require_idle_for_update` guard), so no yt-dlp holds the binary. Skipped on a full-repair hop (that build's components don't describe the baseline being installed). |
| D2 | What a failed FFmpeg swap does to the app update | Nothing. The app update still hands off and restarts. The failure is shown in the modal, logged to `activity.log` as its own line, and retried on the next check/update. |
| D3 | Cancel during the FFmpeg phase | Honoured through the FFmpeg download (same `cancel` event as the app download). Once the swap itself starts it is not interruptible (it takes under a second and rolls back on its own). A cancel after a successful swap leaves the new FFmpeg in place — the marker is correct, so nothing is inconsistent. |
| D4 | Standalone path (app current, FFmpeg stale) | New LOCAL-only method `update.components_apply`. The Update page shows an **Update FFmpeg** button whenever the last check reported `components_update` non-empty and no app update is available. Same modal, titled "Updating components", no app progress bar, no restart. This is what makes `scripts/release.py --ffmpeg` (FFmpeg-only nightlies) reach existing installs. |
| D5 | Decision rule | Unchanged: `ucore.ffmpeg_update_action(manifest, marker, reported_build=probe)`. Skip-proof: it compares the on-disk `ffmpeg.version` marker (and the binary's own `-version` report) with the manifest's `ffmpeg.version`, never the app build. `"adopt"` (no marker yet) writes the marker at check time exactly as the monolith did. |
| D6 | Where FFmpeg lives | `ucore.install_dir()` — the same folder the updater overlays. On a frozen Windows install this is the exe directory, which is also where `bundled_ffmpeg_dir()` points. Gated on `ucore.is_frozen() and not ucore.is_linux()`. |
| D7 | Probe caching | `ffmpeg.exe -version` is spawned at most once per session (`_ffmpeg_probe_cache`), cleared after a swap — mirrors the monolith's `_installed_ffmpeg_build`. |
| D8 | Events | New `update.component` (coalesced, like `update.progress`) with `{key, label, phase, pct, done_mb, total_mb, version, error}`; phases `download`, `verify`, `install`, `done`, `failed`. `update.progress` gains phase `components` (emitted once, only when at least one component will be updated). Standalone completion rides on the existing `job.finished` for the `update` job. |
| D9 | Activity log | The build's `UPDATED` line no longer lists a component this run handled itself. A successful swap writes `UPDATED     \| Component: FFmpeg \| 9.0.1… -> 9.0.2…`; a failed one writes `UPDATE-FAIL \| Component: FFmpeg \| Wanted: 9.0.2… \| Reason: …`. Neither trips the viewer's DOWNLOADED/SKIPPED/ERROR filters. |
| D10 | Docs | The "FFmpeg piggyback stays tkinter-only" section of `docs/specs/2026-08-29-webui-local-updater-design.md` is retracted with a pointer here; the analysis note gets a "Fixed by" line. |

## File map

| File | Change |
|---|---|
| `cratebuilder/updater_core.py` | Add `ffmpeg_update_plan(manifest, install_dir, reported_build=None)` next to `ffmpeg_update_action` (L257-L299). |
| `cratebuilder/activitylog.py` | Add `component_updated(label, old, new)` and `component_failed(label, wanted, reason)` after `catching_up` (L46-L52). |
| `cratebuilder/events.py` | Add `"update.component"` to `DEFAULT_COALESCED_TYPES` (L6-L7). |
| `cratebuilder/service.py` | `__init__` (L852-L967): `_ffmpeg_probe_cache = False`. New `_ffmpeg_plan`, `_run_component_updates`. `update_check` (L3162-L3232): `components_update` + adopt. `update_apply` worker (L3313-L3418): component phase. New `update_components_apply` + `_methods()` entry. |
| `web/app.js` | `aboutBeginApply` (L6773-L6808) takes `opts`; new `aboutPaintComponent`, `aboutSwapCancelForClose`; `aboutPaintApplyProgress` (L6820-L6834) handles `components`; `aboutSettleApply` (L6861-L6881) branches on `componentsOnly`; new `aboutStartComponentsApply`; `renderUpdateControls` (L6912-L7062) adds the button; `subscribeUpdateEvents` (L8052-L8085) subscribes `update.component` and reacts to `components_update` on `update.checked`. |
| `UI-design/ui-contract.json` → `cratebuilder/ui_strings.py` | Tooltip `about.update_components`. |
| `tests/test_ffmpeg_update.py`, `tests/test_activitylog.py`, `tests/test_events.py` (create if absent), `tests/test_service_update.py`, `tests/test_web_about_client.py` | Tests per task. |
| `docs/specs/2026-08-29-webui-local-updater-design.md`, `docs/specs/2026-09-21-ffmpeg-not-updated-by-web-app-analysis.md` | Retraction + status. |

---

### Task 1: `ffmpeg_update_plan` — one call from manifest to "what to do and from where"

**Model:** sonnet — single pure function in one file, tests pin every branch.

**Files:**
- Modify: `cratebuilder/updater_core.py` (insert directly after `ffmpeg_update_action`, which ends at L299)
- Test: `tests/test_ffmpeg_update.py`

**Interfaces:**
- Consumes: `ffmpeg_update_action`, `read_ffmpeg_version`, `validate_ffmpeg_block` (all existing in the same file).
- Produces: `ffmpeg_update_plan(manifest, install_dir, reported_build=None) -> dict` with keys `action` (`"none" | "adopt" | "update"`), and for `adopt`/`update` also `version`, `url`, `sha256` (all `str`). Tasks 3 and 4 call this.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ffmpeg_update.py`; `_manifest(version)` and `uc` already exist there)

```python
def test_plan_is_none_without_a_valid_block(tmp_path):
    assert uc.ffmpeg_update_plan({"build": 40}, str(tmp_path)) == {"action": "none"}
    assert uc.ffmpeg_update_plan("nope", str(tmp_path)) == {"action": "none"}


def test_plan_adopts_when_no_marker_and_carries_the_coordinates(tmp_path):
    plan = uc.ffmpeg_update_plan(_manifest("7.0.2+aa"), str(tmp_path))
    assert plan["action"] == "adopt"
    assert plan["version"] == "7.0.2+aa"
    assert plan["url"] == _manifest("7.0.2+aa")["ffmpeg"]["url"]
    assert plan["sha256"] == _manifest("7.0.2+aa")["ffmpeg"]["sha256"]


def test_plan_updates_when_the_marker_differs(tmp_path):
    uc.write_ffmpeg_version(str(tmp_path), "7.0.2+aa")
    plan = uc.ffmpeg_update_plan(_manifest("7.0.2+bb"), str(tmp_path))
    assert plan["action"] == "update"
    assert plan["version"] == "7.0.2+bb"


def test_plan_is_none_when_the_marker_matches(tmp_path):
    uc.write_ffmpeg_version(str(tmp_path), "7.0.2+aa")
    assert uc.ffmpeg_update_plan(_manifest("7.0.2+aa"), str(tmp_path)) == {"action": "none"}


def test_plan_trusts_the_binary_over_a_lying_marker(tmp_path):
    uc.write_ffmpeg_version(str(tmp_path), "7.0.2+aa")
    plan = uc.ffmpeg_update_plan(_manifest("7.0.2+aa"), str(tmp_path),
                                 reported_build="6.1.1")
    assert plan["action"] == "update"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_ffmpeg_update.py -q -k plan`
Expected: FAIL with `AttributeError: module 'cratebuilder.updater_core' has no attribute 'ffmpeg_update_plan'`

- [ ] **Step 3: Implement**

Insert after `ffmpeg_update_action` (after L299) in `cratebuilder/updater_core.py`:

```python
def ffmpeg_update_plan(manifest, install_dir, reported_build=None):
    """``ffmpeg_update_action`` plus the download coordinates, in one shape.

    Returns ``{"action": "none"}`` when nothing is offered or the install is
    already current, otherwise ``{"action", "version", "url", "sha256"}`` —
    the strings the caller downloads, verifies and marks with. The block is
    only read once ``ffmpeg_update_action`` has validated it, so a malformed
    manifest never reaches the download.
    """
    action = ffmpeg_update_action(manifest, read_ffmpeg_version(install_dir),
                                  reported_build=reported_build)
    if action == "none":
        return {"action": "none"}
    block = manifest.get("ffmpeg") or {}
    return {
        "action": action,
        "version": str(block.get("version", "")).strip(),
        "url": str(block.get("url", "")).strip(),
        "sha256": str(block.get("sha256", "")).strip().lower(),
    }
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_ffmpeg_update.py tests/test_ffmpeg_marker.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/updater_core.py tests/test_ffmpeg_update.py
git commit -m "feat(updater): ffmpeg_update_plan bundles the decision with its coordinates"
```

---

### Task 2: Activity-log lines for a component swap

**Model:** sonnet — two pure string builders, format pinned by tests.

**Files:**
- Modify: `cratebuilder/activitylog.py` (append after `catching_up`, L46-L52)
- Test: `tests/test_activitylog.py`

**Interfaces:**
- Produces: `component_updated(label, old, new) -> str` and `component_failed(label, wanted, reason) -> str`. Task 3 writes both through `self.log_line(...)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_activitylog.py`)

```python
def test_component_updated_names_the_component_and_both_versions():
    line = activitylog.component_updated("FFmpeg", "9.0.1+aa", "9.0.2+bb")
    assert line == "UPDATED     | Component: FFmpeg | 9.0.1+aa -> 9.0.2+bb"


def test_component_updated_without_an_old_version_says_none():
    line = activitylog.component_updated("FFmpeg", None, "9.0.2+bb")
    assert line == "UPDATED     | Component: FFmpeg | (none) -> 9.0.2+bb"


def test_component_failed_names_what_was_wanted_and_why():
    line = activitylog.component_failed("FFmpeg", "9.0.2+bb", "checksum mismatch")
    assert line == ("UPDATE-FAIL | Component: FFmpeg | Wanted: 9.0.2+bb | "
                    "Reason: checksum mismatch")


def test_component_lines_never_trip_the_viewer_filters():
    for line in (activitylog.component_updated("FFmpeg", "a", "b"),
                 activitylog.component_failed("FFmpeg", "b", "boom")):
        assert "DOWNLOADED" not in line
        assert "SKIPPED" not in line
        assert "ERROR" not in line
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_activitylog.py -q -k component`
Expected: FAIL with `AttributeError: ... has no attribute 'component_updated'`

- [ ] **Step 3: Implement** (append after `catching_up` in `cratebuilder/activitylog.py`)

```python
def component_updated(label, old, new):
    """One UPDATED entry for a bundled component the app swapped itself
    (FFmpeg today), written only after the swap really landed — unlike the
    build line, which describes what the new build carries."""
    return (f"UPDATED     | Component: {label} | "
            f"{old or '(none)'} -> {new}")


def component_failed(label, wanted, reason):
    """The matching failure line. Its own verb so the viewer's ERROR filter
    (download failures) stays about downloads."""
    return (f"UPDATE-FAIL | Component: {label} | Wanted: {wanted} | "
            f"Reason: {reason}")
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_activitylog.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/activitylog.py tests/test_activitylog.py
git commit -m "feat(activitylog): component swap lines that record what really changed"
```

---

### Task 3: Service — the component phase inside `update.apply`, and `components_update` on `update.check`

**Model:** opus — touches `service.py`'s update worker, the event bus, and the log; spans three files.

**Files:**
- Modify: `cratebuilder/events.py:6-7` (`DEFAULT_COALESCED_TYPES`)
- Modify: `cratebuilder/service.py` — `__init__` (L852-L967, near `_installed_components_cache = None` at L940), `update_check` (L3162-L3232), `update_apply` worker (L3313-L3418), plus two new private methods placed directly before `update_apply`
- Test: `tests/test_events.py` (create if it does not exist), `tests/test_service_update.py`

**Interfaces:**
- Consumes: `ucore.ffmpeg_update_plan` (Task 1), `activitylog.component_updated` / `component_failed` (Task 2), existing `ucore.download`, `ucore.install_ffmpeg_from_zip`, `ucore.probe_ffmpeg_build`, `ucore.write_ffmpeg_version`, `ucore.read_ffmpeg_version`, `ucore.install_dir`, `ucore.is_frozen`, `ucore.is_linux`, `components.compare`, `components.offered_versions`.
- Produces:
  - `CrateBuilderService._ffmpeg_plan(manifest) -> dict` (`{"action": "none"}` unless frozen-Windows; otherwise Task 1's shape).
  - `CrateBuilderService._run_component_updates(manifest, ws, cancel) -> list[dict]`, each `{"key": "ffmpeg", "label": "FFmpeg", "ok": bool, "version": str, "error": str|None}`. Emits `update.component` events. Raises `ucore.UpdateCancelled` on cancel; never raises otherwise. Task 4 reuses it.
  - `update_check()` result gains `"components_update": list[str]` (component keys the app can update on its own right now; `[]` otherwise).
  - Event `update.component` payload: `{"key", "label", "phase", "version"}` always; plus `pct/done_mb/total_mb` on `download`, `error` on `failed`.
  - Event `update.progress` gains `{"phase": "components"}`.

- [ ] **Step 1: Coalesce the new event — failing test**

Create or append to `tests/test_events.py`:

```python
from cratebuilder import events


def test_update_component_is_coalesced_like_update_progress():
    assert "update.progress" in events.DEFAULT_COALESCED_TYPES
    assert "update.component" in events.DEFAULT_COALESCED_TYPES
```

Run: `python -m pytest tests/test_events.py -q`
Expected: FAIL on the second assert.

- [ ] **Step 2: Add it**

In `cratebuilder/events.py` change L6-L7 to:

```python
DEFAULT_COALESCED_TYPES = ("progress.current", "progress.overall",
                          "update.progress", "update.component")
```

Run: `python -m pytest tests/test_events.py -q` → PASS.

- [ ] **Step 3: Write the failing service tests** (append to `tests/test_service_update.py`; `MANIFEST`, `_Waiter`, `_happy_apply`, `_activity_lines`, `_fake_download`, and the `service` fixture already exist there; the fixture already points `ucore.install_dir()` at `tmp_path / "install"`)

```python
# ── update.apply: the component phase (FFmpeg) ───────────────────────────────

FFMPEG_MANIFEST = dict(MANIFEST, ffmpeg={
    "version": "9.0.2+bb", "url": "https://example.invalid/ffmpeg.zip",
    "sha256": "b" * 64,
}, components={"ffmpeg": "9.0.2+bb", "python": "3.14.7"})


def _frozen_windows(monkeypatch):
    monkeypatch.setattr(service_mod.ucore, "is_frozen", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "is_linux", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "probe_ffmpeg_build",
                        lambda install_dir, _runner=None: "9.0.1")


def _stale_ffmpeg(service, tmp_path):
    install = tmp_path / "install"
    install.mkdir(exist_ok=True)
    service_mod.ucore.write_ffmpeg_version(str(install), "9.0.1+aa")
    service._installed_components_cache = {"python": "3.14.7", "ffmpeg": "9.0.1+aa"}
    return install


def _fake_install(record, fail=None):
    def install(zip_path, expected_sha, install_dir, staged_dir, backup_dir, version):
        record.append({"zip": zip_path, "sha": expected_sha, "dir": install_dir,
                       "version": version})
        if fail:
            raise fail
        service_mod.ucore.write_ffmpeg_version(install_dir, version)
        return True
    return install


def test_apply_swaps_ffmpeg_after_staging_and_before_the_restart(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = _stale_ffmpeg(service, tmp_path)
    _happy_apply(service, monkeypatch, FFMPEG_MANIFEST, tmp_path)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip", _fake_install(swaps))

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert swaps and swaps[0]["version"] == "9.0.2+bb"
    assert swaps[0]["sha"] == "b" * 64
    assert swaps[0]["dir"] == str(install)
    assert service_mod.ucore.read_ffmpeg_version(str(install)) == "9.0.2+bb"

    types = [t for (t, _p) in waiter.events]
    stage_at = max(i for i, (t, p) in enumerate(waiter.events)
                   if t == "update.progress" and p["phase"] == "stage")
    comp_at = [i for i, (t, p) in enumerate(waiter.events)
               if t == "update.progress" and p["phase"] == "components"]
    restart_at = types.index("update.restarting")
    assert comp_at and stage_at < comp_at[0] < restart_at

    phases = [p["phase"] for p in waiter.of_type("update.component")]
    assert phases[0] == "download"
    assert phases[-1] == "done"
    done = waiter.of_type("update.component")[-1]
    assert done["key"] == "ffmpeg" and done["label"] == "FFmpeg"
    assert done["version"] == "9.0.2+bb"
    assert waiter.of_type("update.restarting") == [{"build": 99}]

    lines = [ln for ln in _activity_lines(service) if "UPDATED" in ln]
    assert any("| Component: FFmpeg | 9.0.1+aa -> 9.0.2+bb" in ln for ln in lines)
    build_line = next(ln for ln in lines if "Build: 1 -> 99" in ln)
    assert "FFmpeg" not in build_line


def test_apply_without_an_ffmpeg_block_has_no_component_phase(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    _happy_apply(service, monkeypatch, MANIFEST, tmp_path)

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert waiter.of_type("update.component") == []
    assert [p["phase"] for p in waiter.of_type("update.progress")][-1] == "stage"


def test_apply_skips_the_swap_when_ffmpeg_is_already_current(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = _stale_ffmpeg(service, tmp_path)
    service_mod.ucore.write_ffmpeg_version(str(install), "9.0.2+bb")
    monkeypatch.setattr(service_mod.ucore, "probe_ffmpeg_build",
                        lambda install_dir, _runner=None: "9.0.2")
    _happy_apply(service, monkeypatch, FFMPEG_MANIFEST, tmp_path)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip", _fake_install(swaps))

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert swaps == []
    assert waiter.of_type("update.component") == []


def test_a_failed_ffmpeg_swap_never_stops_the_app_update(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = _stale_ffmpeg(service, tmp_path)
    _happy_apply(service, monkeypatch, FFMPEG_MANIFEST, tmp_path)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip",
                        _fake_install(swaps, fail=ValueError("ffmpeg checksum mismatch")))

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    failed = waiter.of_type("update.component")[-1]
    assert failed["phase"] == "failed"
    assert "checksum" in failed["error"]
    assert waiter.of_type("update.restarting") == [{"build": 99}]
    assert waiter.of_type("job.finished")[-1]["ok"] is True
    assert service_mod.ucore.read_ffmpeg_version(str(install)) == "9.0.1+aa"

    lines = _activity_lines(service)
    assert any("UPDATE-FAIL | Component: FFmpeg | Wanted: 9.0.2+bb" in ln for ln in lines)
    build_line = next(ln for ln in lines if "Build: 1 -> 99" in ln)
    assert "FFmpeg" not in build_line


def test_cancel_during_the_ffmpeg_download_purges_and_says_so(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    _happy_apply(service, monkeypatch, FFMPEG_MANIFEST, tmp_path)

    def download(url, dest, progress_cb=None, timeout=30.0, _opener=None, cancel=None):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"zip")
        if url.endswith("ffmpeg.zip"):
            service.update_cancel()
            raise service_mod.ucore.UpdateCancelled("cancelled")
        return dest
    monkeypatch.setattr(service_mod.ucore, "download", download)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip", _fake_install(swaps))

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()

    assert swaps == []
    assert waiter.of_type("update.restarting") == []
    assert waiter.of_type("update.cancelled") == [{"build": 1}]
    assert waiter.of_type("job.finished")[-1]["ok"] is True
    assert not os.path.exists(service_mod.ucore.default_workspace())


def test_apply_on_a_source_install_never_looks_at_ffmpeg(service, monkeypatch, tmp_path):
    """can_self_update is faked True for the app payload, but is_frozen is
    the real False: the component phase must key off frozen, not off the
    self-update gate."""
    _stale_ffmpeg(service, tmp_path)
    _happy_apply(service, monkeypatch, FFMPEG_MANIFEST, tmp_path)
    monkeypatch.setattr(service_mod.ucore, "is_frozen", lambda: False)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip", _fake_install(swaps))

    waiter = _Waiter(service)
    service.update_apply()
    waiter.wait()
    assert swaps == []


# ── update.check: the standalone offer ───────────────────────────────────────

def test_check_offers_a_standalone_ffmpeg_update_when_the_app_is_current(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    current = dict(FFMPEG_MANIFEST, build=1)
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: current)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    r = service.update_check()
    assert r["available"] is False
    assert r["components_update"] == ["ffmpeg"]


def test_check_adopts_a_markerless_install_and_offers_nothing(
        service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = tmp_path / "install"
    install.mkdir(exist_ok=True)
    monkeypatch.setattr(service_mod.ucore, "probe_ffmpeg_build",
                        lambda install_dir, _runner=None: "9.0.2")
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: FFMPEG_MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    r = service.update_check()
    assert r["components_update"] == []
    assert service_mod.ucore.read_ffmpeg_version(str(install)) == "9.0.2+bb"


def test_check_offers_no_component_update_from_source(service, monkeypatch, tmp_path):
    _stale_ffmpeg(service, tmp_path)
    monkeypatch.setattr(service_mod.ucore, "is_frozen", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: FFMPEG_MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    assert service.update_check()["components_update"] == []


def test_check_probes_the_binary_once_per_session(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    probes = []
    monkeypatch.setattr(service_mod.ucore, "probe_ffmpeg_build",
                        lambda install_dir, _runner=None: probes.append(1) or "9.0.1")
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: FFMPEG_MANIFEST)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    service.update_check()
    service.update_check()
    assert len(probes) == 1
```

- [ ] **Step 4: Run them to verify they fail**

Run: `python -m pytest tests/test_service_update.py -q -k "ffmpeg or component or adopts"`
Expected: FAIL (`KeyError: 'components_update'`, missing `update.component` events, missing swap calls).

- [ ] **Step 5: Implement in `cratebuilder/service.py`**

5a. In `__init__`, right after `self._installed_components_cache = None` (L940):

```python
        # ffmpeg.exe -version is spawned at most once a session; the swap
        # path clears this on its way out so the next check re-reads the
        # truth. False = not probed yet (None is a valid "couldn't say").
        self._ffmpeg_probe_cache = False
```

5b. Add these two methods directly **before** `def update_apply(self):` (L3245):

```python
    def _ffmpeg_plan(self, manifest):
        """What, if anything, to do about the bundled FFmpeg for this
        manifest. Frozen Windows only — a source run finds FFmpeg on PATH
        and Linux gets it from the .deb — so everywhere else this is
        {"action": "none"} and no caller has to ask twice."""
        if not ucore.is_frozen() or ucore.is_linux():
            return {"action": "none"}
        install = ucore.install_dir()
        if self._ffmpeg_probe_cache is False:
            self._ffmpeg_probe_cache = ucore.probe_ffmpeg_build(install)
        return ucore.ffmpeg_update_plan(
            manifest, install, reported_build=self._ffmpeg_probe_cache)

    def _run_component_updates(self, manifest, ws, cancel):
        """Bring every self-updatable bundled component (FFmpeg today) to
        what *manifest* offers, reporting each step as update.component.
        Runs only while the app is idle (the caller's guard) so the swap
        never fights a live yt-dlp handle. A failure is recorded and
        returned, never raised — the caller's own work must go on — except
        a cancel, which is the user's and propagates. Returns one result
        per component attempted (empty when nothing needed doing)."""
        results = []
        plan = self._ffmpeg_plan(manifest)
        if plan["action"] != "update":
            return results
        key, label, version = "ffmpeg", "FFmpeg", plan["version"]
        install = ucore.install_dir()
        before = ucore.read_ffmpeg_version(install)

        def tell(phase, **extra):
            payload = {"key": key, "label": label, "phase": phase,
                       "version": version}
            payload.update(extra)
            self.emit("update.component", payload)

        try:
            if cancel.is_set():
                raise ucore.UpdateCancelled("cancelled before components")
            zip_path = os.path.join(ws, f"ffmpeg-{version}.zip")

            def progress(done, total):
                tell("download",
                     pct=int(done * 100 / total) if total else None,
                     done_mb=done // 1048576,
                     total_mb=(total // 1048576) if total else None)

            tell("download", pct=0, done_mb=0, total_mb=None)
            ucore.download(plan["url"], zip_path, progress_cb=progress,
                           cancel=cancel)
            if cancel.is_set():
                raise ucore.UpdateCancelled("cancelled after component download")
            tell("verify")
            tell("install")
            ucore.install_ffmpeg_from_zip(
                zip_path, plan["sha256"], install,
                os.path.join(ws, "ffmpeg_staged"),
                os.path.join(ws, "ffmpeg_backup"), version)
        except ucore.UpdateCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — reported, retried next time
            self._emit.flush()
            tell("failed", error=str(exc))
            self.log_line(activitylog.component_failed(label, version, str(exc)))
            self._dbg.debug(f"FFMPEG UPDATE | failed: {exc}")
            results.append({"key": key, "label": label, "ok": False,
                            "version": version, "error": str(exc)})
            return results
        # The binary on disk changed: every cached answer about it is stale.
        self._ffmpeg_probe_cache = False
        self._installed_components_cache = None
        self._emit.flush()
        tell("done")
        self.log_line(activitylog.component_updated(label, before, version))
        self._dbg.debug(f"FFMPEG UPDATE | swapped to {version}")
        results.append({"key": key, "label": label, "ok": True,
                        "version": version, "error": None})
        return results
```

5c. In `update_apply`'s worker: declare `handled = []` right after `os.makedirs(ws, exist_ok=True)` (before the `try:`), then replace the block that starts at the comment `# update.progress is coalesced (cratebuilder/events.py); flush` and ends with `self.emit("update.restarting", {"build": install_build})` (currently L3356-L3362) with:

```python
                # The app payload is staged; now the components this build
                # ships alongside it. Only announced when there is one to
                # do, so a build without an FFmpeg change keeps "stage" as
                # its last app phase. A component failure is already
                # reported inside; the app update goes ahead regardless.
                if not full_repair and self._ffmpeg_plan(manifest)["action"] == "update":
                    self._emit.flush()
                    self.emit("update.progress", {"phase": "components"})
                    handled = self._run_component_updates(manifest, ws, cancel)

                # update.progress is coalesced (cratebuilder/events.py); flush
                # its last pending frame so it can never arrive after the
                # events that supersede it — the same reason MaintenanceOps
                # flushes before its own terminal notification.
                self._emit.flush()
                self.emit("update.restarting", {"build": install_build})
```

and, further down, replace the `if full_repair: ... else: self.log_line(activitylog.updated(current, install_build, component_rows))` block with:

```python
            if full_repair:
                self.log_line(activitylog.catching_up(install_build))
            else:
                # A component this run swapped (or failed to) has its own
                # line already; the build line must not claim it too.
                own = {r["key"] for r in handled}
                rows = [r for r in component_rows if r["key"] not in own]
                self.log_line(activitylog.updated(current, install_build, rows))
```

5d. In `update_check`, add `"components_update": [],` to the `result` dict (after `"installer_url"`), and replace the method's final `return result` (the one after the `if result["available"]:` block) with:

```python
        # The standalone offer: a bundled component the app can bring up to
        # date on its own, app build or not. Adopt (no marker yet) is
        # settled here as the monolith did, so a fresh install never pays
        # for a download it doesn't need.
        plan = self._ffmpeg_plan(manifest)
        if plan["action"] == "adopt":
            try:
                ucore.write_ffmpeg_version(ucore.install_dir(), plan["version"])
            except OSError:
                pass
        elif plan["action"] == "update" and ucore.can_self_update():
            result["components_update"] = ["ffmpeg"]
        return result
```

- [ ] **Step 6: Run the update tests**

Run: `python -m pytest tests/test_service_update.py tests/test_events.py tests/test_service_frozen.py -q`
Expected: all PASS, including the pre-existing `test_apply_happy_path` (no FFmpeg block → last phase still `stage`) and `test_apply_logs_the_components_the_build_changes`.

- [ ] **Step 7: Commit**

```bash
git add cratebuilder/events.py cratebuilder/service.py tests/test_events.py tests/test_service_update.py
git commit -m "feat(updater): swap bundled FFmpeg during update.apply and offer it on update.check"
```

---

### Task 4: Service — `update.components_apply` (the standalone path)

**Model:** opus — new dispatch-table method on `service.py` with job/cancel semantics.

**Files:**
- Modify: `cratebuilder/service.py` — `_methods()` (add one entry beside `"update.apply"`), new method placed directly after `update_cancel`
- Test: `tests/test_service_update.py`

**Interfaces:**
- Consumes: `_ffmpeg_plan`, `_run_component_updates` (Task 3), `_require_idle_for_update`, `_start_job`, `UPDATE_JOB`, `_update_cancel`.
- Produces: RPC method `update.components_apply` → `{"job_id": str, "components": ["ffmpeg"], "version": str}`. Emits `update.component` events, then on success a `notification` (level `info`, title `Update`, body `FFmpeg updated to <version>.`) and `job.finished ok=True`; on failure the worker raises `CBError` so `_start_job` emits the failure notification and `job.finished ok=False`; on cancel emits `update.cancelled {"build": current}` and `job.finished ok=True`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_service_update.py`)

```python
# ── update.components_apply ───────────────────────────────────────────────────

def _current_manifest(monkeypatch):
    current = dict(FFMPEG_MANIFEST, build=1)
    monkeypatch.setattr(service_mod.ucore, "fetch_manifest", lambda url: current)
    monkeypatch.setattr(service_mod, "version_info",
                        lambda script_path=None: {"version": "2.0", "build": 1})
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: True)
    monkeypatch.setattr(service_mod.ucore, "download", _fake_download(None))


def test_components_apply_swaps_ffmpeg_and_notifies(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = _stale_ffmpeg(service, tmp_path)
    _current_manifest(monkeypatch)
    swaps = []
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip", _fake_install(swaps))

    waiter = _Waiter(service)
    r = service.update_components_apply()
    assert r["components"] == ["ffmpeg"] and r["version"] == "9.0.2+bb"
    waiter.wait()

    assert swaps and swaps[0]["dir"] == str(install)
    assert service_mod.ucore.read_ffmpeg_version(str(install)) == "9.0.2+bb"
    assert [p["phase"] for p in waiter.of_type("update.component")][-1] == "done"
    assert waiter.of_type("update.restarting") == []
    assert waiter.of_type("update.progress") == []
    notes = [n for n in waiter.of_type("notification") if n["title"] == "Update"]
    assert notes and "FFmpeg updated to 9.0.2+bb" in notes[-1]["body"]
    assert waiter.of_type("job.finished")[-1]["ok"] is True
    assert not os.path.exists(service_mod.ucore.default_workspace())
    # The components table now says "same" without a restart: the swap
    # cleared the installed-versions cache, so the next read sees the new
    # marker (bundled_ffmpeg_dir is what the service reads FFmpeg from; from
    # source it is None, so point it at the fake install).
    monkeypatch.setattr(service_mod, "bundled_ffmpeg_dir", lambda: str(install))
    service._last_update_result = {"valid": True, "latest_build": 1,
                                   "available": False,
                                   "components": {"ffmpeg": "9.0.2+bb"}}
    row = next(r for r in service.update_components()["rows"] if r["key"] == "ffmpeg")
    assert row["installed"] == "9.0.2+bb"
    assert row["state"] == "same"


def test_components_apply_failure_is_a_failed_job(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    _current_manifest(monkeypatch)
    monkeypatch.setattr(service_mod.ucore, "install_ffmpeg_from_zip",
                        _fake_install([], fail=ValueError("ffmpeg checksum mismatch")))

    waiter = _Waiter(service)
    service.update_components_apply()
    waiter.wait()

    assert waiter.of_type("job.finished")[-1]["ok"] is False
    assert any("UPDATE-FAIL | Component: FFmpeg" in ln for ln in _activity_lines(service))
    assert not os.path.exists(service_mod.ucore.default_workspace())


def test_components_apply_refuses_when_ffmpeg_is_current(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    install = _stale_ffmpeg(service, tmp_path)
    service_mod.ucore.write_ffmpeg_version(str(install), "9.0.2+bb")
    monkeypatch.setattr(service_mod.ucore, "probe_ffmpeg_build",
                        lambda install_dir, _runner=None: "9.0.2")
    _current_manifest(monkeypatch)
    with pytest.raises(CBError, match="already up to date"):
        service.update_components_apply()


def test_components_apply_refuses_from_source(service, monkeypatch, tmp_path):
    _stale_ffmpeg(service, tmp_path)
    _current_manifest(monkeypatch)
    monkeypatch.setattr(service_mod.ucore, "is_frozen", lambda: False)
    monkeypatch.setattr(service_mod.ucore, "can_self_update", lambda: False)
    with pytest.raises(CBError, match="running from source"):
        service.update_components_apply()


def test_components_apply_refuses_while_batch_running(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    _current_manifest(monkeypatch)
    gate = threading.Event()
    service._start_job("batch", lambda: gate.wait(5), title="hold")
    try:
        with pytest.raises(CBError):
            service.update_components_apply()
    finally:
        gate.set()


def test_cancel_during_components_apply_says_so(service, monkeypatch, tmp_path):
    _frozen_windows(monkeypatch)
    _stale_ffmpeg(service, tmp_path)
    _current_manifest(monkeypatch)

    def download(url, dest, progress_cb=None, timeout=30.0, _opener=None, cancel=None):
        service.update_cancel()
        raise service_mod.ucore.UpdateCancelled("cancelled")
    monkeypatch.setattr(service_mod.ucore, "download", download)

    waiter = _Waiter(service)
    service.update_components_apply()
    waiter.wait()
    assert waiter.of_type("update.cancelled") == [{"build": 1}]
    assert waiter.of_type("job.finished")[-1]["ok"] is True


def test_remote_transport_refuses_components_apply(tmp_path):
    # Copy the construction + dispatch shape of
    # test_remote_transport_refuses_update_methods (this file, ~L267)
    # exactly; only the method name differs.
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"),
                              transport=REMOTE)
    try:
        with pytest.raises(CBError):
            svc.dispatch("update.components_apply", {})
    finally:
        svc.close()
```

(Match `test_components_apply_refuses_while_batch_running` to how `test_apply_refuses_while_batch_running` at ~L282 starts its batch job — copy that shape if `_start_job("batch", ...)` is not how it holds the slot.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_service_update.py -q -k components_apply`
Expected: FAIL with `AttributeError: 'CrateBuilderService' object has no attribute 'update_components_apply'`.

- [ ] **Step 3: Implement**

3a. In `_methods()` add, next to `"update.apply": self.update_apply,`:

```python
            "update.components_apply": self.update_components_apply,
```

3b. Add after `update_cancel`:

```python
    def update_components_apply(self):
        """Bring the bundled FFmpeg up to what the live manifest offers
        without an app update — the path an FFmpeg-only nightly
        (release.py --ffmpeg) reaches an install by. Same idle guard, same
        job slot and cancel as update.apply, no restart: the binaries are
        swapped in place while nothing holds them."""
        url = self._update_manifest_url()
        if not url:
            raise CBError("Couldn't determine the update server address — "
                          "the app's own source could not be read.")
        manifest = ucore.fetch_manifest(url)
        ok, _reason = ucore.validate_manifest(manifest) if manifest else (False, "")
        if not ok:
            raise CBError("Couldn't reach the update server, or the update "
                          "information looks invalid right now. Try Check "
                          "for updates again in a moment.")
        if ucore.is_linux():
            raise CBError("FFmpeg comes with the .deb on Linux — install the "
                          "latest package to update it.")
        if not ucore.can_self_update():
            raise CBError("You're running from source, so FFmpeg comes from "
                          "your PATH — update it there.")
        plan = self._ffmpeg_plan(manifest)
        if plan["action"] != "update":
            raise CBError("FFmpeg is already up to date.")
        self._require_idle_for_update()
        current = version_info()["build"]
        version = plan["version"]
        cancel = self._update_cancel

        def guard():
            self._require_idle_for_update()
            cancel.clear()

        def worker():
            ws = ucore.default_workspace()
            ucore.purge_dir(ws)
            os.makedirs(ws, exist_ok=True)
            try:
                results = self._run_component_updates(manifest, ws, cancel)
            except ucore.UpdateCancelled:
                self._emit.flush()
                self.emit("notification", {
                    "level": "info",
                    "title": "Update",
                    "body": "Component update cancelled — FFmpeg is unchanged.",
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "job": UPDATE_JOB,
                })
                self.emit("update.cancelled", {"build": current})
                return
            finally:
                ucore.purge_dir(ws)
            failed = [r for r in results if not r["ok"]]
            if failed:
                raise CBError(f"FFmpeg update failed: {failed[0]['error']}")
            self._emit.flush()
            self.emit("notification", {
                "level": "info",
                "title": "Update",
                "body": f"FFmpeg updated to {version}.",
                "at": datetime.now().isoformat(timespec="seconds"),
                "job": UPDATE_JOB,
            })

        job_id = self._start_job(UPDATE_JOB, worker, title="Update FFmpeg",
                                 guard=guard)
        return {"job_id": job_id, "components": ["ffmpeg"], "version": version}
```

Check the existing `update.cancelled` notification in `update_apply` (~L3378-L3388) for the exact `notification` payload keys and copy them if they differ from the above.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_service_update.py tests/test_server.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/service.py tests/test_service_update.py
git commit -m "feat(updater): update.components_apply swaps FFmpeg without an app update"
```

---

### Task 5: Web — component rows in the update modal

**Model:** opus — `web/app.js` event wiring and the text-sliced Node tests.

**Files:**
- Modify: `web/app.js` — the region from `  /* Step two: the progress modal` (L6766) to `  /* Re-fetches update.status` (L6886): `aboutBeginApply`, `aboutPaintApplyProgress`, `aboutSettleApply`, new `aboutPaintComponent` and `aboutSwapCancelForClose` (all must stay **inside** that region — the test harness slices exactly it); `subscribeUpdateEvents` (L8052-L8085)
- Test: `tests/test_web_about_client.py` (uses the existing `_progress(app_js, tmp_path, name, script)` helper, `_PROGRESS_HARNESS`, and its `btn(el, text)` / `modal` / `closeModal` stubs)

**Interfaces:**
- Consumes: events `update.progress {phase: 'components'}` and `update.component {key,label,phase,pct,done_mb,total_mb,version,error}` (Task 3).
- Produces: `aboutBeginApply(opts)` where `opts.componentsOnly === true` opens the "Updating components" variant (Task 6 calls it); `aboutPaintComponent(p)`; `aboutSwapCancelForClose(refs)`; `refs.componentsOnly`, `refs.components` (map key → `{el, status, fill}`), `refs.compHost`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_web_about_client.py`, after `test_a_failed_apply_swaps_cancel_for_a_close_that_can_dismiss`)

```python
def test_component_progress_draws_one_row_per_component_under_the_app_bar(
        app_js, tmp_path):
    r = _progress(app_js, tmp_path, "comp-rows.mjs", """
      aboutBeginApply();
      aboutPaintApplyProgress({ phase: 'stage' });
      aboutPaintApplyProgress({ phase: 'components' });
      const afterPhase = aboutUpdate.view.status.textContent;
      aboutPaintComponent({ key: 'ffmpeg', label: 'FFmpeg', phase: 'download',
                            pct: 40, done_mb: 40, total_mb: 100, version: '9.0.2' });
      const mid = aboutUpdate.view.components.ffmpeg.status.textContent;
      const midFill = aboutUpdate.view.components.ffmpeg.fill.style.width;
      aboutPaintComponent({ key: 'ffmpeg', label: 'FFmpeg', phase: 'verify', version: '9.0.2' });
      const verify = aboutUpdate.view.components.ffmpeg.status.textContent;
      aboutPaintComponent({ key: 'ffmpeg', label: 'FFmpeg', phase: 'install', version: '9.0.2' });
      const install = aboutUpdate.view.components.ffmpeg.status.textContent;
      aboutPaintComponent({ key: 'ffmpeg', label: 'FFmpeg', phase: 'done', version: '9.0.2' });
      const done = aboutUpdate.view.components.ffmpeg.status.textContent;
      const doneFill = aboutUpdate.view.components.ffmpeg.fill.style.width;
      console.log(JSON.stringify({
        afterPhase, mid, midFill, verify, install, done, doneFill,
        rows: Object.keys(aboutUpdate.view.components),
        appFill: aboutUpdate.view.fill.style.width,
        hostChildren: aboutUpdate.view.compHost.children.length,
      }));
    """)
    assert r["afterPhase"] == "Updating components…"
    assert r["appFill"] == "100%"
    assert r["rows"] == ["ffmpeg"]
    assert r["hostChildren"] == 1          # a second event reuses the row
    assert r["mid"] == "FFmpeg: downloading… 40 / 100 MB"
    assert r["midFill"] == "40%"
    assert r["verify"] == "FFmpeg: verifying…"
    assert r["install"] == "FFmpeg: installing…"
    assert r["done"] == "FFmpeg: updated to 9.0.2"
    assert r["doneFill"] == "100%"


def test_a_failed_component_says_so_and_promises_a_retry(app_js, tmp_path):
    r = _progress(app_js, tmp_path, "comp-fail.mjs", """
      aboutBeginApply();
      aboutPaintComponent({ key: 'ffmpeg', label: 'FFmpeg', phase: 'failed',
                            version: '9.0.2', error: 'checksum mismatch' });
      console.log(JSON.stringify({
        text: aboutUpdate.view.components.ffmpeg.status.textContent }));
    """)
    assert r["text"] == ("FFmpeg: update failed — checksum mismatch. "
                         "It will be tried again next time.")


def test_components_only_modal_has_no_app_bar_and_its_own_title(app_js, tmp_path):
    r = _progress(app_js, tmp_path, "comp-only.mjs", """
      aboutBeginApply({ componentsOnly: true });
      console.log(JSON.stringify({
        title: modal.opts.title,
        locked: modal.opts.locked,
        status: aboutUpdate.view.status.textContent,
        bodyClasses: modal.body.children.map((c) => c.className),
        note: aboutUpdate.view.note.textContent,
        only: aboutUpdate.view.componentsOnly,
      }));
    """)
    assert r["title"] == "Updating components"
    assert r["locked"] is True
    assert r["only"] is True
    assert r["status"] == "Starting…"
    assert "cb-bar" not in r["bodyClasses"]
    assert r["note"] == "Cancel stops the download and leaves FFmpeg as it is."


def test_components_only_settles_with_a_close_on_success_and_failure(app_js, tmp_path):
    r = _progress(app_js, tmp_path, "comp-settle.mjs", """
      aboutBeginApply({ componentsOnly: true });
      aboutSettleApply({ ok: true });
      const okText = aboutUpdate.view.status.textContent;
      const okClose = !!btn(modal.foot, 'Close');
      const okCancel = !!btn(modal.foot, 'Cancel');
      closeModal();
      aboutBeginApply({ componentsOnly: true });
      aboutSettleApply({ ok: false });
      const badText = aboutUpdate.view.status.textContent;
      const badClose = !!btn(modal.foot, 'Close');
      console.log(JSON.stringify({ okText, okClose, okCancel, badText, badClose }));
    """)
    assert r["okText"] == "Components updated."
    assert r["okClose"] is True and r["okCancel"] is False
    assert "failed" in r["badText"] and "current build" not in r["badText"]
    assert r["badClose"] is True


def test_a_full_update_still_ignores_an_ok_settle(app_js, tmp_path):
    """The app-update modal is about to be closed by the restart; an ok=true
    job.finished must leave it alone exactly as before."""
    r = _progress(app_js, tmp_path, "comp-noop.mjs", """
      aboutBeginApply();
      aboutShowRestarting(65);
      aboutSettleApply({ ok: true });
      console.log(JSON.stringify({ text: aboutUpdate.view.status.textContent,
                                   cancel: !!btn(modal.foot, 'Cancel') }));
    """)
    assert "Restarting" in r["text"]
    assert r["cancel"] is True


def test_update_component_is_subscribed_beside_the_other_update_events(app_js):
    fn = _slice(app_js, "  function subscribeUpdateEvents()",
                "  let booted = false;")
    assert "cbApi.on('update.component', (p) => aboutPaintComponent(p));" in fn
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_web_about_client.py -q -k "component"`
Expected: FAIL (`aboutPaintComponent is not defined`, title mismatch).

- [ ] **Step 3: Implement in `web/app.js`**

3a. Replace `aboutBeginApply` (L6773-L6808) with:

```js
  function aboutBeginApply(opts) {
    const only = !!(opts && opts.componentsOnly);
    const refs = { componentsOnly: only, components: {} };
    openModal({
      title: only ? 'Updating components' : 'Updating DJ-CrateBuilder',
      width: 460,
      locked: true,
      onClose() { if (aboutUpdate.view === refs) aboutUpdate.view = null; },
      body(body) {
        // Left at this until the first real update.progress payload —
        // never seeded with a fake one, which would claim a % and a done_mb
        // that don't exist yet.
        refs.status = modalNote(only ? 'Starting…' : 'Starting download…');
        const bar = document.createElement('div');
        bar.className = 'cb-bar';
        refs.fill = document.createElement('div');
        refs.fill.className = 'cb-bar__fill';
        refs.fill.style.width = '0%';
        bar.appendChild(refs.fill);
        /* Components (FFmpeg) get a row each, added on their first event so
           a build that ships none never shows an empty block. */
        refs.compHost = document.createElement('div');
        refs.compHost.style.cssText = 'display:grid;gap:6px;margin-top:8px';
        if (only) body.append(refs.status, refs.compHost);
        else body.append(refs.status, bar, refs.compHost);
      },
      foot(foot, api) {
        refs.api = api;
        refs.note = modalNote(only
          ? 'Cancel stops the download and leaves FFmpeg as it is.'
          : 'Cancel stops the download and leaves the app on its current build.');
        refs.cancel = modalButton('Cancel', 'cb-btn--warn', () => {
          refs.cancelRequested = true;
          refs.cancel.disabled = true;
          refs.cancel.textContent = 'Cancelling…';
          call('update.cancel').catch(() => {});
        });
        refs.cancel.style.marginLeft = 'auto';
        foot.append(refs.note, refs.cancel);
      },
    });
    aboutUpdate.view = refs;
  }
```

3b. In `aboutPaintApplyProgress`, add a fourth branch after the `stage` one:

```js
    } else if (p.phase === 'components') {
      refs.fill.style.width = '100%';
      refs.status.textContent = 'Updating components…';
    }
```

3c. Add directly after `aboutPaintApplyProgress`:

```js
  /* update.component: one row per bundled component the host is swapping
     (FFmpeg today), each with its own bar — the app bar above is already
     full by then, and a 100 MB FFmpeg download deserves its own numbers. */
  function aboutPaintComponent(p) {
    const refs = aboutUpdate.view;
    if (!refs || !p || !p.key) return;
    let row = refs.components[p.key];
    if (!row) {
      row = {};
      row.el = document.createElement('div');
      row.status = document.createElement('div');
      row.status.className = 'cb-mut';
      row.status.style.fontSize = '12px';
      const bar = document.createElement('div');
      bar.className = 'cb-bar';
      row.fill = document.createElement('div');
      row.fill.className = 'cb-bar__fill';
      row.fill.style.width = '0%';
      bar.appendChild(row.fill);
      row.el.append(row.status, bar);
      refs.compHost.appendChild(row.el);
      refs.components[p.key] = row;
    }
    const label = p.label || p.key;
    if (p.phase === 'download') {
      row.fill.style.width = (p.pct != null ? p.pct : 0) + '%';
      row.status.textContent = p.total_mb != null
        ? `${label}: downloading… ${p.done_mb} / ${p.total_mb} MB`
        : `${label}: downloading… ${p.done_mb || 0} MB`;
    } else if (p.phase === 'verify') {
      row.fill.style.width = '100%';
      row.status.textContent = `${label}: verifying…`;
    } else if (p.phase === 'install') {
      row.status.textContent = `${label}: installing…`;
    } else if (p.phase === 'done') {
      row.fill.style.width = '100%';
      row.status.textContent = `${label}: updated to ${p.version}`;
    } else if (p.phase === 'failed') {
      row.status.textContent = `${label}: update failed — ${p.error || 'unknown error'}. `
        + 'It will be tried again next time.';
    }
  }

  /* The dialog is locked, so a run that ended (well or badly) needs a way
     out of its own: Cancel (nothing left to cancel) becomes Close. */
  function aboutSwapCancelForClose(refs) {
    if (!refs.cancel || !refs.api) return;
    const close = modalButton('Close', 'cb-btn--quiet', refs.api.close);
    close.style.marginLeft = 'auto';
    refs.cancel.replaceWith(close);
    refs.cancel = null;
    close.focus();
  }
```

3d. Replace the body of `aboutSettleApply` (keep the multi-line comment above it — it still describes the full-update branch) with:

```js
  function aboutSettleApply(payload) {
    const refs = aboutUpdate.view;
    if (!refs) return;
    const ok = !(payload && payload.ok === false);
    if (refs.componentsOnly) {
      /* No restart is coming: the job's end is the dialog's end either
         way, and the wording must not talk about builds. */
      refs.status.textContent = ok
        ? 'Components updated.'
        : 'The component update failed. See the notification for details.';
      if (refs.note) refs.note.textContent = 'You can close this window.';
      aboutSwapCancelForClose(refs);
      return;
    }
    if (ok) return;
    refs.status.textContent =
      'The update failed to install — still on the current build. See '
      + 'the notification for details.';
    if (refs.note) {
      refs.note.textContent = 'You can close this window and try again.';
    }
    aboutSwapCancelForClose(refs);
  }
```

3e. In `subscribeUpdateEvents`, add after the `update.progress` line:

```js
    cbApi.on('update.component', (p) => aboutPaintComponent(p));
```

- [ ] **Step 4: Run all frontend tests**

Run: `python -m pytest -q tests/test_web_*_client.py`
Expected: all PASS (including the four pre-existing progress-modal tests, which still call `aboutBeginApply()` with no argument, and `test_a_failed_apply_swaps_cancel_for_a_close_that_can_dismiss`, which now goes through `aboutSwapCancelForClose`).

- [ ] **Step 5: Verify in the app window**

Run `python web_window.py --screen update`. The modal cannot be driven from source (no frozen install), so verify the page still loads with no console errors in **both** themes (Settings ▸ Appearance). Say explicitly in the task report that the modal's component rows were verified by the Node tests only.

- [ ] **Step 6: Commit**

```bash
git add web/app.js tests/test_web_about_client.py
git commit -m "feat(webui): show per-component progress in the update modal"
```

---

### Task 6: Web — the "Update FFmpeg" button and its tooltip

**Model:** opus — `web/app.js` controls plus the generated tooltip module.

**Files:**
- Modify: `web/app.js` — `renderUpdateControls` (L6912-L7062, the `upRow.append(checkBtn, updateBtn);` line), new `aboutStartComponentsApply` placed directly after `aboutStartApply` (L6749-L6764), `subscribeUpdateEvents`'s `update.checked` handler (L8079-L8084)
- Modify: `UI-design/ui-contract.json` (tooltips block, beside `"about.report"`)
- Regenerate: `cratebuilder/ui_strings.py` via `python scripts/gen_ui_strings.py`
- Test: `tests/test_web_about_client.py`; plus whichever test file pins the generated module (`graft grep "about.report" --in tests`)

**Interfaces:**
- Consumes: `update_check` result field `components_update` (Task 3); RPC `update.components_apply` (Task 4); `aboutBeginApply({componentsOnly: true})` (Task 5).
- Produces: `aboutStartComponentsApply()`; tooltip key `about.update_components`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_web_about_client.py`)

```python
_COMPONENTS_HARNESS = _HARNESS.split("function renderWith(")[0] + """
let began = 0;
function aboutStartComponentsApply() { began += 1; }
function renderComp(result, running) {
  aboutUpdate.result = result; aboutUpdate.status = { running: !!running };
  wl.running = false; cbApi.transport = 'local';
  const host = makeEl('div');
  renderUpdateControls(host);
  const all = buttons(host);
  const ff = all.find((b) => b.textContent.indexOf('Update FFmpeg') !== -1);
  if (ff && !ff.disabled && ff.listeners.click) ff.listeners.click();
  return { present: !!ff, disabled: ff ? ff.disabled : null, began,
           updateNow: !!all.find((b) => b.textContent.indexOf('Update Now') !== -1) };
}
const CURRENT = { reachable: true, valid: true, available: false,
                  current_build: 95, latest_build: 95, can_self_update: true };
console.log(JSON.stringify({
  offered: renderComp(Object.assign({}, CURRENT, { components_update: ['ffmpeg'] })),
  nothing: renderComp(Object.assign({}, CURRENT, { components_update: [] })),
  legacy: renderComp(CURRENT),
  appToo: renderComp(Object.assign({}, CURRENT, { available: true,
                                                   components_update: ['ffmpeg'] })),
  busy: renderComp(Object.assign({}, CURRENT, { components_update: ['ffmpeg'] }), true),
}));
"""


def test_update_ffmpeg_button_appears_only_for_a_standalone_offer(app_js, tmp_path):
    r = _run_node(tmp_path, "comp-btn.mjs",
                  _COMPONENTS_HARNESS % {"slices": _slices(app_js)})
    assert r["offered"]["present"] is True and r["offered"]["disabled"] is False
    assert r["offered"]["began"] == 1
    assert r["offered"]["updateNow"] is True        # still drawn, just disabled
    assert r["nothing"]["present"] is False
    assert r["legacy"]["present"] is False
    assert r["appToo"]["present"] is False           # the app update carries it
    assert r["busy"]["present"] is True and r["busy"]["disabled"] is True


def test_starting_a_components_apply_opens_the_components_modal(app_js, tmp_path):
    slices = _slice(app_js, "  async function aboutStartComponentsApply()",
                    "  /* Step two: the progress modal")
    r = _run_node(tmp_path, "comp-start.mjs", """
const calls = [];
const opened = [];
let closed = 0;
async function call(method) { calls.push(method); return {}; }
function aboutBeginApply(opts) { opened.push(opts); }
function closeModal() { closed += 1; }
const aboutUpdate = { view: null };
%s
async function main() {
  await aboutStartComponentsApply();
  console.log(JSON.stringify({ opened, calls, closed }));
}
main();
""" % slices)
    assert r["opened"] == [{"componentsOnly": True}]
    assert r["calls"] == ["update.components_apply"]
    assert r["closed"] == 0


def test_a_refused_components_apply_closes_the_modal(app_js, tmp_path):
    slices = _slice(app_js, "  async function aboutStartComponentsApply()",
                    "  /* Step two: the progress modal")
    r = _run_node(tmp_path, "comp-refused.mjs", """
let closed = 0;
async function call() { const e = new Error('busy'); e.userFacing = true; throw e; }
function aboutBeginApply() { aboutUpdate.view = {}; }
function closeModal() { closed += 1; aboutUpdate.view = null; }
const aboutUpdate = { view: null };
%s
async function main() {
  await aboutStartComponentsApply();
  console.log(JSON.stringify({ closed, view: aboutUpdate.view }));
}
main();
""" % slices)
    assert r["closed"] == 1 and r["view"] is None


def test_a_silent_check_that_offers_ffmpeg_lights_the_button(app_js):
    checked = _slice(app_js, "    cbApi.on('update.checked', (p) => {", "    });")
    assert "components_update" in checked
    assert "aboutUpdate.result = p;" in checked
    assert "renderUpdate();" in checked


def test_update_ffmpeg_has_a_generated_tooltip():
    from cratebuilder import ui_strings
    assert "about.update_components" in ui_strings.TOOLTIPS
    assert "FFmpeg" in ui_strings.TOOLTIPS["about.update_components"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_web_about_client.py -q -k "ffmpeg or components_apply or silent_check"`
Expected: FAIL.

- [ ] **Step 3: Tooltip** — in `UI-design/ui-contract.json`, inside the `"tooltips"` object next to `"about.report"`, add:

```json
    "about.update_components": { "text": "Downloads the newer FFmpeg this nightly ships and swaps it into the app folder. Nothing else changes and the app does not restart. Only while no download or scan is running.", "source": "new", "screen": "3o" },
```

Then run `python scripts/gen_ui_strings.py` and confirm `git diff cratebuilder/ui_strings.py` shows only the new key.

- [ ] **Step 4: Implement in `web/app.js`**

4a. After `aboutStartApply` (ends L6764) add:

```js
  /* The standalone path: the app is current but the nightly ships a newer
     FFmpeg. Same modal in its components-only dress; a refusal (busy,
     already current) is toasted by call() and the empty dialog just goes. */
  async function aboutStartComponentsApply() {
    aboutBeginApply({ componentsOnly: true });
    try {
      await call('update.components_apply');
    } catch (_) {
      aboutUpdate.view = null;
      closeModal();
    }
  }
```

4b. In `renderUpdateControls`, replace the line `upRow.append(checkBtn, updateBtn);` with:

```js
    upRow.append(checkBtn, updateBtn);
    /* Only when there is no app update to ride on — an app update swaps
       FFmpeg itself, so offering both would be two buttons for one job. */
    const comps = result && Array.isArray(result.components_update)
      ? result.components_update : [];
    if (isLocal && result && !result.available && comps.indexOf('ffmpeg') !== -1) {
      const compBtn = document.createElement('button');
      compBtn.className = 'cb-btn cb-btn--sm';
      compBtn.textContent = '⤓ Update FFmpeg';
      if (status && status.running) {
        setDisabled(compBtn, true, { reason: 'An update is already installing.' });
      } else {
        setDisabled(compBtn, false, { ttKey: 'about.update_components' });
        compBtn.addEventListener('click', () => aboutStartComponentsApply());
      }
      upRow.appendChild(compBtn);
    }
```

4c. In `subscribeUpdateEvents`, replace the `update.checked` handler with:

```js
    cbApi.on('update.checked', (p) => {
      if (!p || !state) return;
      state.update = p;
      renderOverviewUpdate();
      /* A silent check can also find a standalone component update; the
         Update FFmpeg button reads the check result, so give it this one. */
      if (Array.isArray(p.components_update) && p.components_update.length) {
        aboutUpdate.result = p;
        renderUpdate();
      }
      aboutRefreshUpdateStatus();
    });
```

- [ ] **Step 5: Run all frontend tests plus the strings test**

Run: `python -m pytest -q tests/test_web_*_client.py` and the file that pins `ui_strings` (find it with `graft grep "about.report" --in tests`).
Expected: all PASS. `test_the_update_controls_sit_on_three_lines` must still pass — the new button lives in `upRow`, not on a new line.

- [ ] **Step 6: Verify in the app window, both themes**

Run `python web_window.py --screen update`. From source the host reports `components_update: []`, so the button is absent; confirm the page renders, then temporarily force it visible from the window's devtools console:

```js
aboutUpdate.result = Object.assign({}, aboutUpdate.result, { available: false, components_update: ['ffmpeg'] }); renderUpdate();
```

Check the button sits on the same row as Check / Update Now in light **and** dark theme, and that its tooltip reads. Clicking it opens the "Updating components" modal, which closes on the host's refusal toast. Screenshot both themes for the task report.

- [ ] **Step 7: Commit**

```bash
git add web/app.js UI-design/ui-contract.json cratebuilder/ui_strings.py tests/test_web_about_client.py
git commit -m "feat(webui): Update FFmpeg button for a standalone component update"
```

---

### Task 7: Docs — retract the tkinter-only decision and close the analysis

**Model:** sonnet — prose only.

**Files:**
- Modify: `docs/specs/2026-08-29-webui-local-updater-design.md:101-108`
- Modify: `docs/specs/2026-09-21-ffmpeg-not-updated-by-web-app-analysis.md` (the `Status:` line)
- Check: `docs/Packaging_Guide.md` (`grep -n -i "self-update" docs/Packaging_Guide.md`) — if it says the desktop app keeps FFmpeg current, leave it (true again) but make sure it does not name the tkinter app as the thing doing it.

- [ ] **Step 1: Replace the section** `### FFmpeg piggyback stays tkinter-only` (heading and its one paragraph) with:

```markdown
### FFmpeg piggyback — retracted 2026-09-21

This section originally kept the FFmpeg swap in the tkinter monolith on the
reasoning that "the desktop app already keeps FFmpeg current". That stopped
being true the moment the web window became the shipped exe: no process was
left running `_maybe_update_ffmpeg`, and installs sat on a stale FFmpeg while
the activity log claimed otherwise (see
`2026-09-21-ffmpeg-not-updated-by-web-app-analysis.md`). The service now owns
the swap: inside `update_apply` between staging and the restart, and as the
standalone `update.components_apply` when the app is already current. Plan:
`plans/2026-09-21-webui-component-updates.md`.
```

- [ ] **Step 2: In the analysis note** change `Status: analysis only, no fix yet. Written to seed a fix plan.` to `Status: fixed by docs/specs/plans/2026-09-21-webui-component-updates.md (service-side swap + modal progress + standalone Update FFmpeg button).`

- [ ] **Step 3: Commit**

```bash
git add docs/specs/2026-08-29-webui-local-updater-design.md docs/specs/2026-09-21-ffmpeg-not-updated-by-web-app-analysis.md docs/Packaging_Guide.md
git commit -m "docs(updater): retract the tkinter-only FFmpeg decision; close the analysis"
```

---

## Final verification (main thread, after Task 7)

1. `python -m pytest -q tests/test_ffmpeg_update.py tests/test_ffmpeg_marker.py tests/test_activitylog.py tests/test_events.py tests/test_service_update.py tests/test_service_frozen.py tests/test_server.py tests/test_web_*_client.py` — all green. (The full suite only on the maintainer's explicit ask.)
2. `python web_window.py --screen update` in both themes — page renders, no console errors.
3. The real proof is the next nightly: the maintainer ships build 96 with `/build-update`; on this machine (FFmpeg 9.0.1 marker, manifest offering 9.0.2) the modal must show the FFmpeg row between "Preparing files…" and the restart, `C:\Program Files\DJ-CrateBuilder\ffmpeg.version` must read the 9.0.2 string afterwards, and `activity.log` must carry `UPDATED | Component: FFmpeg | 9.0.1… -> 9.0.2…` plus a build line **without** FFmpeg. Say so in the hand-off message; do not claim the swap works on a real install until that happens.

## Self-review notes

- D1–D3 → Task 3 (component phase, failure policy, cancel). D4 → Tasks 4 and 6. D5–D7 → Tasks 1 and 3 (`_ffmpeg_plan`, probe cache, adopt on check). D8 → Tasks 3 and 5. D9 → Tasks 2 and 3 (rows filtered by `handled`). D10 → Task 7.
- Names used across tasks: `ffmpeg_update_plan` (T1 → T3), `component_updated` / `component_failed` (T2 → T3), `_ffmpeg_plan` / `_run_component_updates` (T3 → T4), `components_update` (T3 → T6), `update.component` (T3 → T5), `aboutBeginApply({componentsOnly})` / `aboutPaintComponent` / `aboutSwapCancelForClose` (T5 → T6), `update.components_apply` (T4 → T6), `about.update_components` (T6).
- Pre-existing tests that must keep passing and why: `test_apply_happy_path` (no FFmpeg block → no `components` phase); `test_apply_logs_the_components_the_build_changes` (FFmpeg `same` → not listed; `handled` empty from source); the four progress-modal tests (no-arg `aboutBeginApply()` still opens the app variant); `test_the_update_controls_sit_on_three_lines` (button joins `upRow`).

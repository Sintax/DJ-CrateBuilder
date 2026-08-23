"""The update flow must never fire while the app is busy.

Build 58's rollout proved two failure shapes. Installing while Watch List
scans ran handed updater.exe a process that couldn't exit cleanly, and the
30-second "apply anyway" fallback turned the swap into a silent rollback to
the old build. And the FFmpeg swap's busy-defer had no retry: with
scan-on-startup keeping every launch check busy and a 12-hour check interval,
a pending FFmpeg update just looked broken.

Three guards now exist: the manual install prompt refuses while busy, the
final updater handoff drains in-flight work before quitting, and a deferred
FFmpeg swap retries once a minute until the app is idle.
"""
import types

from cratebuilder import updater_core as ucore


MANIFEST = {
    "version": "1.3", "build": 99,
    "url": "https://example.invalid/build-99.zip", "sha256": "5" * 64,
    "notes": "test build",
    "ffmpeg": {"version": "9.9-test_build+deadbeef",
               "url": "https://example.invalid/ffmpeg.zip",
               "sha256": "5" * 64},
}


# ══════════════════════════════════════════════════════════════════════════════
# The manual install prompt
# ══════════════════════════════════════════════════════════════════════════════
def test_a_busy_app_refuses_the_manual_install(app, cb_mod, monkeypatch):
    """THE build-58 incident, first half: installing over live scans."""
    monkeypatch.setattr(ucore, "can_self_update", lambda: True)
    infos, asked, runs = [], [], []
    monkeypatch.setattr(cb_mod.messagebox, "showinfo",
                        lambda *a, **k: infos.append(a))
    monkeypatch.setattr(cb_mod.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(app, "_run_update", lambda *a: runs.append(a))

    app._wl_scan_active = 1
    try:
        app._prompt_and_update(MANIFEST, 99)
    finally:
        app._wl_scan_active = 0

    assert runs == [] and asked == []
    assert len(infos) == 1   # told why, not silently ignored


def test_an_idle_app_still_gets_the_install_offer(app, cb_mod, monkeypatch):
    """The guard must not block the normal path — idle means the yes/no
    prompt appears exactly as before."""
    monkeypatch.setattr(ucore, "can_self_update", lambda: True)
    asked, runs = [], []
    monkeypatch.setattr(cb_mod.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(app, "_run_update", lambda *a: runs.append(a))

    app._prompt_and_update(MANIFEST, 99)

    assert len(asked) == 1
    assert runs == []        # user said no — nothing runs


# ══════════════════════════════════════════════════════════════════════════════
# The updater handoff
# ══════════════════════════════════════════════════════════════════════════════
def test_the_handoff_drains_in_flight_work_first(app, cb_mod, monkeypatch):
    """Something started after the download began (an auto-download timer, a
    scan): the handoff must cancel it and wait, not kill the process under it."""
    scheduled, quits, pops, notes = [], [], [], []
    monkeypatch.setattr(app, "after",
                        lambda ms, fn=None: scheduled.append(ms) or "after-id")
    monkeypatch.setattr(app, "_quit_app", lambda: quits.append(1))
    monkeypatch.setattr(cb_mod.subprocess, "Popen",
                        lambda *a, **k: pops.append(a))
    app._cancel_flag.clear()
    app._wl_scan_active = 1
    dlg = types.SimpleNamespace(destroy=lambda: None)
    try:
        app._launch_updater_and_quit(dlg, "staged", "ws", notes.append)
    finally:
        app._wl_scan_active = 0
        app._cancel_flag.clear()

    assert scheduled == [500]          # polls again, half a second out
    assert pops == [] and quits == []  # and nothing was killed
    # (the cancel-flag half of this behaviour is pinned by the next test)


def test_the_drain_sets_the_same_flag_cancel_does(app, monkeypatch):
    monkeypatch.setattr(app, "after", lambda *a, **k: "after-id")
    app._cancel_flag.clear()
    app._wl_scan_active = 1
    try:
        app._launch_updater_and_quit(
            types.SimpleNamespace(destroy=lambda: None), "staged", "ws")
        assert app._cancel_flag.is_set()
    finally:
        app._wl_scan_active = 0
        app._cancel_flag.clear()


def test_an_idle_handoff_launches_the_updater_and_quits(app, cb_mod, monkeypatch):
    quits, pops = [], []
    monkeypatch.setattr(app, "_quit_app", lambda: quits.append(1))
    monkeypatch.setattr(cb_mod.subprocess, "Popen",
                        lambda *a, **k: pops.append((a, k)))
    app._cancel_flag.clear()

    app._launch_updater_and_quit(
        types.SimpleNamespace(destroy=lambda: None), "staged", "ws")

    assert len(pops) == 1 and quits == [1]
    assert not app._cancel_flag.is_set()   # the idle path never touches it


# ══════════════════════════════════════════════════════════════════════════════
# The FFmpeg busy-defer retry
# ══════════════════════════════════════════════════════════════════════════════
def _ffmpeg_env(app, cb_mod, monkeypatch, tmp_path, action):
    """Point the FFmpeg decision at a fake frozen install answering *action*."""
    monkeypatch.setattr(ucore, "is_frozen", lambda: True)
    monkeypatch.setattr(ucore, "is_linux", lambda: False)
    monkeypatch.setattr(cb_mod, "bundled_ffmpeg_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ucore, "read_ffmpeg_version", lambda d: "old+aaaaaaaa")
    monkeypatch.setattr(ucore, "ffmpeg_update_action",
                        lambda *a, **k: action)
    app._ffmpeg_build_cache = "old"      # keep the probe subprocess out of it
    started = []
    monkeypatch.setattr(app, "_start_ffmpeg_update",
                        lambda *a: started.append(a))
    return started


def test_a_busy_defer_arms_the_retry_instead_of_giving_up(
        app, cb_mod, monkeypatch, tmp_path):
    """THE build-58 incident, second half: deferred silently, next chance 12h."""
    started = _ffmpeg_env(app, cb_mod, monkeypatch, tmp_path, "update")
    app._wl_scan_active = 1
    try:
        app._maybe_update_ffmpeg(MANIFEST)
        assert started == []
        assert app._ffmpeg_retry_manifest is MANIFEST
        assert app._ffmpeg_retry_after_id is not None
    finally:
        app._wl_scan_active = 0
        app._cancel_ffmpeg_retry()


def test_the_retry_fires_the_swap_once_the_app_goes_idle(
        app, cb_mod, monkeypatch, tmp_path):
    started = _ffmpeg_env(app, cb_mod, monkeypatch, tmp_path, "update")
    app._arm_ffmpeg_retry(MANIFEST)

    app._ffmpeg_retry_tick()

    assert len(started) == 1
    assert app._ffmpeg_retry_manifest is None      # satisfied — disarmed
    assert app._ffmpeg_retry_after_id is None


def test_a_manifest_that_stops_offering_disarms_the_loop(
        app, cb_mod, monkeypatch, tmp_path):
    """A swap that happened some other way (or an offer withdrawn) must not
    leave a retry ticking forever."""
    started = _ffmpeg_env(app, cb_mod, monkeypatch, tmp_path, "none")
    app._arm_ffmpeg_retry(MANIFEST)

    app._ffmpeg_retry_tick()

    assert started == []
    assert app._ffmpeg_retry_manifest is None
    assert app._ffmpeg_retry_after_id is None


def test_rearming_keeps_one_loop_but_freshens_the_manifest(app):
    try:
        app._arm_ffmpeg_retry({"a": 1})
        first = app._ffmpeg_retry_after_id
        newer = {"b": 2}
        app._arm_ffmpeg_retry(newer)
        assert app._ffmpeg_retry_after_id == first   # no stacking
        assert app._ffmpeg_retry_manifest is newer
    finally:
        app._cancel_ffmpeg_retry()


# ══════════════════════════════════════════════════════════════════════════════
# Global Cancel sweeps ghost cards
# ══════════════════════════════════════════════════════════════════════════════
def test_global_cancel_clears_a_ghost_scanning_card(app):
    """A row stuck at 'scanning' with no thread behind it: Cancel used to set
    a flag nobody was polling and the card kept its button forever."""
    wid = app._db.add_watchlist_channel(
        url="https://www.youtube.com/channel/UCghost/videos",
        display_name="Ghost", platform="YouTube", genre="(none)")
    app._db.update_watchlist_status(wid, "scanning")

    app._cancel_all_updates()

    assert app._db.get_watchlist_channel(wid)["status"] == "idle"

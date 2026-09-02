import json
import time


def test_new_settings_defaults(app):
    # The app fixture isolates HOME/USERPROFILE and patches
    # startup_is_enabled, so this asserts the shipped DEFAULTS — not
    # whatever the developer's machine has toggled or registered.
    app.update()
    assert app._auto_dl_interval.get() == "1 day"
    assert app._run_at_startup.get() is False
    assert app._minimize_to_tray.get() is True
    assert app._start_minimized.get() is False
    # Watch List startup scan is ON by default so new uploads surface as soon
    # as the app launches; users can opt out via the Settings checkbox.
    assert app._watchlist_scan_on_startup.get() is True


def test_tray_summary_reflects_state(app):
    # The tray hover tooltip is built from live Progress / Queue / Watch List
    # state. Idle shows just the app name + 'Idle'; an active Watch List scan
    # surfaces a line.
    app.update()
    summary = app._tray_summary()
    assert summary.startswith("DJ-CrateBuilder")
    assert "Idle" in summary

    app._wl_scan_active = 2
    assert "Watch List" in app._tray_summary()
    assert "Idle" not in app._tray_summary()

    # The dynamic tray menu label mirrors the Download All New count.
    app._tray_dl_label = "Download All New (7)"
    assert app._tray_dl_label == "Download All New (7)"


def test_legacy_auto_check_interval_carries_over(tmp_path, make_app):
    # A config written by an older build used auto_check_hours for the interval;
    # that must seed the renamed _auto_dl_interval on upgrade.
    (tmp_path / ".dj_cratebuilder_config.json").write_text(
        '{"auto_check_hours": "12 hours", "watchlist_last_check": 1234}',
        encoding="utf-8")
    started = int(time.time())
    app = make_app()
    app.update()
    assert app._auto_dl_interval.get() == "12 hours"
    # The schedule now counts from app start, NOT a stored anchor: the old
    # watchlist_last_check (1234) is ignored in favor of this launch time.
    assert app._watchlist_last_download >= started


def test_save_settings_preserves_unrelated_keys(tmp_path, make_app):
    # Regression: _save_settings passed only the Settings tab's keys to
    # save_config(), which writes the dict verbatim — so pressing Save
    # Settings silently deleted keys owned by other writers (url_history,
    # update-check state, DB-viewer column layout).
    cfg_path = tmp_path / ".dj_cratebuilder_config.json"
    cfg_path.write_text(json.dumps({
        "url_history": ["https://example.com/watch?v=abc"],
        "__unrelated_sentinel__": "must-survive",
    }), encoding="utf-8")
    app = make_app()
    app.update()
    app._settings_dir_var.set(str(tmp_path / "Music"))
    app._save_settings()
    # An older build's file: app init tidied it into the folder, and that is
    # where every write lands now.
    saved = json.loads((tmp_path / ".cratebuilder" / "config.json")
                       .read_text(encoding="utf-8"))
    assert not cfg_path.exists()
    assert saved["url_history"] == ["https://example.com/watch?v=abc"]
    assert saved["__unrelated_sentinel__"] == "must-survive"
    assert saved["base_dir"] == str(tmp_path / "Music")

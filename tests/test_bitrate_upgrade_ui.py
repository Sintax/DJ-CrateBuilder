"""The bitrate auto-upgrade checkbox: opt-in, persisted, and greyed with
the bitrate combo when 'no conversion' takes the MP3 bitrate away.
"""


def test_checkbox_exists_and_starts_unticked(app):
    cb = app._bitrate_upgrade_cb
    assert "Auto-upgrade bitrate" in str(cb.cget("text"))
    assert app._bitrate_auto_upgrade.get() is False


def test_toggling_the_var_persists_the_setting(app):
    app._bitrate_auto_upgrade.set(True)
    assert app._settings.get("bitrate_auto_upgrade") is True
    app._bitrate_auto_upgrade.set(False)
    assert app._settings.get("bitrate_auto_upgrade") is False


def test_download_policy_carries_the_flag(app):
    assert app._download_policy().bitrate_auto_upgrade is False
    app._bitrate_auto_upgrade.set(True)
    assert app._download_policy().bitrate_auto_upgrade is True


def test_no_conversion_greys_the_checkbox(app):
    app._no_conversion.set(True)
    app._on_no_conversion_toggle()
    assert "disabled" in app._bitrate_upgrade_cb.state()
    app._no_conversion.set(False)
    app._on_no_conversion_toggle()
    assert "disabled" not in app._bitrate_upgrade_cb.state()


def test_download_lock_disables_and_unlock_restores(app):
    app._set_download_lock(True)
    assert "disabled" in app._bitrate_upgrade_cb.state()
    app._set_download_lock(False)
    assert "disabled" not in app._bitrate_upgrade_cb.state()

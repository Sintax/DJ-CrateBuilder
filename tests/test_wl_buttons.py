"""Zero-count gating on the Watch List download buttons, and the cover-art
enable checkbox that now carries the 'off' mode the dropdown used to hold."""
import json


# ── _wl_count_button_style ────────────────────────────────────────────────────

def test_count_button_style_is_live_above_zero(cb_mod):
    style = cb_mod._wl_count_button_style(3)
    assert style["state"] == "normal"
    assert style["cursor"] == "hand2"


def test_count_button_style_is_dimmed_at_zero(cb_mod):
    style = cb_mod._wl_count_button_style(0)
    assert style["state"] == "disabled"
    assert style["fg"] == cb_mod.TEXT_DIM
    assert style["cursor"] == "arrow"


def test_count_button_style_honours_the_active_colour(cb_mod):
    assert cb_mod._wl_count_button_style(1, active_fg=cb_mod.TEXT_MED)["fg"] == \
        cb_mod.TEXT_MED


# ── cover art: checkbox owns 'off', dropdown owns formatting ──────────────────

def test_cover_art_dropdown_no_longer_offers_off(cb_mod):
    assert "off" not in cb_mod._COVER_ART_FORMAT_MODES
    assert "crop" in cb_mod._COVER_ART_FORMAT_MODES


def test_cover_art_unchecked_reports_off(app):
    app.update()
    app._cover_art_enabled.set(True)
    assert app._cover_art_mode_value() != "off"
    app._cover_art_enabled.set(False)
    # The download path still asks one question — is artwork off? — so the
    # checkbox has to answer it through the same accessor.
    assert app._cover_art_mode_value() == "off"
    # …without losing the chosen formatting.
    assert app._cover_art_format_value() != "off"


def test_legacy_off_mode_seeds_the_checkbox_unchecked(cb_mod, tmp_path,
                                                      make_app):
    """A config from before the checkbox existed encodes 'off' in the mode
    string; it must land as an unchecked box, not a dropdown value that no
    longer exists."""
    (tmp_path / ".dj_cratebuilder_config.json").write_text(
        '{"cover_art_mode": "off"}', encoding="utf-8")
    app = make_app()
    app.update()
    assert app._cover_art_enabled.get() is False
    assert app._cover_art_mode_value() == "off"
    assert app._cover_art_format_value() == \
        cb_mod.cb_artwork.DEFAULT_COVER_ART_MODE


def test_cover_art_toggle_persists_both_keys(tmp_path, app):
    app.update()
    app._cover_art_enabled.set(False)
    app.update()
    cfg = json.loads((tmp_path / ".cratebuilder" / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["cover_art_enabled"] is False
    assert cfg["cover_art_mode"] != "off"


def test_cover_art_dropdown_greys_out_when_disabled(app):
    app.update()
    app._cover_art_enabled.set(False)
    app.update()
    assert str(app._cover_art_combo.cget("state")) == "disabled"
    app._cover_art_enabled.set(True)
    app.update()
    assert str(app._cover_art_combo.cget("state")) == "readonly"

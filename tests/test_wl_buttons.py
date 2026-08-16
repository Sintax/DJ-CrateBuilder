"""Zero-count gating on the Watch List download buttons, and the cover-art
enable checkbox that now carries the 'off' mode the dropdown used to hold."""
import importlib.util
import json
import os
import sys

import pytest


def _load():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = m
    spec.loader.exec_module(m)
    return m


def _app(mod):
    try:
        return mod.MP3DownloaderApp()
    except Exception as e:
        pytest.skip(f"no display: {e}")


# ── _wl_count_button_style ────────────────────────────────────────────────────

def test_count_button_style_is_live_above_zero():
    style = _load()._wl_count_button_style(3)
    assert style["state"] == "normal"
    assert style["cursor"] == "hand2"


def test_count_button_style_is_dimmed_at_zero():
    mod = _load()
    style = mod._wl_count_button_style(0)
    assert style["state"] == "disabled"
    assert style["fg"] == mod.TEXT_DIM
    assert style["cursor"] == "arrow"


def test_count_button_style_honours_the_active_colour():
    mod = _load()
    assert mod._wl_count_button_style(1, active_fg=mod.TEXT_MED)["fg"] == \
        mod.TEXT_MED


# ── cover art: checkbox owns 'off', dropdown owns formatting ──────────────────

def test_cover_art_dropdown_no_longer_offers_off():
    mod = _load()
    assert "off" not in mod._COVER_ART_FORMAT_MODES
    assert "crop" in mod._COVER_ART_FORMAT_MODES


def test_cover_art_unchecked_reports_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    mod = _load()
    app = _app(mod); app.update()
    app._cover_art_enabled.set(True)
    assert app._cover_art_mode_value() != "off"
    app._cover_art_enabled.set(False)
    # The download path still asks one question — is artwork off? — so the
    # checkbox has to answer it through the same accessor.
    assert app._cover_art_mode_value() == "off"
    # …without losing the chosen formatting.
    assert app._cover_art_format_value() != "off"
    app.destroy()


def test_legacy_off_mode_seeds_the_checkbox_unchecked(tmp_path, monkeypatch):
    """A config from before the checkbox existed encodes 'off' in the mode
    string; it must land as an unchecked box, not a dropdown value that no
    longer exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".dj_cratebuilder_config.json").write_text(
        '{"cover_art_mode": "off"}', encoding="utf-8")
    mod = _load()
    app = _app(mod); app.update()
    assert app._cover_art_enabled.get() is False
    assert app._cover_art_mode_value() == "off"
    assert app._cover_art_format_value() == mod.cb_artwork.DEFAULT_COVER_ART_MODE
    app.destroy()


def test_cover_art_toggle_persists_both_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    mod = _load()
    app = _app(mod); app.update()
    app._cover_art_enabled.set(False)
    app.update()
    cfg = json.loads((tmp_path / ".dj_cratebuilder_config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["cover_art_enabled"] is False
    assert cfg["cover_art_mode"] != "off"
    app.destroy()


def test_cover_art_dropdown_greys_out_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    mod = _load()
    app = _app(mod); app.update()
    app._cover_art_enabled.set(False)
    app.update()
    assert str(app._cover_art_combo.cget("state")) == "disabled"
    app._cover_art_enabled.set(True)
    app.update()
    assert str(app._cover_art_combo.cget("state")) == "readonly"
    app.destroy()

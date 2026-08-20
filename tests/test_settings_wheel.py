"""Settings dropdowns must not change value on a scroll-wheel notch.

ttk's TCombobox class binding steps the value on every notch, and every
Settings dropdown autosaves, so scrolling the page used to rewrite the
config silently. A widget-level binding now breaks the chain and forwards
the scroll to the page instead.
"""
import pytest


def _combos(app, cb_mod):
    return {name: getattr(app, name) for name in cb_mod.MP3DownloaderApp
            ._SETTINGS_COMBOS}


def test_every_settings_combo_is_covered(app, cb_mod):
    combos = _combos(app, cb_mod)
    assert len(combos) == 8
    for name, combo in combos.items():
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            assert combo.bind(seq), f"{name} has no {seq} binding"


def test_the_main_tab_dropdown_is_left_alone(app):
    """Settings-tab only — the Main tab's skip-mode dropdown keeps ttk's
    stock wheel behaviour."""
    assert not app._skip_mode_combo.bind("<MouseWheel>")


@pytest.mark.parametrize("delta", [120, -120])
def test_a_wheel_notch_leaves_the_value_alone(app, cb_mod, show, delta):
    exercised = []
    for name in cb_mod.MP3DownloaderApp._SETTINGS_COMBOS:
        combo = getattr(app, name)
        if str(combo.cget("state")) == "disabled":
            continue          # a disabled combo ignores the wheel regardless
        show(combo)
        before = combo.get()
        combo.event_generate("<MouseWheel>", delta=delta, when="now")
        assert combo.get() == before, f"{name} changed on a wheel notch"
        exercised.append(name)
    # A stock readonly ttk.Combobox steps its value on every notch, so this
    # only means something while real, enabled dropdowns are being wheeled.
    assert len(exercised) >= 5, exercised


def test_the_page_still_scrolls_over_a_dropdown(app, show, monkeypatch):
    scrolled = []
    monkeypatch.setattr(app._settings_canvas, "yview_scroll",
                        lambda n, what: scrolled.append((n, what)))
    combo = show(app._auto_dl_combo)
    combo.event_generate("<MouseWheel>", delta=-120, when="now")
    assert scrolled == [(1, "units")]
    combo.event_generate("<MouseWheel>", delta=120, when="now")
    assert scrolled[-1] == (-1, "units")

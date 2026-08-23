"""Dropdowns must not change value on a scroll-wheel notch.

ttk's TCombobox class binding steps the value on every notch, so scrolling a
page over a dropdown used to silently change it. On Settings that also
autosaved, rewriting the config; on the Main tab it swapped out the URL about
to be queued — the URL field is an editable combobox, so a notch over it
replaces whatever was typed with an entry from the history.

A widget-level binding now breaks the chain on both tabs and forwards the
scroll to that tab's canvas instead.
"""
import pytest


TABS = (("_SETTINGS_COMBOS", "_settings_canvas", 8),
        ("_MAIN_COMBOS", "_main_canvas", 3))


def _names(cb_mod, attr):
    return getattr(cb_mod.MP3DownloaderApp, attr)


@pytest.mark.parametrize("attr,canvas,count", TABS)
def test_every_dropdown_on_the_tab_is_covered(app, cb_mod, attr, canvas,
                                              count):
    names = _names(cb_mod, attr)
    assert len(names) == count
    for name in names:
        combo = getattr(app, name)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            assert combo.bind(seq), f"{name} has no {seq} binding"


def test_the_two_lists_do_not_overlap(cb_mod):
    """Each dropdown forwards to exactly one canvas; a name in both lists
    would bind twice and scroll the wrong page."""
    settings = set(_names(cb_mod, "_SETTINGS_COMBOS"))
    main = set(_names(cb_mod, "_MAIN_COMBOS"))
    assert settings & main == set()


@pytest.mark.parametrize("attr,canvas,count", TABS)
@pytest.mark.parametrize("delta", [120, -120])
def test_a_wheel_notch_leaves_the_value_alone(app, cb_mod, show, delta, attr,
                                              canvas, count):
    exercised = []
    for name in _names(cb_mod, attr):
        combo = getattr(app, name)
        if str(combo.cget("state")) == "disabled":
            continue          # a disabled combo ignores the wheel regardless
        show(combo)
        before = combo.get()
        combo.event_generate("<MouseWheel>", delta=delta, when="now")
        assert combo.get() == before, f"{name} changed on a wheel notch"
        exercised.append(name)
    # A stock ttk.Combobox steps its value on every notch, so this only means
    # something while real, enabled dropdowns are being wheeled.
    assert len(exercised) >= 3, exercised


@pytest.mark.parametrize("attr,canvas,count", TABS)
def test_the_page_still_scrolls_over_a_dropdown(app, cb_mod, show, monkeypatch,
                                                attr, canvas, count):
    """Swallowing the notch must not cost the user the scroll they wanted —
    it is forwarded to that tab's own canvas, never the other tab's."""
    scrolled = []
    monkeypatch.setattr(getattr(app, canvas), "yview_scroll",
                        lambda n, what: scrolled.append((n, what)))
    combo = show(getattr(app, _names(cb_mod, attr)[0]))

    combo.event_generate("<MouseWheel>", delta=-120, when="now")
    assert scrolled == [(1, "units")]
    combo.event_generate("<MouseWheel>", delta=120, when="now")
    assert scrolled[-1] == (-1, "units")


def test_the_url_field_keeps_what_was_typed_in_it(app, show):
    """The Main tab's sharp edge. _url_entry is editable and carries the URL
    history as its values, so a stray notch over it used to replace a pasted
    link with an old one — and the next Add to Batch would queue the wrong
    channel."""
    app._url_history = ["https://yt/old-one", "https://yt/older-still"]
    app._url_entry.config(values=app._url_history)
    show(app._url_entry)
    app._url_var.set("https://yt/the-one-i-just-pasted")

    for delta in (-120, 120, -120):
        app._url_entry.event_generate("<MouseWheel>", delta=delta, when="now")

    assert app._url_var.get() == "https://yt/the-one-i-just-pasted"

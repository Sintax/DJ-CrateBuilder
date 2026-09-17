"""About tab: the author credit, with no contact address anywhere on it."""
import tkinter as tk


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _labels(app, text):
    return [w for w in _walk(app)
            if isinstance(w, tk.Label) and w.cget("text") == text]


def test_created_by_shows_the_name_not_an_email(cb_mod):
    assert cb_mod.ABOUT_CREATED_BY == "Corrupt Sintax"
    assert "@" not in cb_mod.ABOUT_CREATED_BY
    assert not hasattr(cb_mod, "ABOUT_CONTACT_EMAIL")


def test_about_fields_are_label_value_pairs(cb_mod):
    by_label = {f[0]: f for f in cb_mod.ABOUT_FIELDS}
    assert by_label["Created by"] == ("Created by", cb_mod.ABOUT_CREATED_BY)
    assert by_label["Built with"] == ("Built with", cb_mod.ABOUT_DESCRIPTION)
    assert all(len(f) == 2 for f in cb_mod.ABOUT_FIELDS)


def test_built_with_names_the_harness(cb_mod):
    assert cb_mod.ABOUT_DESCRIPTION == (
        "Vibe-Coded entirely with Claude-Code inside the VS-Code harness.")


def test_the_credit_renders_the_name_and_no_address(shared_app, cb_mod):
    name = _labels(shared_app, cb_mod.ABOUT_CREATED_BY)
    assert len(name) == 1
    # The name's value column used to hold a second, underlined mailto label.
    assert [w for w in name[0].master.winfo_children()
            if isinstance(w, tk.Label)] == name

"""About tab: the author credit and its clickable mailto sub-line."""
import tkinter as tk


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _labels(app, text):
    return [w for w in _walk(app)
            if isinstance(w, tk.Label) and w.cget("text") == text]


def test_created_by_shows_the_name_not_the_email(cb_mod):
    assert cb_mod.ABOUT_CREATED_BY == "Corrupt Sintax"
    assert "@" not in cb_mod.ABOUT_CREATED_BY
    assert "@" in cb_mod.ABOUT_CONTACT_EMAIL


def test_about_fields_carry_the_email_as_an_optional_third_element(cb_mod):
    by_label = {f[0]: f for f in cb_mod.ABOUT_FIELDS}
    assert by_label["Created by"][1] == cb_mod.ABOUT_CREATED_BY
    assert by_label["Created by"][2] == cb_mod.ABOUT_CONTACT_EMAIL
    assert len(by_label["Built with"]) == 2   # rows without an email still work


def test_name_and_email_render_as_two_stacked_labels(shared_app, cb_mod):
    name = _labels(shared_app, cb_mod.ABOUT_CREATED_BY)
    mail = _labels(shared_app, cb_mod.ABOUT_CONTACT_EMAIL)
    assert len(name) == 1 and len(mail) == 1
    assert mail[0].master is name[0].master        # same value column
    assert mail[0].cget("cursor") == "hand2"
    assert "underline" in str(mail[0].cget("font"))


def test_clicking_the_email_opens_a_mailto_url(shared_app, cb_mod,
                                               monkeypatch, show):
    opened = []
    monkeypatch.setattr(cb_mod.webbrowser, "open", opened.append)
    mail = show(_labels(shared_app, cb_mod.ABOUT_CONTACT_EMAIL)[0])
    mail.event_generate("<Button-1>", when="now")
    assert opened == [f"mailto:{cb_mod.ABOUT_CONTACT_EMAIL}"]

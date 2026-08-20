"""The duplicate-rows warning: when it fires, when it stays quiet, and that
it never touches rows on its own."""
import json

import pytest


class _StubDB:
    """Stands in for DownloadsDatabase. The real one is the user's library."""

    def __init__(self, indexed=False, files=3, extra=7):
        self.has_unique_path_index = indexed
        self._counts = (files, extra)

    def count_duplicate_downloads(self):
        return self._counts


def _wire(app, cb_mod, monkeypatch, *, indexed=False, files=3, extra=7,
          answer=True):
    """Point the app at a stub DB and capture what the warning does."""
    app.update()
    app._db = _StubDB(indexed=indexed, files=files, extra=extra)
    seen = {"asked": None, "started": None}

    def fake_ask(f, e):
        seen["asked"] = (f, e)
        return answer

    monkeypatch.setattr(app, "_ask_dedupe", fake_ask)
    monkeypatch.setattr(app, "_start_dedupe",
                        lambda f, e: seen.update(started=(f, e)))
    return seen


def _saved_count(app, tmp_path):
    path = tmp_path / ".dj_cratebuilder_config.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg.get(app._DEDUPE_PROMPT_KEY)


def test_warns_and_runs_when_duplicates_block_the_index(
        app, cb_mod, monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch, files=3, extra=7, answer=True)
    app._prompt_dedupe_after_update()
    # The numbers the user is agreeing to are the ones the dialog is handed.
    assert seen["asked"] == (3, 7)
    assert seen["started"] == (3, 7)


def test_declining_runs_nothing(app, cb_mod, monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch, answer=False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None
    assert seen["started"] is None


def test_silent_when_the_index_is_already_in_place(app, cb_mod, monkeypatch):
    """has_unique_path_index True means one row per file is already enforced —
    there is nothing to offer, so a clean install must never see this."""
    seen = _wire(app, cb_mod, monkeypatch, indexed=True)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None
    assert seen["started"] is None


def test_silent_when_the_count_comes_back_clean(app, cb_mod, monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch, files=0, extra=0)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None


def test_warns_only_once_per_count(app, cb_mod, tmp_path, monkeypatch):
    """Ignoring the warning must not mean seeing it again on every launch."""
    seen = _wire(app, cb_mod, monkeypatch, answer=False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None
    assert _saved_count(app, tmp_path) == 7

    seen["asked"] = None
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None, "warned twice at the same count"


def test_warns_again_once_the_count_moves(app, cb_mod, tmp_path, monkeypatch):
    """Every download against an unprotected database adds more duplicates,
    so a changed number is new information and worth re-raising."""
    seen = _wire(app, cb_mod, monkeypatch, answer=False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] == (3, 7)

    seen["asked"] = None
    app._db = _StubDB(files=4, extra=9)
    app._prompt_dedupe_after_update()
    assert seen["asked"] == (4, 9)
    assert _saved_count(app, tmp_path) == 9


def test_the_setting_switches_the_warning_off_entirely(
        app, cb_mod, tmp_path, monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch)
    app._dupe_check_enabled.set(False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None
    # ...and having stayed quiet, it has not burned this count's one warning.
    assert _saved_count(app, tmp_path) is None
    # Switching it back on brings the warning straight back.
    app._dupe_check_enabled.set(True)
    app._prompt_dedupe_after_update()
    assert seen["asked"] == (3, 7)


def test_the_setting_is_on_by_default_and_persists(app, cb_mod, tmp_path):
    """Fresh installs and updates both start with the check on — duplicate
    protection is off while duplicates exist, so silence is the wrong
    default."""
    assert app._settings.get("dupe_check_enabled") is True
    assert app._dupe_check_enabled.get() is True
    app._dupe_check_enabled.set(False)
    cfg = json.loads(
        (tmp_path / ".dj_cratebuilder_config.json").read_text(encoding="utf-8"))
    assert cfg["dupe_check_enabled"] is False


def test_the_answer_is_recorded_before_the_run(app, cb_mod, tmp_path,
                                               monkeypatch):
    """If the de-dup itself fails, the user still must not be re-warned on
    every launch — so the count lands before the work starts."""
    seen = _wire(app, cb_mod, monkeypatch, answer=True)
    app._prompt_dedupe_after_update()
    assert seen["started"] is not None
    assert _saved_count(app, tmp_path) == 7


def test_stays_quiet_while_a_rebuild_is_running(app, cb_mod, tmp_path,
                                                monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch)
    app._rebuild_in_progress = True
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None
    # ...and having stayed quiet, it has not burned this count's one warning.
    assert _saved_count(app, tmp_path) is None


def test_stays_quiet_while_an_artwork_backfill_is_running(app, cb_mod,
                                                          monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch)
    app._artwork_session = object()
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None
    app._artwork_session = None


def test_a_broken_database_does_not_break_startup(app, cb_mod, monkeypatch):
    """This runs on a timer during launch; an exception here must not escape."""
    class Exploding:
        @property
        def has_unique_path_index(self):
            raise RuntimeError("db is gone")

    app._db = Exploding()
    monkeypatch.setattr(app, "_ask_dedupe",
                        lambda *a: pytest.fail("should not ask"))
    app._prompt_dedupe_after_update()      # must simply return


# ── The dialog itself ──────────────────────────────────────────────────────

def _dialog(app, monkeypatch, press):
    """Open _ask_dedupe, run *press* against the live dialog, return the
    answer. wait_window is stubbed out so the dialog never blocks the test."""
    import tkinter as tk
    holder = {}

    def fake_wait(widget):
        holder["dlg"] = widget
        press(widget)

    monkeypatch.setattr(app, "wait_window", fake_wait)
    answer = app._ask_dedupe(3, 7)
    return answer, holder.get("dlg")


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _button(dlg, needle):
    import tkinter as tk
    for w in _walk(dlg):
        if isinstance(w, tk.Button) and needle in str(w.cget("text")):
            return w
    raise AssertionError(f"no button matching {needle!r}")


def test_the_dialog_shows_the_counts(app, monkeypatch):
    texts = []

    def press(dlg):
        import tkinter as tk
        texts.extend(str(w.cget("text")) for w in _walk(dlg)
                     if isinstance(w, tk.Label))
        dlg.destroy()

    _dialog(app, monkeypatch, press)
    joined = " ".join(texts)
    assert "7" in joined and "3" in joined


def test_remove_now_answers_yes_and_not_now_answers_no(app, monkeypatch):
    yes, _ = _dialog(app, monkeypatch,
                     lambda dlg: _button(dlg, "Remove Duplicates").invoke())
    assert yes is True
    no, _ = _dialog(app, monkeypatch,
                    lambda dlg: _button(dlg, "Not Now").invoke())
    assert no is False


def test_closing_the_dialog_answers_no(app, monkeypatch):
    answer, _ = _dialog(app, monkeypatch, lambda dlg: dlg.destroy())
    assert answer is False


def test_the_dialog_checkbox_switches_the_setting_off(app, monkeypatch,
                                                      tmp_path):
    """Ticking "Don't check for this again" in the dialog is the same control
    as the Settings checkbox — they share one variable."""
    import tkinter as tk
    from tkinter import ttk

    def press(dlg):
        cb = next(w for w in _walk(dlg) if isinstance(w, ttk.Checkbutton))
        assert "again" in str(cb.cget("text"))
        # The box reads inverted — checking it means "stop warning me" — so
        # with the warning on it must start empty, not ticked.
        assert not cb.instate(["selected"])
        cb.invoke()                       # tick "don't check again"
        assert cb.instate(["selected"])
        _button(dlg, "Not Now").invoke()

    assert app._dupe_check_enabled.get() is True
    answer, _ = _dialog(app, monkeypatch, press)
    assert answer is False
    assert app._dupe_check_enabled.get() is False
    assert app._settings.get("dupe_check_enabled") is False
    # The Settings-tab checkbox is driven by the very same variable.
    assert str(app._dupe_check_cb.cget("variable")) == \
        str(app._dupe_check_enabled)

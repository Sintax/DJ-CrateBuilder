"""The post-update duplicate prompt: when it fires, when it stays quiet, and
that it never touches rows on its own."""
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
    """Point the app at a stub DB and capture what the prompt does."""
    app.update()
    app._db = _StubDB(indexed=indexed, files=files, extra=extra)
    seen = {"asked": None, "started": None}

    def fake_ask(title, message, **kw):
        seen["asked"] = (title, message)
        return answer

    monkeypatch.setattr(cb_mod.messagebox, "askyesno", fake_ask)
    monkeypatch.setattr(app, "_start_dedupe",
                        lambda f, e: seen.update(started=(f, e)))
    return seen


def _saved_build(app, tmp_path):
    path = tmp_path / ".dj_cratebuilder_config.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg.get(app._DEDUPE_PROMPT_KEY)


def test_prompts_and_runs_when_duplicates_block_the_index(
        app, cb_mod, monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch, files=3, extra=7, answer=True)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None
    title, message = seen["asked"]
    assert "Duplicate" in title
    # The numbers the user is agreeing to have to be in front of them.
    assert "7" in message and "3" in message
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


def test_asks_only_once_per_build(app, cb_mod, tmp_path, monkeypatch):
    """Declining must not mean being asked again on every single launch."""
    seen = _wire(app, cb_mod, monkeypatch, answer=False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None
    assert _saved_build(app, tmp_path) == cb_mod.APP_BUILD

    seen["asked"] = None
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None, "asked twice on the same build"


def test_asks_again_after_the_next_update(app, cb_mod, tmp_path, monkeypatch):
    """The answer is recorded against a build, so a later update re-offers."""
    seen = _wire(app, cb_mod, monkeypatch, answer=False)
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None

    cfg_path = tmp_path / ".dj_cratebuilder_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg[app._DEDUPE_PROMPT_KEY] = cb_mod.APP_BUILD - 1   # an older build
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    seen["asked"] = None
    app._prompt_dedupe_after_update()
    assert seen["asked"] is not None


def test_the_answer_is_recorded_before_the_run(app, cb_mod, tmp_path,
                                               monkeypatch):
    """If the de-dup itself fails, the user still must not be re-prompted on
    every launch — so the build stamp lands before the work starts."""
    seen = _wire(app, cb_mod, monkeypatch, answer=True)
    app._prompt_dedupe_after_update()
    assert seen["started"] is not None
    assert _saved_build(app, tmp_path) == cb_mod.APP_BUILD


def test_stays_quiet_while_a_rebuild_is_running(app, cb_mod, tmp_path,
                                                monkeypatch):
    seen = _wire(app, cb_mod, monkeypatch)
    app._rebuild_in_progress = True
    app._prompt_dedupe_after_update()
    assert seen["asked"] is None
    # ...and having stayed quiet, it has not burned this build's one ask.
    assert _saved_build(app, tmp_path) is None


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
    monkeypatch.setattr(cb_mod.messagebox, "askyesno",
                        lambda *a, **k: pytest.fail("should not ask"))
    app._prompt_dedupe_after_update()      # must simply return

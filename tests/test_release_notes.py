"""The sticky release notice — the one warning that must ride every build.

An install on build 58 or older runs the old updater: it waits 30s for the app
to exit, then swaps files whether it did or not. A Watch List scan still
running holds files open, the swap rolls back, and the app relaunches on the
same build — which offers the same update again. Users can loop on that
forever, and the only channel that reaches them is the manifest's notes,
because they are running code we can no longer change.

``scripts/`` is maintainer-local (gitignored), so this file skips wholesale
when the script isn't present — a contributor's checkout still runs green.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "release.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_SCRIPT),
    reason="scripts/release.py is maintainer-local (gitignored)")


@pytest.fixture(scope="module")
def rel():
    spec = importlib.util.spec_from_file_location("cb_release_notes", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_release_notes"] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════════
# What the notice says
# ══════════════════════════════════════════════════════════════════════════════
def test_the_notice_shouts(rel):
    """Capitals are the only emphasis a native messagebox renders — it has no
    bold and no markup — so the warning must actually be upper-case."""
    letters = [c for c in rel.STICKY_NOTICE if c.isalpha()]
    assert letters, "the notice has no text"
    assert all(c.isupper() for c in letters)


@pytest.mark.parametrize("phrase", [
    "WATCH LIST", "SCAN", "CANCEL", "LOOP", "BEFORE YOU UPDATE",
])
def test_the_notice_still_says_the_load_bearing_things(rel, phrase):
    """Each phrase is a step the user cannot work out on their own: which tab,
    what to press, and why it matters. Rewording is fine; dropping one is not."""
    assert phrase in rel.STICKY_NOTICE


def test_the_notice_is_plain_ascii(rel):
    """It is printed by this script (a Windows console may not be UTF-8) and
    rendered by a native dialog. Neither is a safe place for exotic glyphs."""
    rel.STICKY_NOTICE.encode("ascii")


def test_no_line_is_too_wide_for_the_dialog(rel):
    """messagebox does not wrap generously; long lines stretch the dialog off
    the screen instead of folding."""
    assert max(len(ln) for ln in rel.STICKY_NOTICE.splitlines()) <= 72


# ══════════════════════════════════════════════════════════════════════════════
# How it is combined with the typed notes
# ══════════════════════════════════════════════════════════════════════════════
def test_the_notice_comes_first(rel):
    out = rel.compose_notes("Fixed the Watch List crash.")
    assert out.startswith(rel.STICKY_NOTICE)
    assert out.endswith("Fixed the Watch List crash.")
    assert "\n\n" in out          # a blank line separates the two


@pytest.mark.parametrize("typed", ["", "   ", None])
def test_empty_notes_still_ship_the_notice(rel, typed):
    """A build published with no notes must not lose the warning."""
    assert rel.compose_notes(typed) == rel.STICKY_NOTICE


def test_clearing_the_constant_leaves_only_the_typed_notes(rel, monkeypatch):
    """The notice is meant to be retired once builds <=58 are gone, and
    clearing it must not leave stray blank lines behind."""
    monkeypatch.setattr(rel, "STICKY_NOTICE", "")
    assert rel.compose_notes("Just the notes.") == "Just the notes."
    assert rel.compose_notes("") == ""

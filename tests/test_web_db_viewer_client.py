"""web/app.js: the Database viewer's client-side invariants that the Python
service can't enforce — the watchlist link scheme gate, the id-keyed artwork
preview call, and Expand All staying a group-only operation."""
import json
import os
import shutil
import subprocess

import pytest

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "web", "app.js")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


# ── watchlist link scheme gate ───────────────────────────────────────────────

def test_watchlist_link_never_reaches_an_href_or_window_open_ungated(app_js):
    """row.link is stored text and this page can reach fs.reveal, so a
    javascript: value there would be arbitrary code on the host."""
    assert "a.href = row.link" not in app_js
    assert "window.open(row.link" not in app_js
    assert "function dbSafeLink" in app_js


def test_db_safe_link_accepts_only_http_and_https(app_js, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the source-contract test still runs")
    start = app_js.index("function dbSafeLink")
    end = app_js.index("\n  }", start) + len("\n  }")
    script = tmp_path / "safelink.mjs"
    script.write_text(
        app_js[start:end] + "\n" + "console.log(JSON.stringify([" +
        "'https://youtube.com/@chan', 'http://x/y', 'HTTPS://X/Y',"
        "\"javascript:cbApi.call('fs.reveal',{mode:'open'})\","
        "'data:text/html,<script>', 'file:///C:/Windows/System32/cmd.exe', ''"
        "].map(dbSafeLink)));", encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         check=True).stdout
    assert json.loads(out) == ["https://youtube.com/@chan", "http://x/y",
                               "HTTPS://X/Y", "", "", "", ""]


# ── artwork preview is keyed by row id, never by a path ──────────────────────

def test_artwork_preview_is_called_with_an_id_not_a_path(app_js):
    assert "call('db.artwork_preview', { id: row.id })" in app_js
    assert "db.artwork_preview', { path:" not in app_js


# ── Expand All expands groups, and stops there ───────────────────────────────

def test_expand_all_stops_at_leaf_level_and_never_fetches_rows(app_js):
    """The registry calls it "Expand all groups"; descending into leaves
    would be one db.query per channel and the whole library in the DOM."""
    start = app_js.index("async function dbExpandRecursive")
    body = app_js[start:app_js.index("async function dbExpandAllDownloads")]
    assert "=== null) return;" in body
    assert "dbLoadRows" not in body
    assert "dbLoadGroups" in body


def test_expand_all_disables_its_button_with_a_reason(app_js):
    start = app_js.index("async function dbExpandAllDownloads")
    body = app_js[start:app_js.index("function dbCollapseAllDownloads")]
    assert "setDisabled(btn, true, { reason:" in body
    assert "setDisabled(btn, false" in body

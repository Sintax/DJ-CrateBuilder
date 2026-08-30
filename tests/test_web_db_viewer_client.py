"""web/app.js: the Database viewer's client-side invariants that the Python
service can't enforce — the watchlist link scheme gate, the id-keyed artwork
preview call, and Expand All staying a group-only operation.

The behavioural checks slice the real functions out of app.js verbatim and run
them in Node against a stub `call()`, rather than matching source text: a
string match passes again the moment someone reformats the line it names, and
misses defects inside the function it claims to guard."""
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


def _slice(app_js, start, end):
    """The source between two markers, verbatim — the functions under test."""
    a = app_js.index(start)
    return app_js[a:app_js.index(end, a)]


def _run_node(tmp_path, name, source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         check=True).stdout
    return json.loads(out)


# ── watchlist link scheme gate ───────────────────────────────────────────────

def test_watchlist_link_never_reaches_an_href_or_window_open_ungated(app_js):
    """row.link is stored text and this page can reach fs.reveal, so a
    javascript: value there would be arbitrary code on the host."""
    assert "a.href = row.link" not in app_js
    assert "window.open(row.link" not in app_js
    assert "function dbSafeLink" in app_js


def test_db_safe_link_accepts_only_http_and_https(app_js, tmp_path):
    start = app_js.index("function dbSafeLink")
    end = app_js.index("\n  }", start) + len("\n  }")
    result = _run_node(tmp_path, "safelink.mjs", app_js[start:end] + "\n" +
        "console.log(JSON.stringify([" +
        "'https://youtube.com/@chan', 'http://x/y', 'HTTPS://X/Y',"
        "\"javascript:cbApi.call('fs.reveal',{mode:'open'})\","
        "'data:text/html,<script>', 'file:///C:/Windows/System32/cmd.exe', ''"
        "].map(dbSafeLink)));")
    assert result == ["https://youtube.com/@chan", "http://x/y",
                      "HTTPS://X/Y", "", "", "", ""]


# ── artwork preview is keyed by row id, never by a path ──────────────────────

def test_artwork_preview_is_called_with_an_id_not_a_path(app_js):
    assert "call('db.artwork_preview', { id: row.id })" in app_js
    assert "db.artwork_preview', { path:" not in app_js


# ── Expand All expands groups, and stops there ───────────────────────────────

def test_expand_all_never_mentions_the_row_loader(app_js):
    """A structural invariant that survives without node: descending into
    leaves would be one db.query per channel and the whole library in the
    DOM. dbExpandRecursive must not be able to reach dbLoadRows at all."""
    body = _slice(app_js, "async function dbExpandRecursive",
                  "async function dbExpandAllDownloads")
    assert "dbLoadRows" not in body
    assert "dbLoadGroups" in body


# 2 platforms x 8 genres x 25 channels = 400 leaf groups, 418 groups in all —
# the shape the paging design exists for.
_EXPAND_HARNESS = """
const PLATFORMS = ['YouTube', 'SoundCloud'];
const GENRES = Array.from({ length: 8 }, (_, i) => 'Genre' + i);
const CHANNELS = Array.from({ length: 25 }, (_, i) => 'Chan' + i);

const calls = [];
const rowLoads = [];
const setDisabledCalls = [];
function $(sel) { return { sel }; }
function setDisabled(el, flag, opts) { setDisabledCalls.push([el.sel, flag, opts]); }
function dbRenderDownloadsTree() {}
async function dbLoadRows(node) { rowLoads.push(node.path); }
async function call(method, params) {
  calls.push(method);
  if (method !== 'db.groups') throw new Error('unexpected method ' + method);
  const f = params.filters || {};
  const keys = !f.platform ? PLATFORMS : !f.genre ? GENRES : CHANNELS;
  return { groups: keys.map((k) => ({ key: k, label: k, count: 40 })) };
}
const DB_PAGE_SIZE = 200;
%(hierarchy)s
const dbState = {
  downloads: { groupPreset: 'Platform \\u203a Genre \\u203a Channel',
    platform: 'All platforms', genre: 'All genres', search: '',
    sortCol: 'downloaded', sortDesc: true, root: null },
};
%(filters)s
%(expand)s

await dbDownloadsReload();
const openTrips = calls.length;
await dbExpandAllDownloads();

let groups = 0, expanded = 0, leaves = 0, leavesExpanded = 0, openButEmpty = 0;
(function walk(n) {
  if (n.depth >= 0) {
    groups += 1;
    if (n.expanded) expanded += 1;
    if (dbNextHierarchyKey(dbState.downloads.groupPreset, n.path) === null) {
      leaves += 1;
      if (n.expanded) leavesExpanded += 1;
    }
    if (n.expanded && n.children === null && n.rows === null) openButEmpty += 1;
  }
  if (n.children) n.children.forEach(walk);
})(dbState.downloads.root);

console.log(JSON.stringify({
  openTrips, expandTrips: calls.length - openTrips, rowLoads: rowLoads.length,
  groups, expanded, leaves, leavesExpanded, openButEmpty, setDisabledCalls,
}));
"""


@pytest.fixture(scope="module")
def expand_result(app_js, tmp_path_factory):
    source = _EXPAND_HARNESS % {
        "hierarchy": _slice(app_js, "  const GROUP_HIERARCHY = {",
                            "  const DB_ARTWORK_FILTERS"),
        "filters": _slice(app_js, "  function dbTreeRootFilters()",
                          "  async function dbLoadRows"),
        "expand": _slice(app_js, "  async function dbLoadGroups",
                         "  function dbCollapseAllDownloads"),
    }
    return _run_node(tmp_path_factory.mktemp("expand"), "expand.mjs", source)


def test_expand_all_leaves_leaf_groups_honestly_collapsed(expand_result):
    """A leaf group marked expanded draws an open caret over nothing, and the
    next click collapses it instead of loading its rows — two clicks and a
    confusing no-op per channel. Every leaf must end the run collapsed."""
    assert expand_result["leaves"] == 400
    assert expand_result["leavesExpanded"] == 0
    assert expand_result["openButEmpty"] == 0


def test_expand_all_expands_every_non_leaf_group(expand_result):
    assert expand_result["groups"] == 418
    assert expand_result["expanded"] == 18       # 2 platforms + 16 genres


def test_expand_all_costs_one_round_trip_per_non_leaf_group_and_no_rows(
        expand_result):
    assert expand_result["openTrips"] == 1
    assert expand_result["expandTrips"] == 18
    assert expand_result["rowLoads"] == 0


def test_expand_all_disables_its_button_with_a_reason(expand_result):
    flags = expand_result["setDisabledCalls"]
    assert [c[0] for c in flags] == ["#db-dl-expand", "#db-dl-expand"]
    assert [c[1] for c in flags] == [True, False]
    assert flags[0][2]["reason"]                 # never disabled without one
    assert flags[1][2]["ttKey"] == "db.expand_all"

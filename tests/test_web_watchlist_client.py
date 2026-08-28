"""web/app.js: the Watch List screen's client-side invariants.

Behavioural checks slice the real functions out of app.js verbatim and run
them in Node against stub state, following tests/test_web_db_viewer_client.py:
a string match passes again the moment someone reformats the line it names,
and misses defects inside the function it claims to guard. The few structural
assertions here are the ones with nothing to execute — an ordering between two
calls, and a rule about which strings may appear at all.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from cratebuilder import ui_strings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")
INDEX_HTML = os.path.join(ROOT, "web", "index.html")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def index_html():
    with open(INDEX_HTML, encoding="utf-8") as fh:
        return fh.read()


def _slice(source, start, end):
    """The source between two markers, verbatim — the functions under test."""
    a = source.index(start)
    return source[a:source.index(end, a)]


def _run_node(tmp_path, name, source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    # encoding is explicit: the harness prints the design's em dashes and middle
    # dots, and the console's locale codec would mangle them on Windows.
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


def _harness(app_js, body):
    """The Watch List's pure helpers, plus whatever `body` asserts."""
    return "\n".join([
        "const num = (n) => Number(n || 0).toLocaleString();",
        _slice(app_js, "  const WL_LOG_LIMIT = 500;", "  const wl = {"),
        "const wl = { running: false, cards: [], current: null, overall: null };",
        _slice(app_js, "  function wlPending()", "  function wlBusyReason("),
        _slice(app_js, "  function wlCurrentLine(row)", "  function wlPaintProgress()"),
        _slice(app_js, "  function wlCandidateMeta(candidate)",
               "  function openFixLink(row, opts)"),
        body,
    ])


# ── the unresolved sentinel never reaches the Edit dialog's URL field ────────

def test_unresolved_sentinel_url_is_blanked_for_the_edit_field(app_js, tmp_path):
    """A row the service could not resolve stores a placeholder URL. Showing it
    in the Edit field would invite the user to save it straight back."""
    result = _run_node(tmp_path, "wlurl.mjs", _harness(app_js, """
console.log(JSON.stringify([
  { url: 'unresolved://Garage Archive' },
  { url: 'https://www.youtube.com/@DeepHouseDaily' },
  { url: '' },
  {},
].map(wlUrl)));
"""))
    assert result == ["", "https://www.youtube.com/@DeepHouseDaily", "", ""]


# ── counts come off the cards, live ──────────────────────────────────────────

def test_pending_and_unresolved_counts_come_off_the_card_payloads(app_js, tmp_path):
    """"Download All New (n)" and "Check Links" both read the cards rather than
    a separate counts call, so a watchlist.card event moves them at once."""
    result = _run_node(tmp_path, "wlcounts.mjs", _harness(app_js, """
wl.cards = [
  { id: 1, new_count: 7, unresolved: false },
  { id: 2, new_count: 12, unresolved: false },
  { id: 3, new_count: 41, unresolved: true },
  { id: 4, unresolved: true },
];
console.log(JSON.stringify({ pending: wlPending(),
                             unresolved: wlUnresolved().map((c) => c.id) }));
"""))
    assert result == {"pending": 60, "unresolved": [3, 4]}


# ── the downloading card's current-track line ────────────────────────────────

def test_live_progress_frames_win_over_the_card_snapshot(app_js, tmp_path):
    """The card carries a coalesced copy of the channel's progress; the
    progress.current frames stamped job:"watchlist" are finer-grained, so the
    line must prefer them and fall back to the card when none has arrived."""
    result = _run_node(tmp_path, "wlline.mjs", _harness(app_js, """
const row = { id: 1, downloaded: 214,
              progress: { done: 5, total: 12, percent: 42,
                          title: 'Card Snapshot', title_percent: 10 } };
const fromCard = wlCurrentLine(row);
wl.current = { job: 'watchlist', title: 'Midnight Circuit (Extended Mix)', percent: 64 };
const fromFrame = wlCurrentLine(row);
wl.current = null;
const bare = wlCurrentLine({ id: 2, downloaded: 0 });
console.log(JSON.stringify([fromCard, fromFrame, bare]));
"""))
    assert result == [
        "Card Snapshot — 10% · 214 downloaded",
        "Midnight Circuit (Extended Mix) — 64% · 214 downloaded",
        "starting… · 0 downloaded",
    ]


# ── Fix Link candidate metadata ──────────────────────────────────────────────

def test_fix_link_candidate_meta_shows_handle_id_and_subscribers(app_js, tmp_path):
    """3m's candidate line is handle · channel id · subscriber count; a
    SoundCloud candidate has no channel id, so it shows its match confidence
    instead of a blank slot."""
    result = _run_node(tmp_path, "wlcand.mjs", _harness(app_js, """
console.log(JSON.stringify([
  { title: 'Garage Archive', handle: 'youtube.com/@GarageArchive',
    channel_id: 'UC8kQm2', followers: 41200 },
  { title: 'Neon Bass Radio', handle: 'neon-bass', channel_id: '',
    followers: null, confidence: 0.82 },
  { title: 'Nothing Known' },
].map(wlCandidateMeta)));
"""))
    assert result == [
        "youtube.com/@GarageArchive · UC8kQm2 · 41,200 subscribers",
        "neon-bass · 82% match",
        "",
    ]


# ── the toolbar's gates ──────────────────────────────────────────────────────

_TOOLBAR_HARNESS = """
const num = (n) => Number(n || 0).toLocaleString();
const TOOLTIPS = { 'main.scan_batch_conflict': 'BATCH-CONFLICT',
                   'wl.scan_all': 'TT-SCAN', 'wl.add_channel': 'TT-ADD',
                   'wl.check_links': 'TT-LINKS', 'wl.download_all_new': 'TT-DLALL',
                   'wl.cancel_all': 'TT-CANCEL' };
const dl = { running: false };
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = {
    id, disabled: false, attrs: {}, className: '', textContent: '', innerHTML: '',
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
  };
  return els[id];
}
%(setDisabled)s
%(consts)s
const wl = { running: false, cards: [], current: null, overall: null };
%(helpers)s
%(gate)s
%(toolbar)s
function snap() {
  return ['wl-add', 'wl-links', 'wl-dl-all', 'wl-scan', 'wl-cancel', 'quick-scan']
    .reduce((out, id) => {
      const e = $('#' + id);
      out[id] = { off: e.disabled, why: e.attrs['data-tt-text'] || null,
                  tt: e.attrs['data-tt'] || null };
      return out;
    }, {});
}
wl.cards = [{ id: 1, new_count: 7, unresolved: false },
            { id: 2, new_count: 0, unresolved: true }];
renderWatchlistToolbar();
const idle = snap();
dl.running = true;
renderWatchlistToolbar();
const batching = snap();
dl.running = false;
wl.running = true;
renderWatchlistToolbar();
const scanning = snap();
wl.running = false;
wl.cards = [{ id: 1, new_count: 0, unresolved: false }];
renderWatchlistToolbar();
const nothingPending = snap();
console.log(JSON.stringify({ idle, batching, scanning, nothingPending,
                             label: $('#wl-dl-all').textContent }));
"""


def _toolbar_source(app_js):
    return _TOOLBAR_HARNESS % {
        "setDisabled": _slice(app_js, "  function setDisabled(el, disabled, opts)",
                              "  function placeBatchControls("),
        "consts": _slice(app_js, "  const WL_LOG_LIMIT = 500;", "  const wl = {"),
        "helpers": _slice(app_js, "  function wlPending()", "  function wlUrl(row)"),
        "gate": _slice(app_js, "  function tipPlus(ttKey, reason)",
                       "  function wlActionButton("),
        "toolbar": _slice(app_js, "  function renderWatchlistToolbar()",
                          "  function renderWatchlist()"),
    }


def test_a_running_batch_closes_the_scan_controls_with_the_3c_reason(app_js, tmp_path):
    """3c: a scan and a batch cannot share the host's yt-dlp session. Both the
    panel quick action and the Watch List's own Scan button close, and the
    reason is the registry's, not a paraphrase written here."""
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    assert r["idle"]["wl-scan"] == {"off": False, "why": None, "tt": "wl.scan_all"}
    assert r["idle"]["quick-scan"]["off"] is False
    for key in ("wl-scan", "quick-scan"):
        assert r["batching"][key]["off"] is True
        # A closed control still says what it does before saying why it is off.
        assert r["batching"][key]["why"] == "TT-SCAN\n\nBATCH-CONFLICT"
    # Downloading is a separate job category from a batch and keeps running.
    assert r["batching"]["wl-dl-all"]["off"] is False


def test_a_running_watch_list_job_arms_cancel_and_closes_the_starts(app_js, tmp_path):
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    for key in ("wl-add", "wl-links", "wl-dl-all", "wl-scan", "quick-scan"):
        assert r["scanning"][key]["off"] is True, key
        assert r["scanning"][key]["why"], key
    assert r["scanning"]["wl-cancel"]["off"] is False
    assert r["idle"]["wl-cancel"]["off"] is True
    assert r["idle"]["wl-cancel"]["why"] == (
        "TT-CANCEL\n\nNo Watch List scan or download is running.")


def test_a_disabled_toolbar_control_keeps_its_registry_description(app_js, tmp_path):
    """Losing the registry half while a control is closed is exactly when the
    user is most likely to be asking what it was for."""
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    for key, tip in (("wl-add", "TT-ADD"), ("wl-links", "TT-LINKS"),
                     ("wl-dl-all", "TT-DLALL"), ("wl-scan", "TT-SCAN")):
        assert r["scanning"][key]["why"].startswith(tip + "\n\n"), key


def test_download_all_new_carries_the_live_count_and_closes_at_zero(app_js, tmp_path):
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    assert r["idle"]["wl-dl-all"]["off"] is False
    assert r["nothingPending"]["wl-dl-all"]["off"] is True
    assert r["nothingPending"]["wl-dl-all"]["why"]
    # Check Links has nothing to check once every entry resolves.
    assert r["idle"]["wl-links"]["off"] is False
    assert r["nothingPending"]["wl-links"]["off"] is True
    assert r["label"] == "⬇ Download All New (0)"


# ── job.finished is the only resync trigger ─────────────────────────────────
# The host releases a job's slot before emitting job.finished, so the snapshot
# a handler asks for can be believed outright. The runs' own terminal events
# (batch.finished, the closing DONE scan line) are emitted while the slot is
# still held — they are display only, and must not refresh.

_EVENTS_HARNESS = """
const dl = { running: false, paused: false, rows: {}, current: null, overall: null };
const wl = { running: false, cards: [], current: null, overall: null };
const mt = { running: false, task: null, current: null, overall: null,
             note: null, view: null };
let state = { counts: {} };
const calls = [];
const handlers = {};
const cbApi = { on(event, fn) { handlers[event] = fn; } };
const num = (n) => String(n);
function refresh() { calls.push('refresh'); return Promise.resolve(); }
function toast() {}
function isBatchProgress(p) { return !p || !p.job || p.job === 'batch'; }
function wlPaintProgress() {}
function maintPaint() {}
function maintSettle() { mt.running = false; calls.push('settle'); }
function wlApplyCard() {}
function wlLogAppend(e) { calls.push('log:' + (e.text || '')); }
function renderCurrent() {}
function renderQueueLog() {}
function renderOverall() {}
function renderPanelBatchMini() {}
function renderOverviewRunning() {}
function renderBatch() {}
function renderDownloads() {}
function renderShell() {}
function renderOverview() {}
function logHandleAppend() {}
%(subscribe)s

subscribeDownloadEvents();
const out = {};

// A run's own terminal events never resync.
dl.running = true;
handlers['batch.finished']({ downloaded: 3, skipped: 0, errors: 0, cancelled: false });
out.batchFinishedRefreshed = calls.includes('refresh');
out.batchFinishedClearedLocally = dl.running;

wl.running = true;
handlers['scan.line']({ ts: '14:22:41', level: 'downloaded',
                        text: 'DONE Scan complete — 63 new across 5 channels' });
out.doneLineRefreshed = calls.includes('refresh');
out.doneLineStillRunning = wl.running;
out.doneLineLogged = calls.filter((c) => c.startsWith('log:')).length;

// job.finished is what clears and resyncs, per category.
handlers['job.finished']({ job: 'watchlist' });
out.watchlistCleared = !wl.running;
out.refreshesAfterWatchlist = calls.filter((c) => c === 'refresh').length;

dl.running = true;
handlers['job.finished']({ job: 'batch' });
out.batchCleared = !dl.running;
out.refreshesAfterBatch = calls.filter((c) => c === 'refresh').length;

// The maintenance category settles its own dialog and resyncs like the rest.
mt.running = true;
handlers['job.finished']({ job: 'maintenance' });
out.maintenanceSettled = calls.includes('settle') && !mt.running;
out.refreshesAfterMaintenance = calls.filter((c) => c === 'refresh').length;

// A category this screen knows nothing about must not trigger a resync.
handlers['job.finished']({ job: 'gardening' });
handlers['job.finished']({});
out.refreshesAfterUnknown = calls.filter((c) => c === 'refresh').length;
console.log(JSON.stringify(out));
"""


def test_only_job_finished_resyncs_state(app_js, tmp_path):
    source = _EVENTS_HARNESS % {
        "subscribe": _slice(app_js, "  function subscribeDownloadEvents()",
                            "  async function boot()"),
    }
    r = _run_node(tmp_path, "wlevents.mjs", source)
    # batch.finished / the DONE scan line: local display only, no refresh.
    assert r["batchFinishedRefreshed"] is False
    assert r["batchFinishedClearedLocally"] is False
    assert r["doneLineRefreshed"] is False
    assert r["doneLineStillRunning"] is True
    assert r["doneLineLogged"] == 1
    # job.finished clears its own category and resyncs, once each.
    assert r["watchlistCleared"] is True
    assert r["refreshesAfterWatchlist"] == 1
    assert r["batchCleared"] is True
    assert r["refreshesAfterBatch"] == 2
    assert r["maintenanceSettled"] is True
    assert r["refreshesAfterMaintenance"] == 3
    assert r["refreshesAfterUnknown"] == 3


_REFRESH_HARNESS = """
const dl = { running: false };
const wl = { running: false, cards: [], current: null, overall: null };
const mt = { running: false, task: null };
let state = null;
let snapshot = { running: { batch: true, watchlist: true, maintenance: true,
                           maintenance_task: 'db.rebuild' },
                 watchlist: [{ id: 1 }],
                 counts: {}, genres: [], settings: {}, host: {}, library: {} };
async function call() { return JSON.parse(JSON.stringify(snapshot)); }
function renderShell() {}
function renderOverview() {}
function renderGenres() {}
function renderDownloads() {}
function renderWatchlist() {}
function renderSettings() {}
function bindTips() {}
const document = {};
%(refresh)s

const out = {};
await refresh();
out.armedFromSnapshot = [dl.running, wl.running, mt.running];
out.taskFromSnapshot = mt.task;
out.cardsFromSnapshot = wl.cards.length;
snapshot.running = { batch: false, watchlist: false, maintenance: false };
await refresh();
out.clearedFromSnapshot = [dl.running, wl.running, mt.running];
out.taskCleared = mt.task;
console.log(JSON.stringify(out));
"""


def test_refresh_takes_the_snapshot_at_face_value(app_js, tmp_path):
    """No guard, no sequence number: job.finished is emitted after the slot is
    released, so nothing that triggers a refresh can be answered with a stale
    `running` any more."""
    source = _REFRESH_HARNESS % {
        "refresh": _slice(app_js, "  async function refresh()",
                          "  // A Main-tab batch and a Watch List download"),
    }
    r = _run_node(tmp_path, "wlrefresh.mjs", source)
    assert r == {"armedFromSnapshot": [True, True, True],
                 "taskFromSnapshot": "db.rebuild",
                 "cardsFromSnapshot": 1,
                 "clearedFromSnapshot": [False, False, False],
                 "taskCleared": None}


# ── Smart-Edit hands over, it does not stack ─────────────────────────────────

def test_smart_edit_closes_the_edit_dialog_before_opening_fix_link(app_js):
    """3m's modal-grab rule: two dialogs must never be open at once. Ordering,
    so there is nothing to execute — the assertion is that the close call
    precedes the open call inside the one handler."""
    handler = _slice(app_js, "const smart = modalButton('🛠 Smart-Edit Link'",
                     "tools.append(")
    assert handler.index("closeModal()") < handler.index("openFixLink(row)")


# ── cancellation never promises an immediate stop ────────────────────────────

def test_cancel_copy_never_claims_an_immediate_stop(app_js):
    """Cancellation takes effect between channels — it cannot interrupt a
    listing already in flight. Every cancel affordance says so."""
    notes = _slice(app_js, "  const WL_CANCEL_ALL_NOTE", "  const WL_URL_HINT")
    assert "finishes" in notes
    for forbidden in ("immediately", "at once", "right away", "stops now"):
        assert forbidden not in notes.lower()


# ── every descriptive tooltip comes from the generated registry ──────────────

_TT_PATTERNS = (
    r"""data-tt["']\s*,\s*["']([a-z_]+\.[a-z_]+)""",   # setAttribute('data-tt', k)
    r"""data-tt=["']([a-z_]+\.[a-z_]+)["']""",          # markup
    r"""ttKey:\s*["']([a-z_]+\.[a-z_]+)["']""",         # setDisabled(..., {ttKey})
    r"""TOOLTIPS\[["']([a-z_]+\.[a-z_]+)["']\]""",      # composed reasons
)


def _tooltip_keys_used(app_js, index_html):
    """Every registry key the bundle names in a tooltip position — never one
    that merely appears in prose or a comment."""
    used = set()
    for source in (app_js, index_html):
        for pattern in _TT_PATTERNS:
            used |= set(re.findall(pattern, source))
    return used


def test_every_tooltip_key_the_bundle_names_exists_in_the_registry(app_js, index_html):
    """Descriptive tooltip copy lives in cratebuilder/ui_strings.py and nowhere
    else. A key the bundle names but the registry has lost would render as a
    silently empty tooltip, which is how the two copies start to drift."""
    used = _tooltip_keys_used(app_js, index_html)
    assert used, "no tooltip keys found — the patterns stopped matching"
    missing = sorted(k for k in used if k not in ui_strings.TOOLTIPS)
    assert missing == []


def test_the_watch_list_controls_carry_their_registry_keys(app_js, index_html):
    """The 3d/3m controls this screen wires, each named by the key the registry
    already holds for it — so a control losing its tooltip fails here rather
    than shipping bare.

    A key must appear as a quoted string, not merely somewhere in the file:
    the ones passed to wlActionButton/modalButton are arguments rather than
    `data-tt` literals, but a key surviving only in a comment still fails."""
    quoted = set(re.findall(r"""["']([a-z_]+\.[a-z_]+)["']""",
                            app_js + index_html))
    used = _tooltip_keys_used(app_js, index_html) | quoted
    expected = {"wl.add_channel", "wl.check_links", "wl.download_all_new",
                "wl.scan_all", "wl.cancel_all", "wl.card_scan", "wl.card_force",
                "wl.card_download_new", "wl.card_edit", "wl.card_remove",
                "wl.card_cancel", "wl.card_fix_link", "wl.card_title",
                "wl.card_open_folder", "wl.card_smart_edit",
                "wl.card_forget_unavailable", "wl.clear_scan_log",
                "wl.open_activity_log", "main.scan_batch_conflict",
                "main.new_genre", "db.genre_remove"}
    assert sorted(expected - used) == []

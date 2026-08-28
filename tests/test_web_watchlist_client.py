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
        "const wl = { running: false, cards: [], current: null, "
        "overall: null, doneSeq: 0 };",
        _slice(app_js, "  function wlPending()", "  function wlBusyReason("),
        _slice(app_js, "  function wlCurrentLine(row)", "  function wlPaintProgress()"),
        _slice(app_js, "  function wlIsRunDone(entry)",
               "  /* ── Add Channel (plain dialog)"),
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


# ── the terminal DONE line is the only thing that ends a run ─────────────────

def test_only_a_terminal_done_line_ends_a_watch_list_run(app_js, tmp_path):
    """There is no watchlist.finished event, so the run-over signal is read out
    of the scan log. add/remove/resolve emit DONE lines too — mistaking one of
    those for the end of a run would re-arm every control mid-scan."""
    result = _run_node(tmp_path, "wldone.mjs", _harness(app_js, """
console.log(JSON.stringify([
  { text: 'DONE Scan complete — 63 new across 5 channels' },
  { text: 'DONE Download complete — 12 tracks downloaded' },
  { text: 'DONE Added Deep House Daily' },
  { text: 'DONE Removed Garage Archive' },
  { text: 'DONE Channel set: Garage Archive' },
  { text: 'DONE Garage Archive — 14 genre tags updated' },
  { text: 'SCAN Deep House Daily — enumerating uploads…' },
  {},
].map(wlIsRunDone)));
"""))
    assert result == [True, True, False, False, False, False, False, False]


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
                   'wl.scan_all': 'tt', 'wl.add_channel': 'tt',
                   'wl.check_links': 'tt', 'wl.download_all_new': 'tt',
                   'wl.cancel_all': 'tt' };
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
const wl = { running: false, cards: [], current: null, overall: null, doneSeq: 0 };
%(helpers)s
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
        assert r["batching"][key]["why"] == "BATCH-CONFLICT"
    # Downloading is a separate job category from a batch and keeps running.
    assert r["batching"]["wl-dl-all"]["off"] is False


def test_a_running_watch_list_job_arms_cancel_and_closes_the_starts(app_js, tmp_path):
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    for key in ("wl-add", "wl-links", "wl-dl-all", "wl-scan", "quick-scan"):
        assert r["scanning"][key]["off"] is True, key
        assert r["scanning"][key]["why"], key
    assert r["scanning"]["wl-cancel"]["off"] is False
    assert r["idle"]["wl-cancel"]["off"] is True
    assert r["idle"]["wl-cancel"]["why"] == "No Watch List scan or download is running."


def test_download_all_new_carries_the_live_count_and_closes_at_zero(app_js, tmp_path):
    r = _run_node(tmp_path, "wltoolbar.mjs", _toolbar_source(app_js))
    assert r["idle"]["wl-dl-all"]["off"] is False
    assert r["nothingPending"]["wl-dl-all"]["off"] is True
    assert r["nothingPending"]["wl-dl-all"]["why"]
    # Check Links has nothing to check once every entry resolves.
    assert r["idle"]["wl-links"]["off"] is False
    assert r["nothingPending"]["wl-links"]["off"] is True
    assert r["label"] == "⬇ Download All New (0)"


# ── the end-of-run resync must not re-arm the run it just ended ─────────────

_REFRESH_HARNESS = """
const dl = { running: false };
const wl = { running: false, cards: [], current: null, overall: null, doneSeq: 0 };
let state = null;
// The host emits its terminal DONE line from inside the run's own `finally`,
// so a snapshot taken right afterwards still reports the job as running.
let snapshot = { running: { batch: false, watchlist: true }, watchlist: [],
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
wl.running = true;
await refresh();
out.plainRefreshTrustsTheSnapshot = wl.running;
// what the scan.line handler does when it sees "DONE Scan complete"
wl.running = false;
wl.doneSeq += 1;
await refresh({ watchlistEnded: true });
out.afterDoneStaysCleared = wl.running;
// a fresh page load while a run really is going must still arm
wl.running = false;
await refresh();
out.bootArmsFromTheSnapshot = wl.running;
console.log(JSON.stringify(out));
"""


def test_the_end_of_run_resync_does_not_re_arm_the_finished_run(app_js, tmp_path):
    """The DONE line is emitted before the host releases the job slot, so the
    snapshot that follows still says "running". Believing it would leave every
    control locked behind a job that has already ended."""
    source = _REFRESH_HARNESS % {
        "refresh": _slice(app_js, "  async function refresh(opts)",
                          "  // A Main-tab batch and a Watch List download"),
    }
    result = _run_node(tmp_path, "wlrefresh.mjs", source)
    assert result == {
        "plainRefreshTrustsTheSnapshot": True,
        "afterDoneStaysCleared": False,
        "bootArmsFromTheSnapshot": True,
    }


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


def test_every_tooltip_key_the_bundle_names_exists_in_the_registry(app_js, index_html):
    """Descriptive tooltip copy lives in cratebuilder/ui_strings.py and nowhere
    else. A key the bundle names but the registry has lost would render as a
    silently empty tooltip, which is how the two copies start to drift."""
    used = set()
    for source in (app_js, index_html):
        for pattern in _TT_PATTERNS:
            used |= set(re.findall(pattern, source))
    assert used, "no tooltip keys found — the patterns stopped matching"
    missing = sorted(k for k in used if k not in ui_strings.TOOLTIPS)
    assert missing == []


def test_the_watch_list_controls_carry_their_registry_keys(app_js, index_html):
    """The 3d/3m controls this screen wires, each named by the key the registry
    already holds for it — so a control losing its tooltip fails here rather
    than shipping bare."""
    both = app_js + index_html
    for key in ("wl.add_channel", "wl.check_links", "wl.download_all_new",
                "wl.scan_all", "wl.cancel_all", "wl.card_scan", "wl.card_force",
                "wl.card_download_new", "wl.card_edit", "wl.card_remove",
                "wl.card_cancel", "wl.card_fix_link", "wl.card_title",
                "wl.card_open_folder", "wl.card_smart_edit",
                "wl.card_forget_unavailable", "wl.clear_scan_log",
                "wl.open_activity_log", "main.scan_batch_conflict",
                "main.new_genre", "db.genre_remove"):
        assert key in both, key

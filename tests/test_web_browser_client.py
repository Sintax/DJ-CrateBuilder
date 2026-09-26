"""web/app.js: browser-extension sends — the prefilled flows a window-mode
send opens, the boot-time drain, and the Settings rows' local-only gate.
Static half pins the wiring; the Node half runs handleBrowserSend against a
stub DOM, sliced out of app.js verbatim like the other client tests."""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def app_js():
    with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def _slice(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


def _run_node(tmp_path, name, source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


# ── static: the wiring exists ────────────────────────────────────────────────

def test_the_page_subscribes_to_both_browser_events(app_js):
    assert "cbApi.on('browser.send'" in app_js
    assert "cbApi.on('browser.inbox'" in app_js
    assert "subscribeBrowserEvents();" in _slice(app_js, "  async function boot()", "\n  }\n")


def test_live_sends_are_acted_on_by_the_app_window_only(app_js):
    handler = _slice(app_js, "    cbApi.on('browser.send'", "    });")
    assert "cbApi.transport !== 'local'" in handler


def test_boot_drains_the_parked_sends_after_the_first_screen_is_shown(app_js):
    """After show(): drainBrowserPending navigates to the Watch List or
    Downloads, and boot's own show(hash || 'overview') must not run after it
    and flip the screen back under the dialog it just opened."""
    body = _slice(app_js, "  async function boot()", "\n  }\n")
    assert body.index("show(location.hash.slice(1) || 'overview');") \
        < body.index("drainBrowserPending();")


def test_the_add_channel_modal_takes_a_prefill(app_js):
    body = _slice(app_js, "  function openAddChannel(prefill)", "  /* ── Remove (plain yes/no)")
    assert "typeof prefill === 'string'" in body
    assert "urlEl.value = " in body


def test_the_handler_toggle_is_local_only_like_run_at_startup(app_js):
    body = _slice(app_js, "  function applySettingsDependencies()", "\n  }\n")
    assert "set('browser_handler', remoteMount," in body


def test_the_section_carries_its_help_icon(app_js):
    assert "'Browser Integration': 'settings.browser_integration'" in app_js


# ── node: handleBrowserSend opens the right flow ─────────────────────────────

_HARNESS = """
const shown = [];
function show(name) { shown.push(name); }
const opened = [];
function openAddChannel(prefill) { opened.push(prefill); }
const toasts = [];
function toast(m) { toasts.push(m); }
const closed = [];
function closeModal() { closed.push(true); }
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, value: '', events: [], focused: false,
    dispatchEvent(e) { this.events.push(e.type); },
    focus() { this.focused = true; } };
  return els[id];
}
global.Event = class { constructor(type) { this.type = type; } };
const state = { browser: { pending: [
  { kind: 'channel', url: 'https://soundcloud.com/a' },
  { kind: 'track', url: 'https://www.youtube.com/watch?v=x' } ] } };
%(fn)s
%(drain)s
handleBrowserSend(null);
handleBrowserSend({ kind: 'track', url: '' });
const untouched = { shown: shown.slice(), opened: opened.slice(),
  closed: closed.length };
drainBrowserPending();
/* handleBrowserSend finishes its work a tick later (so a dying dialog's
   re-open cannot land on top of it); this timer is queued behind both. */
setTimeout(() => {
  console.log(JSON.stringify({ untouched, shown, opened, toasts,
    closed: closed.length,
    url: $('#dl-url').value, events: $('#dl-url').events,
    genreFocused: $('#dl-genre').focused }));
}, 0);
"""


def test_a_channel_opens_add_channel_prefilled_and_a_track_prefills_downloads(app_js, tmp_path):
    r = _run_node(tmp_path, "browsersend.mjs", _HARNESS % {
        "fn": _slice(app_js, "  function handleBrowserSend(send)",
                     "  function drainBrowserPending()"),
        "drain": _slice(app_js, "  function drainBrowserPending()",
                        "  function subscribeBrowserEvents()"),
    })
    assert r["untouched"] == {"shown": [], "opened": [], "closed": 0}  # empty sends do nothing
    assert r["shown"] == ["watchlist", "downloads"]
    assert r["opened"] == ["https://soundcloud.com/a"]
    # A second send replaces the first one's dialog instead of stacking on it.
    # Two closes per send: once now, once on the tick the flow opens on.
    assert r["closed"] == 4
    assert r["url"] == "https://www.youtube.com/watch?v=x"
    assert r["events"] == ["input"]                           # renderBatch's trigger
    assert r["genreFocused"] is True
    assert any("pick a genre" in t for t in r["toasts"])


# A dialog whose onClose hands straight on to another one a tick later —
# openGenreMoveConfirm → openEditChannel, and openFixLink → the next queued
# Fix Link, both real. The send has to end up on top of that hand-over.
_REOPEN_HARNESS = """
let visible = null;            // the dialog on screen
let openDialog = null;         // its handlers, the way openModal keeps them
function closeModal() {
  const dying = openDialog;
  visible = null; openDialog = null;
  if (dying && dying.onClose) dying.onClose();
}
function openEditChannel() { visible = { name: 'edit-channel' }; openDialog = {}; }
function openAddChannel(prefill) {
  visible = { name: 'add-channel', prefill: prefill || '' };
  openDialog = {};
}
const shown = [];
function show(name) { shown.push(name); }
function toast() {}
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, value: '', dispatchEvent() {}, focus() {} };
  return els[id];
}
global.Event = class { constructor(type) { this.type = type; } };
// Up when the send lands: a confirm that re-opens Edit Channel on close.
visible = { name: 'genre-move' };
openDialog = { onClose: () => { setTimeout(openEditChannel, 0); } };
%(fn)s
handleBrowserSend({ kind: 'channel', url: 'https://soundcloud.com/a' });
setTimeout(() => { console.log(JSON.stringify({ visible, shown })); }, 0);
"""


def test_a_send_lands_on_top_of_a_dialog_that_reopens_another_on_close(app_js,
                                                                       tmp_path):
    """Closing the confirm re-opens Edit Channel a tick later. If the send
    opened its own dialog straight away, that re-open would land on top and
    the user would be looking at Edit Channel instead of the Add Channel the
    link they clicked was meant to fill in."""
    r = _run_node(tmp_path, "browserreopen.mjs", _REOPEN_HARNESS % {
        "fn": _slice(app_js, "  function handleBrowserSend(send)",
                     "  function drainBrowserPending()"),
    })
    assert r["visible"] == {"name": "add-channel",
                            "prefill": "https://soundcloud.com/a"}
    assert r["shown"] == ["watchlist"]


# ── the inbox ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def index_html():
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as fh:
        return fh.read()


def test_the_inbox_button_exists_hidden_and_is_wired(app_js, index_html):
    assert 'id="wl-inbox" hidden' in index_html
    assert 'data-tt="wl.browser_inbox"' in index_html
    assert "$('#wl-inbox').addEventListener('click', openBrowserInbox);" in app_js
    assert "renderBrowserInbox();" in _slice(app_js, "  function renderWatchlist()", "\n  }\n")


def test_the_inbox_modal_processes_through_the_same_send_handler(app_js):
    body = _slice(app_js, "  function openBrowserInbox()", "\n  }\n")
    assert "cbApi.call('browser.inbox_list')" in body
    assert "cbApi.call('browser.inbox_take', { id: row.id })" in body
    assert "cbApi.call('browser.inbox_remove', { id: row.id })" in body
    assert "handleBrowserSend(send)" in body


def test_the_overview_counts_waiting_sends_under_needs_attention(app_js):
    body = _slice(app_js, "  function renderOverviewAttention()", "\n  }\n")
    assert "browserInboxCount()" in body
    assert "Browser Inbox" in body
    # …and only on the host, where the button it points at is drawn.
    assert "state.host.transport === 'local'" in body


_INBOX_HARNESS = """
const num = (n) => Number(n || 0).toLocaleString();
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, hidden: false, textContent: '' };
  return els[id];
}
let state = { host: { transport: 'local' }, browser: { inbox_count: 0 } };
%(count)s
%(render)s
renderBrowserInbox();
const empty = { hidden: $('#wl-inbox').hidden, text: $('#wl-inbox').textContent };
state.browser.inbox_count = 3;
renderBrowserInbox();
const three = { hidden: $('#wl-inbox').hidden, text: $('#wl-inbox').textContent };
state.host.transport = 'remote';
renderBrowserInbox();
const remote = { hidden: $('#wl-inbox').hidden };
state = null;
renderBrowserInbox();
const noState = { hidden: $('#wl-inbox').hidden };
console.log(JSON.stringify({ empty, three, remote, noState }));
"""


def test_the_inbox_button_hides_at_zero_on_remote_and_counts_otherwise(app_js,
                                                                       tmp_path):
    r = _run_node(tmp_path, "inboxbtn.mjs", _INBOX_HARNESS % {
        "count": _slice(app_js, "  function browserInboxCount()",
                        "  function renderBrowserInbox()"),
        "render": _slice(app_js, "  function renderBrowserInbox()",
                         "  function openBrowserInbox()"),
    })
    assert r["empty"]["hidden"] is True
    assert r["three"] == {"hidden": False, "text": "Browser Inbox (3)"}
    # The inbox is the host desktop's; `browser.` is refused on the remote
    # transport, so a paired device is never offered the button.
    assert r["remote"]["hidden"] is True
    assert r["noState"]["hidden"] is True


# ── right-click choices: Add to batch / Download now ────────────────────────

def test_a_track_with_a_choice_asks_for_a_genre_instead_of_prefilling(app_js):
    body = _slice(app_js, "  function handleBrowserSend(send)",
                  "  function drainBrowserPending()")
    assert "send.then === 'batch' || send.then === 'download'" in body
    assert body.index("openBrowserAction(send);") < body.index("if (send.kind === 'channel')")


def test_the_genre_dialog_labels_its_button_with_the_choice(app_js):
    body = _slice(app_js, "  function openBrowserAction(send)",
                  "  async function confirmBrowserAction(send, genre)")
    assert "title: 'Which genre?'" in body
    assert "'Download now' : 'Add to batch'" in body
    assert "modalButton('Cancel', 'cb-btn--quiet', closeModal)" in body
    assert "genreRow(sel, () => platform)" in body


def test_start_shares_the_state_reset_with_the_right_click_path(app_js):
    start = _slice(app_js, "$('#dl-start').addEventListener('click'",
                   "$('#dl-cancel').addEventListener('click'")
    assert "markDownloadsStarted();" in start


_ACTION_HARNESS = """
const NO_GENRE_VALUE = '(none)';
const calls = [];
const toasts = [];
let gateAnswer = true;
const reopened = [];
const dl = { running: %(running)s, paused: true, rows: { a: 1 }, current: 'x', overall: 1 };
const state = { batch: [] };
let startFails = %(start_fails)s;
async function call(method, params) {
  calls.push([method, params || null]);
  return method === 'batch.list' ? [{ id: 1 }] : {};
}
const cbApi = { call: async (method) => {
  calls.push([method, null]);
  if (startFails) { const e = new Error('A Watch List run is in progress.'); e.userFacing = true; throw e; }
  return {};
} };
function closeModal() {}
async function openNoGenreGate() { return gateAnswer; }
function openBrowserAction(send) { reopened.push(send.url); }
function platformFromUrl(u) { return /soundcloud/.test(u) ? 'SoundCloud' : 'YouTube'; }
function renderBatch() {}
let rendered = 0;
function renderDownloads() { rendered += 1; }
function toast(m, warn) { toasts.push([m, !!warn]); }
%(mark)s
%(confirm)s
(async () => {
  await confirmBrowserAction({ kind: 'track', url: 'https://soundcloud.com/a/b', then: '%(then)s' }, '%(genre)s');
  console.log(JSON.stringify({ calls, toasts, reopened, dl, rendered }));
})();
"""


def _action_src(app_js, harness=_ACTION_HARNESS, *, then, genre="House",
                running="false", start_fails="false"):
    return harness % {
        "then": then, "genre": genre, "running": running, "start_fails": start_fails,
        "mark": _slice(app_js, "  function markDownloadsStarted()", "\n  }\n") + "\n  }\n",
        "confirm": _slice(app_js, "  async function confirmBrowserAction(send, genre)",
                          "  /* ── browser extension sends"),
    }


def test_add_to_batch_queues_with_the_chosen_genre_and_starts_nothing(app_js, tmp_path):
    r = _run_node(tmp_path, "action_batch.mjs", _action_src(app_js, then="batch"))
    assert r["calls"][0] == ["batch.add", {"url": "https://soundcloud.com/a/b",
                                           "genre": "House", "platform": "SoundCloud"}]
    assert ["download.start", None] not in r["calls"]
    assert r["toasts"] == [["Added to batch", False]]


def test_download_now_queues_then_starts(app_js, tmp_path):
    r = _run_node(tmp_path, "action_dl.mjs", _action_src(app_js, then="download"))
    methods = [c[0] for c in r["calls"]]
    assert methods.index("batch.add") < methods.index("download.start")
    assert r["dl"]["running"] is True and r["dl"]["paused"] is False
    assert r["dl"]["rows"] == {} and r["rendered"] == 1


def test_download_now_during_a_running_batch_joins_it(app_js, tmp_path):
    r = _run_node(tmp_path, "action_join.mjs",
                  _action_src(app_js, then="download", running="true"))
    assert "download.start" not in [c[0] for c in r["calls"]]
    assert r["toasts"] == [["Added — it will download when its turn comes.", False]]


def test_download_now_blocked_by_a_watch_list_run_stays_queued(app_js, tmp_path):
    r = _run_node(tmp_path, "action_blocked.mjs",
                  _action_src(app_js, then="download", start_fails="true"))
    assert r["calls"][0][0] == "batch.add"
    assert r["toasts"] == [["Added to batch. A Watch List run is in progress.", True]]


def test_backing_out_of_the_no_genre_gate_reopens_the_genre_dialog(app_js, tmp_path):
    harness = _ACTION_HARNESS.replace("let gateAnswer = true;", "let gateAnswer = false;")
    r = _run_node(tmp_path, "action_gate.mjs",
                  _action_src(app_js, harness, then="batch", genre="(none)"))
    assert r["calls"] == []
    assert r["reopened"] == ["https://soundcloud.com/a/b"]

"""web/app.js: the Update screen (the self-updater), client-side.

Same method as tests/test_web_downloads_client.py — the real functions are
sliced out of app.js verbatim and run in Node against stub state, so a test
cannot pass just because someone reformatted the line it names.

Covers the update-blocked remedy: while a known-available build is stuck
behind a live Watch List run (the host refuses update.apply with
UPDATE_NEEDS_IDLE_JOBS), Download and install stops the run itself — the
user never has to press a separate Stop and notice when it has finished.
"""
import json
import os
import shutil
import subprocess

import pytest

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


_HARNESS = """
const TOOLTIPS = {};
const ABOUT_UPDATER_NOTE = 'remote note';
const WL_CANCEL_ALL_NOTE = 'STOPPING-NOW';
const aboutUpdate = { status: null, result: null, checking: false, view: null };
const wl = { running: false };
const cbApi = { transport: 'local' };
const calls = [];
async function call(method, params) { calls.push(method); return {}; }
const toasts = [];
function toast(t) { toasts.push(t); }
function setDisabled(el, off, opts) { el.disabled = !!off; }
function divNode() { return makeEl('div'); }
function tagNode() { return makeEl('span'); }
function aboutCheckUpdates() {}
function aboutConfirmUpdate() {}
function aboutUpdateStatusLine() { return ''; }
function renderUpdate() {}
function makeEl(tag) {
  return {
    tag, children: [], listeners: {}, attrs: {}, style: {},
    className: '', textContent: '', value: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
  };
}
const document = { createElement: makeEl };
function buttons(el) {
  let out = el.tag === 'button' ? [el] : [];
  for (const c of el.children) out = out.concat(buttons(c));
  return out;
}
%(slices)s
function renderWith(result, running, transport) {
  aboutUpdate.result = result;
  wl.running = running;
  cbApi.transport = transport || 'local';
  const host = makeEl('div');
  renderUpdateControls(host);
  const all = buttons(host);
  const update = all.find((b) => b.textContent.indexOf('Update Now') !== -1);
  return { labels: all.map((b) => b.textContent),
           updateOn: !!update && !update.disabled };
}
const AVAILABLE = { reachable: true, valid: true, available: true,
                    current_build: 64, latest_build: 65, can_self_update: true };
async function main() {
  console.log(JSON.stringify({
    blocked: renderWith(AVAILABLE, true),
    idle: renderWith(AVAILABLE, false),
  }));
}
main();
"""


def _slices(app_js):
    return _slice(app_js, "  function renderUpdateControls(host)",
                  "  function renderUpdate()")


def test_the_card_never_grows_a_separate_stop_button(app_js, tmp_path):
    """A live Watch List run must not close Update Now or add a Stop beside
    it: the confirm modal's own install does the stopping, so a card-level
    Stop would be a second step whose completion the user has to notice."""
    r = _run_node(tmp_path, "aboutstop.mjs",
                  _HARNESS % {"slices": _slices(app_js)})

    for state in ("blocked", "idle"):
        assert r[state]["updateOn"] is True
        assert not [l for l in r[state]["labels"] if "Stop" in l]
    assert "aboutStopWatchlistButton" not in app_js


def test_the_update_confirm_modal_stops_the_run_from_install_itself(app_js):
    """The modal is where the refusal actually bites — the user is one click
    from install. With a run live at open it carries the warning, and the one
    install button sends the Watch List's own cancel before counting down."""
    body = _slice(app_js, "  function aboutConfirmUpdate(",
                  "  /* Step two: the progress modal")
    assert "wl.running" in body
    assert "Stop Watch List and install" not in body
    assert "watchlist.cancel_all" in body
    assert "cannot install" in body


def test_refresh_repaints_update_so_the_screen_tracks_the_run(app_js):
    """job.started and job.finished both resync through refresh(); without
    renderUpdate() there the Update screen would show stale run state."""
    body = _slice(app_js, "  async function refresh()",
                  "  function isBatchProgress(")
    assert "renderUpdate();" in body


# ── Update is its own screen, under Settings in the nav ─────────────────────

def test_update_is_a_nav_item_between_settings_and_about(index_html):
    nav = _slice(index_html, "<nav ", "</nav>")
    items = [line for line in nav.splitlines() if "data-screen=" in line]
    screens = [line.split('data-screen="')[1].split('"')[0] for line in items]
    assert screens == ["overview", "downloads", "watchlist", "settings",
                       "update", "about"]
    assert 'id="screen-update"' in index_html
    assert 'id="update-body"' in index_html


def test_the_router_opens_the_update_screen_and_about_no_longer_hosts_it(app_js):
    show = _slice(app_js, "  function show(name)", "  /* The nav is real anchors")
    assert "if (name === 'update') updateOpen();" in show
    assert "'update'," in _slice(app_js, "  const SCREENS = [", "];")
    about = _slice(app_js, "  function renderAbout()",
                   "  /* ── Update (3o)")
    assert "renderUpdateControls" not in about
    assert "aboutUpdate" not in about
    update = _slice(app_js, "  function renderUpdate()",
                    "  async function updateOpen()")
    assert "renderUpdateControls(host);" in update
    assert "$('#update-body')" in update


# ── Download and install during a run: stop → countdown → install ───────────
# The confirm modal's own functions run in Node against a stub modal and faked
# timers; the Watch List's job.finished is simulated by watchlistFinished(),
# whose real counterpart is pinned structurally in the last test.

_FLOW_HARNESS = """
const TOOLTIPS = {};
const WL_CANCEL_ALL_NOTE = 'STOPPING-NOW';
const aboutUpdate = { status: null, result: null, checking: false, view: 'progress',
                      onWatchlistStopped: null };
const wl = { running: RUNNING_AT_OPEN };
const calls = [];
let refuse = null;
async function call(method) {
  calls.push(method);
  if (refuse === method) { const e = new Error('SLOT-TAKEN'); e.userFacing = true; throw e; }
  return {};
}
const toasts = [];
function toast(t) { toasts.push(t); }
function setDisabled(el, off, opts) { el.disabled = !!off; el.reason = off && opts ? opts.reason : null; }
function makeEl(tag) {
  return {
    tag, children: [], listeners: {}, attrs: {}, style: {},
    className: '', textContent: '', disabled: false,
    classList: { add() {} },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    replaceChildren() { this.children = []; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
  };
}
const document = { createElement: makeEl };
function aboutAvWarningNode() { return makeEl('div'); }
function bindTips() {}
let began = 0;
function aboutBeginApply() { began += 1; aboutUpdate.view = 'progress'; }
let modal = null;
function openModal(opts) {
  if (modal) closeModal();
  const body = makeEl('div'), foot = makeEl('div');
  const api = { body, foot, err: null, close: closeModal, busy() {},
                error(m) { api.err = m; } };
  modal = { opts, body, foot, api };
  if (opts.body) opts.body(body, api);
  if (opts.foot) opts.foot(foot, api);
  return api;
}
function closeModal() {
  if (!modal) return;
  const dying = modal; modal = null;
  if (dying.opts.onClose) dying.opts.onClose();
}
function modalButton(label, cls, onClick) {
  const b = makeEl('button'); b.className = 'cb-btn cb-btn--sm ' + (cls || '');
  b.textContent = label; b.listeners.click = onClick; return b;
}
function modalNote(text) { const p = makeEl('p'); p.textContent = text; return p; }
function modalQuoteMark(mark) { const b = makeEl('b'); b.textContent = mark; return b; }
/* Faked timers: tick(n) advances the countdown n seconds. */
const timers = [];
global.setInterval = (fn) => { timers.push({ fn, live: true }); return timers.length; };
global.clearInterval = (id) => { if (timers[id - 1]) timers[id - 1].live = false; };
function tick(n) { for (let i = 0; i < n; i++) timers.filter((t) => t.live).forEach((t) => t.fn()); }
function liveTimers() { return timers.filter((t) => t.live).length; }
function btn(el, text) {
  if (el.tag === 'button' && el.textContent.indexOf(text) !== -1) return el;
  for (const c of el.children) { const h = btn(c, text); if (h) return h; }
  return null;
}
function texts(el) {
  let out = el.textContent ? [el.textContent] : [];
  for (const c of el.children) out = out.concat(texts(c));
  return out.join(' | ');
}
/* What the real job.finished handler does for the Watch List (pinned
   structurally by its own test below): mark it idle and wake the hook. */
function watchlistFinished() {
  wl.running = false;
  const fn = aboutUpdate.onWatchlistStopped;
  aboutUpdate.onWatchlistStopped = null;
  if (fn) fn();
}
SLICES
aboutUpdate.result = { available: true, current_build: 64, latest_build: 65, notes: '' };
async function main() {
SCRIPT
}
main();
"""


def _flow(app_js, tmp_path, name, running, script):
    slices = _slice(app_js, "  /* Once the Watch List has been stopped",
                    "  /* Step two: the progress modal")
    source = (_FLOW_HARNESS
              .replace("RUNNING_AT_OPEN", "true" if running else "false")
              .replace("SLICES", slices)
              .replace("SCRIPT", script))
    return _run_node(tmp_path, name, source)


def test_with_a_run_live_install_stops_it_then_counts_down_then_installs(
        app_js, tmp_path):
    """One click on the only install button: Cancel All goes out, the button
    itself says it is stopping, the Watch List's job.finished starts a
    five-second countdown, and only when it reaches zero does the progress
    modal open and update.apply go out. No separate Stop button anywhere."""
    r = _flow(app_js, tmp_path, "flow1.mjs", True, """
  aboutConfirmUpdate();
  const go = btn(modal.foot, 'Download and install');
  const before = { stop: !!btn(modal.foot, 'Stop Watch List'), goOff: go.disabled,
                   tip: go.attrs['data-tt-text'] || '', warned: texts(modal.body) };
  await go.listeners.click();
  const stopping = { calls: calls.slice(), label: btn(modal.foot, 'Stopping').textContent,
                     off: btn(modal.foot, 'Stopping').disabled,
                     goGone: !btn(modal.foot, 'Download and install'),
                     hooked: typeof aboutUpdate.onWatchlistStopped === 'function',
                     status: texts(modal.body), began };
  watchlistFinished();
  const countdown = { text: texts(modal.body), cancel: !!btn(modal.foot, 'Cancel'),
                      goGone: !btn(modal.foot, 'Download and install'),
                      stopGone: !btn(modal.foot, 'Stopping'), began };
  tick(4);
  const almost = { text: texts(modal.body), began, applied: calls.includes('update.apply') };
  tick(1);
  console.log(JSON.stringify({ before, stopping, countdown, almost,
                               done: { began, calls, timers: liveTimers() } }));
""")
    assert r["before"]["stop"] is False
    assert r["before"]["goOff"] is False
    assert "Stops the Watch List run first" in r["before"]["tip"]
    assert "cannot install" in r["before"]["warned"]

    assert r["stopping"]["calls"] == ["watchlist.cancel_all"]
    assert r["stopping"]["off"] is True and "Stopping" in r["stopping"]["label"]
    assert r["stopping"]["goGone"] is True
    assert r["stopping"]["hooked"] is True
    assert "Stopping the Watch List run" in r["stopping"]["status"]
    assert r["stopping"]["began"] == 0

    assert "Update starts in 5" in r["countdown"]["text"]
    assert r["countdown"]["cancel"] is True
    assert r["countdown"]["goGone"] and r["countdown"]["stopGone"]
    assert r["countdown"]["began"] == 0

    assert "Update starts in 1" in r["almost"]["text"]
    assert r["almost"]["began"] == 0 and r["almost"]["applied"] is False

    assert r["done"]["began"] == 1
    assert r["done"]["calls"] == ["watchlist.cancel_all", "update.apply"]
    assert r["done"]["timers"] == 0


def test_cancel_during_the_countdown_never_installs(app_js, tmp_path):
    r = _flow(app_js, tmp_path, "flow2.mjs", True, """
  aboutConfirmUpdate();
  await btn(modal.foot, 'Download and install').listeners.click();
  watchlistFinished();
  tick(2);
  btn(modal.foot, 'Cancel').listeners.click();
  tick(10);
  const go = btn(modal.foot, 'Download and install');
  console.log(JSON.stringify({ began, calls, text: texts(modal.body),
    goBack: !!go && !go.disabled, plain: !!go && !go.attrs['data-tt-text'],
    timers: liveTimers(), hooked: aboutUpdate.onWatchlistStopped !== null }));
""")
    assert r["began"] == 0
    assert r["calls"] == ["watchlist.cancel_all"]
    assert "Update cancelled" in r["text"] and "still on build 64" in r["text"]
    # The run is already stopped, so install is back to its plain, immediate
    # form — no stop-first tooltip.
    assert r["goBack"] is True and r["plain"] is True
    assert r["timers"] == 0 and r["hooked"] is False


def test_closing_the_modal_during_the_countdown_drops_it(app_js, tmp_path):
    """Escape, ✕ and the backdrop all close through onClose — the timer must
    not keep counting toward an install behind a modal that is gone."""
    r = _flow(app_js, tmp_path, "flow3.mjs", True, """
  aboutConfirmUpdate();
  await btn(modal.foot, 'Download and install').listeners.click();
  watchlistFinished();
  closeModal();
  tick(10);
  console.log(JSON.stringify({ began, calls, timers: liveTimers() }));
""")
    assert r["began"] == 0
    assert r["calls"] == ["watchlist.cancel_all"]
    assert r["timers"] == 0


def test_with_nothing_running_install_is_immediate_with_no_countdown(
        app_js, tmp_path):
    """The countdown is the heads-up after an automated stop; a user pressing
    install on an idle app has already decided."""
    r = _flow(app_js, tmp_path, "flow4.mjs", False, """
  aboutConfirmUpdate();
  const go = btn(modal.foot, 'Download and install');
  const plain = !go.attrs['data-tt-text'];
  await go.listeners.click();
  console.log(JSON.stringify({ plain, goOff: go.disabled, began, calls,
                               timers: timers.length }));
""")
    assert r["plain"] is True and r["goOff"] is False
    assert r["began"] == 1 and r["calls"] == ["update.apply"]
    assert r["timers"] == 0


def test_a_run_that_ended_on_its_own_before_the_click_still_counts_down(
        app_js, tmp_path):
    """No job.finished is coming for the hook when the run was already over
    by the time install was pressed — the countdown must start regardless."""
    r = _flow(app_js, tmp_path, "flow5.mjs", True, """
  aboutConfirmUpdate();
  wl.running = false;   // the scan finished between open and click
  await btn(modal.foot, 'Download and install').listeners.click();
  console.log(JSON.stringify({ text: texts(modal.body), timers: liveTimers(),
                               hooked: aboutUpdate.onWatchlistStopped !== null }));
""")
    assert "Update starts in 5" in r["text"]
    assert r["timers"] == 1 and r["hooked"] is False


def test_a_refused_install_reopens_the_confirm_with_the_hosts_reason(
        app_js, tmp_path):
    """The Watch List's scheduler can take the job slot back in the gap; the
    host's one-line reason lands in the modal, not only in a toast."""
    r = _flow(app_js, tmp_path, "flow6.mjs", False, """
  refuse = 'update.apply';
  aboutConfirmUpdate();
  await btn(modal.foot, 'Download and install').listeners.click();
  console.log(JSON.stringify({ began, calls, reopened: !!modal,
    err: modal && modal.api.err, view: aboutUpdate.view,
    go: !!(modal && btn(modal.foot, 'Download and install')) }));
""")
    assert r["began"] == 1 and r["calls"] == ["update.apply"]
    assert r["reopened"] is True and r["err"] == "SLOT-TAKEN"
    assert r["view"] is None and r["go"] is True


def test_a_cancel_pressed_while_apply_is_in_flight_is_asked_again(
        app_js, tmp_path):
    """update.apply fetches the manifest before the job exists; a Cancel
    pressed in that gap finds nothing to cancel on the host. Once apply
    has returned, the dialog's cancelRequested is honoured with a second
    update.cancel — and only then."""
    r = _flow(app_js, tmp_path, "flow7.mjs", False, """
  let release = null;
  const realCall = call;
  call = (method) => method === 'update.apply'
    ? new Promise((res) => { calls.push(method); release = res; })
    : realCall(method);
  aboutBeginApply = () => { began += 1; aboutUpdate.view = { cancelRequested: false }; };
  aboutConfirmUpdate();
  const pending = btn(modal.foot, 'Download and install').listeners.click();
  const duringApply = calls.slice();
  aboutUpdate.view.cancelRequested = true;       // Cancel pressed in the gap
  release({});
  await pending;
  await Promise.resolve();
  const withCancel = calls.slice();
  calls.length = 0;
  call = realCall;
  aboutUpdate.view = { cancelRequested: false };
  await aboutStartApply();
  await Promise.resolve();
  console.log(JSON.stringify({ duringApply, withCancel, without: calls }));
""")
    assert r["duringApply"] == ["update.apply"]
    assert r["withCancel"] == ["update.apply", "update.cancel"]
    assert r["without"] == ["update.apply"]


def test_the_watch_lists_job_finished_wakes_the_waiting_confirm(app_js):
    """The hook the modal leaves is honoured by the real handler — and cleared
    before it runs, so a countdown can never be started twice."""
    handler = _slice(app_js, "    cbApi.on('job.finished'",
                     "    cbApi.on('update.progress'")
    branch = _slice(handler, "} else if (job === 'watchlist') {",
                    "} else if (job === 'maintenance')")
    assert "aboutUpdate.onWatchlistStopped" in branch
    assert branch.index("aboutUpdate.onWatchlistStopped = null") < branch.index("stopped()")


def test_update_now_does_not_hand_the_click_event_to_the_confirm(app_js):
    """aboutConfirmUpdate(errorText) prints whatever it is given as an error
    box at the foot of the dialog. Bound directly as a click listener it was
    given the PointerEvent, which rendered as "[object PointerEvent]"."""
    assert "addEventListener('click', aboutConfirmUpdate)" not in app_js
    assert "updateBtn.addEventListener('click', () => aboutConfirmUpdate());" in app_js


def test_the_confirm_leads_with_the_notes_and_boxes_the_notice(app_js):
    """Order in the confirm: the build line in bold, what is in the build
    (larger than a hint, in bold quotation marks), then the scan notice in
    its orange box. The code-signing caution is for a fresh install, which
    a nightly is not, so it stays out unless the result says so."""
    confirm = _slice(app_js, "  function aboutConfirmUpdate(", "  /* The install itself.")
    body = _slice(confirm, "      body(body) {", "      foot(foot) {")
    build_at = body.index("is available")
    lead_at = body.index("lead.classList.add('cb-mnote--lead');")
    notes_at = body.index("notes.classList.add('cb-mnote--notes');")
    quote_at = body.index("modalQuoteMark('\u201C'), result.notes, modalQuoteMark('\u201D')")
    notice_at = body.index("notice.className = 'cb-warnbox';")
    assert build_at < lead_at < notes_at < quote_at < notice_at
    assert body.count("aboutAvWarningNode()") == 1
    assert "if (result.fresh_install) body.appendChild(aboutAvWarningNode());" in body
    assert "notice.textContent = result.notice;" in body
    assert "notes: p.notes, notice: p.notice," in app_js
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert ".cb-mnote--lead { color: var(--cb-text); font-weight: 700; }" in css
    assert ".cb-mnote__quote { font-weight: 700; }" in css
    assert ".cb-mnote--notes { color: var(--cb-text); font-size: 14px;" in css


# ── the components table ─────────────────────────────────────────────────────

_COMPONENTS_HARNESS = """
const aboutUpdate = { status: %(status)s, componentsOpen: %(open)s };
let renders = 0;
function renderUpdate() { renders += 1; }
function makeEl(tag) {
  const e = {
    tag, children: [], style: {}, className: '', textContent: '', attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(ev, fn) { this.on = fn; },
  };
  e.classList = { add(c) { e.className += ' ' + c; } };
  return e;
}
const document = {
  createElement: makeEl,
  createTextNode(t) { return { tag: '#text', textContent: t, children: [] }; },
};
function divNode() { return makeEl('div'); }
function tagNode(text, cls) { const s = makeEl('span'); s.textContent = text; s.className = 'cb-tag ' + cls; return s; }
%(fn)s
const host = makeEl('div');
renderUpdateComponents(host);
function text(el) { return el.textContent + el.children.map(text).join(''); }
const table = host.children.find((c) => c.className === 'cb-comp');
const rows = table ? table.children[0].children[1].children.map((tr) => ({
  cls: tr.className, cells: tr.children.map(text) })) : null;
const heads = table ? table.children[0].children[0].children[0].children.map(text) : null;
const head = host.children[1];
let toggle = null;
host.children.forEach((c) => {
  const b = c.children && c.children.find((k) => k.tag === 'button');
  if (b) toggle = b;
});
if (toggle) toggle.on();
console.log(JSON.stringify({
  n: host.children.length, heads, rows,
  note: (host.children.find((c) => c.className.indexOf('cb-comp__note') !== -1) || {}).textContent,
  badge: (head ? head.children.filter((c) => c.className.indexOf('cb-tag') !== -1).map(text) : []),
  toggle: toggle ? { text: toggle.textContent, expanded: toggle.attrs['aria-expanded'] } : null,
  afterClick: { open: aboutUpdate.componentsOpen, renders },
}));
"""


def _components(app_js, tmp_path, status, open=True):
    fn = _slice(app_js, "  const COMPONENT_STATE_TEXT = {", "  /* Loads what About and Update share")
    return _run_node(tmp_path, "components.mjs", _COMPONENTS_HARNESS % {
        "status": json.dumps(status), "open": json.dumps(open), "fn": fn})


_ROWS = [
    {"key": "python", "label": "Python", "installed": "3.14.5", "offered": "3.14.5", "state": "same"},
    {"key": "yt-dlp", "label": "yt-dlp", "installed": "2026.8.19", "offered": "2026.9.1", "state": "newer"},
    {"key": "ffmpeg", "label": "FFmpeg", "installed": None, "offered": None, "state": "missing"},
]


def test_the_update_page_draws_the_components_table_after_the_controls(app_js):
    update = _slice(app_js, "  function renderUpdate()", "  /* ── Update: what the build is made of")
    assert update.index("renderUpdateControls(host);") < update.index("renderUpdateComponents(host);")
    # Both silent-check verdicts refresh the status the table is drawn from.
    available = _slice(app_js, "    cbApi.on('update.available', (p) => {", "    });")
    checked = _slice(app_js, "    cbApi.on('update.checked', (p) => {", "    });")
    assert "aboutRefreshUpdateStatus();" in available
    assert "aboutRefreshUpdateStatus();" in checked
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert ".cb-comp__row.is-newer td { background: var(--cb-attn-wash); }" in css
    assert ".cb-comp__new { color: var(--cb-warn); font-weight: 700; }" in css


def test_the_components_list_starts_folded_behind_a_show_button(app_js, tmp_path):
    """Folded: the heading and its badge still say whether anything would
    change, but no note and no table. Show flips the flag and redraws;
    open, the same button reads Hide."""
    status = {"components": {"build": 83, "available": True, "rows": _ROWS}}
    assert "componentsOpen: false," in app_js
    folded = _components(app_js, tmp_path, status, open=False)
    assert folded["rows"] is None
    assert folded.get("note") is None
    assert folded["badge"] == ["1 will update"]
    assert folded["toggle"] == {"text": "Show", "expanded": "false"}
    assert folded["afterClick"] == {"open": True, "renders": 1}
    shown = _components(app_js, tmp_path, status, open=True)
    assert shown["rows"] is not None
    assert shown["toggle"] == {"text": "Hide", "expanded": "true"}
    assert shown["afterClick"] == {"open": False, "renders": 1}


def test_a_newer_component_stands_out_with_its_new_version(app_js, tmp_path):
    r = _components(app_js, tmp_path, {"components": {"build": 83, "available": True, "rows": _ROWS}})
    assert r["heads"] == ["Component", "You have", "In build 83"]
    assert r["badge"] == ["1 will update"]
    assert "Compared against build 83" in r["note"]
    by = {row["cells"][0]: row for row in r["rows"]}
    assert by["yt-dlp"]["cls"] == "cb-comp__row is-newer"
    assert by["yt-dlp"]["cells"][1:] == ["2026.8.19", "2026.9.1 will update"]
    assert by["Python"]["cls"] == "cb-comp__row is-same"
    assert by["Python"]["cells"][1:] == ["3.14.5", "3.14.5"]
    assert by["FFmpeg"]["cells"][1:] == ["not installed", "not listed"]
    assert by["FFmpeg"]["cls"] == "cb-comp__row is-missing"


def test_a_component_missing_here_still_shows_what_the_build_carries(app_js, tmp_path):
    """A source run has no bundled FFmpeg; the live column still says what
    the build ships rather than hiding it behind 'not installed'."""
    rows = [dict(_ROWS[2], offered="9.0.1", state="missing")]
    r = _components(app_js, tmp_path, {"components": {"build": 83, "available": True, "rows": rows}})
    assert r["rows"][0]["cells"] == ["FFmpeg", "not installed", "9.0.1"]
    assert r["badge"] == ["All current"]


def test_a_build_before_the_block_shows_only_what_you_have(app_js, tmp_path):
    rows = [dict(r, offered=None, state="unknown") for r in _ROWS if r["installed"]]
    r = _components(app_js, tmp_path, {"components": {"build": 82, "available": False, "rows": rows}})
    assert r["heads"][2] == "In build 82"
    assert r["badge"] == ["All current"]
    assert "Build 82 was published before" in r["note"]
    assert {row["cells"][2] for row in r["rows"]} == {"not listed"}


def test_before_any_check_the_live_column_is_blank_and_the_note_says_to_check(app_js, tmp_path):
    rows = [dict(r, offered=None, state="unknown") for r in _ROWS]
    r = _components(app_js, tmp_path, {"components": {"build": None, "available": False, "rows": rows}})
    assert r["heads"][2] == "Live build"
    assert r["badge"] == []
    assert "Check for updates to compare" in r["note"]


def test_no_status_draws_nothing(app_js, tmp_path):
    assert _components(app_js, tmp_path, None)["n"] == 0
    assert _components(app_js, tmp_path, {"components": {"rows": []}})["n"] == 0


def test_about_has_a_report_a_problem_flow(app_js):
    """The About screen is built in JS, so the button, its tooltip key and
    the two host calls all live in app.js."""
    assert "'about-report'" in app_js
    assert "'about.report'" in app_js
    assert "function openReportDialog(" in app_js
    assert "call('support.preview'" in app_js
    assert "call('fs.support_send'" in app_js
    assert "Save bundle & open GitHub" in app_js
    assert "Nothing is sent until you press" in app_js
    assert "Available in the app window on the host machine." in app_js


def test_the_show_hide_toggle_wears_the_apps_red_like_check_for_updates(app_js):
    """The toggle was drawn quiet (grey); it now reads as the other Update
    controls do — the base button, red outline and text."""
    fn = _slice(app_js, "    const toggle = document.createElement('button');",
                "    toggle.addEventListener('click'")
    assert "toggle.className = 'cb-btn cb-btn--sm';" in fn
    assert "cb-btn--quiet" not in fn


def test_report_a_bug_stands_on_its_own_line_full_size_and_red(app_js):
    """The report button leaves the link row for a line of its own, at the
    base button's full size and colour, so a user looking for where to
    report something finds it without reading the links."""
    fn = _slice(app_js, "    const report = document.createElement('button');",
                "    if (info.note) {")
    assert "report.className = 'cb-btn';" in fn
    assert "cb-btn--quiet" not in fn and "cb-btn--sm" not in fn
    assert "'Report a Bug'" in fn
    assert "report.setAttribute('data-ic', 'bug')" in fn
    # Its own row, appended after the links row — not inside it.
    assert "reportRow.appendChild(report)" in fn
    assert fn.index("host.appendChild(links)") < fn.index("host.appendChild(reportRow)")
    start = fn.index("links.append(")
    assert "report" not in fn[start:fn.index(");", start)]


# ── the progress modal's real Cancel ─────────────────────────────────────────
# The "Updating DJ-CrateBuilder" dialog is locked (no X, no Escape, no click
# on the dim) and carries a Cancel that really stops the host's worker. The
# modal shell's `locked` option is run for real below, against a stub DOM; the
# dialog's own functions run against a stub modal like the flow harness's.

_PROGRESS_HARNESS = """
const aboutUpdate = { status: null, result: null, checking: false, view: null };
const cbApi = { transport: 'local' };
const calls = [];
async function call(method) { calls.push(method); return {}; }
const everything = [];
function makeEl(tag) {
  const el = {
    tag, children: [], listeners: {}, attrs: {}, style: {},
    className: '', textContent: '', disabled: false, focused: false,
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
    focus() { this.focused = true; },
    replaceWith(other) {
      for (const p of everything) {
        const i = p.children.indexOf(this);
        if (i !== -1) p.children[i] = other;
      }
    },
  };
  everything.push(el);
  return el;
}
const document = { createElement: makeEl };
let modal = null;
let closes = 0;
function openModal(opts) {
  if (modal) closeModal();
  const body = makeEl('div'), foot = makeEl('div');
  const api = { body, foot, close: closeModal, busy() {}, error() {} };
  modal = { opts, body, foot, api };
  if (opts.body) opts.body(body, api);
  if (opts.foot) opts.foot(foot, api);
  return api;
}
function closeModal() {
  if (!modal) return;
  closes += 1;
  const dying = modal; modal = null;
  if (dying.opts.onClose) dying.opts.onClose();
}
function modalButton(label, cls, onClick) {
  const b = makeEl('button'); b.className = 'cb-btn cb-btn--sm ' + (cls || '');
  b.textContent = label; b.listeners.click = onClick; return b;
}
function modalNote(text) { const p = makeEl('p'); p.className = 'cb-mnote'; p.textContent = text; return p; }
function btn(el, text) {
  if (el.tag === 'button' && el.textContent.indexOf(text) !== -1) return el;
  for (const c of el.children) { const h = btn(c, text); if (h) return h; }
  return null;
}
SLICES
async function main() {
SCRIPT
}
main();
"""


def _progress(app_js, tmp_path, name, script):
    slices = _slice(app_js, "  /* Step two: the progress modal",
                    "  /* Re-fetches update.status")
    source = _PROGRESS_HARNESS.replace("SLICES", slices).replace("SCRIPT", script)
    return _run_node(tmp_path, name, source)


def test_the_progress_modal_is_locked_and_its_cancel_asks_the_host(
        app_js, tmp_path):
    out = _progress(app_js, tmp_path, "progress_cancel.js", """
      aboutBeginApply();
      const cancel = btn(modal.foot, 'Cancel');
      const before = { locked: modal.opts.locked, label: cancel.textContent,
                       disabled: cancel.disabled, cls: cancel.className,
                       margin: cancel.style.marginLeft,
                       note: modal.foot.children[0].textContent,
                       requested: !!aboutUpdate.view.cancelRequested };
      cancel.listeners.click();
      await Promise.resolve();
      console.log(JSON.stringify({ before, calls,
        after: { label: cancel.textContent, disabled: cancel.disabled,
                 requested: aboutUpdate.view.cancelRequested },
        stillOpen: !!modal }));
    """)
    assert out["before"]["locked"] is True
    assert out["before"]["label"] == "Cancel"
    assert out["before"]["disabled"] is False
    assert "cb-btn--warn" in out["before"]["cls"]
    assert out["before"]["margin"] == "auto"
    assert out["before"]["note"] == (
        "Cancel stops the download and leaves the app on its current build.")
    assert out["before"]["requested"] is False
    assert out["calls"] == ["update.cancel"]
    assert out["after"] == {"label": "Cancelling…", "disabled": True,
                            "requested": True}
    # The click only asks; the dialog waits for the host's update.cancelled.
    assert out["stillOpen"] is True


def test_update_cancelled_closes_the_dialog(app_js, tmp_path):
    out = _progress(app_js, tmp_path, "progress_cancelled.js", """
      aboutBeginApply();
      aboutCancelledApply();
      const closedOnce = closes;
      aboutCancelledApply();                 // nothing open: a no-op
      console.log(JSON.stringify({ closedOnce, closes, view: aboutUpdate.view,
                                   open: !!modal }));
    """)
    assert out["closedOnce"] == 1
    assert out["closes"] == 1
    assert out["view"] is None
    assert out["open"] is False


def test_restarting_disables_cancel_past_the_point_of_no_return(
        app_js, tmp_path):
    out = _progress(app_js, tmp_path, "progress_restarting.js", """
      aboutBeginApply();
      btn(modal.foot, 'Cancel').listeners.click();   // lost the race
      aboutShowRestarting(65);
      const cancel = btn(modal.foot, 'Cancel');
      console.log(JSON.stringify({ disabled: cancel.disabled, label: cancel.textContent,
                                   status: aboutUpdate.view.status.textContent }));
    """)
    assert out["disabled"] is True
    assert out["label"] == "Cancel"          # not left reading "Cancelling…"
    assert "build 65" in out["status"]


def test_a_failed_apply_swaps_cancel_for_a_close_that_can_dismiss(
        app_js, tmp_path):
    out = _progress(app_js, tmp_path, "progress_failed.js", """
      aboutBeginApply();
      aboutSettleApply({ ok: false });
      const cancel = btn(modal.foot, 'Cancel');
      const close = btn(modal.foot, 'Close');
      const snap = { cancelGone: !cancel, closeCls: close && close.className,
                     closeMargin: close && close.style.marginLeft,
                     focused: close && close.focused,
                     note: aboutUpdate.view.note.textContent };
      close.listeners.click();
      console.log(JSON.stringify({ ...snap, open: !!modal, view: aboutUpdate.view }));
    """)
    assert out["cancelGone"] is True
    assert "cb-btn--quiet" in out["closeCls"]
    assert out["closeMargin"] == "auto"
    assert out["focused"] is True
    assert out["note"] == "You can close this window and try again."
    assert out["open"] is False
    assert out["view"] is None


def test_the_old_close_does_not_stop_it_note_is_gone(app_js):
    assert "Closing this window does not stop the update" not in app_js


def test_update_cancelled_is_subscribed_beside_the_other_update_events(app_js):
    fn = _slice(app_js, "  function subscribeUpdateEvents()",
                "  let booted = false;")
    assert "cbApi.on('update.cancelled', () => aboutCancelledApply());" in fn


# ── openModal's `locked` option, run for real ────────────────────────────────

_MODAL_HARNESS = """
function makeEl(tag) {
  return {
    tag, children: [], listeners: {}, attrs: {}, style: {}, parent: null,
    className: '', textContent: '', disabled: false, tabIndex: 0,
    offsetParent: {}, focused: 0,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); },
    focus() { this.focused += 1; document.activeElement = this; },
    contains(el) { for (let e = el; e; e = e.parent) if (e === this) return true; return false; },
  };
}
const docListeners = {};
const document = {
  body: makeEl('body'), activeElement: null,
  createElement: makeEl,
  addEventListener(name, fn) { docListeners[name] = fn; },
  removeEventListener(name) { delete docListeners[name]; },
};
function $$(sel, root) {
  let out = [];
  const walk = (el) => { if (el.tag === 'button') out.push(el); el.children.forEach(walk); };
  walk(root);
  return out;
}
function tagNode() { return makeEl('span'); }
function hideTip() {}
function bindTips() {}
SLICES
function modalButton(label, cls, onClick) {
  const b = makeEl('button'); b.className = 'cb-btn cb-btn--sm ' + (cls || '');
  b.textContent = label; b.listeners.click = onClick; return b;
}
function headButtons() {
  const dim = document.body.children[0];
  return dim ? dim.children[0].children[0].children.filter((c) => c.tag === 'button').length : -1;
}
function tryEscape() {
  if (docListeners.keydown) docListeners.keydown({ key: 'Escape', stopPropagation() {} });
}
function tryDim() {
  const dim = document.body.children[0];
  if (dim) dim.listeners.mousedown({ target: dim });
}
function isOpen() { return document.body.children.length > 0; }
SCRIPT
"""


def _modal(app_js, tmp_path, name, script):
    slices = _slice(app_js, "  let openDialog = null;",
                    "  function modalButton(")
    return _run_node(tmp_path, name,
                     _MODAL_HARNESS.replace("SLICES", slices).replace("SCRIPT", script))


def test_a_locked_modal_has_no_x_and_ignores_escape_and_the_dim(app_js, tmp_path):
    out = _modal(app_js, tmp_path, "modal_locked.js", """
      let cancel = null;
      openModal({ title: 'T', locked: true,
                  foot(foot) { cancel = modalButton('Cancel', '', () => {}); foot.append(cancel); } });
      const xButtons = headButtons();
      tryEscape();
      const afterEscape = isOpen();
      tryDim();
      const afterDim = isOpen();
      const focusedCancel = cancel.focused;
      /* Tab is still trapped: with one focusable, Tab wraps back onto it. */
      let prevented = 0;
      document.activeElement = cancel;
      docListeners.keydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented += 1; } });
      closeModal();
      console.log(JSON.stringify({ xButtons, afterEscape, afterDim, focusedCancel,
                                   prevented, afterClose: isOpen() }));
    """)
    assert out["xButtons"] == 0
    assert out["afterEscape"] is True
    assert out["afterDim"] is True
    assert out["focusedCancel"] == 1
    assert out["prevented"] == 1
    assert out["afterClose"] is False        # closeModal() itself still works


def test_an_unlocked_modal_keeps_its_three_casual_exits(app_js, tmp_path):
    out = _modal(app_js, tmp_path, "modal_unlocked.js", """
      openModal({ title: 'T' });
      const xButtons = headButtons();
      tryEscape();
      const afterEscape = isOpen();
      openModal({ title: 'T' });
      tryDim();
      const afterDim = isOpen();
      openModal({ title: 'T', locked: false });
      const xWhenFalse = headButtons();
      closeModal();
      console.log(JSON.stringify({ xButtons, afterEscape, afterDim, xWhenFalse }));
    """)
    assert out["xButtons"] == 1
    assert out["afterEscape"] is False
    assert out["afterDim"] is False
    assert out["xWhenFalse"] == 1

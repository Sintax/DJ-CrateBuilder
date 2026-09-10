"""web/app.js: the About screen's Updates card, client-side.

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


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
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
function renderAbout() {}
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
  renderAboutUpdates(host);
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
    return _slice(app_js, "  function renderAboutUpdates(host)",
                  "  async function aboutOpen()")


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


def test_refresh_repaints_about_so_the_card_tracks_the_run(app_js):
    """job.started and job.finished both resync through refresh(); without
    renderAbout() there the Updates card would show stale run state."""
    body = _slice(app_js, "  async function refresh()",
                  "  function isBatchProgress(")
    assert "renderAbout();" in body


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


def test_the_watch_lists_job_finished_wakes_the_waiting_confirm(app_js):
    """The hook the modal leaves is honoured by the real handler — and cleared
    before it runs, so a countdown can never be started twice."""
    handler = _slice(app_js, "    cbApi.on('job.finished'",
                     "    cbApi.on('update.progress'")
    branch = _slice(handler, "} else if (job === 'watchlist') {",
                    "} else if (job === 'maintenance')")
    assert "aboutUpdate.onWatchlistStopped" in branch
    assert branch.index("aboutUpdate.onWatchlistStopped = null") < branch.index("stopped()")

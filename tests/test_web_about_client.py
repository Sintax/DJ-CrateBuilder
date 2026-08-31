"""web/app.js: the About screen's Updates card, client-side.

Same method as tests/test_web_downloads_client.py — the real functions are
sliced out of app.js verbatim and run in Node against stub state, so a test
cannot pass just because someone reformatted the line it names.

Covers the update-blocked remedy: while a known-available build is stuck
behind a live Watch List run (the host refuses update.apply with
UPDATE_NEEDS_IDLE_JOBS), the card and the confirm modal offer the stop —
never just the refusal.
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
const WL_CANCEL_ALL_NOTE = 'Cancelling - the channel in flight finishes.';
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
function findStop(el) {
  if (el.textContent && el.textContent.indexOf('Stop Watch List') !== -1
      && el.tag === 'button') return el;
  for (const c of el.children) { const hit = findStop(c); if (hit) return hit; }
  return null;
}
%(slices)s
function renderWith(result, running, transport) {
  aboutUpdate.result = result;
  wl.running = running;
  cbApi.transport = transport || 'local';
  const host = makeEl('div');
  renderAboutUpdates(host);
  return !!findStop(host);
}
const AVAILABLE = { reachable: true, valid: true, available: true,
                    current_build: 64, latest_build: 65, can_self_update: true };
const CURRENT = Object.assign({}, AVAILABLE, { available: false });
async function main() {
  const blocked = renderWith(AVAILABLE, true);
  const idleRun = renderWith(AVAILABLE, false);
  const noBuild = renderWith(CURRENT, true);
  const remote = renderWith(AVAILABLE, true, 'remote');

  cbApi.transport = 'local';
  const b = aboutStopWatchlistButton();
  await b.listeners.click();
  console.log(JSON.stringify({
    blocked, idleRun, noBuild, remote,
    cls: b.className, clicked: calls, off: !!b.disabled,
    label: b.textContent, toasted: toasts.length > 0,
  }));
}
main();
"""


def _slices(app_js):
    return (_slice(app_js, "  function aboutStopWatchlistButton()",
                   "  function aboutConfirmUpdate()")
            + _slice(app_js, "  function renderAboutUpdates(host)",
                     "  async function aboutOpen()"))


def test_the_stop_button_appears_only_while_a_build_waits_behind_a_run(
        app_js, tmp_path):
    """Available build + live Watch List run is the one state the remedy is
    for. No build, no run, or a remote session (whose update controls are
    disabled anyway) must not grow the extra button."""
    r = _run_node(tmp_path, "aboutstop.mjs",
                  _HARNESS % {"slices": _slices(app_js)})

    assert r["blocked"] is True
    assert r["idleRun"] is False
    assert r["noBuild"] is False
    assert r["remote"] is False


def test_the_stop_button_sends_the_watch_lists_own_cancel(app_js, tmp_path):
    """One cancel, the same RPC the Watch List toolbar sends — and the button
    settles into a disabled 'Stopping…' so a second click cannot double-send
    while the channel in flight finishes."""
    r = _run_node(tmp_path, "aboutstop2.mjs",
                  _HARNESS % {"slices": _slices(app_js)})

    assert r["clicked"] == ["watchlist.cancel_all"]
    assert r["cls"] == "cb-btn cb-btn--warn cb-btn--sm"
    assert r["off"] is True
    assert "Stopping" in r["label"]
    assert r["toasted"] is True


def test_the_update_confirm_modal_offers_the_same_remedy(app_js):
    """The modal is where the refusal actually bites — the user is one click
    from install. Its body has to carry the warning and the same button when
    a run is live at open."""
    body = _slice(app_js, "  function aboutConfirmUpdate()",
                  "  /* Step two: the progress modal")
    assert "wl.running" in body
    assert "aboutStopWatchlistButton()" in body
    assert "cannot" in body and "install" in body


def test_refresh_repaints_about_so_the_button_tracks_the_run(app_js):
    """job.started and job.finished both resync through refresh(); without
    renderAbout() there the stop button would outlive the run it stops."""
    body = _slice(app_js, "  async function refresh()",
                  "  function isBatchProgress(")
    assert "renderAbout();" in body

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
const untouched = { shown: shown.slice(), opened: opened.slice() };
drainBrowserPending();
console.log(JSON.stringify({ untouched, shown, opened, toasts,
  url: $('#dl-url').value, events: $('#dl-url').events,
  genreFocused: $('#dl-genre').focused }));
"""


def test_a_channel_opens_add_channel_prefilled_and_a_track_prefills_downloads(app_js, tmp_path):
    r = _run_node(tmp_path, "browsersend.mjs", _HARNESS % {
        "fn": _slice(app_js, "  function handleBrowserSend(send)",
                     "  function drainBrowserPending()"),
        "drain": _slice(app_js, "  function drainBrowserPending()",
                        "  function subscribeBrowserEvents()"),
    })
    assert r["untouched"] == {"shown": [], "opened": []}      # empty sends do nothing
    assert r["shown"] == ["watchlist", "downloads"]
    assert r["opened"] == ["https://soundcloud.com/a"]
    assert r["url"] == "https://www.youtube.com/watch?v=x"
    assert r["events"] == ["input"]                           # renderBatch's trigger
    assert r["genreFocused"] is True
    assert any("pick a genre" in t for t in r["toasts"])

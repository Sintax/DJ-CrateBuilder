"""web/api.js: the remote transport's socket lifecycle, run in Node.

The real `api.js` is loaded verbatim against a browser stub — a string match
would pass again the moment someone reformats the line it names, and would miss
a leak entirely, since the defect these guard is "how many sockets exist", not
"what the source says". Same pattern as tests/test_web_watchlist_client.py.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_JS = os.path.join(ROOT, "web", "api.js")

# A browser, reduced to what api.js touches: a WebSocket that records every
# construction and its open/closed state, a localStorage, a fetch that answers
# the pairing routes, and a setTimeout that queues rather than fires.
_HARNESS = """
const fs = require('fs');
let created = 0;
const all = [];
class FakeSocket {
  constructor(url) { this.url = url; this.readyState = 1; this.onmessage = null;
                     this.onclose = null; created += 1; all.push(this); }
  close() { if (this.readyState === 3) return; this.readyState = 3;
            if (this.onclose) this.onclose({ code: 1000 }); }
}
const store = new Map();
const timers = [];
// waitForLocal() polls on a real 4-second budget. Rather than sleep it out
// five times over, hand it a clock that jumps and an interval that fires once:
// there is no pywebview bridge here, so the answer is always "remote".
let clockMs = 0;
const FakeDate = { now: () => (clockMs += 5000) };
const win = {
  Date: FakeDate,
  setInterval: (fn) => { setImmediate(fn); return 1; },
  clearInterval: () => {},
  WebSocket: FakeSocket,
  location: { protocol: 'http:', host: '127.0.0.1:8770' },
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  addEventListener() {},
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: (id) => { if (id) timers[id - 1] = null; },
  console,
  fetch: async (url) => {
    if (url === '/pair') return { ok: true, status: 200, json: async () => ({
      token: 'TOKEN-ABC', device: { id: 'd1', name: 'Driver' },
      session: { can_write: false } }) };
    if (url === '/pair/info') return { ok: true, status: 200,
      json: async () => ({ require_pairing: true }) };
    return { ok: true, status: 200, json: async () => ({ ok: true, result: {} }) };
  },
};
win.window = win;
const src = fs.readFileSync(%(api)s, 'utf8');
new Function('window', 'setTimeout', 'clearTimeout', 'setInterval',
             'clearInterval', 'Date', 'fetch', 'WebSocket', 'localStorage',
             'location', 'console', src)(
  win, win.setTimeout, win.clearTimeout, win.setInterval, win.clearInterval,
  FakeDate, win.fetch, win.WebSocket, win.localStorage, win.location, console);
const cbApi = win.cbApi;
const openCount = () => all.filter((s) => s.readyState !== 3).length;
const snap = () => ({ created, open: openCount() });
(async () => {
%(body)s
})();
"""


def _run_node(tmp_path, body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    # .cjs, not .mjs: the harness uses require(), and Node reads .mjs as an ES
    # module where require does not exist.
    script = tmp_path / "apisockets.cjs"
    script.write_text(
        _HARNESS % {"api": json.dumps(API_JS), "body": body},
        encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


def test_an_unpaired_browser_opens_no_socket(tmp_path):
    """No token means no socket and no calls — the pairing screen is the only
    thing an unpaired browser may reach."""
    r = _run_node(tmp_path, """
  await cbApi.connect();
  console.log(JSON.stringify({ ...snap(), transport: cbApi.transport }));
""")
    assert r == {"created": 0, "open": 0, "transport": "remote"}


def test_pairing_then_booting_leaves_exactly_one_socket(tmp_path):
    """`pair()` opens the socket and `boot()` calls `connect()` straight after.
    Two sockets used to survive that, each subscribing on the host
    independently, so every progress frame and log line arrived twice."""
    r = _run_node(tmp_path, """
  await cbApi.connect();
  await cbApi.pair('123456', 'Driver');
  const afterPair = snap();
  await cbApi.connect();
  console.log(JSON.stringify({ afterPair, afterBoot: snap() }));
""")
    assert r["afterPair"] == {"created": 1, "open": 1}
    assert r["afterBoot"] == {"created": 1, "open": 1}


def test_retrying_replaces_the_socket_rather_than_adding_one(tmp_path):
    """The offline bar's Retry calls reconnect() and then boot()'s connect().
    A new socket is fine; a second LIVE one is not."""
    r = _run_node(tmp_path, """
  await cbApi.connect();
  await cbApi.pair('123456', 'Driver');
  cbApi.reconnect();
  await cbApi.connect();
  const first = snap();
  cbApi.reconnect();
  await cbApi.connect();
  console.log(JSON.stringify({ first, second: snap() }));
""")
    assert r["first"]["open"] == 1
    assert r["second"]["open"] == 1


def test_a_dropped_socket_schedules_exactly_one_reconnect(tmp_path):
    """Closing a socket we are replacing must not schedule a retry of its own —
    that is how a leak becomes two sockets and then four."""
    r = _run_node(tmp_path, """
  await cbApi.connect();
  await cbApi.pair('123456', 'Driver');
  cbApi.reconnect();
  await cbApi.connect();
  all.filter((s) => s.readyState !== 3).forEach((s) => {
    s.readyState = 3;
    if (s.onclose) s.onclose({ code: 1006 });
  });
  const pending = timers.filter(Boolean).length;
  timers.filter(Boolean).forEach((fn) => fn());
  console.log(JSON.stringify({ pending, ...snap() }));
""")
    assert r["pending"] == 1
    assert r["open"] == 1


def test_a_4401_close_drops_the_token_and_stops_retrying(tmp_path):
    """Revoked: the host closed the socket with 4401, so the browser must
    forget its dead token and show the pairing screen rather than reconnect
    with it every three seconds."""
    r = _run_node(tmp_path, """
  await cbApi.connect();
  await cbApi.pair('123456', 'Driver');
  const events = [];
  cbApi.on('auth.required', (p) => events.push(p.reason));
  all.filter((s) => s.readyState !== 3).forEach((s) => {
    s.readyState = 3;
    if (s.onclose) s.onclose({ code: 4401 });
  });
  console.log(JSON.stringify({ events, paired: cbApi.paired(),
                               pending: timers.filter(Boolean).length,
                               ...snap() }));
""")
    assert r["events"] == ["revoked"]
    assert r["paired"] is False
    assert r["pending"] == 0
    assert r["open"] == 0

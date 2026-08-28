"""web/app.js: the Overview screen, the notification store, and the tooltip
engine's hit-test tracker.

Behavioural checks slice the real functions out of app.js verbatim and run
them in Node against stub state, following tests/test_web_watchlist_client.py.
The tooltip tests are the interesting ones: a disabled form control dispatches
no mouse events in any engine, so the engine has to find it by hit-testing
instead — and that is the difference between a disabled-reason tooltip that
renders and one that does not.
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


# ── which job the Now-running card is about ─────────────────────────────────

_JOB_HARNESS = """
const dl = { running: false, paused: false, current: null, overall: null };
const wl = { running: false, current: null, overall: null, cards: [] };
const mt = { running: false, task: null, current: null, overall: null };
const MAINT_TASKS = { 'db.repair_tags': { run: 'Repair Tags' } };
%(job)s
function pick() {
  const j = overviewJob();
  return j ? { key: j.key, tag: j.tag, href: j.href, pausable: j.pausable,
               reason: j.pauseReason || null } : null;
}
const out = {};
out.idle = pick();
mt.running = true; mt.task = 'db.repair_tags';
out.maintenance = pick();
wl.running = true;
out.watchlistWins = pick();
dl.running = true;
out.batchWins = pick();
console.log(JSON.stringify(out));
"""


def test_the_now_running_card_names_whichever_job_is_running(app_js, tmp_path):
    """3a draws the batch case, but a Watch List run and a maintenance sweep
    are just as much "what is happening" — an idle-looking card during one of
    those would be the Overview lying about the host."""
    r = _run_node(tmp_path, "ovjob.mjs", _JOB_HARNESS % {
        "job": _slice(app_js, "  function overviewJob()",
                      "  const OV_IDLE_REASON")})
    assert r["idle"] is None
    assert r["maintenance"]["key"] == "maintenance"
    assert r["maintenance"]["tag"] == "Repair Tags"
    assert r["watchlistWins"]["key"] == "watchlist"
    assert r["batchWins"]["key"] == "batch"


def test_only_a_batch_offers_a_pause_and_the_others_say_why_not(app_js, tmp_path):
    r = _run_node(tmp_path, "ovjob.mjs", _JOB_HARNESS % {
        "job": _slice(app_js, "  function overviewJob()",
                      "  const OV_IDLE_REASON")})
    assert r["batchWins"]["pausable"] is True
    for key in ("watchlistWins", "maintenance"):
        assert r[key]["pausable"] is False
        assert r[key]["reason"], key


# ── the notification store ───────────────────────────────────────────────────

_NOTES_HARNESS = """
const num = (n) => Number(n || 0).toLocaleString();
let stored = null;
global.localStorage = {
  getItem: () => stored,
  setItem: (k, v) => { stored = v; },
  removeItem: () => { stored = null; },
};
const badge = { textContent: '', hidden: true };
function $(sel) { return sel === '#ov-bell-count' ? badge : null; }
let recentPainted = 0;
function renderOverviewRecent() { recentPainted += 1; }
function renderNotifications() {}
%(notes)s
const out = {};
loadNotes();
out.startsEmpty = notes.items.length;
for (let i = 0; i < 60; i += 1) {
  pushNote({ level: 'info', title: 'T' + i, body: 'b', at: '2026-08-28T10:00:00',
             job: 'batch' });
}
out.capped = notes.items.length;
out.newestFirst = notes.items[0].title;
out.unread = unreadNotes();
notes.items.forEach((n) => { n.read = true; });
out.unreadAfterMark = unreadNotes();
renderBell();
out.painted = recentPainted > 0 && badge.hidden === true;
out.badgeCaps = (function () {
  notes.items.forEach((n) => { n.read = false; });
  renderBell();
  return badge.textContent;
})();
out.persisted = JSON.parse(stored).length;
// A second page load reads the same store back.
notes.items = [];
loadNotes();
out.reloaded = notes.items.length;
out.levels = ['info', 'warn', 'error', 'warning'].map(
  (level) => noteLevelClass({ level: level }));
out.jump = [ { job: 'watchlist' }, { job: 'batch' }, { job: 'maintenance' } ]
  .map((n) => noteJumpFor(n) && noteJumpFor(n)[1]);
// Corrupt storage must not take the page down with it.
stored = 'not json';
notes.items = [{ title: 'x' }];
loadNotes();
out.corruptStore = notes.items.length;
console.log(JSON.stringify(out));
"""


def _notes_source(app_js):
    return _NOTES_HARNESS % {
        "notes": _slice(app_js, "  const NOTE_LIMIT = 50;",
                        "  function closeNotifications()")
        + _slice(app_js, "  function noteJumpFor(n)",
                 "  function renderNotifications()"),
    }


def test_the_bell_keeps_a_capped_newest_first_list(app_js, tmp_path):
    r = _run_node(tmp_path, "ovnotes.mjs", _notes_source(app_js))
    assert r["startsEmpty"] == 0
    assert r["capped"] == 50
    assert r["newestFirst"] == "T59"
    assert r["unread"] == 50
    assert r["unreadAfterMark"] == 0
    assert r["painted"] is True
    assert r["badgeCaps"] == "50"


def test_notifications_survive_a_reload_and_a_broken_store(app_js, tmp_path):
    """localStorage is the same store the database viewer's column widths use,
    and a private window can refuse it outright — an unreadable store is an
    empty list, never an exception on the way to first paint."""
    r = _run_node(tmp_path, "ovnotes.mjs", _notes_source(app_js))
    assert r["persisted"] == 50
    assert r["reloaded"] == 50
    assert r["corruptStore"] == 0


def test_a_notification_s_level_and_jump_are_derived_not_guessed(app_js, tmp_path):
    r = _run_node(tmp_path, "ovnotes.mjs", _notes_source(app_js))
    assert r["levels"] == ["", " is-warn", " is-err", " is-warn"]
    assert r["jump"] == ["watchlist", "downloads", None]


# ── the tooltip engine (the carried F8 fix) ─────────────────────────────────

_TIP_HARNESS = """
const TOOLTIPS = { 'main.cancel_batch': 'REGISTRY-TEXT' };
let shown = null;
const tip = { el: null, timer: null, host: null };
/* The stubs keep the real ones' one shared piece of state: showTip records
   which control the live bubble belongs to, and hideTip clears it. */
function showTip(host, text) { shown = { id: host.id, text: text }; tip.host = host; }
function hideTip() { shown = null; tip.host = null; }
/* The point of the fix: `disabled` is what stops the browser DISPATCHING mouse
   events, and the disabled button below dispatches none — but it is still the
   element under the cursor, which is what elementFromPoint answers with. */
const disabledBtn = { id: 'dl-cancel', disabled: true, tagName: 'BUTTON',
  getAttribute: (k) => (k === 'data-tt-text'
    ? 'REGISTRY-TEXT\\n\\nNo download is running.' : null),
  closest: function () { return this; } };
const plainDiv = { id: 'card', getAttribute: () => null,
  closest: function () { return null; } };
let under = disabledBtn;
let tipUnder = null;
global.document = { elementFromPoint: () => under };
%(track)s
%(text)s
const out = {};
const timers = [];
global.setTimeout = (fn) => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};
tipTrack(10, 10);
timers.splice(0).forEach((fn) => fn());
out.disabledControl = shown;
under = plainDiv;
tipTrack(20, 20);
timers.splice(0).forEach((fn) => fn());
out.nothingUnderCursor = shown;
console.log(JSON.stringify(out));
"""


def test_a_disabled_control_is_found_by_hit_testing_not_by_a_mouse_event(
        app_js, tmp_path):
    """The carried fix. A disabled form control dispatches no mouseenter — not
    to itself and not to an ancestor — so a per-element listener could never
    fire and NO disabled-reason tooltip in the bundle rendered. Hit-testing is
    not suppressed by `disabled`, so the tracker finds it."""
    r = _run_node(tmp_path, "ovtip.mjs", _TIP_HARNESS % {
        "track": _slice(app_js, "  function tipHostAt(x, y)",
                        "  document.addEventListener('pointermove'"),
        "text": _slice(app_js, "  function tipText(host)",
                       "  /* The hover half of the engine"),
    })
    assert r["disabledControl"]["id"] == "dl-cancel"
    assert r["disabledControl"]["text"].startswith("REGISTRY-TEXT")
    assert "No download is running." in r["disabledControl"]["text"]
    assert r["nothingUnderCursor"] is None


def test_bindtips_no_longer_binds_the_hover_pair(app_js):
    """Structural, because there is nothing to execute: if `mouseenter` comes
    back into bindTips, the per-element hover path is back and the disabled
    half of the tooltip contract silently stops working again."""
    body = _slice(app_js, "  function bindTips(root)",
                  "  document.addEventListener('keydown'")
    assert "mouseenter" not in body
    assert "mouseleave" not in body
    assert "'focus'" in body and "'blur'" in body


def test_the_hover_tracker_is_bound_once_at_the_document(app_js):
    assert "document.addEventListener('pointermove'" in app_js


def test_disabled_controls_stay_in_hit_testing():
    """pointer-events:none WOULD remove a disabled control from
    elementFromPoint, which is the one way to break the fix from CSS."""
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    rule = css[css.index(".cb-btn[disabled], .cb-in[disabled]"):]
    rule = rule[:rule.index("}")]
    assert "pointer-events: auto" in rule


# ── the shell's live badge and host footer ──────────────────────────────────

def test_the_nav_badge_is_written_in_exactly_one_place(app_js):
    """The contract allows the nav one count. Two writers is how it goes stale
    on one path and not the other."""
    assert app_js.count("$('#nav-count')") == 1


def test_the_host_footer_row_carries_the_registry_s_own_explainer(index_html):
    assert 'data-tt="remote.host_status"' in index_html
    assert 'id="host-status"' in index_html


def test_the_overview_bell_carries_its_registry_tooltip(index_html):
    assert 'data-tt="main.notifications"' in index_html

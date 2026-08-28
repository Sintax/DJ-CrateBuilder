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
const tip = { el: null, timer: null, host: null, described: null };
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
let tipAt = null;
let hits = 0;
global.document = { elementFromPoint: () => { hits += 1; return under; } };
%(track)s
%(text)s
const out = {};
const timers = [];
global.setTimeout = (fn) => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};
function settle() { timers.splice(0).forEach((fn) => fn()); }

tipTrack(10, 10);
settle();
out.disabledControl = shown;
out.hitsAfterFirstMove = hits;

// (M2) A pointer resting on a control: every move lands within the slop, so
// nothing is hit-tested again.
hits = 0;
for (let i = 0; i < 100; i += 1) tipTrack(10, 10);
out.hitTestsFor100StationaryMoves = hits;
// A jitter of a pixel or two is still the same rest position.
for (let i = 0; i < 100; i += 1) tipTrack(11, 12);
out.hitTestsAfterJitter = hits;
// A real move re-tests.
tipTrack(60, 60);
out.hitsAfterRealMove = hits;

// (M1) A scroll moves the page under a still pointer. tipForget is what the
// scroll handler calls; without it the control the cursor rests on is mute.
tipTrack(10, 10);
settle();
shown = null;
out.reshowsWithoutForget = (tipTrack(10, 10), settle(), shown);
tipForget();
hideTip();
out.reshowsAfterScroll = (tipTrack(10, 10), settle(), shown && shown.id);

// Moving onto something with no tooltip hides the bubble.
under = plainDiv;
tipTrack(200, 200);
settle();
out.nothingUnderCursor = shown;
console.log(JSON.stringify(out));
"""


def _tip_source(app_js):
    return _TIP_HARNESS % {
        "track": _slice(app_js, "  const TIP_SLOP = 4;",
                        "  /* Coalesced to one hit test per frame"),
        "text": _slice(app_js, "  function tipText(host)",
                       "  /* The hover half of the engine"),
    }


def test_a_disabled_control_is_found_by_hit_testing_not_by_a_mouse_event(
        app_js, tmp_path):
    """The carried fix. A disabled form control dispatches no mouseenter — not
    to itself and not to an ancestor — so a per-element listener could never
    fire and NO disabled-reason tooltip in the bundle rendered. Hit-testing is
    not suppressed by `disabled`, so the tracker finds it."""
    r = _run_node(tmp_path, "ovtip.mjs", _tip_source(app_js))
    assert r["disabledControl"]["id"] == "dl-cancel"
    assert r["disabledControl"]["text"].startswith("REGISTRY-TEXT")
    assert "No download is running." in r["disabledControl"]["text"]
    assert r["nothingUnderCursor"] is None


def test_a_resting_pointer_costs_no_hit_tests(app_js, tmp_path):
    """elementFromPoint forces a style/layout flush, and this runs at
    pointer-event rate while progress bars are being written — a pointer that
    has not meaningfully moved must not pay for it."""
    r = _run_node(tmp_path, "ovtip.mjs", _tip_source(app_js))
    assert r["hitsAfterFirstMove"] == 1
    assert r["hitTestsFor100StationaryMoves"] == 0
    assert r["hitTestsAfterJitter"] == 0
    assert r["hitsAfterRealMove"] == 1


def test_a_scroll_lets_the_control_under_the_cursor_speak_again(app_js, tmp_path):
    """A scroll moves the page under a still pointer. Hiding the bubble without
    clearing the tracker leaves that control mute until the pointer leaves it
    and comes back — ordinary wheel-scrolling on Settings or the log viewers."""
    r = _run_node(tmp_path, "ovtip.mjs", _tip_source(app_js))
    # Same coordinates, same element, tracker still armed: nothing re-shows.
    assert r["reshowsWithoutForget"] is None
    # After the scroll handler's tipForget, the same position re-arms.
    assert r["reshowsAfterScroll"] == "dl-cancel"


def test_the_scroll_handler_invalidates_the_tracker(app_js):
    """Structural: the executed test above proves tipForget does the job, this
    proves the scroll listener is the thing that calls it."""
    line = _slice(app_js, "  addEventListener('scroll'", "\n")
    assert "tipForget()" in line and "hideTip()" in line


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


_ARIA_HARNESS = """
const TOOLTIPS = {};
let shown = null;
const tip = { el: null, timer: null, host: null, described: null };
function refreshTip() {}
let nextNode = null;
function makeEl(id) {
  const el = { id: id, disabled: false, attrs: {}, after: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    insertAdjacentElement(where, node) {
      if (node.parent && node.parent !== this) node.parent.after = null;
      node.parent = this; this.after = node; return node; },
  };
  return el;
}
global.document = { createElement: () => {
  const node = { className: '', id: '', textContent: '', parent: null,
    remove() { if (this.parent) this.parent.after = null; this.parent = null; } };
  nextNode = node;
  return node;
} };
%(setDisabled)s
const out = {};
const btn = makeEl('dl-cancel');
setDisabled(btn, true, { reason: 'No download is running.' });
out.describedBy = btn.attrs['aria-describedby'] || null;
out.nodeId = btn.after ? btn.after.id : null;
out.nodeText = btn.after ? btn.after.textContent : null;
out.nodeClass = btn.after ? btn.after.className : null;
out.idsMatch = !!(btn.after && btn.attrs['aria-describedby'] === btn.after.id);
out.stillNativelyDisabled = btn.disabled === true;
out.noAriaDisabled = btn.attrs['aria-disabled'] === undefined;
out.noTabindex = btn.attrs['tabindex'] === undefined;

// The reason changes: the same node is reused, not a second one stacked up.
setDisabled(btn, true, { reason: 'A batch is already running.' });
out.reusedNode = btn.after && btn.after.id === out.nodeId;
out.updatedText = btn.after ? btn.after.textContent : null;

// Re-enabled: the node and the wiring both go.
setDisabled(btn, false, { ttKey: 'main.cancel_batch' });
out.afterEnable = btn.after;
out.describedAfterEnable = btn.attrs['aria-describedby'] === undefined;

// Disabled with no reason at all leaves nothing dangling either.
setDisabled(btn, true, {});
out.noReasonNoNode = btn.after === null
  && btn.attrs['aria-describedby'] === undefined;

// A second control gets its own id.
const other = makeEl('wl-cancel');
setDisabled(other, true, { reason: 'Nothing is running.' });
out.distinctIds = other.after.id !== out.nodeId;
console.log(JSON.stringify(out));
"""


def test_a_disabled_control_s_reason_is_wired_with_aria_describedby(
        app_js, tmp_path):
    """The pointer tracker is the only way to READ the reason — a disabled
    control cannot be focused — but a screen reader announces a described-by
    node regardless of focusability. Native `disabled` stays: the desktop app
    disables the same controls, and aria-disabled + tabindex would add dead
    stops to the tab order that the tkinter UI does not have."""
    r = _run_node(tmp_path, "ovaria.mjs", _ARIA_HARNESS % {
        "setDisabled": _slice(app_js, "  let reasonSeq = 0;",
                              "  function placeBatchControls("),
    })
    assert r["idsMatch"] is True
    assert r["nodeId"].startswith("cb-why-")
    assert r["nodeText"] == "No download is running."
    assert r["nodeClass"] == "cb-sr"
    assert r["stillNativelyDisabled"] is True
    assert r["noAriaDisabled"] is True
    assert r["noTabindex"] is True
    assert r["reusedNode"] is True
    assert r["updatedText"] == "A batch is already running."
    assert r["afterEnable"] is None
    assert r["describedAfterEnable"] is True
    assert r["noReasonNoNode"] is True
    assert r["distinctIds"] is True


def test_the_reason_node_is_taken_out_of_flow():
    """`.cb-sr` sits next to its control so it dies with it on a re-render.
    position:absolute is what stops it being a flex item and adding a `gap` to
    the row — visually-hidden alone would shift every toolbar it appears in."""
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    rule = css[css.index(".cb-sr {"):]
    rule = rule[:rule.index("}")]
    assert "position: absolute" in rule
    assert "clip-path" in rule or "clip:" in rule


def test_the_live_bubble_hands_the_describedby_back(app_js):
    """showTip borrows aria-describedby for the bubble; a disabled control
    already points it at its own reason node, so hideTip has to restore it
    rather than leave the control undescribed."""
    body = _slice(app_js, "  function hideTip()", "  /* The bubble is a snapshot")
    assert "tip.described" in body
    assert "setAttribute('aria-describedby', tip.described)" in body


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

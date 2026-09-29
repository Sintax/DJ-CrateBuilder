"""web/app.js: the Downloads screen's client-side invariants.

Same method as tests/test_web_watchlist_client.py — the real functions are
sliced out of app.js verbatim and run in Node against stub state, so a test
cannot pass just because someone reformatted the line it names.

Covers the two things a running job used to get wrong here: the panel changing
height the moment a download started, and a run the page did not start itself
never arming anything at all — and the Skip row, which 3b draws on this screen
as well as in Settings and which used to be wired to nothing at all.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")
APP_CSS = os.path.join(ROOT, "web", "app.css")
INDEX_HTML = os.path.join(ROOT, "web", "index.html")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def app_css():
    with open(APP_CSS, encoding="utf-8") as fh:
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


# ── the panel keeps its height while a run is going ──────────────────────────

def test_nothing_on_the_downloads_screen_is_hidden_by_a_run(app_js):
    """The height jump this fix is about. `hidden` on a layout container takes
    its whole height out of the screen (app.css makes [hidden] display:none),
    so every card below it stepped up the moment a download started. Nothing
    in the Downloads render path may do that any more."""
    render = _slice(app_js, "  function renderDownloadsHeader()",
                    "  function renderCurrent()")
    for banned in ("dl-actions-row", "dl-header-actions", ".hidden = "):
        assert banned not in render, f"{banned!r} is back in the render path"


def test_the_run_controls_never_leave_the_action_row(app_js, index_html):
    """They used to move up beside the header while a run was going, which is
    what forced the bottom row to be hidden. Kept in one place, the idle
    geometry holds in both states and the run simply arms them where they are."""
    assert "placeBatchControls" not in app_js
    assert "dl-header-actions" not in index_html
    row = index_html[index_html.index('id="dl-actions-row"'):]
    row = row[:row.index("</div>")]
    for control in ('id="dl-start"', 'id="dl-cancel"', 'id="dl-pause"'):
        assert control in row


_CANCEL_HARNESS = """
const DL_MARK = {}, WL_QROW_MARK = {};
const WL_NO_PAUSE_REASON = 'A Watch List run has no pause';
const dl = { running: false, current: null, overall: null };
const wl = { running: false, rows: [], current: null, overall: null };
const els = {
  'dl-cancel': { id: 'dl-cancel', className: 'cb-btn cb-btn--quiet' },
};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, className: '' };
  const el = els[id];
  if (el.style === undefined) el.style = {};
  if (el.textContent === undefined) el.textContent = '';
  return el;
}
function updatePauseLabel() {}
function renderWatchlistToolbar() {}
function gateWrite(el, reason) { el.disabled = !!reason; }
%(view)s
%(header)s
function snap() {
  return { cls: $('#dl-cancel').className, off: !!$('#dl-cancel').disabled,
           tag: $('#dl-state').textContent };
}
renderDownloadsHeader();
const idle = snap();
dl.running = true;
renderDownloadsHeader();
const batch = snap();
dl.running = false;
// A Watch List download borrowing the panel: the rows are what dlView tests,
// not wl.running — a Watch List SCAN has no queue to show here.
wl.running = true;
wl.rows = [{ id: 1, index: 0, state: 'active', title: 'Channel 1' }];
renderDownloadsHeader();
const borrowed = snap();
// A scan, which claims the same job category but shows nothing here.
wl.rows = [];
renderDownloadsHeader();
const scanning = snap();
console.log(JSON.stringify({ idle, batch, borrowed, scanning }));
"""


def test_the_cancel_button_goes_red_while_a_run_is_going(app_js, tmp_path):
    """The one control that stops what is happening has to read like it, and
    read the same as the Watch List's Cancel, which already does."""
    r = _run_node(tmp_path, "dlcancel.mjs", _CANCEL_HARNESS % {
        "view": _slice(app_js, "  function dlView()",
                       "  function renderDownloadsHeader()"),
        "header": _slice(app_js, "  function renderDownloadsHeader()",
                         "  function renderCurrent()"),
    })

    assert r["idle"]["cls"] == "cb-btn cb-btn--quiet"
    assert r["idle"]["off"] is True

    assert r["batch"]["cls"] == "cb-btn cb-btn--warn"
    assert r["batch"]["off"] is False

    # A Watch List download drives this panel too, and this Cancel stops it.
    assert r["borrowed"]["cls"] == "cb-btn cb-btn--warn"
    assert r["borrowed"]["off"] is False

    # A scan owns the job category but has nothing here to cancel.
    assert r["scanning"]["cls"] == "cb-btn cb-btn--quiet"
    assert r["scanning"]["off"] is True


def test_both_cancel_buttons_use_the_same_two_classes(app_js):
    """Divergence here is exactly the kind that goes unnoticed — one screen's
    Cancel red, the other's grey, for the same running run."""
    rule = "'cb-btn ' + (%s ? 'cb-btn--warn' : 'cb-btn--quiet')"
    assert rule % "v.running" in app_js
    assert rule % "wl.running" in app_js


def test_the_activity_feed_is_boxed_rather_than_floored(app_css):
    """min-height let the card grow one line per step the moment a run
    started. It has to be a fixed height that scrolls."""
    rule = _slice(app_css, "#dl-activity {", "}")
    assert "height: 178px" in rule and "min-height" not in rule   # nine lines
    assert "overflow-y: auto" in rule


# ── a run the page did not start still arms the controls ─────────────────────

_JOB_HARNESS = """
const dl = { running: false };
const wl = { running: false };
const mt = { running: false };
const handlers = {};
const cbApi = { on(name, fn) { handlers[name] = fn; } };
let refreshes = 0;
function refresh() { refreshes += 1; }
const resets = [];
function resetActivity(job, jobId) { resets.push([job, jobId]); }
%(handler)s
function fire(job) {
  dl.running = wl.running = mt.running = false;
  refreshes = 0;
  handlers['job.started']({ job, job_id: 42 });
  return { dl: dl.running, wl: wl.running, mt: mt.running, refreshes };
}
console.log(JSON.stringify({
  subscribed: typeof handlers['job.started'] === 'function',
  batch: fire('batch'),
  watchlist: fire('watchlist'),
  maintenance: fire('maintenance'),
  update: fire('update'),
  unknown: fire('something-else'),
  resets,
}));
"""


def test_job_started_arms_the_category_it_names(app_js, tmp_path):
    """The startup scan, the tray's Scan Now and a second browser all reach
    the page only through this. Before it, the Watch List toolbar sat reading
    idle for the whole run — offering a scan the host would refuse and a
    Cancel that was closed."""
    r = _run_node(tmp_path, "dljob.mjs", _JOB_HARNESS % {
        "handler": _slice(app_js, "    cbApi.on('job.started'",
                          "    /* The one event that means a job category"),
    })

    assert r["subscribed"] is True
    assert r["batch"] == {"dl": True, "wl": False, "mt": False, "refreshes": 1}
    assert r["watchlist"] == {"dl": False, "wl": True, "mt": False,
                              "refreshes": 1}
    assert r["maintenance"] == {"dl": False, "wl": False, "mt": True,
                                "refreshes": 1}
    # A new run starts the Activity feed afresh; nothing else touches it.
    assert r["resets"] == [["batch", 42], ["watchlist", 42]]


def test_job_started_leaves_the_update_job_to_the_about_screen(app_js, tmp_path):
    """update.status drives those controls; a snapshot resync would fight it."""
    r = _run_node(tmp_path, "dljobup.mjs", _JOB_HARNESS % {
        "handler": _slice(app_js, "    cbApi.on('job.started'",
                          "    /* The one event that means a job category"),
    })

    assert r["update"] == {"dl": False, "wl": False, "mt": False,
                           "refreshes": 0}
    assert r["unknown"]["refreshes"] == 0


def test_the_running_row_is_kept_in_view_without_moving_the_page(app_js):
    """A boxed list can hide the row that matters. scrollIntoView would drag
    the screen behind it, and offsetTop answers relative to whichever ancestor
    happens to be positioned — neither is safe here."""
    fn = _slice(app_js, "  function scrollBoxToActive(",
                "  /* The kept run's title line")
    assert "getBoundingClientRect" in fn
    assert "scrollIntoView" not in fn
    assert "offsetTop" not in fn


def test_the_batch_rows_are_boxed_and_follow_the_running_row(app_css, app_js):
    """The Batch queue card used to grow one row per URL past four. It is a
    fixed four-row box now, so it has to scroll to the row that is
    downloading — in both the manual batch and the borrowed Watch List
    view."""
    rule = _slice(app_css, "#dl-rows {", "}")
    assert "height: 171px" in rule and "min-height" not in rule
    assert "overflow-y: auto" in rule
    for start, end in [("  function renderBatch()", "  /* The batch rows are boxed"),
                       ("  function renderWatchlistQueue()", "  function renderBatch()")]:
        assert "scrollBoxToActive(host, '.cb-qrow.is-active');" in _slice(app_js, start, end), start


# ── the Activity feed ────────────────────────────────────────────────────────
# One line per step of a run (run.activity), appended as it arrives rather
# than redrawn, newest at the bottom. When a run ends the host keeps its lines
# (snapshot `last_run.activity`, event queue.last_run); the panel shows them,
# with a small Clear beside its title, until they are cleared or another run
# takes the panel.

_ACT_HARNESS = """
function node(tag) {
  return {
    tag, children: [], className: '', style: {}, hidden: false, parent: null,
    scrollTop: 0, scrollHeight: 0, clientHeight: 100, _t: '',
    get textContent() {
      return this.children.length
        ? this.children.map((c) => c.textContent).join('') : this._t;
    },
    set textContent(v) { this._t = v; this.children = []; this.scrollHeight = 0; },
    appendChild(c) {
      const kids = c.frag ? c.children.splice(0) : [c];
      kids.forEach((k) => { k.parent = this; this.children.push(k); });
      this._t = '';
      this.scrollHeight = this.children.length * 20;
      return c;
    },
    remove() {
      const sib = this.parent.children;
      sib.splice(sib.indexOf(this), 1);
      this.parent.scrollHeight = sib.length * 20;
    },
    get firstElementChild() { return this.children[0] || null; },
    get childElementCount() { return this.children.length; },
  };
}
const document = {
  createElement: node,
  createDocumentFragment() { const f = node('#frag'); f.frag = true; return f; },
};
const els = { '#dl-activity': node(), '#dl-activity-meta': node(),
              '#dl-activity-clear': node() };
function $(sel) { return els[sel] || node(); }
function num(n) { return String(n == null ? 0 : n); }
const gated = [];
function gateWrite(el) { gated.push(el === els['#dl-activity-clear']); }
let view = %(view)s;
function dlView() { return view; }
const state = { last_run: %(last_run)s };
%(consts)s
%(fns)s
const log = els['#dl-activity'];
function dump() {
  return {
    lines: log.children.map((l) => l.children.map((c) => c.textContent).join('|')),
    classes: log.children.map((l) => l.className),
    text: log.children.length ? '' : log.textContent,
    meta: els['#dl-activity-meta'].textContent,
    clearHidden: els['#dl-activity-clear'].hidden,
    gated,
    scrollTop: log.scrollTop,
    scrollHeight: log.scrollHeight,
  };
}
const out = {};
%(script)s
console.log(JSON.stringify(out));
"""

_BATCH_VIEW = "{ kind: 'batch', running: true }"
_IDLE_VIEW = "{ kind: 'batch', running: false }"

_KEPT_ACTIVITY = [
    {"seq": 1, "ts": "03:10:00", "kind": "info", "title": "Channel Preview Channel", "detail": "3 tracks to download"},
    {"seq": 2, "ts": "03:10:01", "kind": "start", "title": "Track A", "detail": ""},
    {"seq": 3, "ts": "03:10:30", "kind": "downloaded", "title": "Track A", "detail": ""},
    {"seq": 4, "ts": "03:10:31", "kind": "skipped", "title": "Track B", "detail": "in database"},
    {"seq": 5, "ts": "03:10:40", "kind": "error", "title": "Track C", "detail": "private video"},
]


def _act(app_js, tmp_path, script, view=_IDLE_VIEW, last_run=None):
    return _run_node(tmp_path, "activity.mjs", _ACT_HARNESS % {
        "view": view,
        "last_run": json.dumps(last_run),
        "consts": _slice(app_js, "  const ACTIVITY_LIMIT = ", "\n  /* ── tooltips"),
        "fns": _slice(app_js, "  function lastRunMeta(",
                      "  /* Every write control funnels through here"),
        "script": script,
    })


def test_a_kept_run_fills_the_panel_with_its_steps(app_js, tmp_path):
    # 03:14 local, this morning — so the title line shows a time, not a date.
    import datetime as _dt
    at = _dt.datetime.now().replace(hour=3, minute=14, second=0, microsecond=0)
    r = _act(app_js, tmp_path, "renderActivityLog(); out.r = dump();", last_run={
        "job": "watchlist", "finished_at": at.timestamp(), "ok": True,
        "error": None, "rows": [], "tally": None,
        "activity": _KEPT_ACTIVITY})["r"]
    assert r["lines"] == [
        "03:10:00  |· |Channel Preview Channel| — 3 tracks to download",
        "03:10:01  |▸ |Downloading|  Track A",
        "03:10:30  |✓ |Downloaded|  Track A",
        "03:10:31  |↷ |Skipped|  Track B| — in database",
        "03:10:40  |✕ |Failed|  Track C| — private video",
    ]
    assert r["classes"] == ["cb-act cb-act--info", "cb-act cb-act--start",
                            "cb-act cb-act--downloaded", "cb-act cb-act--skipped",
                            "cb-act cb-act--error"]
    # No tally of its own (a Watch List run): the steps are counted instead.
    assert r["meta"].startswith("Watch List run finished ")
    assert r["meta"].endswith(" · 1 downloaded · 1 skipped · 1 failed")
    assert r["clearHidden"] is False
    assert r["gated"] == [True]
    # Opens on the newest line.
    assert r["scrollTop"] == r["scrollHeight"] == 100


def test_a_kept_batch_prefers_its_own_tally_and_says_cancelled(app_js, tmp_path):
    r = _act(app_js, tmp_path, "renderActivityLog(); out.r = dump();", last_run={
        "job": "batch", "finished_at": 1_700_000_000, "ok": True, "error": None,
        "rows": [], "activity": _KEPT_ACTIVITY[:1],
        "tally": {"downloaded": 4, "skipped": 2, "errors": 1, "cancelled": True}})["r"]
    assert len(r["lines"]) == 1
    assert r["meta"].startswith("Batch cancelled ")
    assert r["meta"].endswith(" · 4 downloaded · 2 skipped · 1 error")


def test_a_kept_run_without_steps_still_explains_itself(app_js, tmp_path):
    r = _act(app_js, tmp_path, "renderActivityLog(); out.r = dump();", last_run={
        "job": "watchlist", "finished_at": 1, "ok": True, "error": None,
        "rows": [{"id": 1, "index": 0, "state": "done", "title": "C", "detail": ""}],
        "tally": None})["r"]
    assert r["text"] == "No steps were recorded for this run."
    assert r["meta"].endswith(" · 1 done · 0 skipped · 0 errors")


def test_with_nothing_kept_the_panel_invites_a_start(app_js, tmp_path):
    r = _act(app_js, tmp_path, "renderActivityLog(); out.r = dump();")["r"]
    assert r["text"] == ("Nothing yet — press Start Downloads and each step "
                         "will show here.")
    assert r["meta"] == "empty"
    assert r["clearHidden"] is True


def test_a_live_run_appends_one_line_per_step_without_redrawing(app_js, tmp_path):
    """The kept run yields to a live one, and each step adds exactly one line
    — the nodes already drawn are the same nodes afterwards, not a rebuild."""
    r = _act(app_js, tmp_path, """
resetActivity('batch');
renderActivityLog();
out.start = dump();
appendActivity({ job: 'batch', seq: 1, ts: '14:02:11', kind: 'start', verb: 'Reading link', title: 'https://x/y', detail: '' });
const first = log.children[0];
appendActivity({ job: 'batch', seq: 2, ts: '14:02:15', kind: 'start', title: 'Track A', detail: '' });
appendActivity({ job: 'batch', seq: 3, ts: '14:02:40', kind: 'downloaded', title: 'Track A', detail: '' });
appendActivity({ job: 'batch', seq: 4, ts: '14:02:41', kind: 'skipped', title: 'Track B', detail: 'in database' });
appendActivity({ job: 'batch', seq: 5, ts: '14:02:50', kind: 'error', title: '<b>Track C</b>', detail: 'private video' });
renderActivityLog();
out.sameNode = log.children[0] === first;
out.tips = log.children.map((l) => l.title);
out.r = dump();
""", view=_BATCH_VIEW, last_run={
        "job": "watchlist", "finished_at": 1, "ok": True, "error": None,
        "rows": [], "tally": None, "activity": _KEPT_ACTIVITY})
    assert r["start"]["text"] == "Starting…"
    assert r["start"]["clearHidden"] is True
    assert r["sameNode"] is True
    assert r["r"]["lines"] == [
        "14:02:11  |▸ |Reading link|  https://x/y",
        "14:02:15  |▸ |Downloading|  Track A",
        "14:02:40  |✓ |Downloaded|  Track A",
        "14:02:41  |↷ |Skipped|  Track B| — in database",
        "14:02:50  |✕ |Failed|  <b>Track C</b>| — private video",
    ]
    assert r["r"]["meta"] == "1 downloaded · 1 skipped · 1 failed"
    # Lines are clipped to one row; the full text is the line's tooltip.
    assert r["tips"] == [l.replace("|", "") for l in r["r"]["lines"]]


def test_a_step_already_seen_is_not_drawn_twice(app_js, tmp_path):
    r = _act(app_js, tmp_path, """
resetActivity('batch');
renderActivityLog();
appendActivity({ job: 'batch', seq: 1, ts: 't', kind: 'downloaded', title: 'A', detail: '' });
appendActivity({ job: 'batch', seq: 1, ts: 't', kind: 'downloaded', title: 'A', detail: '' });
out.r = dump();
""", view=_BATCH_VIEW)["r"]
    assert len(r["lines"]) == 1
    assert r["meta"] == "1 downloaded · 0 skipped · 0 failed"


def test_the_feed_follows_the_newest_line_only_from_the_bottom(app_js, tmp_path):
    """Scrolled back to read an earlier line, the reader stays put."""
    r = _act(app_js, tmp_path, """
resetActivity('batch');
renderActivityLog();
let n = 0;
const step = () => appendActivity({ job: 'batch', seq: ++n, ts: 't', kind: 'info', title: 'step ' + n, detail: '' });
for (let i = 0; i < 10; i += 1) step();
out.following = log.scrollTop;
log.scrollTop = 0;
step();
out.reading = log.scrollTop;
""", view=_BATCH_VIEW)
    assert r["following"] == 200
    assert r["reading"] == 0


def test_the_feed_is_capped(app_js, tmp_path):
    r = _act(app_js, tmp_path, """
resetActivity('batch');
renderActivityLog();
for (let i = 1; i <= ACTIVITY_LIMIT + 5; i += 1) {
  appendActivity({ job: 'batch', seq: i, ts: 't', kind: 'downloaded', title: 'T' + i, detail: '' });
}
out.dom = log.childElementCount;
out.kept = act.lines.batch.length;
out.firstDrawn = log.children[0].children[3].textContent;
out.meta = els['#dl-activity-meta'].textContent;
""", view=_BATCH_VIEW)
    assert r["dom"] == r["kept"] == 1000
    assert r["firstDrawn"] == "  T6"
    # The tally counts every step, not only the ones still on screen.
    assert r["meta"] == "1005 downloaded · 0 skipped · 0 failed"


def test_another_jobs_steps_are_kept_but_not_drawn(app_js, tmp_path):
    """A Watch List download running beside a batch keeps its own feed for
    the moment it borrows the panel."""
    r = _act(app_js, tmp_path, """
resetActivity('batch');
resetActivity('watchlist');
renderActivityLog();
appendActivity({ job: 'watchlist', seq: 1, ts: 't', kind: 'start', title: 'W', detail: '' });
out.drawn = dump().lines.length;
view = { kind: 'watchlist', running: true };
renderActivityLog();
out.r = dump();
""", view=_BATCH_VIEW)
    assert r["drawn"] == 0
    assert r["r"]["lines"] == ["t  |▸ |Downloading|  W"]


def test_a_reload_mid_run_merges_the_hosts_steps_with_newer_ones(app_js, tmp_path):
    """A step can arrive while the snapshot is in flight: it is newer than
    the snapshot's last `seq`, so it stays on the end instead of being lost."""
    r = _act(app_js, tmp_path, """
appendActivity({ job: 'batch', seq: 3, ts: 't3', kind: 'skipped', title: 'C', detail: '' });
syncActivity({ running: { batch: true, watchlist: false },
               run_activity: { batch: { job_id: null, lines: [
                 { seq: 1, ts: 't1', kind: 'start', title: 'A', detail: '' },
                 { seq: 2, ts: 't2', kind: 'downloaded', title: 'A', detail: '' } ] } } });
renderActivityLog();
out.r = dump();
""", view=_BATCH_VIEW)["r"]
    assert [l.split("|")[0] for l in r["lines"]] == ["t1  ", "t2  ", "t3  "]
    assert r["meta"] == "1 downloaded · 1 skipped · 0 failed"


def test_a_page_that_missed_a_runs_start_does_not_merge_two_runs(app_js, tmp_path):
    """seq restarts at 1 every run. A remote page whose socket dropped across
    run A ending and run B starting still holds A's lines; B's must replace
    them, not be dropped as already seen."""
    r = _act(app_js, tmp_path, """
resetActivity('batch', 7);
renderActivityLog();
for (let i = 1; i <= 5; i += 1) {
  appendActivity({ job: 'batch', job_id: 7, seq: i, ts: 'a', kind: 'downloaded', title: 'A' + i, detail: '' });
}
appendActivity({ job: 'batch', job_id: 8, seq: 1, ts: 'b', kind: 'error', title: 'B1', detail: '' });
out.live = dump();
resetActivity('batch', 7);
appendActivity({ job: 'batch', job_id: 7, seq: 1, ts: 'a', kind: 'downloaded', title: 'A1', detail: '' });
syncActivity({ running: { batch: true },
               run_activity: { batch: { job_id: 9, lines: [
                 { seq: 1, ts: 'c', kind: 'skipped', title: 'C1', detail: '' } ] } } });
renderActivityLog();
out.synced = dump();
""", view=_BATCH_VIEW)
    assert r["live"]["lines"] == ["b  |✕ |Failed|  B1"]
    assert r["live"]["meta"] == "0 downloaded · 0 skipped · 1 failed"
    assert r["synced"]["lines"] == ["c  |↷ |Skipped|  C1"]
    assert r["synced"]["meta"] == "0 downloaded · 1 skipped · 0 failed"


def test_another_jobs_reset_or_a_resync_keeps_the_readers_place(app_js, tmp_path):
    """A Watch List scan starting (the automation timer) must not redraw the
    batch feed, and a resync that does redraw it leaves a reader who had
    scrolled back where they were."""
    r = _act(app_js, tmp_path, """
resetActivity('batch', 1);
renderActivityLog();
for (let i = 1; i <= 20; i += 1) {
  appendActivity({ job: 'batch', job_id: 1, seq: i, ts: 't', kind: 'info', title: 's' + i, detail: '' });
}
const first = log.children[0];
log.scrollTop = 40;
resetActivity('watchlist', 2);
renderActivityLog();
out.untouched = log.children[0] === first;
out.afterScan = log.scrollTop;
syncActivity({ running: { batch: true },
               run_activity: { batch: { job_id: 1, lines: [
                 { seq: 21, ts: 't', kind: 'info', title: 's21', detail: '' } ] } } });
renderActivityLog();
out.redrawn = log.children[0] !== first;
out.afterSync = log.scrollTop;
out.count = log.childElementCount;
log.scrollTop = log.scrollHeight - log.clientHeight;
syncActivity({ running: { batch: true },
               run_activity: { batch: { job_id: 1, lines: [
                 { seq: 22, ts: 't', kind: 'info', title: 's22', detail: '' } ] } } });
renderActivityLog();
out.followed = log.scrollTop === log.scrollHeight;
""", view=_BATCH_VIEW)
    assert r["untouched"] is True
    assert r["afterScan"] == 40
    assert r["redrawn"] is True
    assert r["afterSync"] == 40
    assert r["count"] == 21
    assert r["followed"] is True


def test_activity_is_subscribed_and_drawn_as_text(app_js):
    assert "cbApi.on('run.activity', appendActivity);" in app_js
    body = _slice(app_js, "  function activityLine(e)", "  function resetActivity(")
    assert "innerHTML" not in body
    assert "syncActivity(state);" in _slice(app_js, "  async function refresh()",
                                            "  function isBatchProgress(")
    for gone in ("renderQueueLog", "queueLogLine", "DL_LOG_CLASS", "#dl-queue"):
        assert gone not in app_js, gone


def test_clear_asks_the_host_and_the_event_repaints_every_page(app_js, index_html):
    assert 'id="dl-activity-clear"' in index_html
    assert '<span class="cb-kick">Activity</span>' in index_html
    assert 'id="dl-queue' not in index_html
    clear = _slice(app_js, "    $('#dl-activity-clear').addEventListener('click'",
                   "    $$('#dl-platform > span')")
    assert "await call('queue.clear_last_run');" in clear
    assert "state.last_run = null;" in clear
    handler = _slice(app_js, "    cbApi.on('queue.last_run', (last) => {", "    });")
    assert "state.last_run = last || null;" in handler
    assert "renderActivityLog();" in handler


# ── the Skip row is the same setting the Settings screen shows ───────────────
# skip_existing and skip_mode are drawn twice — the Downloads screen's Skip row
# and the Downloads section of Settings. The row used to be filled from nothing
# and wired to nothing: unchecked whatever the host held, and a click that
# changed nothing. Both copies now paint from the host's stored value, write
# through the same save(), and follow each other.

_SKIP_HARNESS = """
const registry = [];
function el(id, tag, type, key) {
  const e = { id, tagName: tag, type: type || '', checked: false, value: '',
              options: [], disabled: false, reason: '', dataset: {}, attrs: {},
              setAttribute(k, v) { this.attrs[k] = v; },
              removeAttribute(k) { delete this.attrs[k]; },
              insertBefore(opt) { this.options.unshift(opt); },
              get firstChild() { return this.options[0] || null; } };
  if (key) e.dataset.key = key;
  registry.push(e);
  return e;
}
function option(text) { return { value: text, textContent: text }; }
const MODES = ['In Database ~ In Folder', 'In Folder Only', 'In Database Only'];
const els = {
  'dl-skip': el('dl-skip', 'INPUT', 'checkbox', 'skip_existing'),
  'dl-skipmode': el('dl-skipmode', 'SELECT', 'select-one', 'skip_mode'),
};
MODES.forEach((m) => els['dl-skipmode'].options.push(option(m)));
// The Settings grid's copies of the same two keys.
const gridBox = el('grid-skip', 'INPUT', 'checkbox', 'skip_existing');
const gridMode = el('grid-mode', 'SELECT', 'select-one', 'skip_mode');
MODES.forEach((m) => gridMode.options.push(option(m)));
function $(sel) { return els[sel.slice(1)] || null; }
function $$(sel) {
  const m = /\\[data-key="([^"]+)"\\]/.exec(sel);
  return m ? registry.filter((e) => e.dataset.key === m[1]) : [];
}
const document = {
  createElement: (tag) => ({ tagName: tag.toUpperCase(), value: '', textContent: '' }),
};
function gateWrite(e, reason) { e.disabled = !!reason; e.reason = reason || ''; }
const toasts = [];
function toast(text, isError) { toasts.push({ text, isError: !!isError }); }
function applySettingsDependencies() {}
const dl = { running: false };
const wl = { running: false };
const state = { settings: { skip_existing: false, skip_mode: 'In Folder Only' } };
const calls = [];
let refuse = null;
const cbApi = { call: async (method, params) => {
  calls.push({ method, params });
  if (refuse) { const err = new Error(refuse); err.userFacing = true; throw err; }
  return { value: params.value };
} };
%(helpers)s
%(skip)s
%(save)s
const box = $('#dl-skip');
const mode = $('#dl-skipmode');
function copies() {
  return { row: { checked: box.checked, mode: mode.value },
           grid: { checked: gridBox.checked, mode: gridMode.value } };
}
%(scenario)s
"""


def _skip_harness(app_js, scenario):
    return _SKIP_HARNESS % {
        "helpers": _slice(app_js, "  function paintSettingControl(",
                          "  async function save("),
        "skip": _slice(app_js, "  const SKIP_LOCKED_REASON =",
                       "  function renderDownloads()"),
        "save": _slice(app_js, "  async function save(key, value, el)",
                       "  /* ── database maintenance (3m long-job shell)"),
        "scenario": scenario,
    }


def test_the_skip_row_shows_the_stored_setting(app_js, tmp_path):
    """Painted from state.settings on every render — the row used to sit
    unchecked whatever the host actually held."""
    r = _run_node(tmp_path, "dlskip_show.mjs", _skip_harness(app_js, """
renderDownloadsSkip();
const off = copies();
state.settings.skip_existing = true;
state.settings.skip_mode = 'In Database Only';
renderDownloadsSkip();
const on = copies();
// A stored value the markup does not list is still shown, as the grid does.
state.settings.skip_mode = 'Somewhere Else';
renderDownloadsSkip();
const odd = { mode: mode.value, first: mode.options[0].value };
console.log(JSON.stringify({ off, on, odd }));
"""))

    assert r["off"] == {"row": {"checked": False, "mode": "In Folder Only"},
                        "grid": {"checked": False, "mode": "In Folder Only"}}
    assert r["on"] == {"row": {"checked": True, "mode": "In Database Only"},
                       "grid": {"checked": True, "mode": "In Database Only"}}
    assert r["odd"] == {"mode": "Somewhere Else", "first": "Somewhere Else"}


def test_a_change_on_either_screen_saves_once_and_moves_the_other_copy(app_js, tmp_path):
    """Both copies write the same key through the same save(), and the copy
    that was not clicked follows the host's answer."""
    r = _run_node(tmp_path, "dlskip_save.mjs", _skip_harness(app_js, """
renderDownloadsSkip();
(async () => {
  box.checked = true;                                   // the click
  await save('skip_existing', box.checked, box);
  const fromRow = { call: calls[0], stored: state.settings.skip_existing,
                    copies: copies() };
  mode.value = 'In Database Only';
  await save('skip_mode', mode.value, mode);
  const modeFromRow = { call: calls[1], copies: copies() };
  gridBox.checked = false;                              // the grid's turn
  await save('skip_existing', gridBox.checked, gridBox);
  const fromGrid = { call: calls[2], copies: copies() };
  console.log(JSON.stringify({ fromRow, modeFromRow, fromGrid, n: calls.length }));
})();
"""))

    assert r["fromRow"]["call"] == {"method": "settings.set",
                                    "params": {"key": "skip_existing", "value": True}}
    assert r["fromRow"]["stored"] is True
    assert r["fromRow"]["copies"]["grid"]["checked"] is True
    assert r["modeFromRow"]["call"]["params"] == {"key": "skip_mode",
                                                  "value": "In Database Only"}
    assert r["modeFromRow"]["copies"]["grid"]["mode"] == "In Database Only"
    assert r["fromGrid"]["copies"]["row"]["checked"] is False
    assert r["n"] == 3


def test_a_refused_change_snaps_both_copies_back_to_the_stored_value(app_js, tmp_path):
    """The host refuses these keys mid-run. A refused checkbox already flipped
    back; a refused select used to keep showing the value the host never
    took."""
    r = _run_node(tmp_path, "dlskip_refused.mjs", _skip_harness(app_js, """
renderDownloadsSkip();
refuse = 'A download is running, so the skip mode is frozen until it finishes.';
(async () => {
  mode.value = 'In Database Only';
  await save('skip_mode', mode.value, mode);
  box.checked = true;
  await save('skip_existing', box.checked, box);
  console.log(JSON.stringify({ copies: copies(), toasts,
                               stored: state.settings }));
})();
"""))

    assert r["copies"] == {"row": {"checked": False, "mode": "In Folder Only"},
                           "grid": {"checked": False, "mode": "In Folder Only"}}
    assert r["stored"] == {"skip_existing": False, "skip_mode": "In Folder Only"}
    assert all(t["isError"] for t in r["toasts"]) and len(r["toasts"]) == 2


def test_the_skip_row_is_locked_for_the_length_of_a_download(app_js, tmp_path):
    """_set_download_lock's two widgets, and the host's own rule: a batch or a
    Watch List job alike freezes DOWNLOAD_LOCKED_SETTINGS."""
    r = _run_node(tmp_path, "dlskip_lock.mjs", _skip_harness(app_js, """
function snap() {
  return { box: [box.disabled, box.reason], mode: [mode.disabled, mode.reason] };
}
renderDownloadsSkip();
const idle = snap();
dl.running = true;
renderDownloadsSkip();
const batch = snap();
dl.running = false; wl.running = true;
renderDownloadsSkip();
const watch = snap();
wl.running = false;
renderDownloadsSkip();
console.log(JSON.stringify({ idle, batch, watch, again: snap(),
                             reason: SKIP_LOCKED_REASON }));
"""))

    assert r["idle"] == {"box": [False, ""], "mode": [False, ""]}
    assert r["batch"] == {"box": [True, r["reason"]], "mode": [True, r["reason"]]}
    assert r["watch"] == r["batch"]
    assert r["again"] == r["idle"]
    assert "download is running" in r["reason"]


def test_the_skip_row_is_wired_and_rendered(app_js, index_html):
    """The markup carries the keys, the controls write through save(), and the
    Downloads render path paints the row — the three things that were missing."""
    assert 'id="dl-skip" data-key="skip_existing"' in index_html
    assert 'id="dl-skipmode" data-key="skip_mode"' in index_html
    assert "$('#dl-skip').addEventListener('change'" in app_js
    assert "save('skip_existing', $('#dl-skip').checked" in app_js
    assert "save('skip_mode', $('#dl-skipmode').value" in app_js
    body = _slice(app_js, "  function renderDownloads()", "  /* ── modal shell")
    assert "renderDownloadsSkip();" in body


# ── a link with no genre is asked about before it joins the queue ────────────
# The Main tab's "No Genre Selected" ask, and its "Start runs the URL box"
# shortcut, both went missing in the v2.0 rewrite: a link filed under (none)
# went straight into _No Genre with no word said, and Start stayed grey until
# the user also pressed Add. Both are back, sharing one add path.

_ADD_HARNESS = """
const els = {
  '#dl-url': { value: '', focused: false, focus() { this.focused = true; } },
  '#dl-genre': { value: '(none)', focused: false, focus() { this.focused = true; } },
  '#dl-platform .is-on': { dataset: { platform: 'YouTube' } },
};
function $(sel) { return els[sel] || null; }
const state = { batch: [] };
const calls = [], toasts = [], notes = [];
let painted = 0;
function renderBatch() { painted += 1; }
function toast(text, isError) { toasts.push({ text, isError: !!isError }); }
async function call(method, params) {
  calls.push({ method, params });
  return method === 'batch.list' ? [{ id: 1, url: params && params.url }] : {};
}
function modalNote(text) { notes.push(text); return { text }; }
function modalButton(label, cls, onClick) {
  return { label, cls, onClick, style: {} };
}
let dialog = null;
function openModal(opts) {
  const foot = { buttons: [], append(...b) { this.buttons.push(...b); } };
  const body = { appendChild() {} };
  dialog = { opts, foot };
  opts.body(body, {});
  opts.foot(foot, {});
}
function closeModal() {
  const d = dialog; dialog = null;
  if (d && d.opts.onClose) d.opts.onClose();
}
function press(label) {
  dialog.foot.buttons.find((b) => b.label === label).onClick();
}
%(add)s
%(scenario)s
"""


def _add_harness(app_js, scenario):
    return _ADD_HARNESS % {
        "add": _slice(app_js, "  const NO_GENRE_VALUE =", "  function wire()"),
        "scenario": scenario,
    }


def test_a_link_with_a_genre_is_added_without_a_word(app_js, tmp_path):
    r = _run_node(tmp_path, "dladd_genre.mjs", _add_harness(app_js, """
(async () => {
  els['#dl-url'].value = '  https://x/y  ';
  els['#dl-genre'].value = 'Techno';
  const added = await addToBatch();
  console.log(JSON.stringify({ added, asked: !!dialog, calls, painted,
                               box: els['#dl-url'].value, toasts }));
})();
"""))
    assert r["added"] is True
    assert r["asked"] is False
    assert r["calls"][0] == {"method": "batch.add",
                             "params": {"url": "https://x/y", "genre": "Techno",
                                        "platform": "YouTube"}}
    assert r["calls"][1]["method"] == "batch.list"
    assert r["painted"] == 1 and r["box"] == ""
    assert r["toasts"] == [{"text": "Added to batch", "isError": False}]


def test_a_link_with_no_genre_waits_on_the_gate(app_js, tmp_path):
    """Nothing reaches the host until the user answers; backing out — the
    button, Escape, ✕ or the dim all land in onClose the same way — adds
    nothing, keeps the link in the box, and hands focus to the genre list."""
    r = _run_node(tmp_path, "dladd_gate.mjs", _add_harness(app_js, """
(async () => {
  els['#dl-url'].value = 'https://x/y';
  const pending = addToBatch();
  const opened = { title: dialog.opts.title, notes: notes.slice(),
                   buttons: dialog.foot.buttons.map((b) => b.label),
                   callsSoFar: calls.length };
  press('Pick a genre first');
  const backedOut = { added: await pending, calls: calls.length,
                      box: els['#dl-url'].value,
                      genreFocused: els['#dl-genre'].focused };
  els['#dl-genre'].focused = false;
  const again = addToBatch();
  press('Add without one');
  const wentAhead = { added: await again, calls: calls.map((c) => c.method),
                      genre: calls[0].params.genre, box: els['#dl-url'].value,
                      genreFocused: els['#dl-genre'].focused };
  console.log(JSON.stringify({ opened, backedOut, wentAhead }));
})();
"""))
    assert r["opened"]["title"] == "No genre picked"
    assert r["opened"]["callsSoFar"] == 0
    assert r["opened"]["buttons"] == ["Pick a genre first", "Add without one"]
    assert any("_No Genre" in n for n in r["opened"]["notes"])
    assert r["backedOut"] == {"added": False, "calls": 0, "box": "https://x/y",
                              "genreFocused": True}
    assert r["wentAhead"]["added"] is True
    assert r["wentAhead"]["calls"] == ["batch.add", "batch.list"]
    assert r["wentAhead"]["genre"] == "(none)"
    assert r["wentAhead"]["box"] == ""
    assert r["wentAhead"]["genreFocused"] is False


def test_an_empty_box_is_refused_before_any_gate(app_js, tmp_path):
    r = _run_node(tmp_path, "dladd_empty.mjs", _add_harness(app_js, """
(async () => {
  els['#dl-url'].value = '   ';
  const added = await addToBatch();
  console.log(JSON.stringify({ added, asked: !!dialog, calls: calls.length, toasts }));
})();
"""))
    assert r == {"added": False, "asked": False, "calls": 0,
                 "toasts": [{"text": "Paste a YouTube or SoundCloud link first.",
                             "isError": True}]}


# ── Start wakes up on a pasted link, and takes it along ──────────────────────

_START_GATE_HARNESS = """
function node() {
  return { children: [], textContent: '', className: '', style: {},
           innerHTML: '', appendChild(c) { this.children.push(c); } };
}
const document = { createElement: node };
const els = { '#dl-rows': node(), '#dl-count': node(),
              '#dl-url': { value: '' } };
function $(sel) { return els[sel] || node(); }
function dlView() { return { kind: 'batch' }; }
const dl = { running: false, rows: {} };
const state = { batch: [] };
const gates = [];
function setStartDisabled(off, why) { gates.push([!!off, why]); }
function gateWrite() {}
function renderActivityLog() {}
function scrollBoxToActive() {}
%(pending)s
%(batch)s
const out = {};
renderBatch(); out.blank = gates.pop();
els['#dl-url'].value = '  https://x/y ';
renderBatch(); out.pasted = gates.pop();
els['#dl-url'].value = '   ';
renderBatch(); out.cleared = gates.pop();
console.log(JSON.stringify(out));
"""


def test_start_is_open_the_moment_a_link_is_pasted(app_js, tmp_path):
    """The empty-queue branch reads the box: text in it opens Start, clearing
    it closes Start again with the same reason as before."""
    r = _run_node(tmp_path, "dlstart_gate.mjs", _START_GATE_HARNESS % {
        "pending": _slice(app_js, "  function pendingUrl()",
                          "  /* The Main tab's \"No Genre Selected\" ask"),
        "batch": _slice(app_js, "  function renderBatch()",
                        "  /* The batch rows are boxed"),
    })
    why = "Add a link to the queue before starting a download."
    assert r["blank"] == [True, why]
    assert r["pasted"] == [False, why]
    assert r["cleared"] == [True, why]


def test_start_adds_the_pasted_link_before_it_starts(app_js):
    """Typing repaints the empty queue, and Start runs the shared add path
    first — so a backed-out genre gate starts nothing."""
    assert "$('#dl-url').addEventListener('input'" in app_js
    start = _slice(app_js, "$('#dl-start').addEventListener('click'",
                   "$('#dl-cancel').addEventListener('click'")
    assert "if (pendingUrl() && !(await addToBatch())) return;" in start
    assert start.index("addToBatch()") < start.index("call('download.start')")


def test_auth_trouble_event_opens_the_dialog(app_js):
    assert "cbApi.on('auth.trouble'" in app_js
    assert "function openAuthTroubleDialog(" in app_js
    # once per job, and a session mute
    assert "authTrouble.shownFor" in app_js
    assert "authTrouble.muted" in app_js
    # a dialog the user is already typing in must not be thrown away for it:
    # once per job still, but a toast instead of the pop-up while one is open
    handler = app_js[app_js.index("cbApi.on('auth.trouble'"):]
    handler = handler[:handler.index("openAuthTroubleDialog(p);")]
    assert "authTrouble.shownFor[job] = true;" in handler
    assert "if (openDialog) {" in handler.split("authTrouble.shownFor[job] = true;")[1]
    assert "check Settings ▸ Browser & Cookies" in handler
    # the three cookie states each get their own copy
    assert "switching browser cookies off" in app_js
    assert "cookie file may have expired" in app_js
    assert "Open cookie settings" in app_js

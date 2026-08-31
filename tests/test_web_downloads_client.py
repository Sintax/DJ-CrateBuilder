"""web/app.js: the Downloads screen's client-side invariants.

Same method as tests/test_web_watchlist_client.py — the real functions are
sliced out of app.js verbatim and run in Node against stub state, so a test
cannot pass just because someone reformatted the line it names.

Covers the two things a running job used to get wrong here: the panel changing
height the moment a download started, and a run the page did not start itself
never arming anything at all.
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


def test_the_queue_log_is_boxed_rather_than_floored(app_css):
    """min-height let the card grow one line per queued track the moment a
    run started. It has to be a fixed height that scrolls."""
    rule = _slice(app_css, "#dl-queue {", "}")
    assert "height: 119px" in rule and "min-height" not in rule
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
%(handler)s
function fire(job) {
  dl.running = wl.running = mt.running = false;
  refreshes = 0;
  handlers['job.started']({ job });
  return { dl: dl.running, wl: wl.running, mt: mt.running, refreshes };
}
console.log(JSON.stringify({
  subscribed: typeof handlers['job.started'] === 'function',
  batch: fire('batch'),
  watchlist: fire('watchlist'),
  maintenance: fire('maintenance'),
  update: fire('update'),
  unknown: fire('something-else'),
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


def test_job_started_leaves_the_update_job_to_the_about_screen(app_js, tmp_path):
    """update.status drives those controls; a snapshot resync would fight it."""
    r = _run_node(tmp_path, "dljobup.mjs", _JOB_HARNESS % {
        "handler": _slice(app_js, "    cbApi.on('job.started'",
                          "    /* The one event that means a job category"),
    })

    assert r["update"] == {"dl": False, "wl": False, "mt": False,
                           "refreshes": 0}
    assert r["unknown"]["refreshes"] == 0


def test_the_running_line_is_kept_in_view_without_moving_the_page(app_js):
    """A boxed log can hide the line that matters. scrollIntoView would drag
    the screen behind it, and offsetTop answers relative to whichever ancestor
    happens to be positioned — neither is safe here."""
    fn = _slice(app_js, "  function scrollQueueLogToActive(",
                "  function renderQueueLog()")
    assert "getBoundingClientRect" in fn
    assert "scrollIntoView" not in fn
    assert "offsetTop" not in fn
    # Both renderQueueLog branches end by calling it — the Watch List run
    # borrows this same log.
    body = _slice(app_js, "  function renderQueueLog()",
                  "  /* Every write control funnels through here")
    assert body.count("scrollQueueLogToActive(log)") == 2

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
function renderQueueLog() {}
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
                        "  /* One line of the queue log"),
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

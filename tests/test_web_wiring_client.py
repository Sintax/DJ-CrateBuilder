"""web/app.js: every control the bundle draws is wired to the host, or says
why not — never a stub that looks live.

The static half asserts the stubs are gone for good; the Node half runs the
pieces that replaced them — the How-To button's relabelling, the New Genre
form's OK gating and its call, the Genre row's Remove — against stub state,
sliced out of app.js verbatim like the other client tests.
"""
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


@pytest.fixture(scope="module")
def index_html():
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as fh:
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


# ── nothing is left as a stub ────────────────────────────────────────────────

def test_no_control_is_a_stub_any_more(app_js, index_html):
    """The phrases the stubs used to carry, the helper that drew them, and the
    two buttons the desktop app never had (Test authentication, Re-embed)."""
    for gone in ("Not wired up yet", "stubButton", "WL_NO_HOST_ACTION",
                 "Test authentication", "change it in the desktop app"):
        assert gone not in app_js, gone
    assert "db-art-reembed" not in index_html
    assert "db-art-reembed" not in app_js


def test_the_downloads_new_genre_button_is_live(app_js, index_html):
    assert 'id="dl-newgenre"' in index_html
    assert "$('#dl-newgenre').addEventListener('click'" in app_js
    body = _slice(app_js, "  function renderDownloads()", "  /* ── modal shell")
    assert "renderDownloadsGenre();" in body


def test_the_artwork_tabs_fetch_button_starts_the_same_job(app_js):
    assert ("$('#db-art-fetch').addEventListener('click', "
            "() => maintConfirm('db.fetch_artwork'))") in app_js
    for fn in ("  function maintBegin(task) {", "  function maintSettle(payload) {"):
        assert "dbGateArtworkFetch();" in _slice(app_js, fn, "\n  }\n")


def test_the_channel_dialogs_hand_the_genre_row_a_platform(app_js):
    assert "genreRow(genreEl, () => platformFromUrl(urlEl.value))" in app_js
    assert ("genreRow(genreEl,\n          () => row.platform || "
            "platformFromUrl(urlEl.value))") in app_js


# ── the How-To button follows the browser and the cookies toggle ─────────────

_HOWTO_HARNESS = """
const state = { settings: { use_cookies: true, cookies_browser: 'Firefox',
                            cookie_method: 'Browser Profile', sleep_enabled: true,
                            sleep_mode: 'Auto', limit_enabled: true },
                host: { transport: 'local' } };
const REMOTE_SETTING_KEYS = [];
function setDisabled(el, disabled, opts) { el.disabled = !!disabled; el.opts = opts || {}; }
function bindTips() {}
function writeBlocked() { return ''; }
const howto = { id: 'cookie-howto', textContent: '' };
const grid = { querySelector: () => null };
function $(sel) {
  if (sel === '#settings-grid') return grid;
  if (sel === '#cookie-howto') return howto;
  return null;
}
function $$() { return []; }
%(fn)s
applySettingsDependencies();
const on = { text: howto.textContent, off: howto.disabled, tt: howto.opts.ttKey };
state.settings.cookies_browser = 'Brave';
applySettingsDependencies();
const brave = howto.textContent;
state.settings.use_cookies = false;
applySettingsDependencies();
const off = { off: howto.disabled, reason: howto.opts.reason };
console.log(JSON.stringify({ on, brave, off }));
"""


def test_the_howto_button_names_the_browser_and_greys_with_cookies_off(app_js, tmp_path):
    """The monolith's _update_howto_label and its cookies-toggle greying."""
    r = _run_node(tmp_path, "howto.mjs", _HOWTO_HARNESS % {
        "fn": _slice(app_js, "  function applySettingsDependencies()",
                     "  /* One setting, drawn twice"),
    })

    assert r["on"] == {"text": "📖 How-To: Setting Up a Dedicated Firefox Profile",
                       "off": False, "tt": "settings.firefox_profile_howto"}
    assert r["brave"] == "📖 How-To: Setting Up a Dedicated Brave Profile"
    assert r["off"] == {"off": True, "reason": "Turn on Use Browser Cookies first."}


# ── the New Genre form and the Genre row ─────────────────────────────────────

_GENRE_HARNESS = """
const made = [], buttons = [], toasts = [], calls = [];
let answer = null, refuse = null;
function mk(tag) {
  const el = { tagName: tag.toUpperCase(), children: [], handlers: {}, style: {},
    className: '', textContent: '', hidden: false, options: [], disabled: false,
    innerHTML: '', attrs: {},
    appendChild(c) { this.children.push(c); if (tag === 'select') this.options.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(n, fn) { this.handlers[n] = fn; },
    fire(n, ev) { return this.handlers[n] ? this.handlers[n](ev || {}) : undefined; },
    remove() { this.removed = true; },
    setAttribute(k, v) { this.attrs[k] = v; },
    querySelector() { return null; },
    focus() { this.focused = true; } };
  let v = '';
  Object.defineProperty(el, 'value', {
    get() { return v || (tag === 'select' && el.options[0] ? el.options[0].value : ''); },
    set(x) { v = x; } });
  made.push(el);
  return el;
}
const document = { createElement: mk };
function modalButton(label, cls, onClick) {
  const b = mk('button'); b.textContent = label; b.onClick = onClick; buttons.push(b); return b;
}
function labelled(text, control) { const w = mk('div'); w.label = text; w.appendChild(control); return w; }
function modalNote(t) { const p = mk('p'); p.textContent = t; return p; }
function setDisabled(el, d, opts) { el.disabled = !!d; el.reason = (opts && opts.reason) || ''; }
function toast(text, isError) { toasts.push({ text, isError: !!isError }); }
const state = { genres: ['House'] };
function genreSelect(current) {
  const s = mk('select');
  ['House', '(none)'].concat(state.genres.filter((g) => g !== 'House')).forEach((g) => {
    const o = mk('option'); o.value = g; o.textContent = g; s.appendChild(o);
  });
  s.value = current;
  return s;
}
const cbApi = { call: async (method, params) => {
  calls.push({ method, params });
  if (refuse) { const e = new Error(refuse); e.userFacing = true; throw e; }
  return answer;
} };
%(code)s
%(scenario)s
"""


def _genre_harness(app_js, scenario):
    return _GENRE_HARNESS % {
        "code": _slice(app_js, "  const GENRE_PLATFORMS = ",
                       "  function openAddChannel()"),
        "scenario": scenario,
    }


def test_ok_stays_shut_until_a_platform_is_picked_then_the_host_is_asked(app_js, tmp_path):
    """The monolith's greyed OK, and its "already exists" box rather than an
    error when the folder is there."""
    r = _run_node(tmp_path, "newgenre.mjs", _genre_harness(app_js, """
(async () => {
  const created = [];
  answer = { genre: 'Deep House', platform: 'YouTube', existed: false,
             genres: ['Deep House', 'House'] };
  const form = newGenreForm({ platform: '', onCreated: (res) => created.push(res) });
  const ok = buttons.find((b) => b.textContent === 'OK');
  const picker = made.find((e) => e.tagName === 'SELECT');
  const shut = ok.disabled;
  picker.value = 'YouTube';
  picker.fire('change');
  const open = ok.disabled;
  made.find((e) => e.tagName === 'INPUT').value = 'Deep House';
  await ok.onClick();
  const first = { calls: calls.slice(), created: created.slice(), toast: toasts[0],
                  genres: state.genres };
  answer = { genre: 'House', platform: 'YouTube', existed: true, genres: ['House'] };
  await ok.onClick();
  console.log(JSON.stringify({ shut, open, first, existed: toasts[1],
                               picks: picker.options.map((o) => o.value) }));
})();
"""))

    assert r["shut"] is True
    assert r["open"] is False
    assert r["picks"] == ["Choose Platform", "YouTube", "SoundCloud"]
    assert r["first"]["calls"] == [{"method": "genres.create",
                                    "params": {"name": "Deep House", "platform": "YouTube"}}]
    assert r["first"]["created"][0]["genre"] == "Deep House"
    assert r["first"]["genres"] == ["Deep House", "House"]
    assert "Created the genre folder 'Deep House' under YouTube" in r["first"]["toast"]["text"]
    assert r["existed"] == {"text": "'House' already exists under YouTube.", "isError": False}


def test_a_known_platform_needs_no_picker_and_a_refusal_stays_in_the_form(app_js, tmp_path):
    r = _run_node(tmp_path, "newgenre_known.mjs", _genre_harness(app_js, """
(async () => {
  refuse = "That name isn't usable as a folder.";
  const form = newGenreForm({ platform: 'SoundCloud', onCreated: () => {} });
  const ok = buttons.find((b) => b.textContent === 'OK');
  const picker = made.find((e) => e.tagName === 'SELECT');
  const label = made.find((e) => e.label).label;
  await ok.onClick();
  const err = made.find((e) => e.className === 'cb-merr');
  console.log(JSON.stringify({ picker: !!picker, okOpen: !ok.disabled, label,
                               err: { hidden: err.hidden, text: err.textContent },
                               call: calls[0], toasts }));
})();
"""))

    assert r["picker"] is False
    assert r["label"] == "Enter a genre / category name (under SoundCloud):"
    assert r["call"]["params"]["platform"] == "SoundCloud"
    assert r["err"] == {"hidden": False, "text": "That name isn't usable as a folder."}
    assert r["okOpen"] is True            # re-armed for another try
    assert r["toasts"] == []


def test_remove_needs_a_genre_and_a_platform_then_confirms_inline(app_js, tmp_path):
    """_remove_genre's refusals, then askyesno as an inline panel — a second
    modal cannot open over the dialog."""
    r = _run_node(tmp_path, "genrerow.mjs", _genre_harness(app_js, """
(async () => {
  let platform = '';
  const sel = genreSelect('(none)');
  const wrap = genreRow(sel, () => platform);
  const drop = made.find((e) => e.textContent === '− Remove');
  drop.fire('click');
  const none = toasts.pop();
  sel.value = 'House';
  drop.fire('click');
  const noPlatform = toasts.pop();
  platform = 'YouTube';
  drop.fire('click');
  const panel = wrap.children[wrap.children.length - 1];
  const question = panel.children[0].textContent;
  const beforeYes = calls.length;
  answer = { genre: 'House', platform: 'YouTube', genres: [] };
  await buttons.find((b) => b.textContent === 'Delete').onClick();
  console.log(JSON.stringify({ none, noPlatform, question, beforeYes,
                               call: calls[0], after: sel.value, removed: panel.removed,
                               toast: toasts.pop() }));
})();
"""))

    assert r["none"]["text"] == "Select a genre to remove first."
    assert r["noPlatform"]["text"] == "Paste the channel URL first, so the platform is known."
    assert r["question"] == ("Delete the empty YouTube genre folder 'House'? "
                             "This cannot be undone.")
    assert r["beforeYes"] == 0                      # nothing sent until Delete
    assert r["call"] == {"method": "genres.remove",
                         "params": {"name": "House", "platform": "YouTube"}}
    assert r["after"] == "(none)"
    assert r["removed"] is True
    assert "Removed the empty genre folder 'House'" in r["toast"]["text"]


# ── Folders Cleanup ──────────────────────────────────────────────────────────

def test_folders_cleanup_is_wired_end_to_end(app_js):
    assert "$('#db-wl-cleanup').addEventListener('click', cleanupConfirm)" in app_js
    assert "call('db.cleanup_start', { channel_ids: ids })" in app_js
    assert "cbApi.on('cleanup.review'" in app_js
    assert "cbApi.on('cleanup.channel'" in app_js
    assert "subscribeCleanupEvents();" in app_js
    # The run holds the maintenance slot, so its end arrives as job.finished
    # for 'maintenance' and maintSettle hands it over.
    settle = _slice(app_js, "  function maintSettle(payload) {", "\n  }\n")
    assert "cleanupFinished(payload)" in settle


_REVIEW_HARNESS = """
const made = [], buttons = [];
function mk(tag) {
  const el = { tagName: tag.toUpperCase(), children: [], handlers: {}, style: {},
    className: '', textContent: '', hidden: false, checked: false, dataset: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(n, fn) { this.handlers[n] = fn; },
    fire(n) { return this.handlers[n] ? this.handlers[n]({}) : undefined; } };
  made.push(el);
  return el;
}
const document = { createElement: mk };
function modalButton(label, cls, onClick) {
  const b = mk('button'); b.textContent = label; b.onClick = onClick; buttons.push(b); return b;
}
function modalNote(t) { const p = mk('p'); p.textContent = t; return p; }
const num = (n) => String(n);
%(code)s
const decisions = [];
const flagged = [
  { filename: 'Gone A.mp3', full_path: 'C:/crate/Gone A.mp3', size_bytes: 3 * 1024 * 1024,
    mtime: 1700000000, video_id: 'za', confidence: 'strong', reason: 'In your library, no longer on the channel' },
  { filename: 'Gone B.mp3', full_path: 'C:/crate/Gone B.mp3', size_bytes: 512,
    mtime: 0, video_id: 'zb', confidence: 'strong', reason: 'In your library, no longer on the channel' },
  { filename: 'Maybe.mp3', full_path: 'C:/crate/Maybe.mp3', size_bytes: 2048,
    mtime: 1700000000, video_id: null, confidence: 'weak', reason: 'No record this was ever on the channel' },
];
const panel = cleanupReviewPanel({ name: 'Deep House Daily', index: 0, total: 2, flagged },
                                 (action, paths) => decisions.push({ action, paths }));
const boxes = made.filter((e) => e.tagName === 'INPUT');
const start = boxes.map((b) => b.checked);
const labels = buttons.map((b) => b.textContent);
const rows = made.filter((e) => e.tagName === 'LABEL').map((e) => e.className);
buttons.find((b) => b.textContent === 'Confirm Deletions').onClick();
buttons.find((b) => b.textContent === 'Deselect All').onClick();
const afterNone = panel.selected();
buttons.find((b) => b.textContent === 'Select All').onClick();
const afterAll = panel.selected();
buttons.find((b) => b.textContent === 'Skip Channel').onClick();
buttons.find((b) => b.textContent === 'Cancel Scans').onClick();
const meta = made.filter((e) => e.className === 'cb-cleanup-row__meta').map((e) => e.textContent);
buttons.length = 0;
cleanupReviewPanel({ name: 'Solo', index: 0, total: 1, flagged: flagged.slice(0, 1) }, () => {});
const solo = buttons.map((b) => b.textContent);
console.log(JSON.stringify({ start, labels, rows, decisions, afterNone, afterAll, meta, solo }));
"""


def test_the_review_starts_with_strong_rows_ticked_and_sends_only_ticks(app_js, tmp_path):
    """_CleanupReviewWindow: strong rows checked, weak rows not; Confirm sends
    exactly the ticked paths; Skip only exists in a multi-channel run."""
    r = _run_node(tmp_path, "cleanup_review.mjs", _REVIEW_HARNESS % {
        "code": _slice(app_js, "  function cleanupFmtSize(n)",
                       "  function cleanupShowReview("),
    })

    assert r["start"] == [True, True, False]
    assert r["rows"] == ["cb-cleanup-row", "cb-cleanup-row", "cb-cleanup-row is-weak"]
    assert r["labels"] == ["Select All", "Deselect All", "Confirm Deletions",
                           "Skip Channel", "Cancel Scans"]
    assert r["decisions"] == [
        {"action": "confirm", "paths": ["C:/crate/Gone A.mp3", "C:/crate/Gone B.mp3"]},
        {"action": "skip", "paths": []},
        {"action": "cancel", "paths": []},
    ]
    assert r["afterNone"] == []
    assert r["afterAll"] == ["C:/crate/Gone A.mp3", "C:/crate/Gone B.mp3",
                             "C:/crate/Maybe.mp3"]
    assert r["meta"][0] == "3.0 MB  2023-11-14"
    assert r["meta"][2] == "512 B  "
    assert r["solo"] == ["Select All", "Deselect All", "Confirm Deletions", "Cancel Scan"]

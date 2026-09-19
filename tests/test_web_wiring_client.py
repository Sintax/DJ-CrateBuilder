"""web/app.js: every control the bundle draws is wired to the host, or says
why not — never a stub that looks live.

The static half asserts the stubs are gone for good; the Node half runs the
pieces that replaced them — the How-To button's relabelling, the New Genre
form's OK gating and its call, the Genre row's Remove — against stub state,
sliced out of app.js verbatim like the other client tests.
"""
import json
import os
import re
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
const REMOTE_PARKED_REASON = 'parked';
const dl = { running: false };
const wl = { running: false };
const TOOLTIPS = {};
function tipPlus(ttKey, reason) { return reason; }
function remoteAccessAvailable() { return true; }
function setDisabled(el, disabled, opts) { el.disabled = !!disabled; el.opts = opts || {}; }
function bindTips() {}
function writeBlocked() { return ''; }
const howto = { id: 'cookie-howto', textContent: '',
                setAttribute(k, v) { this.attrs = this.attrs || {}; this.attrs[k] = v; } };
const grid = { querySelector: () => null };
function $(sel) {
  if (sel === '#settings-grid') return grid;
  if (sel === '#cookie-howto') return howto;
  return null;
}
function $$() { return []; }
%(fn)s
applySettingsDependencies();
const on = { text: howto.textContent, off: howto.disabled, tt: howto.opts.ttKey,
             icon: howto.attrs['data-ic'] };
state.settings.cookies_browser = 'Brave';
applySettingsDependencies();
const brave = howto.textContent;
state.settings.cookies_browser = 'Chrome';
applySettingsDependencies();
const chrome = howto.textContent;
state.settings.use_cookies = false;
applySettingsDependencies();
const off = { off: howto.disabled, reason: howto.opts.reason };
console.log(JSON.stringify({ on, brave, chrome, off }));
"""


def _unreadable_browsers(app_js):
    return _slice(app_js, "  const UNREADABLE_BROWSERS = {",
                  "  /* The gate on Use Browser Cookies.")


def test_the_howto_button_names_the_browser_and_greys_with_cookies_off(app_js, tmp_path):
    """The monolith's _update_howto_label and its cookies-toggle greying."""
    r = _run_node(tmp_path, "howto.mjs", _HOWTO_HARNESS % {
        "fn": _unreadable_browsers(app_js)
        + _slice(app_js, "  function applySettingsDependencies()",
                 "  /* One setting, drawn twice"),
    })

    assert r["on"] == {"text": "How-To: Setting Up a Dedicated Firefox Profile",
                       "off": False, "tt": "settings.firefox_profile_howto",
                       "icon": "book"}
    assert r["brave"] == "How-To: Setting Up a Dedicated Brave Profile"
    assert r["chrome"] == "How-To: Using Chrome Cookies via a Cookie File"
    assert r["off"] == {"off": True, "reason": "Turn on Use Browser Cookies first."}


# ── the per-track policy is greyed while a download or scan runs ─────────────
# The host refuses these writes (DOWNLOAD_LOCKED_SETTINGS) for a batch or a
# Watch List job alike; the grid says so before the click, the way the
# Downloads screen's Skip row already does.

_RUN_LOCK_HARNESS = """
const state = { settings: { use_cookies: false, cookies_browser: 'Firefox',
                            cookie_method: 'Browser Profile', sleep_enabled: true,
                            sleep_mode: 'Manual', limit_enabled: true,
                            geo_bypass: false, rotate_ua: true,
                            cover_art_enabled: true, cover_art_mode: 'Off',
                            skip_existing: true, limit_minutes: 8 },
                host: { transport: 'local' } };
const REMOTE_SETTING_KEYS = [];
const REMOTE_PARKED_REASON = 'parked';
const dl = { running: false };
const wl = { running: %(wl)s };
const TOOLTIPS = { 'settings.geo_bypass': 'geo tip' };
function tipPlus(ttKey, reason) {
  return (ttKey && TOOLTIPS[ttKey] ? TOOLTIPS[ttKey] + '\\n\\n' : '') + reason;
}
function remoteAccessAvailable() { return true; }
function setDisabled(el, disabled, opts) { el.disabled = !!disabled; el.opts = opts || {}; }
function bindTips() {}
function writeBlocked() { return ''; }
const els = {};
const grid = { querySelector: (sel) => {
  const key = sel.slice('[data-key="'.length, -2);
  if (!els[key]) els[key] = { key, dataset: { origTt: key === 'geo_bypass' ? 'settings.geo_bypass' : '' } };
  return els[key];
} };
function $(sel) { return sel === '#settings-grid' ? grid : null; }
function $$() { return []; }
%(fn)s
applySettingsDependencies();
const out = {};
Object.keys(els).forEach((k) => { out[k] = { off: els[k].disabled, reason: els[k].opts.reason || '' }; });
console.log(JSON.stringify(out));
"""


def _run_lock(app_js, tmp_path, running):
    return _run_node(tmp_path, "runlock.mjs", _RUN_LOCK_HARNESS % {
        "wl": "true" if running else "false",
        "fn": _unreadable_browsers(app_js)
        + _slice(app_js, "  const RUN_LOCKED_SETTINGS = [",
                 "  /* One setting, drawn twice"),
    })


def test_the_policy_keys_grey_while_a_watch_list_job_runs(app_js, tmp_path):
    r = _run_lock(app_js, tmp_path, running=True)
    for key in ("cover_art_enabled", "cover_art_mode", "limit_minutes",
                "limit_minutes__minus", "limit_minutes__plus", "limit_enabled",
                "geo_bypass", "rotate_ua", "sleep_enabled", "use_cookies",
                "skip_existing", "skip_mode", "bitrate_quality",
                "bitrate_auto_upgrade", "no_conversion", "base_dir"):
        assert r[key]["off"] is True, key
        assert "frozen until it finishes" in r[key]["reason"], key
    # The registry tooltip still travels with the reason.
    assert r["geo_bypass"]["reason"].startswith("geo tip\n\n")
    # The throttle's tuning stays live mid-run: Manual mode here, so the
    # bounds are enabled and the preset is greyed for the mode, not the run.
    assert r["sleep_mode"]["off"] is False
    assert r["sleep_min"]["off"] is False
    assert r["sleep_max"]["off"] is False
    assert "frozen" not in r["sleep_preset"]["reason"]


def test_the_policy_keys_are_live_when_nothing_runs(app_js, tmp_path):
    """The grid is rebuilt enabled on every refresh, so a key no dependency
    pass names must simply go untouched here — never greyed."""
    r = _run_lock(app_js, tmp_path, running=False)
    for key in ("cover_art_enabled", "cover_art_mode", "geo_bypass", "rotate_ua",
                "sleep_enabled", "use_cookies", "limit_minutes"):
        assert r.get(key, {"off": False})["off"] is False, key


def test_the_run_lock_list_matches_the_hosts(app_js):
    """One list, two copies: the grid greys exactly what the host refuses."""
    import re
    from cratebuilder import service as cb_service
    body = _slice(app_js, "  const RUN_LOCKED_SETTINGS = [", "];")
    assert set(re.findall(r"'([a-z_]+)'", body)) == \
        set(cb_service.DOWNLOAD_LOCKED_SETTINGS)


# ── turning Browser Cookies on is gated ──────────────────────────────────────

def test_the_cookies_box_hands_its_tick_to_the_gate(app_js):
    """Ticking Use Browser Cookies must not save straight away: the box goes
    back to off and the gate dialog decides. Unticking stays a plain save,
    and every other checkbox is untouched."""
    body = _slice(app_js, "    if (entry.type === 'bool') {",
                  "    if (entry.key === 'limit_minutes') {")
    assert "if (entry.key === 'use_cookies' && box.checked) {" in body
    assert "box.checked = false;\n          openCookieGate(box);\n          return;" in body
    assert "save(entry.key, box.checked, box);" in body


def test_chrome_is_greyed_in_the_browser_list_with_its_reason(app_js):
    body = _slice(app_js, "    if (entry.type === 'enum') {",
                  "    } else if (entry.type === 'int') {")
    assert "if (entry.key === 'cookies_browser' && UNREADABLE_BROWSERS[o]) {" in body
    assert "opt.disabled = true;" in body
    assert "opt.title = UNREADABLE_BROWSERS[o].reason;" in body
    assert "Chrome: {" in _unreadable_browsers(app_js)


_GATE_HARNESS = """
const state = { settings: { cookies_browser: %(browser)r } };
const saves = [], howto = [];
let opened = null, closes = 0;
function mkEl() {
  const e = { children: [], style: {}, className: '', textContent: '' };
  e.appendChild = (c) => { e.children.push(c); return c; };
  e.append = (...c) => { e.children.push(...c); };
  return e;
}
async function save(key, value, el) { saves.push([key, value, el.checked]); }
function modalNote(text) { const p = mkEl(); p.textContent = text; return p; }
function modalButton(label, cls, onClick) {
  return { label, cls, onClick, style: {} };
}
function closeModal() { closes += 1; if (opened && opened.onClose) opened.onClose(); }
function openCookieHowto(browser) { howto.push(browser); }
function openModal(opts) {
  opened = opts;
  opened.bodyEl = mkEl(); opened.footEl = mkEl();
  opts.body(opened.bodyEl, {}); opts.foot(opened.footEl, {});
  return {};
}
%(fn)s
const box = { checked: false };
openCookieGate(box);
const texts = opened.bodyEl.children.map((c) => c.textContent);
const buttons = opened.footEl.children.map((b) => b.label);
const press = (label) => opened.footEl.children.find((b) => b.label === label).onClick();
const out = { texts, buttons, checkedOnOpen: box.checked,
              warned: texts.some((t) => t.includes('currently set to')),
              guideLeft: opened.footEl.children[1].style.marginLeft };
press(%(press)r);
out.checked = box.checked; out.saves = saves; out.howto = howto; out.closes = closes;
console.log(JSON.stringify(out));
"""


def _gate(app_js, tmp_path, browser, press):
    fn = _unreadable_browsers(app_js) + _slice(
        app_js, "  function openCookieGate(box) {", "  /* Section-level help")
    return _run_node(tmp_path, "gate.mjs",
                     _GATE_HARNESS % {"browser": browser, "press": press, "fn": fn})


def test_the_gate_opens_with_the_box_off_and_three_choices(app_js, tmp_path):
    r = _gate(app_js, tmp_path, "Firefox", "Keep cookies off")
    assert r["checkedOnOpen"] is False
    assert r["buttons"] == ["Keep cookies off", "Open the setup guide",
                            "Got it, turn it on"]
    assert r["guideLeft"] == "auto"
    assert r["texts"][0].startswith("This is not a quick fix.")
    assert any("setup guide" in t for t in r["texts"])
    assert any("Chrome cannot be read directly" in t for t in r["texts"])
    assert r["warned"] is False


def test_keeping_cookies_off_saves_nothing(app_js, tmp_path):
    r = _gate(app_js, tmp_path, "Firefox", "Keep cookies off")
    assert r["checked"] is False
    assert r["saves"] == []
    assert r["closes"] == 1


def test_got_it_turns_cookies_on_through_the_ordinary_save(app_js, tmp_path):
    r = _gate(app_js, tmp_path, "Firefox", "Got it, turn it on")
    assert r["checked"] is True
    assert r["saves"] == [["use_cookies", True, True]]
    assert r["howto"] == []


def test_the_guide_button_turns_cookies_on_and_opens_the_browsers_guide(app_js, tmp_path):
    """Asking for the guide is committing: cookies go on, the gate closes, and
    the walkthrough for the selected browser opens in its own window. Closing
    the gate that way must not undo the tick."""
    r = _gate(app_js, tmp_path, "Brave", "Open the setup guide")
    assert r["checked"] is True
    assert r["saves"] == [["use_cookies", True, True]]
    assert r["closes"] == 1
    assert r["howto"] == ["Brave"]


def test_a_chrome_selection_is_called_out_in_red(app_js, tmp_path):
    r = _gate(app_js, tmp_path, "Chrome", "Keep cookies off")
    assert r["warned"] is True
    assert any("currently set to Chrome" in t for t in r["texts"])


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


# ── Remote Access parked while it is worked out ─────────────────────────────

def test_a_red_rule_separates_remote_access_from_the_settings_above(app_js):
    """The Remote Access card is set apart from the everyday settings by a
    full-width red rule, placed in the grid just ahead of it."""
    settings = _slice(app_js, "  function renderSettings()", "  /* ── About (3n)")
    assert "if (sec.name === 'Remote Access') {" in settings
    assert "sep.className = 'cb-set-sep cb-span-2';" in settings
    assert settings.index("grid.appendChild(sep);") < settings.index(
        "grid.appendChild(card);", settings.index("grid.appendChild(sep);"))
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert ".cb-set-sep { border-top: 2px solid var(--cb-accent);" in css


def test_the_remote_access_card_is_greyed_with_a_red_notice_while_parked(app_js):
    """host.remote_available false (remoteauth's kill switch) greys the three
    remote toggles, pairing and revoke on both mounts and says why in red;
    the notify toggles stay live because they also gate the local bell."""
    assert "state.host.remote_available === false" in app_js
    assert "set(key, remoteMount || remoteParked," in app_js
    card = _slice(app_js, "    'Remote Access': (card) => {", "    'Browser Cookies': (card) => {")
    assert "const parked = !remoteAccessAvailable();" in card
    assert "notice.className = 'cb-remote-parked';" in card
    assert "still in development" in card
    assert "setDisabled(pairBtn, parked || !local," in card
    assert "setDisabled(revoke, parked || !local || !count," in card
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert ".cb-remote-parked {" in css and "color: var(--cb-err); font-weight: 700" in css



def test_the_sidebar_names_the_app_in_full_with_the_mount_tag_beneath(index_html):
    """The brand reads "DJ-CrateBuilder"; the Local / Remote tag sits on its
    own line under it rather than beside the name."""
    assert '<span class="cb-brand">DJ-CrateBuilder</span>' in index_html
    row = index_html[index_html.index('<div class="cb-brandrow">'):
                     index_html.index('<nav ')]
    assert row.index('cb-brand">') < row.index('id="mount-tag"')
    assert row.count('</div>') == 2          # the name's row closes before the tag
    with open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        assert ".cb-brandrow { display: flex; flex-direction: column;" in fh.read()


# ── buttons and headings draw Core Line glyphs, not emoji ────────────────────

# Every emoji the swap retired from a button, heading, dialog title or menu
# label. The queue-log marks (✓ ⊘ ✗ ○ ⬇ in DL_MARK / WL_QROW_MARK), the ⚠ in
# prose, and the → on links are deliberately not here.
SWAPPED_EMOJI = "⏸⬇⏭⚡🔍🛠🧹🔄🏷📂🗂🖼👁⚙✏📋📤📥🐞↗🌐📖❔⏳🔐✕"


def test_index_links_the_icon_sheet_and_no_button_starts_with_an_emoji(index_html):
    """The glyph is a `data-ic` attribute painted by icons.css, so the label
    stays a plain text node; an emoji left in the text would draw twice."""
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)">', index_html)
    assert links.index("icons.css") == links.index("app.css") + 1
    assert links.index("icons.css") < links.index("theme-dark.css")

    offenders = []
    for m in re.finditer(r"<(button|h4)[^>]*>([^<]*)", index_html):
        text = m.group(2).strip()
        if text and text[0] in SWAPPED_EMOJI:
            offenders.append(m.group(0))
    assert offenders == []


# ── the close confirmation, drawn in-page ────────────────────────────────────
# The desktop window cancels the OS close and raises app.close_requested; the
# page draws the monolith's question in its own theme — as its own layer, not
# an openModal dialog, so whatever is open underneath (an update's locked
# progress dialog, an Edit with typed text) is left exactly as it was. The
# real showCloseConfirm runs here against a stub DOM.

_CLOSE_HARNESS = """
const calls = [];
async function call(method) { calls.push(method); return {}; }
function mkEl(tag) {
  const e = { tag, children: [], style: {}, attrs: {}, listeners: {}, parent: null,
              className: '', textContent: '', disabled: false, focused: 0 };
  e.appendChild = (c) => { e.children.push(c); c.parent = e; return c; };
  e.append = (...cs) => { cs.forEach((c) => e.appendChild(c)); };
  e.setAttribute = (k, v) => { e.attrs[k] = v; };
  e.addEventListener = (name, fn) => { e.listeners[name] = fn; };
  e.remove = () => { if (e.parent) e.parent.children = e.parent.children.filter((c) => c !== e); };
  e.focus = () => { e.focused += 1; document.activeElement = e; };
  return e;
}
const winListeners = [];
const window = {
  addEventListener(name, fn, capture) { winListeners.push({ name, fn, capture }); },
  removeEventListener(name, fn) {
    const i = winListeners.findIndex((l) => l.fn === fn);
    if (i !== -1) winListeners.splice(i, 1);
  },
};
const document = { body: mkEl('body'), activeElement: null, createElement: mkEl };
/* What openModal would have left open underneath: its own capture-phase
   document listener, which must never hear the overlay's Escape. */
let underneathHeard = [];
const openModalDialog = { open: true };
function modalNote(text) { const p = mkEl('p'); p.className = 'cb-mnote'; p.textContent = text; return p; }
function modalButton(label, cls, onClick) {
  const b = mkEl('button'); b.className = 'cb-btn cb-btn--sm ' + (cls || '');
  b.textContent = label; b.listeners.click = onClick; return b;
}
function key(k, extra) {
  const e = Object.assign({ key: k, stopped: 0, prevented: 0,
    stopPropagation() { this.stopped += 1; }, preventDefault() { this.prevented += 1; } }, extra || {});
  for (const l of winListeners.slice()) if (l.name === 'keydown') l.fn(e);
  if (!e.stopped) underneathHeard.push(k);
  return e;
}
function overlay() { return document.body.children.find((c) => c.className.indexOf('cb-dim--close') !== -1) || null; }
function btn(el, text) {
  if (el.tag === 'button' && el.textContent === text) return el;
  for (const c of el.children) { const h = btn(c, text); if (h) return h; }
  return null;
}
function texts(el) {
  let out = el.textContent ? [el.textContent] : [];
  for (const c of el.children) out = out.concat(texts(c));
  return out;
}
%(fn)s
async function main() {
%(script)s
}
main();
"""


def _close_confirm(app_js, tmp_path, name, script):
    fn = _slice(app_js, "  let closeAsk = null;", "  /* A host that is down")
    return _run_node(tmp_path, name, _CLOSE_HARNESS % {"fn": fn, "script": script})


def test_the_close_dialog_is_subscribed_and_acknowledged(app_js):
    fn = _slice(app_js, "  function subscribeSessionEvents()",
                "  let closeAsk = null;")
    handler = _slice(fn, "cbApi.on('app.close_requested', () => {", "    });")
    assert "showCloseConfirm();" in handler
    assert "call('app.close_seen').catch(() => {});" in handler
    # The bus reaches every paired browser too; without this a phone would
    # draw the host's close question and toast a LOCAL_ONLY refusal.
    assert handler.index("if (cbApi.transport !== 'local') return;") \
        < handler.index("showCloseConfirm();")


def test_the_close_question_is_its_own_layer_over_whatever_is_open(
        app_js, tmp_path):
    r = _close_confirm(app_js, tmp_path, "close_layer.mjs", """
  const typing = mkEl('input'); typing.focus();          // an Edit underneath
  document.body.appendChild(mkEl('div')).className = 'cb-dim';   // ... in a dialog
  showCloseConfirm();
  const dim = overlay();
  const card = dim.children[0];
  const foot = card.children[2];
  const buttons = foot.children;
  const out = {
    layers: document.body.children.map((c) => c.className),
    dimClass: dim.className, cardClass: card.className,
    role: card.attrs.role, label: card.attrs['aria-label'], width: card.style.maxWidth,
    head: card.children[0].className, title: card.children[0].children[0].textContent,
    bodyClass: card.children[1].className, texts: texts(card.children[1]),
    footClass: foot.className,
    labels: buttons.map((b) => b.textContent), classes: buttons.map((b) => b.className),
    focused: document.activeElement.textContent,
    winKeydowns: winListeners.filter((l) => l.name === 'keydown' && l.capture).length,
  };
  console.log(JSON.stringify(out));
""")
    assert r["layers"] == ["cb-dim", "cb-dim cb-dim--close"]   # over, not instead
    assert r["cardClass"] == "cb-modal" and r["width"] == "420px"
    assert r["role"] == "dialog" and r["label"] == "Close DJ-CrateBuilder"
    assert r["head"] == "cb-mhead" and r["title"] == "Close DJ-CrateBuilder"
    assert r["bodyClass"] == "cb-mbody"
    assert r["texts"] == ["Are you sure you want to close DJ-CrateBuilder?",
                          "Auto-downloads won't run while it's closed."]
    assert r["footClass"] == "cb-mfoot"
    assert r["labels"] == ["Stay open", "Close DJ-CrateBuilder"]
    assert "cb-btn--quiet" in r["classes"][0]
    assert "cb-btn--warn" in r["classes"][1]
    assert r["focused"] == "Stay open"        # a stray Enter keeps it running
    assert r["winKeydowns"] == 1              # window, capture: ahead of openModal's


def test_every_casual_exit_stays_open_and_gives_focus_back(app_js, tmp_path):
    r = _close_confirm(app_js, tmp_path, "close_stay.mjs", """
  const typing = mkEl('input'); typing.focus();
  const out = {};
  showCloseConfirm();
  btn(overlay(), 'Stay open').listeners.click();
  out.afterStay = { open: !!overlay(), focus: document.activeElement === typing,
                    listeners: winListeners.length };
  showCloseConfirm();
  const esc = key('Escape');
  out.afterEscape = { open: !!overlay(), stopped: esc.stopped,
                      underneath: underneathHeard.slice(), focus: document.activeElement === typing };
  showCloseConfirm();
  const dim = overlay();
  dim.listeners.mousedown({ target: dim.children[0] });     // on the card: nothing
  out.cardClick = !!overlay();
  dim.listeners.mousedown({ target: dim });                  // on the dim: stay
  out.afterDim = { open: !!overlay(), focus: document.activeElement === typing };
  out.calls = calls;
  console.log(JSON.stringify(out));
""")
    assert r["afterStay"] == {"open": False, "focus": True, "listeners": 0}
    assert r["afterEscape"] == {"open": False, "stopped": 1, "underneath": [],
                                "focus": True}
    assert r["cardClick"] is True
    assert r["afterDim"] == {"open": False, "focus": True}
    assert r["calls"] == []                   # the host is never asked to stay


def test_tab_stays_inside_the_question_and_the_dialog_underneath_never_hears_it(
        app_js, tmp_path):
    r = _close_confirm(app_js, tmp_path, "close_tab.mjs", """
  showCloseConfirm();
  const stay = btn(overlay(), 'Stay open'), quit = btn(overlay(), 'Close DJ-CrateBuilder');
  const t1 = key('Tab'); const a1 = document.activeElement === quit;
  const t2 = key('Tab', { shiftKey: true }); const a2 = document.activeElement === stay;
  const other = key('a');
  console.log(JSON.stringify({ a1, a2, prevented: t1.prevented + t2.prevented,
    stopped: t1.stopped + t2.stopped, otherStopped: other.stopped,
    underneath: underneathHeard }));
""")
    assert r["a1"] is True and r["a2"] is True
    assert r["prevented"] == 2 and r["stopped"] == 2
    assert r["otherStopped"] == 0             # ordinary keys pass through
    assert r["underneath"] == ["a"]


def test_the_warn_button_quits_through_the_host(app_js, tmp_path):
    r = _close_confirm(app_js, tmp_path, "close_quit.mjs", """
  showCloseConfirm();
  const quit = btn(overlay(), 'Close DJ-CrateBuilder');
  quit.listeners.click();
  await Promise.resolve();
  console.log(JSON.stringify({ calls, disabled: quit.disabled, open: !!overlay() }));
""")
    assert r["calls"] == ["app.quit"]
    assert r["disabled"] is True
    assert r["open"] is True                  # the window goes; nothing to tidy


def test_a_second_request_refocuses_the_open_question(app_js, tmp_path):
    r = _close_confirm(app_js, tmp_path, "close_twice.mjs", """
  showCloseConfirm();
  const stay = btn(overlay(), 'Stay open');
  const quit = btn(overlay(), 'Close DJ-CrateBuilder');
  quit.focus();
  showCloseConfirm();
  console.log(JSON.stringify({
    overlays: document.body.children.filter((c) => c.className.indexOf('cb-dim--close') !== -1).length,
    listeners: winListeners.length, stayFocused: stay.focused,
    active: document.activeElement === stay }));
""")
    assert r["overlays"] == 1
    assert r["listeners"] == 1
    assert r["stayFocused"] == 2 and r["active"] is True

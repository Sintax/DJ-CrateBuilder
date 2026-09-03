"""web/app.js, index.html and theme-dark.css: the dark theme, client-side.

Same method as tests/test_web_about_client.py — storedTheme, applyTheme and
appearanceCard are sliced out of app.js verbatim and run in Node against a
stub document and localStorage. The rest is structural: the stylesheet order
index.html declares, the pre-paint script agreeing with app.js on the storage
key, theme.css still being the design's untouched drop-in, every token
theme.css and app.css declare having a dark value, and the dark sheet never
reaching a page that has not asked for it.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DESIGN = os.path.join(ROOT, "UI-design")


def _read(name, folder=WEB):
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        return fh.read()


def _slice(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


_HARNESS = """
'use strict';
function makeStore() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
  };
}
const refusing = {
  getItem() { throw new Error('refused'); },
  setItem() { throw new Error('refused'); },
  removeItem() { throw new Error('refused'); },
};
let localStorage = makeStore();
function makeEl(tag) {
  const cls = new Set();
  const el = {
    tag, children: [], listeners: {}, attrs: {}, style: {}, dataset: {},
    className: '', textContent: '', tabIndex: -1, id: '',
    classList: {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c),
      toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); },
      contains: (c) => cls.has(c),
    },
    appendChild(c) { el.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => el.children.push(c)); },
    addEventListener(name, fn) { el.listeners[name] = fn; },
    setAttribute(k, v) { el.attrs[k] = String(v); },
    removeAttribute(k) { delete el.attrs[k]; },
    get firstChild() { return el.children[0]; },
    set innerHTML(_) { el.children = [makeEl('span')]; },
  };
  return el;
}
const document = { createElement: makeEl, documentElement: makeEl('html') };
let segs = [];
const $$ = () => segs;
function readOnlyOk(el) { el.dataset.readOk = '1'; return el; }
%(slices)s
function snapshot() {
  let stored;
  try { stored = localStorage.getItem('cb_theme'); } catch (_) { stored = 'refused'; }
  return {
    attr: document.documentElement.attrs['data-theme'] || null,
    stored,
    on: segs.filter((s) => s.classList.contains('is-on')).map((s) => s.dataset.theme),
    checked: segs.map((s) => s.attrs['aria-checked']),
  };
}
const out = {};
out.initial = storedTheme();
applyTheme('dark');
out.dark = snapshot();
applyTheme('bogus');
out.bogus = snapshot();
applyTheme('dark');
const card = appearanceCard();
const seg = card.children[1].children[1];
segs = seg.children;
out.card = {
  title: card.children[0].firstChild.textContent,
  role: seg.attrs.role, id: seg.id,
  options: segs.map((s) => [s.dataset.theme, s.textContent, s.attrs.role,
                            s.attrs['aria-checked'], s.tabIndex,
                            s.dataset.readOk, s.classList.contains('is-on')]),
  hint: card.children[2].textContent,
};
segs[0].listeners.click();
out.clickedLight = snapshot();
let prevented = false;
segs[1].listeners.keydown({ key: ' ', preventDefault: () => { prevented = true; } });
out.spaceDark = Object.assign(snapshot(), { prevented });
segs[0].listeners.keydown({ key: 'x', preventDefault: () => {} });
out.otherKey = snapshot();
localStorage = refusing;
out.refusedStored = storedTheme();
applyTheme('dark');
out.refusedApply = snapshot();
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app_js = _read("app.js")
    slices = (_slice(app_js, "  const THEME_KEY = ",
                     "  /* ── notifications (3n)")
              + _slice(app_js, "  function appearanceCard() {",
                       "  function renderSettings() {"))
    script = tmp_path_factory.mktemp("theme") / "theme_harness.cjs"
    script.write_text(_HARNESS % {"slices": slices}, encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


# ── the behaviour, in Node ───────────────────────────────────────────────────

def test_nothing_stored_means_light(result):
    assert result["initial"] == "light"


def test_dark_marks_the_page_and_remembers_it(result):
    assert result["dark"] == {"attr": "dark", "stored": "dark",
                              "on": [], "checked": []}


def test_an_unknown_value_falls_back_to_light(result):
    assert result["bogus"] == {"attr": None, "stored": "light",
                               "on": [], "checked": []}


def test_the_card_draws_two_radio_options_reading_the_stored_theme(result):
    card = result["card"]
    assert (card["title"], card["role"], card["id"]) == (
        "Appearance", "radiogroup", "settings-theme")
    assert card["options"] == [
        ["light", "Light", "radio", "false", 0, "1", False],
        ["dark", "Dark", "radio", "true", 0, "1", True],
    ]
    assert "this device" in card["hint"]


def test_a_click_or_a_key_switches_the_theme_and_repaints_the_control(result):
    assert result["clickedLight"] == {"attr": None, "stored": "light",
                                      "on": ["light"],
                                      "checked": ["true", "false"]}
    assert result["spaceDark"] == {"attr": "dark", "stored": "dark",
                                   "on": ["dark"], "checked": ["false", "true"],
                                   "prevented": True}
    # A key that is not Enter or Space changes nothing.
    assert result["otherKey"] == {k: v for k, v in result["spaceDark"].items()
                                  if k != "prevented"}


def test_a_store_that_refuses_still_paints_the_page(result):
    assert result["refusedStored"] == "light"
    assert result["refusedApply"]["attr"] == "dark"
    assert result["refusedApply"]["stored"] == "refused"


# ── the page and the sheets ──────────────────────────────────────────────────

def test_index_applies_the_stored_theme_before_any_stylesheet_loads():
    html = _read("index.html")
    assert html.index("localStorage.getItem('cb_theme')") < html.index(
        'href="theme.css"')
    assert "setAttribute('data-theme', 'dark')" in html
    assert (html.index('href="theme.css"') < html.index('href="app.css"')
            < html.index('href="theme-dark.css"'))


def test_the_page_and_the_script_agree_on_the_storage_key():
    key = re.search(r"const THEME_KEY = '([^']+)'", _read("app.js")).group(1)
    assert f"localStorage.getItem('{key}')" in _read("index.html")


def test_theme_css_is_still_the_designs_drop_in():
    """The dark theme is an overlay precisely so this stays true."""
    assert _read("theme.css") == _read("theme.css", DESIGN)


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _tokens(css, selector):
    css = _strip_comments(css)
    block = css[css.index(selector):]
    block = block[:block.index("}")]
    return set(re.findall(r"(--cb-[\w-]+)\s*:", block))


# Tokens that are not colours, and the one colour that is deliberately shared:
# the Fix Link label is near-black on orange whichever ground the page has.
_SAME_IN_BOTH = {"--cb-radius", "--cb-radius-sm", "--cb-font", "--cb-mono",
                 "--cb-panel-w", "--cb-touch", "--cb-fix-ink"}


def test_every_colour_token_the_light_sheets_declare_has_a_dark_value():
    light = (_tokens(_read("theme.css"), ":root {")
             | _tokens(_read("app.css"), ":root {")) - _SAME_IN_BOTH
    dark = _tokens(_read("theme-dark.css"), ':root[data-theme="dark"] {')
    assert light <= dark, sorted(light - dark)
    assert "color-scheme" in _read("theme-dark.css")


def test_the_dark_sheet_never_reaches_a_page_that_did_not_ask():
    css = _strip_comments(_read("theme-dark.css"))
    for group in re.findall(r"(?:^|\})\s*([^{}]+)\{", css):
        for selector in group.split(","):
            assert selector.strip().startswith(':root[data-theme="dark"]'), \
                selector.strip()

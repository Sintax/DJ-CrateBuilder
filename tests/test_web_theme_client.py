"""web/app.js, index.html and theme-dark.css: the dark theme, client-side.

Same method as tests/test_web_about_client.py — storedTheme, applyTheme and
appearanceRows are sliced out of app.js verbatim and run in Node against a
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
  };
  return el;
}
const document = { createElement: makeEl, documentElement: makeEl('html') };
let segs = [];
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
const card = makeEl('div');
appearanceRows(card);
const seg = card.children[0].children[1];
segs = seg.children;
out.card = {
  role: seg.attrs.role, id: seg.id,
  options: segs.map((s) => [s.dataset.theme, s.textContent, s.attrs.role,
                            s.attrs['aria-checked'], s.tabIndex,
                            s.dataset.readOk, s.classList.contains('is-on')]),
  rows: card.children.length,
  labels: card.children.map((row) => row.children[0].textContent),
};
const sizeSel = card.children[1].children[1];
function sizeSnapshot() {
  let stored;
  try { stored = localStorage.getItem('cb_text_size'); } catch (_) { stored = 'refused'; }
  return { attr: document.documentElement.attrs['data-text-size'] || null, stored };
}
out.size = {
  tag: sizeSel.tag, id: sizeSel.id, readOk: sizeSel.dataset.readOk,
  options: sizeSel.children.map((o) => [o.value, o.textContent]),
  value: sizeSel.value, initial: storedTextSize(),
};
sizeSel.value = 'large';
sizeSel.listeners.change();
out.sizeLarge = sizeSnapshot();
sizeSel.value = 'xl';
sizeSel.listeners.change();
out.sizeXl = sizeSnapshot();
applyTextSize('bogus');
out.sizeBogus = sizeSnapshot();
sizeSel.value = 'normal';
sizeSel.listeners.change();
out.sizeNormal = sizeSnapshot();
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
out.refusedSize = storedTextSize();
applyTextSize('large');
out.refusedSizeApply = sizeSnapshot();
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
              + _slice(app_js, "  function appearanceRows(card) {",
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
    assert (card["role"], card["id"]) == ("radiogroup", "settings-theme")
    assert card["options"] == [
        ["light", "Light", "radio", "false", 0, "1", False],
        ["dark", "Dark", "radio", "true", 0, "1", True],
    ]
    # Two labelled rows and nothing else: the "kept on this device" hint
    # that used to follow the theme switch is gone.
    assert card["rows"] == 2
    assert card["labels"] == ["Theme", "Text size"]


# ── text size ────────────────────────────────────────────────────────────────

def test_the_text_size_control_is_a_select_of_three_sizes_reading_the_store(result):
    """A dropdown, not a segmented switch: three options, the stored size
    selected, live in a read-only remote session like the theme switch."""
    size = result["size"]
    assert (size["tag"], size["id"], size["readOk"]) == ("select", "settings-text-size", "1")
    assert size["options"] == [["normal", "Normal"], ["large", "Large"],
                               ["xl", "Extra-Large"]]
    assert size["initial"] == "normal"
    assert size["value"] == "normal"


def test_choosing_a_size_marks_the_page_and_remembers_it(result):
    """Normal is today's size, so it clears the mark rather than setting one;
    the other two set it for app.css to answer. An unknown value falls
    back to normal."""
    assert result["sizeLarge"] == {"attr": "large", "stored": "large"}
    assert result["sizeXl"] == {"attr": "xl", "stored": "xl"}
    assert result["sizeBogus"] == {"attr": None, "stored": "normal"}
    assert result["sizeNormal"] == {"attr": None, "stored": "normal"}


def test_a_store_that_refuses_still_sizes_the_page(result):
    assert result["refusedSize"] == "normal"
    assert result["refusedSizeApply"] == {"attr": "large", "stored": "refused"}


def test_index_applies_the_stored_text_size_before_any_stylesheet_loads():
    html = _read("index.html")
    key = re.search(r"const TEXT_SIZE_KEY = '([^']+)'", _read("app.js")).group(1)
    assert key == "cb_text_size"
    assert html.index(f"localStorage.getItem('{key}')") < html.index('href="theme.css"')
    assert "setAttribute('data-text-size'" in html


def test_app_css_scales_the_whole_page_for_each_larger_size():
    """The sizes are one zoom rule each on the root, so text, controls and
    spacing grow together and no screen has to be re-laid-out by hand.
    theme.css stays the design's drop-in, so the rules live in app.css."""
    css = _read("app.css")
    assert re.search(r'html\[data-text-size="large"\]\s*\{\s*zoom:\s*1\.08;', css)
    assert re.search(r'html\[data-text-size="xl"\]\s*\{\s*zoom:\s*1\.15;', css)
    assert "zoom" not in _read("theme.css")


def test_nothing_is_sized_from_the_viewport_units_zoom_leaves_alone():
    """vh and vw are not shrunk by zoom: a 100vh shell at 115% is a sixth
    taller than the window and every screen's bottom is cut off. The two
    variables divide the zoom back out, so each size declares them and no
    rule reaches for the raw unit."""
    css = _strip_comments(_read("app.css"))
    assert re.search(r':root\s*\{\s*--cb-vh:\s*1vh;\s*--cb-vw:\s*1vw;\s*\}', css)
    for size, zoom in (("large", "1.08"), ("xl", "1.15")):
        rule = re.search(r'html\[data-text-size="%s"\]\s*\{([^}]*)\}' % size, css).group(1)
        assert f"--cb-vh: calc(1vh / {zoom})" in rule
        assert f"--cb-vw: calc(1vw / {zoom})" in rule
    bare = [m.group(0) for m in re.finditer(r"[^\n]*\b\d+v[hw]\b[^\n]*", css)
            if "--cb-v" not in m.group(0)]
    assert bare == [], bare
    assert ".cb-shell { display: flex; height: 100%; }" in css


def test_everything_placed_from_a_measurement_divides_by_the_page_zoom():
    """Under CSS zoom a rectangle, a pointer position and innerWidth answer
    in viewport pixels while style.left and scrollTop are written in the
    page's own, larger pixels. Every site that mixes the two goes through
    pageZoom(), or a tooltip lands 15–30% away from its control."""
    app_js = _read("app.js")
    assert "function pageZoom()" in app_js
    assert "currentCSSZoom" in app_js
    for start, end in [
        ("  function showTip(host, text) {", "  function hideTip()"),
        ("  function toggleNotifications() {", "  function renderNotifications()"),
        ("  function scrollBoxToActive(", "  function renderQueueLog()"),
        ("      resize.addEventListener('mousedown'", "        function onUp()"),
        ("  function dbShowMenu(x, y, items) {", "  async function dbCopyText("),
    ]:
        assert "pageZoom()" in _slice(app_js, start, end), start


def test_the_section_is_seeded_first_and_filled_like_the_other_extras():
    app_js = _read("app.js")
    assert "const sections = [{ name: 'Appearance', items: [] }];" in app_js
    assert "'Appearance': appearanceRows," in app_js


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
                 "--cb-panel-w", "--cb-touch", "--cb-fix-ink", "--cb-vh", "--cb-vw"}


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


def _z_index(css, selector):
    css = _strip_comments(css)
    block = css[css.index(selector + " {"):]
    block = block[:block.index("}")]
    return int(re.search(r"z-index:\s*(\d+)", block).group(1))


def test_the_tooltip_bubble_paints_above_every_layer_that_hosts_one():
    """showTip appends the bubble to <body>, so it shares a stacking context
    with every other fixed layer there. A dialog's scrim and the notification
    panel both carry tooltip-bearing controls; the bubble has to sit above
    them or it is drawn underneath the very window it was hovered in."""
    css = _read("app.css")
    tip = _z_index(css, ".cb-tip")
    for host in (".cb-dim", ".cb-notif"):
        assert tip > _z_index(css, host), host

"""web/howto.html and the Browser Cookies guide, client-side.

The cookie setup guide opens in a window of its own — web/howto.html in a
second desktop window (or a browser popup) — so the steps stay readable
while the user works through them in Settings. A modal closed on the first
click back into the app, which is exactly when the guide is needed.

Same method as the other tests/test_web_*_client.py files: the static half
reads the bundle as text, the Node half slices openCookieHowto out of app.js
verbatim and runs it against a stub host.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")
HOWTO_HTML = os.path.join(ROOT, "web", "howto.html")
INDEX_HTML = os.path.join(ROOT, "web", "index.html")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def howto_html():
    with open(HOWTO_HTML, encoding="utf-8") as fh:
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


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_guide_page_is_a_bundle_page_over_the_same_transport(howto_html):
    """Served beside index.html by both mounts, reaching the host only
    through api.js — so the desktop window and a browser popup render the
    same guide the same way."""
    assert '<script src="api.js"></script>' in howto_html
    assert "cbApi.connect()" in howto_html
    assert "cbApi.call('cookies.howto', { browser })" in howto_html
    assert "fetch(" not in howto_html
    assert "window.pywebview" not in howto_html


def test_the_guide_page_shares_the_apps_theme_and_stylesheets(howto_html):
    # The same theme bootstrap, ahead of the stylesheets: the stored theme is
    # the main window's, and a dark page must not flash light on its way up.
    boot = _slice(howto_html, "<script>", "</script>")
    assert "localStorage.getItem('cb_theme') === 'dark'" in boot
    assert "setAttribute('data-theme', 'dark')" in boot
    assert howto_html.index(boot) < howto_html.index('href="theme.css"')
    # And the colour theme, or the guide stays red while the app is green.
    assert "localStorage.getItem('cb_accent') === 'green'" in boot
    assert "setAttribute('data-accent', 'green')" in boot
    for sheet in ("theme.css", "app.css", "theme-dark.css", "theme-green.css"):
        assert f'<link rel="stylesheet" href="{sheet}">' in howto_html
    # Full-window: the modal's height cap comes off the walkthrough box.
    assert ".cb-howto { max-height: none;" in howto_html


def test_the_guide_page_tags_lines_exactly_as_app_js_does(howto_html, app_js):
    ours = _slice(howto_html, "  function howtoLineClass(line) {", "\n  }\n")
    theirs = _slice(app_js, "  function howtoLineClass(line) {", "\n  }\n")
    assert ours == theirs


def test_the_guide_page_reads_the_browser_from_its_hash(howto_html):
    assert "decodeURIComponent(location.hash.slice(1)) || 'Chrome'" in howto_html


# ── openCookieHowto: window first, modal only as the fallback ────────────────

_HARNESS = """
const cbApi = { transport: %(transport)r };
const hostCalls = [], toasts = [], opened = [], modals = [];
cbApi.call = async (method, params) => {
  hostCalls.push([method, params]);
  if (method === 'cookies.howto_window' && %(refuse)s) throw new Error('no window here');
  if (method === 'cookies.howto') return { title: 'T', text: 'Setting Up\\nStep 1' };
  return {};
};
async function call(method, params) {
  try { return await cbApi.call(method, params); }
  catch (err) { toasts.push(err.message); throw err; }
}
const window = {
  open(url, name, features) {
    opened.push({ url, name, features });
    return %(popup)s;
  },
};
function mkEl() {
  const e = { children: [], style: {}, className: '', textContent: '' };
  e.appendChild = (c) => { e.children.push(c); return c; };
  return e;
}
const document = { createElement: () => mkEl() };
function modalButton(label, cls, onClick) { return { label, cls, onClick, style: {} }; }
function openModal(opts) {
  const body = mkEl(), foot = mkEl();
  opts.body(body, {}); opts.foot(foot, { close() {} });
  modals.push({ title: opts.title, icon: opts.icon, lines: body.children[0].children.length });
}
function howtoLineClass() { return ''; }
%(fn)s
openCookieHowto('Firefox').then(() => {
  console.log(JSON.stringify({ hostCalls, toasts, opened, modals }));
});
"""


def _open(app_js, tmp_path, transport, refuse=False, popup="{ focus() {} }"):
    fn = _slice(app_js, "  async function openCookieHowtoWindow(browser) {",
                "  /* Browsers the Browser Profile method cannot read at all.")
    return _run_node(tmp_path, "howto.mjs", _HARNESS % {
        "transport": transport, "refuse": json.dumps(refuse), "popup": popup,
        "fn": fn})


def test_the_desktop_window_asks_the_host_for_a_window_and_shows_no_modal(app_js, tmp_path):
    r = _open(app_js, tmp_path, "local")
    assert r["hostCalls"] == [["cookies.howto_window", {"browser": "Firefox"}]]
    assert r["opened"] == []
    assert r["modals"] == []


def test_a_host_that_cannot_open_a_window_gets_the_modal_quietly(app_js, tmp_path):
    """The refusal is the fallback's cue, not an error to toast."""
    r = _open(app_js, tmp_path, "local", refuse=True)
    assert [m for m, _ in r["hostCalls"]] == ["cookies.howto_window",
                                              "cookies.howto"]
    assert r["toasts"] == []
    assert r["opened"] == []
    assert r["modals"] == [{"title": "T", "icon": "book", "lines": 2}]


def test_a_browser_opens_the_guide_page_as_a_popup(app_js, tmp_path):
    r = _open(app_js, tmp_path, "remote")
    assert r["hostCalls"] == []
    (popup,) = r["opened"]
    assert popup["url"] == "howto.html#Firefox"
    assert popup["name"] == "cb-howto"
    assert "popup" in popup["features"]
    assert r["modals"] == []


def test_a_blocked_popup_falls_back_to_the_modal(app_js, tmp_path):
    r = _open(app_js, tmp_path, "remote", popup="null")
    assert len(r["opened"]) == 1
    assert r["hostCalls"] == [["cookies.howto", {"browser": "Firefox"}]]
    assert r["modals"] == [{"title": "T", "icon": "book", "lines": 2}]


def test_the_gate_closes_itself_before_handing_over_to_the_guide(app_js):
    """A modal the guide replaced used to close by being replaced; a guide
    in its own window leaves the gate to close itself."""
    gate = _slice(app_js, "  function openCookieGate(box) {",
                  "  /* Section-level help")
    assert "          turnOn();\n          closeModal();\n          openCookieHowto(browser);" in gate

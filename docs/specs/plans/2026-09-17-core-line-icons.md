# Core Line icons for the web UI

**Goal:** Replace every emoji drawn on a button, heading, dialog title or menu
label in `web/` with the matching glyph from Streamline's "Core Line – Free"
icon set, so the interface looks the same on every machine and every icon takes
its host's colour in both themes.

**Decisions already made (by the maintainer, 2026-09-17):**

- Icon set: Core Line – Free. Two stand-ins are accepted: a paintbrush for the
  cleanup broom, a clock for the hourglass.
- Only buttons, headings, dialog titles and menu labels change. **Stay as text:**
  the status marks in the queue log and batch rows (`DL_MARK`, `WL_QROW_MARK`,
  `✓ ✗ ⊘ ▶ · ○ ⬇`), the log stat-bar chips (`✓ N downloaded`, `⚠ N warnings`,
  `ℹ`, `·`), the `⚠` sign inside sentences and tags, the artwork table's `✓`/`✗`
  cells, the `▲ ▼` row buttons, the `⬇  Watch List — downloading…` header line
  in the queue panel, and every `→` inside prose or tooltips.
- Arrows: `→` on **links** (`<a>` elements, including the `job.link` text the
  Overview sets into `#ov-open`) stay text. `↗` on **buttons** becomes the Core
  `ext-link` glyph.
- A prose sentence that names a button ("press ✕ Cancel on the card") drops the
  emoji and keeps the button's name.

## How icons work (already in place — `web/icons.css`)

`web/icons.css` (committed on this branch, generated from the Streamline SVGs)
defines the mechanism: an element with `data-ic="name"` draws that glyph before
its text; `data-ic-end="name"` draws it after. The glyph is a CSS mask painted
with `currentColor`, sized in em. **The label stays a plain text node** — no JS
builds SVG markup, `textContent` keeps working, and an element built by
`document.createElement` needs only `setAttribute('data-ic', name)`.

Inside `.cb-btn` (an inline-flex host) the button's own `gap` spaces the glyph
from the text. Anywhere else (`h4.cb-h`, `.cb-mtitle`) the stylesheet adds its
own margin. An icon-only button (`.cb-icon`) has `data-ic` and empty text.

Available glyph names: `pause play download skip bolt search wrench broom
refresh tag folder database image eye gear edit clipboard export import bug
ext-link globe book help clock lock close`.

## Global constraints

- Load the project skill `changing-the-web-ui` before editing. Its four rules
  bind: host access only via `cbApi` (irrelevant here — no host calls change),
  the tests slice `app.js` **as text** by exact function markers, colours are
  `--cb-*` tokens (icons use `currentColor`, nothing new), `ui_strings.py` is
  generated (untouched).
- **Never edit `web/theme.css`.** The generic icon rules live in
  `web/icons.css`; `web/app.css` needs no change.
- `web/app.js` is one IIFE; keep the two-space indentation and the existing
  `/* … */` "why" comment style. Do not rename, re-indent or reorder any
  function — the text-sliced tests key off exact lines such as
  `"  function renderDownloads()"`.
- Do not add `innerHTML`-built icon markup anywhere; icons are attributes only.
- Keep the emoji in code comments as they are (they describe the UI to a
  reader; e.g. the comment at `web/app.js:1381`, `:2838`, `:7037`, `:7348`,
  `:7719`).
- Nothing outside `web/index.html`, `web/app.js` and `tests/test_web_*_client.py`
  changes. `tests/test_web_window.py` is Python-side log text and stays.
- Verification for "done": `python -m pytest -q tests/test_web_*_client.py`
  must pass in full (Node is present; the Node halves must run, not skip), and
  the diff must contain **no** remaining emoji from the mapping tables below in
  a button, heading, title or menu label.
- Conventional Commits, direct on this branch: `feat(web): …` for the swap,
  `test(web): …` if the test edits are committed separately.

## Task 1: Swap the emoji for `data-ic` glyphs in the markup, the screens and the tests

**Model:** opus — touches `web/app.js` at ~60 sites plus the text-sliced
tests; the project's floor for anything in `app.js`.

**Files:** `web/index.html`, `web/app.js`, `tests/test_web_about_client.py`,
`tests/test_web_howto_client.py`, `tests/test_web_watchlist_client.py`,
`tests/test_web_wiring_client.py` (and any other `tests/test_web_*_client.py`
that a run shows pinning old emoji text).

### 1a. `web/index.html`

Add `<link rel="stylesheet" href="icons.css">` directly after the `app.css`
link (line 26), before `theme-dark.css`.

Then, on each element below, remove the emoji (and the space after it) from
the text and add the attribute. Leave every `id`, class and `data-tt` as is.

| Line | Element text today | Attribute | Text after |
|---|---|---|---|
| 121 | `⏸ Pause` (`#ov-pause`) | `data-ic="pause"` | `Pause` |
| 140 | `⬇ Download All New (0)` (`#ov-dl-all`) | `data-ic="download"` | `Download All New (0)` |
| 234 | `📂 Open Main Folder` | `data-ic="folder"` | `Open Main Folder` |
| 242 | `⏸ Pause` (`#dl-pause`) | `data-ic="pause"` | `Pause` |
| 284 | `🛠 Check Links` | `data-ic="wrench"` | `Check Links` |
| 285 | `⬇ Download All New` (`#wl-dl-all`) | `data-ic="download"` | `Download All New` |
| 286 | `🔍 Scan for new` | `data-ic="search"` | `Scan for new` |
| 288 | `✕ Cancel` | `data-ic="close"` | `Cancel` |
| 291 | `📤 Export List` | `data-ic="export"` | `Export List` |
| 292 | `📥 Import List` | `data-ic="import"` | `Import List` |
| 304 | `Open Activity Log ↗` | `data-ic-end="ext-link"` | `Open Activity Log` |
| 314 | `<h4 class="cb-h">⚙ Settings</h4>` | `data-ic="gear"` | `Settings` |
| 340 | `✕` (`#log-activity-clear`, icon-only) | `data-ic="close"` | *(empty)* |
| 373 | `✕` (`#log-debug-clear`, icon-only) | `data-ic="close"` | *(empty)* |
| 395 | `⬇ Downloads` | `data-ic="download"` | `Downloads` |
| 396 | `👁 Watch List` | `data-ic="eye"` | `Watch List` |
| 397 | `🖼 Artwork` | `data-ic="image"` | `Artwork` |
| 399 | `❔ Help` | `data-ic="help"` | `Help` |
| 433 | `🧹 Folders Cleanup ‹Smart›` | `data-ic="broom"` | `Folders Cleanup ‹Smart›` |
| 453 | `🖼 Fetch Missing Artwork` | `data-ic="image"` | `Fetch Missing Artwork` |

The six `<a … >… →</a>` links (lines 123, 141, 152, 160, 167, 172) stay exactly
as they are.

### 1b. `web/app.js` — labels set with `textContent`

Setting `textContent` does not clear attributes, so an element whose glyph
never changes gets `setAttribute('data-ic', …)` once where it is created (or
already has it from `index.html`); only the text loses its emoji. Where the
glyph changes with state (Pause/Resume), set the attribute alongside the text.

| Line | Today | Change |
|---|---|---|
| 1077 | `` `⬇ Download All New (${num(pending)})` `` | text `` `Download All New (${num(pending)})` `` (the glyph is on the element in `index.html`) |
| 1190 | `dl.paused ? '▶ Resume' : '⏸ Pause'` | text `'Resume'`/`'Pause'`; `b.setAttribute('data-ic', dl.paused ? 'play' : 'pause')` |
| 1354 | same pair | same treatment |
| 1365 | `warn ? '⏭ Skip' : '⏭'` | text `warn ? 'Skip' : ''`; `b.setAttribute('data-ic', 'skip')` |
| 1395 | `'⏭ Skip'` | text `'Skip'`; `b.setAttribute('data-ic', 'skip')` |
| 1551 | `['✕', 'main.row_remove', …]` in the `▲ ▼ ✕` row-button list | the remove button gets `data-ic="close"` and empty text; `▲` and `▼` stay text. Extend the tuple (e.g. a fourth `icon` entry, `null` for the arrows) rather than special-casing the label string |
| 1816 | `closeBtn.textContent = '✕'` (modal close) | empty text + `closeBtn.setAttribute('data-ic', 'close')` |
| 2242 | `` `⬇ Download All New (${num(pending)})` `` | text without the emoji (glyph is on `#wl-dl-all` in `index.html`) |
| 2646 | `local ? '📂 Open Folder' : '📋 Copy folder path'` | text `'Open Folder'`/`'Copy folder path'`; `folderBtn.setAttribute('data-ic', local ? 'folder' : 'clipboard')` |
| 2654 | `'🌐 Open Link'` | `'Open Link'` + `data-ic="globe"` |
| 5002–5003 | `` `📖 How-To: …` `` (both branches) | drop `📖 `; `howto.setAttribute('data-ic', 'book')` once |
| 5495 | `'📋 Activity Log'` | `'Activity Log'` + `data-ic="clipboard"` |
| 5501 | `'🔍 Debug Log'` | `'Debug Log'` + `data-ic="search"` |
| 5524 | `'🗂 Open Database'` | `'Open Database'` + `data-ic="database"` |
| 5543 | `'⏳ Show progress'` | `'Show progress'` + `data-ic="clock"` |
| 6322 | `'🐞 Report a Bug'` | `'Report a Bug'` + `report.setAttribute('data-ic', 'bug')` |
| 7423, 7541, 7548 | `⚠ …` | **unchanged** (prose / tag) |

### 1c. `web/app.js` — labels that go through a helper

Give each helper an optional icon and let the call sites name the glyph. Keep
the existing positional parameters in place so untouched call sites still work.

- `wlActionButton(label, ttKey, cls, onClick, disabledReason)` → add a sixth
  parameter `icon`; when given, `b.setAttribute('data-ic', icon)`. Call sites
  (2149–2175): `🔍 Scan`→`search`, `⚡ Force Download`→`bolt`,
  `⬇ Download New (N)`→`download`, `🛠 Fix Link`→`wrench`, `✏ Edit`→`edit`,
  `✕ Cancel`→`close`, `✕ Remove`→`close`. Labels lose the emoji.
- `modalButton(label, cls, onClick, ttKey)` → add a fifth parameter `icon`,
  same treatment. Call site 2665: `🛠 Smart-Edit Link`→`wrench`.
- `aboutLinkButton(label, url, ttKey)` → add a fourth parameter `icon`; here
  the glyph is **trailing** on `View on GitHub ↗` (6328) and leading on
  `↗ Submit Issues / Suggestions` (6329). Simplest faithful shape: a fifth
  parameter or an options object choosing `data-ic` vs `data-ic-end`; both
  use the `ext-link` glyph. Text loses the arrow.
- `openModal(opts)` → honour `opts.icon`: `title.setAttribute('data-ic',
  opts.icon)` when set. The `aria-label` stays the plain title. Call sites:
  2860 `` `🛠 Fix Link — ${row.name}` ``→`wrench`; 4461 and 4500
  `🧹 Folders Cleanup ‹Smart›`→`broom`; 5776 `` `📖 ${page.title}` ``→`book`;
  5899 `🔐 Looks like a sign-in problem`→`lock`; 6221 `🐞 Report a Bug`→`bug`.
- `MAINT_TASKS` (5130–5190): each entry gets `icon:` (`db.rebuild`→`refresh`,
  `db.dedupe`→`broom`, `db.repair_tags`→`tag`, `db.fetch_artwork`→`image`);
  `label` and `title` lose their emoji. Wherever `spec.label` becomes a
  button (5531) set `data-ic` from `spec.icon`; wherever `spec.title` opens a
  modal pass `icon: spec.icon`.

### 1d. `web/app.js` — prose that names a button

Drop the emoji, keep the name: 1960 `'🔍 Scan for new first.'` →
`'Scan for new first.'`; 2013–2014 `press ✕ Cancel on the card` → `press Cancel
on the card`; 2159 `run 🔍 Scan first` → `run Scan first`; 2661 `use 🛠
Smart-Edit Link` → `use Smart-Edit Link`.

### 1e. Tests

Run `python -m pytest -q tests/test_web_*_client.py` first to see what the
swap breaks, then update the assertions to the new text and, where a sliced
function now calls `setAttribute` on a stub element that lacks it, add
`setAttribute(k, v) { this.attrs = this.attrs || {}; this.attrs[k] = v; }`
to that harness's element stub. Known pins:

- `tests/test_web_about_client.py:628` — `"'🐞 Report a Bug'" in fn` → assert
  the new text **and** that the slice sets `data-ic` to `bug`.
- `tests/test_web_howto_client.py:158,175` — modal title `"📖 T"` → `"T"`; if
  the harness records the modal, also record and assert `icon == "book"`.
- `tests/test_web_watchlist_client.py:297` — `"⬇ Download All New (0)"` →
  `"Download All New (0)"`.
- `tests/test_web_watchlist_client.py:636` — `["⏭ Skip"]` → `["Skip"]`.
- `tests/test_web_watchlist_client.py:702` — slice marker
  `"const smart = modalButton('🛠 Smart-Edit Link'"` → the new literal.
- `tests/test_web_wiring_client.py:133–136` — How-To labels without `📖 `.
- Add one static test (in `tests/test_web_theme_client.py` or
  `tests/test_web_wiring_client.py`, whichever already reads `index.html`)
  asserting `index.html` links `icons.css` and that no button in `index.html`
  still starts with one of the swapped emoji.

Tests that assert the log marks (`✓ ⊘ ✗ ⬇ ○`) and the
`⬇  Watch List — downloading` header must keep passing unchanged — those
glyphs are out of scope.

### Done when

- `python -m pytest -q tests/test_web_*_client.py` passes with the Node halves
  running.
- `grep` of `web/index.html` and `web/app.js` for the swapped emoji
  (`⏸ ⬇ ⏭ ⚡ 🔍 🛠 🧹 🔄 🏷 📂 🗂 🖼 👁 ⚙ ✏ 📋 📤 📥 🐞 ↗ 🌐 📖 ❔ ⏳ 🔐 ✕`)
  finds them only in code comments, in `DL_MARK`/`WL_QROW_MARK` and the
  `⬇  Watch List — downloading` header (`⬇`), and in the `<a>` links (`→` is
  not in the list at all).
- Commit(s) on this branch with the report written to the path the dispatch
  names.

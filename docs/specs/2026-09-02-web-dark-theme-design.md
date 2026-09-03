# Web UI: a dark theme, chosen in Settings

## Problem

The web UI ships one look: the near-white ground `UI-design/HANDOFF.md` §7
describes, with YouTube red as the line that separates everything. The desktop
app it replaced was dark (`DJ-CrateBuilder_v2.0.py`: `BG #0f0f0f`, `SURFACE
#1a1a1a`, `TEXT #f0f0f0`), and a DJ reading a scan log in a dim booth wants that
back. Two constraints shape how: `web/theme.css` is the design's drop-in and is
never edited, and `UI-design/ui-contract.json` — which lists every Settings key
the screen draws — is never edited either.

## Design

### An overlay, keyed on one attribute

`web/theme-dark.css` re-declares every `--cb-*` token `theme.css` and `app.css`
declare, under `:root[data-theme="dark"]`, and restates the few component rules
where those sheets paint a literal instead of a token (`theme.css`'s secondary
ink `#4B4F59` on labels, nav links, segmented options and quiet buttons; the
select's arrow; hover tints and red washes tuned for near-white). Every selector
in the file starts with that attribute, so with it absent the sheet is inert and
the light theme is byte-for-byte what it was. `color-scheme: dark` on the same
rule hands the native controls and scrollbars over too.

The three inline literals `index.html` carried, and the two `app.css` rules and
one `app.js` style that painted a literal, now use tokens — three of them new,
declared in `app.css`'s own `:root` block beside the four it already keeps for
the same reason (`--cb-text-2`, `--cb-dot-read`, `--cb-ok-wash`).

### The palette

Near-black ground, the desktop app's own. The red line is lifted to `#E84A4A`,
which is what it takes for a 1px hairline to read on `#1A1A1D` and for red text
on a card to clear 4.5:1; the solid fill stays `#E00000`, because white text on
it is what clears 4.5:1 there. Muted text goes *lighter* than the desktop app's
`TEXT_DIM #888` — the same dimming that fails on near-white fails again on
near-black, which is `theme.css`'s own rule read from the other side. Status
colours are lifted the same way (`#3DC46E` / `#E9A93B` / `#FF5C5C`), each above
7:1 on the card surface.

### Applied before first paint

An inline script at the top of `index.html`'s `<head>` reads the stored choice
and sets `data-theme="dark"` on `<html>` before the stylesheets load, so a dark
page never flashes light on its way up. `app.js`'s `applyTheme` uses the same key
(`cb_theme`) for a change made while the page is open; a test holds the two to
the same key.

### Per device, not per host

The choice is a property of the screen it is read on, not of the host: the app
window on the host machine and a phone paired from the sofa each keep their own,
the way the database viewer's column widths do (HANDOFF §2). So it lives in
`localStorage` — the pywebview window keeps that across launches
(`private_mode=False`) — never in `config.json`, and a read-only remote session
can still change it, since it writes nothing to the host. This is also what
keeps the contract untouched: the Settings screen draws the contract's keys and
nothing here adds one.

### The control

An **Appearance** card, drawn ahead of the contract's sections in Settings, with
one row: *Theme* and a `.cb-seg` segmented control — Light | Dark — built the
way the Downloads screen's platform switch is, plus the radio role, a tab stop
and Enter/Space that the mockup's hover-only control lacks. A hint line says the
choice is kept on this device. The options are marked `readOnlyOk`, so the
read-only sweep `renderSettings` runs over host-bound controls leaves them live.

## Testing

`tests/test_web_theme_client.py`: `storedTheme`, `applyTheme` and
`appearanceCard` sliced out of `app.js` and run in Node against a stub document
and store (the default, the switch, an unknown value, a store that refuses);
`index.html`'s script and stylesheet order; the storage key shared by page and
script; `theme.css` identical to the design's copy; every colour token in the
light sheets having a dark value; every selector in the dark sheet scoped to the
attribute.

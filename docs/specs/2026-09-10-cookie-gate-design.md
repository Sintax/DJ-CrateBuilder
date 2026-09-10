# Browser Cookies gate — design note (2026-09-10)

## What the user sees

Ticking **Use Browser Cookies** on the Settings screen no longer just switches
it on. The box snaps back to off and a dialog appears first:

> **Before you turn on Browser Cookies**
> This is not a quick fix. YouTube only serves some tracks to a signed-in
> browser, and for the app to borrow that sign-in you need a dedicated browser
> profile — or an exported cookie file — set up exactly as the guide describes.
> Follow the setup guide for the browser you pick under Browser…
> Chrome cannot be read directly: it encrypts its cookies in a way only Chrome
> can unlock, so it is greyed out in the Browser list…

Three buttons:

| Button | What happens |
|---|---|
| **Keep cookies off** | Dialog closes. The box stays off. Nothing is saved. |
| **Open the setup guide** | Cookies are switched on, then the How-To for the selected browser replaces the dialog. |
| **Got it, turn it on** | Cookies are switched on. Dialog closes. |

Escape, the ✕, or a click outside behave like *Keep cookies off*. Turning
cookies **off** is unchanged — a plain tick.

If the stored Browser is Chrome, the dialog adds a red line saying so and
pointing at Firefox or the Cookie File method.

The dialog comes back every time the box is ticked. No "don't show again":
turning cookies on is rare, the reminder is cheap, and a remembered dismissal
is one more hidden setting to explain.

## Chrome

Chrome 127+ locks its cookie store with app-bound encryption that only Chrome
itself can unlock, so yt-dlp's browser-profile reader fails for every Chrome
profile ("could not copy Chrome cookie database", DPAPI errors). Three
consequences:

1. **Chrome is greyed out in the Browser dropdown**, with the reason on the
   option itself. It is not removed: a user who already had Chrome stored
   still sees Chrome, not a silently substituted browser.
2. **The Chrome guide is rewritten** as the cookie-file route: dedicated
   profile → throwaway account → a cookies.txt export extension → Method:
   Cookie File. Vivaldi, which has no guide of its own, keeps reading the
   Chrome one, which is right — it is Chromium too.
3. **The How-To button's label** for Chrome reads "Using Chrome Cookies via a
   Cookie File" rather than "Setting Up a Dedicated Chrome Profile".

Other Chromium browsers (Edge, Brave, Opera, Chromium) are left as they were.
Only Chrome was named by the maintainer, and only Chrome is known to fail.

## Where it lives

- `web/app.js` — the bool control's change handler diverts `use_cookies` to
  `openCookieGate`; the enum control disables entries in `UNREADABLE_BROWSERS`;
  `applySettingsDependencies` picks the How-To label.
- `DJ-CrateBuilder_v2.0.py` `COOKIE_HOWTO_TEXTS["Chrome"]` — the guide text,
  which `service.cookies_howto` reads as source.
- No new settings key, no new host method, no tooltip-registry change (the
  generator marks the contract read-only; the dialog carries its own copy).

## Tests

`tests/test_web_wiring_client.py` runs `openCookieGate` under Node against a
stub modal and asserts each button's effect; static checks pin the diverted
change handler and the greyed option. `tests/test_service_cookies_howto.py`
pins the Chrome guide to the cookie-file route.

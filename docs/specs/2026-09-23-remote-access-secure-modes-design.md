# Remote Access: secure Home network and Internet modes — design

**Date:** 2026-09-23
**Status:** draft — awaiting maintainer review

## In plain terms

Remote Access lets a phone control DJ-CrateBuilder from a browser. It was
built in August and parked on 2026-09-13. This design makes it safe and
actually usable:

- **Two modes, chosen in Settings:** *Home network* (phone on the same Wi-Fi)
  or *Over the internet* (anywhere, through the free Tailscale app).
- **Everything is encrypted, always.** No unencrypted connection is accepted
  in either mode.
- **No server of our own.** Nothing to rent, pay for, or keep secure.
- **A QR code** on the PC gets the phone to the right address; pairing with a
  6-digit code is always required.
- It stays switched off in nightly builds until both modes are built and
  tested on a real phone.

## Background — where Remote Access stands today

Built as Task 11 of the web-UI plan (`docs/specs/plans/webui-v2-implementation.md`,
ledger in `.superpowers/sdd/webui-v2-implementation/`): 6-digit pairing
(5-minute code, per-address attempt limit), per-device tokens stored hashed,
revoke, read-only mode, a single-writer "Take control" lock, Host/Origin
checks, and `update.*` / `fs.*` refused on the remote transport. Parked in
`efe8984` behind `remoteauth.REMOTE_ACCESS_AVAILABLE = False`.

Three gaps this design closes:

1. **Plain `http` only.** Device tokens and all traffic cross the network
   unencrypted.
2. **It never reached the network in the shipped app.** `web_window.py`
   starts the mount only at launch, and `server.bind_host` binds off loopback
   only when the process was started with `--lan` — which the installed app
   never is. A phone could not have connected.
3. **No way to reach the PC from outside the home.** No static IP, no relay.

## Decisions (maintainer, 2026-09-23)

| # | Decision |
|---|---|
| D1 | Two modes: **Home network** and **Over the internet**. Off by default. |
| D2 | Encryption is mandatory in both modes. |
| D3 | Home network: the app makes its **own certificate**. The phone warns once; the user taps through once per phone. The app pre-announces every legitimate re-warning; an unannounced warning means "don't tap through". |
| D4 | Internet: **Tailscale**, with a real `https` address from Tailscale (`tailscale serve`), the app listening on loopback only. Requiring users to install Tailscale for this mode is acceptable. |
| D5 | Add two dependencies: **`cryptography`** (certificate) and **`segno`** (QR code, pure Python). |
| D6 | **Pairing is always required** — the "Require pairing" switch is removed. |
| D7 | No self-hosted relay, no Azure, no Cloudflare Tunnel, no Tailscale Funnel (public exposure). |

## 1. What the user sees — Settings ▸ Remote Access

1. **Mode:** *Off* / *Home network* / *Over the internet* (replaces the
   `remote_enabled` switch). Internet mode also works at home.
2. **Kept:** *Read-only* switch, the three *Notify* switches, *Paired
   devices* + *Revoke all & re-pair*.
3. **Removed:** *Require pairing* switch (D6); the red "still in development"
   notice (when the kill switch flips, Stage 3).
4. **Connect a phone** panel, shown when a mode is on:
   - QR code of the address, the address as text, and — home mode — other
     candidate addresses under "Not working? Try…".
   - The existing *Pairing code* button and 5-minute countdown.
   - Home mode: the one-line rule — *"The first time, your phone will warn
     that the connection isn't private. Tap through once. If you ever see that
     warning again and this panel hasn't told you to expect it, don't."* — and,
     after a certificate change, *"Your phones will warn once more — this is
     expected."*
   - Home mode, first switch-on: *"Windows will ask whether to allow
     DJ-CrateBuilder on your network — click Allow (Private networks)."*
   - Internet mode, until Tailscale is ready: a checklist instead of the QR —
     ✓/✗ *Tailscale installed*, *Signed in*, *MagicDNS on*, *HTTPS
     certificates on* — with **Open setup guide** (a guide window like the
     cookie guide) and **Check again**.
5. On a remote mount the whole card stays read-only with its existing reason
   (a paired phone must not change who can connect).

## 2. Home network mode

- **Certificate** — new pure-logic module `cratebuilder/remotecert.py`:
  - EC P-256 key + self-signed X.509 certificate via `cryptography`.
  - Subject Alternative Names: the PC's home-network IPv4 addresses, its host
    name and `<hostname>.local`.
  - Validity 397 days (Safari refuses longer-lived server certificates).
  - Stored next to `cratebuilder_remote.json` (same folder as the database,
    `service.py:906`) as `cratebuilder_remote.crt` / `.key`. The key never
    leaves the machine and is never logged; POSIX mode `0600`.
  - **Reissued** when fewer than 30 days remain, or when the current home
    address is not in the certificate. A reissue records
    `expect_warning_since` so the panel can pre-announce the re-warning (D3).
  - The SHA-256 fingerprint is kept in the remote store for display and tests.
- **Address choice** — `home_addresses()`: private IPv4 (10/8, 172.16/12,
  192.168/16) only; excludes link-local 169.254/16 and 100.64/10 (CGNAT /
  Tailscale); the interface carrying the default route ranks first.
- **Serving** — uvicorn with `ssl_certfile` / `ssl_keyfile`, bound to
  `0.0.0.0:8770`. There is no plain-`http` listener in this mode.
  `web/api.js:239` already switches to `wss:` on an `https:` page.
- **Firewall** — rely on Windows' own first-listen prompt (installer rule is
  out of scope; the `.iss` is not touched).

## 3. Over the internet mode (Tailscale)

- New pure-logic module `cratebuilder/tailscale.py`. Every call is a
  subprocess with a timeout, no shell, and never raises.
  - **Locate the CLI:** Windows `C:\Program Files\Tailscale\tailscale.exe`,
    else `tailscale` on `PATH`.
  - **`status()`** from `tailscale status --json`: installed, backend running
    (`BackendState == "Running"`), signed in, `Self.DNSName` (trailing dot
    stripped), MagicDNS on, HTTPS certificates on (non-empty `CertDomains`).
    These are the checklist ticks.
  - **`publish(port)` / `unpublish()`** via `tailscale serve` in background
    mode: https on 443 → `http://127.0.0.1:8770`. Before publishing, read
    `tailscale serve status --json`; if :443 is already serving something
    that isn't ours, refuse with a clear message rather than overwrite.
    `unpublish()` removes only our entry.
- **Serving** — the mount binds `127.0.0.1:8770`, plain `http` (Tailscale
  terminates TLS with a real certificate). Nothing listens on the LAN.
- **Host allow-list** — the `*.ts.net` name is added to `extra_hosts`
  automatically on publish.
- **Pairing rate limit** — behind `tailscale serve` every request's peer is
  `127.0.0.1`, so all attempts share one budget. That is stricter, and
  acceptable. Forwarded headers stay untrusted (`server.uvicorn_kwargs`).
- **If Tailscale stops or signs out**, the checklist shows it on the next
  check (startup, opening Settings, *Check again*).

## 4. Shared mechanics

- **Mode storage** — `cratebuilder_remote.json` gains
  `"mode": "off" | "home" | "internet"`, replacing `enabled`.
  `require_pairing` is dropped (always on). **Migration:** any existing file
  loads as `mode: "off"`, because the old "enabled" meant unencrypted `http`;
  the user re-chooses. Settings key `remote_mode` replaces `remote_enabled` /
  `remote_require_pairing` (`service.py:159`, `web/app.js`
  `REMOTE_SETTING_KEYS`).
- **Live mount lifecycle** — a mode change starts, stops or restarts the
  uvicorn server immediately (no app restart). Switching to *Off* stops the
  server, cuts live sockets (existing behaviour) and unpublishes from
  Tailscale. The `--lan` flag and the flag-plus-intent rule in
  `server.bind_host` are replaced by the mode rule:

  | Mode | Bind | TLS |
  |---|---|---|
  | off | nothing listens | — |
  | home | `0.0.0.0:8770` | app certificate |
  | internet | `127.0.0.1:8770` | Tailscale |

- **Headless `web_server.py`** follows the stored mode the same way;
  `--host-allow` stays.
- **Local-only methods** — the new mode-change, connect-info (QR, address),
  Tailscale-check and guide-window methods are refused on the remote
  transport (a `LOCAL_ONLY` prefix or an explicit transport check).
- **QR** — generated host-side with `segno` as SVG, returned to the local page
  by the connect-info method. It encodes the address only, never a code or a
  token.
- **Tooltips** — new keys go in `UI-design/ui-contract.json`, then regenerate
  with `scripts/gen_ui_strings.py`.

## 5. Safety rules that do not change

Pairing code for every device; one controlling device at a time; read-only
mode; `update.*` / `fs.*` never over the remote transport; revoke cuts live
sessions; Host/Origin checks; token hashes only on disk; token redaction in
logs.

## 6. Dependencies and packaging

- `requirements.txt`: add `cryptography`, `segno`.
- `scripts/release.py`: hidden imports for both (they are imported lazily,
  only when a mode is on).
- Linux `.deb` build: include both.
- Update page components list (`cratebuilder/components.py`): add both so
  users see their versions.

## 7. Build stages and testing

Security work, so per CLAUDE.md: implementation by subagents with two-stage
review, one subagent at a time, `opus` for implementers and reviewers.

| Stage | What | Proof |
|---|---|---|
| 0 — spike | On the maintainer's real phone: does the browser keep a live `wss:` connection to a self-signed host after tapping through once? (iOS Safari is known to be unreliable here.) Confirm the current `tailscale serve` syntax. | Findings written back into this doc. No kept code. |
| 1 — Home mode | mode storage + migration, live mount lifecycle, `remotecert`, address choice, TLS serving, the Settings card and QR | unit tests, server tests, web client tests, both themes |
| 2 — Internet mode | `tailscale` module (fake CLI in tests), publish/unpublish, checklist, setup guide window | unit tests with a fake `tailscale`, web client tests |
| 3 — Real-device test, then flip | maintainer tests both modes on their phone; `REMOTE_ACCESS_AVAILABLE = True` in its own commit; `tests/test_remote_parked.py` retired | checklist below |

Until Stage 3 the kill switch stays off in shipped builds. Development and
the Stage 3 test use a harness that lifts it without changing shipped code
(the preview recipe already does this).

**Real-device checklist (Stage 3).**

- **Home:** scan the QR; the warning appears once; pair; Take control; start a
  download; switch to *Off* and the phone is cut off; restart the app and there
  is no warning; change the PC's address and the panel pre-announces the
  warning.
- **Internet:** Tailscale installed on the PC and the phone; checklist all
  green; on mobile data the phone gets a padlock and no warning; pair and
  control; switch to *Off* and `tailscale serve status` no longer lists us.

## 8. Risks and things to verify

1. **iOS + self-signed + WebSocket** (Stage 0). If iOS will not hold `wss:`
   after the tap-through, Home mode on iPhone either needs a polling fallback
   for live updates or iPhone users are pointed at Internet mode. Decide
   after the spike.
2. **`tailscale serve` CLI syntax** changed across versions (1.52+). Target
   the current syntax and detect the version.
3. **Windows firewall prompt** for the frozen exe on first `0.0.0.0` listen.
   Confirm in Stage 3.
4. **Several home addresses** (VPN adapters, Hyper-V, Docker). The ranking is
   best-effort; the "Not working? Try…" list is the fallback.

## Out of scope

A self-hosted relay; Tailscale Funnel / any public exposure; a native phone
app; an installer firewall rule; putting the pairing code in the QR; revoking
one device at a time (today it's "revoke all"); the cross-process
"two frontends, one database" item (F4) from the web-UI review.

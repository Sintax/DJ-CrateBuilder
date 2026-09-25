# Remote Access secure modes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Remote Access usable and always encrypted. Settings chooses *Off* / *Home network* (the app's own certificate, `https` on the LAN) / *Over the internet* (Tailscale `serve`, loopback bind), with a QR code to connect. Shipped builds keep it switched off until a real Android phone has tested both modes.

**Architecture:** `remoteauth.RemoteState` stores a `mode` instead of the old `enabled` switch. A new `RemoteMount` starts, stops and restarts the uvicorn listener live whenever the mode changes. `remotecert` makes and reissues the home-network certificate, and `tailscale` wraps the Tailscale CLI. A new local-only `remote.connect_info` gives the Settings card its address, QR code and Tailscale checklist.

**Tech Stack:** Python 3.14, FastAPI + uvicorn (`ssl_certfile` / `ssl_keyfile`), `cryptography>=42`, `segno`, the Tailscale CLI, the plain-JS `web/` bundle, pytest with Node-sliced frontend tests.

**Spec:** `docs/specs/2026-09-23-remote-access-secure-modes-design.md`. Every executor reads it before starting a task.

## Global Constraints

- **Android only** (spec D8). The phone side targets Chrome on Android. No work for iOS browsers.
- **Encryption is mandatory** (D2). No plain-`http` listener off loopback in any mode. Home binds `0.0.0.0:8770` with TLS; Internet binds `127.0.0.1:8770` behind Tailscale; Off listens nowhere.
- **Pairing always needs a code** (D6). The `require_pairing` flag is removed.
- **New dependencies:** `cryptography`, `segno`, nothing else (D5). No relay, no Azure, no Cloudflare Tunnel, no Tailscale Funnel (D7).
- Port **8770**. Certificate validity **397 days**, reissued when fewer than **30 days** remain or the home address changes. A reissue is pre-announced in Settings for **14 days**.
- `remoteauth.REMOTE_ACCESS_AVAILABLE` stays **`False`** until Task 10.
- Project rules (CLAUDE.md):
  - never push;
  - never bump `APP_BUILD` / `APP_VERSION`;
  - never edit `web/theme.css`;
  - `cratebuilder/ui_strings.py` changes only by editing `UI-design/ui-contract.json` then running `python scripts/gen_ui_strings.py`;
  - colours only via `--cb-*` tokens;
  - the page reaches the host only via `cbApi`;
  - no tkinter in `cratebuilder/`;
  - `cratebuilder/` modules get one-line module docstrings;
  - `web/app.js` comments explain *why*.
- Navigate with graft: `graft grep "<symbol>"`, `graft skeleton <file>`, `graft callers <symbol> --depth 2`. Raw grep only for unindexed files.
- **Tests.** Run the files each task names. Any task touching `web/` also runs **all** `tests/test_web_*_client.py`. No full-suite run without asking the maintainer.
- **Execution.** Subagents run **one at a time**. After each implementation task, two `opus` reviews run in sequence: spec compliance, then code quality.
- `scripts/release.py` is gitignored. Edits to it are local-only and never appear in a commit.
- Commit per task with a Conventional Commit subject. Every commit message ends with the attribution line the session provides.

## File map

| File | Status | Responsibility |
|---|---|---|
| `cratebuilder/remoteauth.py` | modify | `mode` replaces `enabled`/`require_pairing`; certificate metadata; pairing always needs a code |
| `cratebuilder/remotecert.py` | **create** | home addresses; make/reuse/reissue the self-signed certificate |
| `cratebuilder/remotemount.py` | **create** | `RemoteMount`: start/stop/restart the listener per mode; Tailscale publish |
| `cratebuilder/tailscale.py` | **create** | find the CLI, `status()`, `ready()`, `publish()`, `unpublish()` |
| `cratebuilder/server.py` | modify | gates read `is_enabled()`; `listen_plan(mode)` replaces `bind_host` |
| `cratebuilder/service.py` | modify | `remote_mode` setting; `remote_mount`; `remote.connect_info`; `remote.tailscale_check` |
| `cratebuilder/components.py` | modify | list `cryptography`, `segno` on the Update page |
| `web_window.py` | modify | build one `RemoteMount`, apply the stored mode, stop it on exit |
| `web_server.py` | modify | headless: same `RemoteMount`, `--lan` removed |
| `UI-design/ui-contract.json` → `cratebuilder/ui_strings.py` | modify + regenerate | `remote_mode` enum replaces the two old toggles |
| `web/app.js`, `web/app.css` | modify | Connect-a-phone panel, QR, checklist, Tailscale guide |
| `requirements.txt` | modify | `cryptography>=42`, `segno` |
| `scripts/release.py` | modify (local only) | freeze and refresh the two new packages |
| tests | create/modify | per task, below |

---

### Task 0: Stage 0 spike on the maintainer's Android phone

**Model:** none. The controller (main thread) runs it with the maintainer, because it needs their phone. No subagent.

**Files:**
- Create (scratchpad only, never committed): `<session scratchpad>/spike_tls_ws.py`
- Modify: `docs/specs/2026-09-23-remote-access-secure-modes-design.md` §8 (record the findings)

**Interfaces:** none. Throwaway code.

- [ ] **Step 1: Install the two approved libraries into the dev environment**

Run: `python -m pip install "cryptography>=42" segno`
Expected: both install. `python -c "import cryptography, segno"` prints nothing.

- [ ] **Step 2: Write the spike script in the scratchpad**

```python
"""Throwaway: does Chrome on Android hold a live wss: socket to a self-signed host?"""
import asyncio
import datetime
import ipaddress
import os
import socket
import sys
import tempfile

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

PAGE = """<!doctype html><meta name=viewport content="width=device-width">
<h1 id=t>connecting…</h1><script>
const ws = new WebSocket('wss://' + location.host + '/ws');
ws.onmessage = (e) => { document.getElementById('t').textContent = 'live: ' + e.data; };
ws.onclose = () => { document.getElementById('t').textContent = 'socket closed'; };
</script>"""

app = FastAPI()


@app.get("/")
def page():
    return HTMLResponse(PAGE)


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    n = 0
    while True:
        n += 1
        await sock.send_text(str(n))
        await asyncio.sleep(1)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


def make_cert(ip, days, folder):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "spike")])
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(folder, f"spike{days}.crt")
    key_path = os.path.join(folder, f"spike{days}.key")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return cert_path, key_path


if __name__ == "__main__":
    port, days = int(sys.argv[1]), int(sys.argv[2])
    ip = lan_ip()
    cert, key = make_cert(ip, days, tempfile.mkdtemp())
    print(f"Open https://{ip}:{port}/ on the phone ({days}-day certificate)")
    uvicorn.run(app, host="0.0.0.0", port=port, ssl_certfile=cert, ssl_keyfile=key)
```

- [ ] **Step 3: Run the 397-day spike and walk the maintainer through it**

Run in the background: `python <scratchpad>/spike_tls_ws.py 8771 397`

If Windows asks whether to allow Python on the network, the maintainer clicks **Allow** for Private networks. On the Android phone (same Wi-Fi), in Chrome, they open the printed URL and tap **Advanced → Proceed**.

Record each of these as PASS or FAIL:
1. The heading counts up once per second (`live: 1, 2, 3…`).
2. After fully closing Chrome and reopening the same URL, there's no warning and it still counts.
3. After about 60 seconds with the screen on, it's still counting.

- [ ] **Step 4: Run the 825-day spike**

Stop the first spike. Run `python <scratchpad>/spike_tls_ws.py 8772 825` and open it on the phone.

Record whether Chrome allows tapping through, and whether it counts. A `NET::ERR_CERT_VALIDITY_TOO_LONG` page with no Proceed link means "no".

- [ ] **Step 5: Write the findings into the spec and decide**

In spec §8, replace risk 1's text with the recorded results.

- If Step 3's item 1 **failed**, stop. Home mode needs a redesign, so tell the maintainer before Task 1.
- If Step 4 passed, change the spec's "Validity 397 days" bullet to "Validity 825 days (Stage 0: Chrome on Android accepts it)". Then use `VALIDITY_DAYS = 825` in Task 3 instead of 397.

- [ ] **Step 6: Commit the spec update**

```bash
git add docs/specs/2026-09-23-remote-access-secure-modes-design.md
git commit -m "docs(specs): record the Android TLS spike results for Remote Access"
```

Stop the spike process.

---

### Task 1: Dependencies

**Model:** `sonnet`. Small, fully specified, no security logic.

**Files:**
- Modify: `requirements.txt`
- Modify: `cratebuilder/components.py:15-29` (`COMPONENTS`)
- Modify (local only, not committed): `scripts/release.py:114-124` (`BUNDLED_DEPS`)
- Test: `tests/test_components.py`

**Interfaces:**
- Produces: `cryptography` and `segno` are importable in the app and listed as Update-page components with keys `"cryptography"` and `"segno"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_components.py`:

```python
def test_the_remote_access_libraries_are_listed():
    keys = {k: dist for k, _label, dist in comp.COMPONENTS}
    assert keys["cryptography"] == "cryptography"
    assert keys["segno"] == "segno"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_components.py::test_the_remote_access_libraries_are_listed -q`
Expected: FAIL with `KeyError: 'cryptography'`.

- [ ] **Step 3: Add the rows, the requirements and the release refresh**

In `cratebuilder/components.py`, insert after the `("uvicorn", "Remote server (uvicorn)", "uvicorn"),` row:

```python
    ("cryptography", "Remote encryption (cryptography)", "cryptography"),
    ("segno", "Remote QR code (segno)", "segno"),
```

In `requirements.txt`, append:

```
cryptography>=42
segno
```

In `scripts/release.py` `BUNDLED_DEPS`, change `"pywebview", "bottle", "fastapi", "uvicorn[standard]",` to:

```python
    "pywebview", "bottle", "fastapi", "uvicorn[standard]",
    # Remote Access: the home-network certificate and the connect QR code.
    "cryptography", "segno",
```

`component_metadata_flags()` already adds `--copy-metadata` for every `COMPONENTS` entry. PyInstaller follows imports inside function bodies too, and `--collect-submodules cratebuilder` pulls in the new modules that import both libraries, so no hidden-import is needed. The build's smoke test loads every `COMPONENTS` package, so a freeze that dropped either one fails before it ships.

Linux needs nothing extra: `packaging/deb/build-deb.sh:52` copies `requirements.txt` into the package, and the install step builds its virtual environment from it.

- [ ] **Step 4: Run the component tests**

Run: `python -m pytest tests/test_components.py -q`
Expected: all pass. If a test asserts an exact row count or order, update it to include the two new rows.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt cratebuilder/components.py tests/test_components.py
git commit -m "build(remote): add cryptography and segno for Remote Access"
```

---

### Task 2: The switch becomes a mode

**Model:** `opus`. It's the security boundary (`remoteauth.py`) and spans more than two files plus `web/app.js`.

**Files:**
- Modify: `cratebuilder/remoteauth.py` (constants L52-60; `DISABLED_REASON` L150-155; `_load` L246-280; `_save` L282-306; flags section L308-353; `claim` L590-626)
- Modify: `cratebuilder/server.py` (`bind_host` L66-77; `require_enabled` L285-294; `pair_info` L355-360; `events` L475)
- Modify: `cratebuilder/service.py` (`REMOTE_SETTINGS_KEYS` L153-161; `settings_all` L2260-2275; `settings_get` L2277-2287; `settings_set` L2300-2312; `remote_config` L2402-2423)
- Modify: `UI-design/ui-contract.json` (settings rows L131-133; tooltip `remote.enabled`), then regenerate `cratebuilder/ui_strings.py`
- Modify: `web/app.js` (`REMOTE_SETTING_KEYS` L5482; hint L5768-5780)
- Modify: `web_window.py:1245`, `web_server.py:119`
- Test: create `tests/test_remote_modes.py`; update `tests/test_server.py`, `tests/test_remote_parked.py`, `tests/test_web_window.py`, `tests/test_service.py`

**Interfaces:**
- Produces, in `cratebuilder.remoteauth`:
  - `MODE_OFF = "off"`, `MODE_HOME = "home"`, `MODE_INTERNET = "internet"`, `MODES`
  - `RemoteState.mode() -> str`: the effective mode, `"off"` whenever `REMOTE_ACCESS_AVAILABLE` is False
  - `RemoteState.is_enabled() -> bool`
  - `RemoteState.set_mode(mode) -> str`: raises `ValueError` for an unknown mode; any change cuts live sockets with `CLOSE_DISABLED` and drops the control lock
  - `RemoteState.config() -> {"mode", "read_only"}`
  - `RemoteState.get_flag/set_flag`: only `"read_only"` now
  - `RemoteState.note_certificate(fingerprint)`
  - `RemoteState.certificate_info() -> {"fingerprint": str, "expect_warning_since": int}`
- Produces, in `cratebuilder.service`:
  - setting key `remote_mode`, display values `"Off" | "Home network" | "Over the internet"`
  - `REMOTE_MODE_LABELS`
  - `CrateBuilderService._apply_remote_mode(mode)`
  - `remote.config` now carries `mode` and `enabled`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remote_modes.py`:

```python
"""Remote Access modes: the stored mode that replaced the old on/off switch."""
import json

import pytest

from cratebuilder import remoteauth
from cratebuilder.remoteauth import (CLOSE_DISABLED, MODE_HOME, MODE_INTERNET,
                                     MODE_OFF, PairingRefused, RemoteState)
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


def _state(tmp_path, payload=None):
    path = tmp_path / "cratebuilder_remote.json"
    if payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return RemoteState(str(path))


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def test_a_fresh_store_is_off(tmp_path):
    state = _state(tmp_path)
    assert state.mode() == MODE_OFF
    assert state.is_enabled() is False
    assert state.config() == {"mode": MODE_OFF, "read_only": False}


def test_the_mode_is_saved_and_read_back(tmp_path):
    state = _state(tmp_path)
    assert state.set_mode(MODE_HOME) == MODE_HOME
    again = RemoteState(state.path)
    assert again.mode() == MODE_HOME
    assert again.is_enabled() is True


def test_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _state(tmp_path).set_mode("everywhere")


def test_a_store_from_before_modes_loads_as_off(tmp_path):
    """The old switch meant unencrypted http; nobody is carried into it."""
    state = _state(tmp_path, {"enabled": True, "require_pairing": False,
                              "read_only": True, "devices": []})
    assert state.mode() == MODE_OFF
    assert state.get_flag("read_only") is True       # the other choice survives


def test_the_old_flags_are_gone(tmp_path):
    state = _state(tmp_path)
    for key in ("enabled", "require_pairing"):
        with pytest.raises(KeyError):
            state.get_flag(key)
    state.set_mode(MODE_HOME)
    with open(state.path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["mode"] == MODE_HOME
    assert "enabled" not in saved and "require_pairing" not in saved


def test_any_mode_change_cuts_live_sockets_and_the_control_lock(tmp_path):
    state = _state(tmp_path)
    state.set_mode(MODE_HOME)
    closed = []
    state.register_connection("dev1", on_close=closed.append)
    state.claim_control("dev1", "Phone")
    state.set_mode(MODE_INTERNET)
    assert closed == [CLOSE_DISABLED]
    assert state.control_holder() is None


def test_setting_the_same_mode_again_cuts_nothing(tmp_path):
    state = _state(tmp_path)
    state.set_mode(MODE_HOME)
    closed = []
    state.register_connection("dev1", on_close=closed.append)
    state.set_mode(MODE_HOME)
    assert closed == []


def test_pairing_always_needs_a_code(tmp_path):
    state = _state(tmp_path)
    state.set_mode(MODE_HOME)
    with pytest.raises(PairingRefused):
        state.claim("", "Phone", client="10.0.0.2")
    code = state.begin_pairing()["code"]
    assert state.claim(code, "Phone", client="10.0.0.2")["token"]


def test_the_kill_switch_still_reads_as_off(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.set_mode(MODE_HOME)
    monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", False)
    assert state.mode() == MODE_OFF
    monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", True)
    assert state.mode() == MODE_HOME


def test_a_new_certificate_after_the_first_is_announced(tmp_path):
    state = _state(tmp_path)
    state.note_certificate("a" * 64)
    # The first certificate is covered by the standing "warns once" rule.
    assert state.certificate_info()["expect_warning_since"] == 0
    state.note_certificate("a" * 64)
    assert state.certificate_info()["expect_warning_since"] == 0
    state.note_certificate("b" * 64)
    assert state.certificate_info()["expect_warning_since"] > 0
    again = RemoteState(state.path)
    assert again.certificate_info()["fingerprint"] == "b" * 64


def test_the_setting_speaks_labels(service):
    assert service.call("settings.get", {"key": "remote_mode"})["value"] == "Off"
    out = service.call("settings.set", {"key": "remote_mode",
                                        "value": "Home network"})
    assert out == {"key": "remote_mode", "value": "Home network"}
    assert service.remote_state.mode() == MODE_HOME
    assert service.settings_all()["remote_mode"] == "Home network"


def test_an_unknown_label_is_refused(service):
    with pytest.raises(CBError):
        service.call("settings.set", {"key": "remote_mode", "value": "Everywhere"})


def test_the_old_setting_keys_are_gone(service):
    values = service.settings_all()
    assert "remote_enabled" not in values
    assert "remote_require_pairing" not in values
    assert values["remote_mode"] == "Off"


def test_remote_config_carries_the_mode(service):
    service.call("settings.set", {"key": "remote_mode",
                                  "value": "Over the internet"})
    cfg = service.call("remote.config", {})
    assert cfg["mode"] == MODE_INTERNET
    assert cfg["enabled"] is True
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_remote_modes.py -q`
Expected: collection error, `ImportError: cannot import name 'MODE_HOME'`.

- [ ] **Step 3: Change `remoteauth.py`**

Replace `FLAG_KEYS` and `DEFAULTS` (L52-60) with:

```python
# Where the remote transport listens, chosen in Settings ▸ Remote Access. Off
# means nothing outside this machine can reach the host at all; the other two
# are both encrypted — home by the app's own certificate, internet by
# Tailscale. Never moved off "off" by any code path but an explicit setting
# write.
MODE_OFF = "off"
MODE_HOME = "home"
MODE_INTERNET = "internet"
MODES = (MODE_OFF, MODE_HOME, MODE_INTERNET)

FLAG_KEYS = ("read_only",)

DEFAULTS = {
    "mode": MODE_OFF,
    "read_only": False,
}

CERTIFICATE_KEY = "certificate"
```

Replace `DISABLED_REASON` (L150-155) with:

```python
# The mode is a live gate, not a startup-time bind decision: switching it to
# Off has to shut remote clients out of a host that is already listening.
DISABLED_REASON = (
    "Remote Access is set to Off on this host. Choose Home network or Over "
    "the internet in Settings ▸ Remote Access on the host machine to reach "
    "it from another device.")
```

In `_load`, replace the first flag block

```python
        out = dict(DEFAULTS)
        for key in FLAG_KEYS:
            if key in data:
                out[key] = bool(data.get(key))
```

with:

```python
        out = dict(DEFAULTS)
        # A store written before modes existed carries "enabled", which meant
        # plain http. It is ignored, not translated: nobody is carried into an
        # encrypted mode they never chose, nor left on the unencrypted one.
        mode = data.get("mode")
        out["mode"] = mode if mode in MODES else MODE_OFF
        for key in FLAG_KEYS:
            if key in data:
                out[key] = bool(data.get(key))
        cert = data.get(CERTIFICATE_KEY) if isinstance(data.get(CERTIFICATE_KEY), dict) else {}
        fingerprint = str(cert.get("fingerprint") or "")
        out[CERTIFICATE_KEY] = {
            "fingerprint": fingerprint if len(fingerprint) == 64 else "",
            "expect_warning_since": int(cert.get("expect_warning_since") or 0),
        }
```

In `_save`, replace

```python
        payload = {key: bool(self._data.get(key)) for key in FLAG_KEYS}
```

with:

```python
        payload = {"mode": self._data.get("mode", MODE_OFF)}
        payload.update({key: bool(self._data.get(key)) for key in FLAG_KEYS})
        payload[CERTIFICATE_KEY] = dict(self._data.get(CERTIFICATE_KEY) or {})
```

Replace the whole `# ── flags ──` section (`config`, `get_flag`, `set_flag`, L308-353) with:

```python
    # ── mode and flags ───────────────────────────────────────────────────────

    def mode(self):
        """The effective mode — Off whenever the feature is parked, so every
        gate that consults it refuses without its own switch, while the
        user's stored choice is left alone for when the switch flips."""
        if not REMOTE_ACCESS_AVAILABLE:
            return MODE_OFF
        with self._lock:
            return self._data.get("mode", MODE_OFF)

    def is_enabled(self):
        return self.mode() != MODE_OFF

    def set_mode(self, mode):
        """Persist the mode. ANY change cuts every live socket and the lock.

        Off is the promise that nothing is streaming any more; a move between
        Home and Internet replaces the listener itself, so a socket from the
        old one would only die a moment later without its client being told
        why. Cut them here with the disabled reason, which tells a client to
        keep its token and retry.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown Remote Access mode: {mode!r}")
        closers = []
        with self._lock:
            changed = self._data.get("mode") != mode
            self._data["mode"] = mode
            if changed:
                for conns in self._connections.values():
                    closers.extend(cb for cb in conns.values() if cb)
                self._connections = {}
                self._control = None
            self._save()
        for close in closers:
            try:
                close(CLOSE_DISABLED)
            except Exception:
                pass            # a socket already gone is not a failed switch
        return mode

    def config(self):
        return {"mode": self.mode(),
                **{key: self.get_flag(key) for key in FLAG_KEYS}}

    def get_flag(self, key):
        if key not in FLAG_KEYS:
            raise KeyError(key)
        with self._lock:
            return bool(self._data.get(key))

    def set_flag(self, key, value):
        if key not in FLAG_KEYS:
            raise KeyError(key)
        with self._lock:
            self._data[key] = bool(value)
            self._save()
            return bool(self._data[key])

    # ── the home-network certificate ─────────────────────────────────────────

    def certificate_info(self):
        with self._lock:
            return dict(self._data.get(CERTIFICATE_KEY) or {})

    def note_certificate(self, fingerprint):
        """Record the certificate now being served. A change from a previous
        one stamps when the phones' browsers will warn again, so Settings can
        say so first — an unannounced warning is the user's signal to stop."""
        with self._lock:
            cert = dict(self._data.get(CERTIFICATE_KEY) or {})
            previous = cert.get("fingerprint") or ""
            if previous == fingerprint:
                return dict(cert)
            cert["fingerprint"] = fingerprint
            cert["expect_warning_since"] = int(self._now()) if previous else 0
            self._data[CERTIFICATE_KEY] = cert
            self._save()
            return dict(cert)
```

In `claim` (L590-626), replace the docstring's last paragraph and the code condition. Change

```python
        With `require_pairing` off, a client on a host the user has already
        decided is reachable may pair without a code — still rate-limited, so
        the flag lowers the bar rather than removing it.
        """
        with self._lock:
            self._rate_limit(client)
            wanted = str(code or "").strip().replace(" ", "")
            if self._data.get("require_pairing") or wanted:
                live = self.active_code()
                if live is None or not wanted:
                    raise PairingRefused(BAD_CODE_REASON)
                if not _matches(wanted, live["code"]):
                    raise PairingRefused(BAD_CODE_REASON)
                self._code = None       # single use, cleared before we mint
```

to:

```python
        Every device needs a code — there is no way to switch that off.
        """
        with self._lock:
            self._rate_limit(client)
            wanted = str(code or "").strip().replace(" ", "")
            live = self.active_code()
            if live is None or not wanted or not _matches(wanted, live["code"]):
                raise PairingRefused(BAD_CODE_REASON)
            self._code = None           # single use, cleared before we mint
```

Also update the prose that still names the old switch:
- the kill-switch comment above `REMOTE_ACCESS_AVAILABLE` (L10-17): `the "enabled" flag reads as off` becomes `the mode reads as Off`;
- the `RemoteState` class docstring (L199-200): "holds the three remote-access flags" becomes "holds the Remote Access mode, the read-only flag, the home-network certificate's fingerprint".

- [ ] **Step 4: Change `server.py`**

In `bind_host`: `return ANY_INTERFACE if remote_state.get_flag("enabled") else None` becomes `return ANY_INTERFACE if remote_state.is_enabled() else None`. Task 4 replaces this function.

In `require_enabled`: `if not remote_state.get_flag("enabled"):` becomes `if not remote_state.is_enabled():`.

In `events`: `enabled = remote_state.get_flag("enabled")` becomes `enabled = remote_state.is_enabled()`.

In `pair_info`, replace the body's return with the constant below and add a sentence to its docstring:

```python
        return {"require_pairing": True}      # always: the switch is gone (D6)
```

- [ ] **Step 5: Change `service.py`**

Replace `REMOTE_SETTINGS_KEYS` (L153-161) and its comment with:

```python
# The Remote Access settings the contract lists but the app's own config
# schema has no room for: they govern who may reach this control surface, so
# they live in cratebuilder_remote.json beside the tokens they gate. Mapped
# here so settings.get/set still speak the contract's names and labels.
REMOTE_MODE_KEY = "remote_mode"
REMOTE_MODE_LABELS = {
    remoteauth.MODE_OFF: "Off",
    remoteauth.MODE_HOME: "Home network",
    remoteauth.MODE_INTERNET: "Over the internet",
}
REMOTE_SETTINGS_KEYS = {
    "remote_read_only": "read_only",
}


def _remote_mode_from(value):
    """A mode from its Settings label (or its own name); CBError otherwise."""
    wanted = str(value or "").strip()
    for mode, label in REMOTE_MODE_LABELS.items():
        if wanted in (mode, label):
            return mode
    raise CBError(f"Unknown Remote Access mode: {wanted or '(empty)'}")
```

`remoteauth` is already imported at the top of `service.py` (L32), and `CBError` only has to exist when `_remote_mode_from` is *called*, so this block can sit where the old one was.

In `settings_all`, before `flag = REMOTE_SETTINGS_KEYS.get(key)`, add:

```python
            if key == REMOTE_MODE_KEY:
                out[key] = REMOTE_MODE_LABELS[self.remote_state.mode()]
                continue
```

In `settings_get`, after the `if not key:` guard, add:

```python
        if key == REMOTE_MODE_KEY:
            return {"key": key,
                    "value": REMOTE_MODE_LABELS[self.remote_state.mode()]}
```

In `settings_set`, after `self._refuse_frozen_setting(key)`, add:

```python
        if key == REMOTE_MODE_KEY:
            if self.transport != LOCAL:
                raise CBError(REMOTE_SETTING_REFUSAL)
            if not remoteauth.REMOTE_ACCESS_AVAILABLE:
                raise CBError(remoteauth.IN_DEVELOPMENT_REASON)
            self._apply_remote_mode(_remote_mode_from(value))
            return {"key": key,
                    "value": REMOTE_MODE_LABELS[self.remote_state.mode()]}
```

In the existing `flag = REMOTE_SETTINGS_KEYS.get(key)` branch, delete the now-dead lines

```python
            if flag == "enabled" and not stored:
                # set_flag has just closed every live socket and dropped the
                # control lock with them; the host's own Remote Access card is
                # still drawing whoever held it.
                self.emit("control.holder", {})
```

Add this method directly after `_require_local_remote_admin`:

```python
    def _apply_remote_mode(self, mode):
        """Store the mode and tell every card. set_mode has already cut the
        live sockets and the control lock when the mode actually changed, and
        the host's own Remote Access card is still drawing whoever held it."""
        before = self.remote_state.mode()
        self.remote_state.set_mode(mode)
        if before != mode:
            self.emit("control.holder", {})
        self.emit("remote.mode", {"mode": self.remote_state.mode()})
```

In `remote_config`, after `out = dict(state.config())`, add:

```python
        out["enabled"] = state.is_enabled()
```

- [ ] **Step 6: Change the contract, then regenerate**

In `UI-design/ui-contract.json`, replace the two rows

```json
    { "key": "remote_enabled", "label": "Allow remote control over the internet", "type": "bool", "default": false, "section": "Remote Access", "source": "new", "tooltip": "remote.enabled" },
    { "key": "remote_require_pairing", "label": "Require pairing code for new devices", "type": "bool", "default": true, "section": "Remote Access", "source": "new" },
```

with:

```json
    { "key": "remote_mode", "label": "Mode", "type": "enum", "options": ["Off", "Home network", "Over the internet"], "default": "Off", "section": "Remote Access", "source": "new", "tooltip": "remote.mode" },
```

In the `tooltips` object, replace the `"remote.enabled"` entry (L234) with:

```json
    "remote.mode": { "text": "Off: nothing outside this PC can reach the app. Home network: phones on your Wi-Fi connect over an encrypted link using the app's own certificate. Over the internet: phones anywhere connect through Tailscale.", "source": "new", "screen": "3j" },
```

Run: `python scripts/gen_ui_strings.py`
Expected: `cratebuilder/ui_strings.py` rewritten. `git diff --stat` shows it changed.

Run: `graft grep "remote.enabled"` and fix every hit in `web/` and `tests/` to `remote.mode`.

- [ ] **Step 7: Change `web/app.js`**

Replace L5479-5483

```js
  /* The three contract keys the host keeps in cratebuilder_remote.json rather
     than the ordinary settings file — they govern who may reach this control
     surface, so they live beside the device tokens they gate. */
  const REMOTE_SETTING_KEYS = ['remote_enabled', 'remote_require_pairing',
                               'remote_read_only'];
```

with:

```js
  /* The two contract keys the host keeps in cratebuilder_remote.json rather
     than the ordinary settings file — they govern who may reach this control
     surface, so they live beside the device tokens they gate. */
  const REMOTE_SETTING_KEYS = ['remote_mode', 'remote_read_only'];

  /* The host's mode names as the Mode select shows them — the same labels
     service.REMOTE_MODE_LABELS sends — so a pushed mode can repaint it. */
  const REMOTE_MODE_LABELS = {
    off: 'Off', home: 'Home network', internet: 'Over the internet',
  };

  /* One line under the card per mode, in the words the mode choice uses.
     The old "takes effect the next time the host starts" is gone: a mode
     change re-plans the listener on the spot. */
  const REMOTE_MODE_HINTS = {
    off: 'Remote Access is off. Nothing outside this PC can reach the app.',
    home: 'Phones on your home Wi-Fi can connect over an encrypted link.',
    internet: 'Phones signed in to your Tailscale account can connect from ' +
      'anywhere, over Tailscale\u2019s encrypted link.',
  };
```

In the Remote Access card's `paint(cfg)`, replace

```js
          : cfg && cfg.enabled
          ? 'Remote access is on. The host serves this bundle to paired ' +
            'devices on the network; a change here takes effect the next time ' +
            'the host starts. Plain HTTP is LAN-only — put it behind a tunnel ' +
            'for anything wider.'
          : 'Remote access is off. The host answers on this machine only, and ' +
            'no paired device can reach it from elsewhere.';
```

with:

```js
          : REMOTE_MODE_HINTS[(cfg && cfg.mode) || 'off'] || REMOTE_MODE_HINTS.off;
```

- [ ] **Step 8: Change the two entry points**

`web_window.py` L1245: `if service.remote_state.get_flag("enabled"):` becomes `if service.remote_state.is_enabled():`.
`web_server.py` L119: `enabled = state.get_flag("enabled")` becomes `enabled = state.is_enabled()`.

Task 4 rewrites both call sites. This keeps them working until then.

- [ ] **Step 9: Update the existing tests**

1. **`tests/test_server.py`**
   - Add `MODE_HOME, MODE_OFF` to the `cratebuilder.remoteauth` import.
   - Replace every `state.set_flag("enabled", True)` with `state.set_mode(MODE_HOME)`, every `state.set_flag("enabled", False)` with `state.set_mode(MODE_OFF)`, and every `state.get_flag("enabled")` with `state.is_enabled()`. Also update the fixture docstring at L45-47 to say "Remote Access in Home network mode".
   - Replace `test_pairing_without_a_code_when_the_host_does_not_require_one` with:

     ```python
     def test_pairing_without_a_code_is_always_refused(client, state):
         res = client.post("/pair", json={"device_name": "LAN laptop"})
         assert res.status_code != 200
         assert "token" not in res.json()
     ```

   - In `test_remote_settings_cannot_be_changed_from_a_remote_client`, replace the loop and the two asserts with:

     ```python
         for key, value in (("remote_mode", "Off"), ("remote_read_only", False)):
             body = client.post("/rpc", json={"method": "settings.set",
                                              "params": {"key": key, "value": value}},
                                headers=auth(token)).json()
             assert body["ok"] is False, key
         assert state.mode() == MODE_HOME
     ```

     Also update its comment to say "switching Remote Access Off".
   - `test_the_host_window_hears_that_the_holder_went` (L767-776): `service.settings_set("remote_enabled", True)` becomes `service.settings_set("remote_mode", "Home network")`, and `service.settings_set("remote_enabled", False)` becomes `service.settings_set("remote_mode", "Off")`.
   - `test_pair_info_leaks_only_the_one_boolean` (L218) still holds unchanged.
   - `test_the_bind_rule_needs_both_consent_and_intent`: the `set_flag` / `get_flag` replacements above are all it needs.
2. **`tests/test_web_window.py`**: the same replacements at L101, L113 and L132 (`set_flag("enabled", True)` → `set_mode(MODE_HOME)`, `get_flag("enabled") is False` → `is_enabled() is False`), with `from cratebuilder.remoteauth import MODE_HOME, MODE_OFF`. The `web_server` and `web_window` console text that still names the old switch is replaced in Task 4, so the string assertions at L128 and L179 stay as they are for now.
3. **`tests/test_service.py:136`**: `assert values["remote_enabled"] is False` becomes `assert values["remote_mode"] == "Off"`.
4. **`tests/test_remote_parked.py`**: rewrite the four tests that use the old flags:

   ```python
   from cratebuilder.remoteauth import MODE_HOME, MODE_OFF


   def test_the_mode_reads_off_even_when_the_file_says_home(tmp_path, parked):
       state = RemoteState(str(tmp_path / "remote.json"))
       state.set_mode(MODE_HOME)
       assert state.mode() == MODE_OFF
       assert state.is_enabled() is False
       assert state.config()["mode"] == MODE_OFF


   def test_the_saved_choice_comes_back_when_the_switch_flips(tmp_path, monkeypatch):
       state = RemoteState(str(tmp_path / "remote.json"))
       state.set_mode(MODE_HOME)
       monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", False)
       assert state.mode() == MODE_OFF
       monkeypatch.setattr(remoteauth, "REMOTE_ACCESS_AVAILABLE", True)
       assert state.mode() == MODE_HOME


   def test_lan_bind_is_refused_while_parked(tmp_path, parked):
       state = RemoteState(str(tmp_path / "remote.json"))
       state.set_mode(MODE_HOME)
       assert server.bind_host(state, lan=True) is None


   def test_remote_settings_and_pairing_are_refused(service, parked):
       for key, value in (("remote_mode", "Home network"),
                          ("remote_read_only", True)):
           with pytest.raises(CBError, match="still in development"):
               service.call("settings.set", {"key": key, "value": value})
       with pytest.raises(CBError, match="still in development"):
           service.call("remote.pair_begin", {})
   ```

   In `test_both_frontends_are_told`, `assert cfg["enabled"] is False` stays valid. Add `assert cfg["mode"] == "off"`.
5. Run `graft grep "require_pairing|remote_enabled|remote_require_pairing"` and fix any remaining test or `web/` hit. The one expected survivor is `app.js` `showPairing`'s `info.require_pairing === false` branch: leave it, since the host now always sends `true`.

- [ ] **Step 10: Run the tests**

Run: `python -m pytest -q tests/test_remote_modes.py tests/test_remote_parked.py tests/test_server.py tests/test_web_window.py tests/test_service.py`
Expected: all pass.

Run: `python -m pytest -q tests/test_web_*_client.py`
Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add cratebuilder/remoteauth.py cratebuilder/server.py cratebuilder/service.py \
  UI-design/ui-contract.json cratebuilder/ui_strings.py web/app.js web_window.py \
  web_server.py tests/test_remote_modes.py tests/test_remote_parked.py \
  tests/test_server.py tests/test_web_window.py tests/test_service.py
git commit -m "feat(remote): replace the on/off switch with Off / Home / Internet modes"
```

---

### Task 3: The home-network certificate

**Model:** `opus`. Cryptographic key handling is security-sensitive.

**Files:**
- Create: `cratebuilder/remotecert.py`
- Test: create `tests/test_remotecert.py`

**Interfaces:**
- Produces:
  - `remotecert.CERT_FILE = "cratebuilder_remote.crt"`, `KEY_FILE = "cratebuilder_remote.key"`, `VALIDITY_DAYS` (397, or 825 per Task 0), `RENEW_WITHIN_DAYS = 30`, `EXPECT_WARNING_DAYS = 14`
  - `remotecert.home_addresses(_default=None, _others=None) -> list[str]`: private IPv4, default route first
  - `remotecert.ensure_certificate(folder, addresses, hostname, now=None) -> CertInfo`
  - `CertInfo(cert_path, key_path, fingerprint, not_after, issued)`: frozen dataclass; `fingerprint` is SHA-256 hex, lowercase; `not_after` is epoch seconds; `issued` is True when this call wrote a new certificate

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remotecert.py`:

```python
"""remotecert: the Home network mode's own TLS certificate."""
import datetime
import ipaddress
import os
import stat
import sys

import pytest
from cryptography import x509

from cratebuilder import remotecert

NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)


def _cert(info):
    with open(info.cert_path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def _sans(info):
    ext = _cert(info).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return ([str(ip) for ip in ext.value.get_values_for_type(x509.IPAddress)],
            ext.value.get_values_for_type(x509.DNSName))


def test_the_first_call_issues_a_certificate(tmp_path):
    info = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    assert info.issued is True
    assert os.path.isfile(info.cert_path) and os.path.isfile(info.key_path)
    assert len(info.fingerprint) == 64
    assert info.fingerprint == info.fingerprint.lower()


def test_it_names_every_address_and_the_host(tmp_path):
    info = remotecert.ensure_certificate(
        str(tmp_path), ["192.168.1.23", "10.0.0.5"], "DJPC", now=NOW)
    ips, names = _sans(info)
    assert ips == ["192.168.1.23", "10.0.0.5"]
    assert names == ["DJPC", "DJPC.local"]


def test_a_host_name_that_is_not_a_dns_label_is_left_out(tmp_path):
    info = remotecert.ensure_certificate(
        str(tmp_path), ["192.168.1.23"], "DJ PC_1", now=NOW)
    assert _sans(info)[1] == []


def test_the_lifetime_is_the_validity_days(tmp_path):
    info = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    cert = _cert(info)
    span = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert span == datetime.timedelta(days=remotecert.VALIDITY_DAYS)


def test_a_second_call_reuses_it(tmp_path):
    first = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    again = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC",
                                          now=NOW + datetime.timedelta(days=5))
    assert again.issued is False
    assert again.fingerprint == first.fingerprint


def test_a_new_home_address_reissues_it(tmp_path):
    first = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    moved = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.40"], "DJPC", now=NOW)
    assert moved.issued is True
    assert moved.fingerprint != first.fingerprint


def test_no_address_at_all_keeps_the_one_it_has(tmp_path):
    first = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    again = remotecert.ensure_certificate(str(tmp_path), [], "DJPC", now=NOW)
    assert again.issued is False and again.fingerprint == first.fingerprint


def test_it_is_renewed_near_expiry(tmp_path):
    first = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    late = NOW + datetime.timedelta(days=remotecert.VALIDITY_DAYS - 10)
    renewed = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=late)
    assert renewed.issued is True and renewed.fingerprint != first.fingerprint


def test_a_corrupt_file_is_replaced(tmp_path):
    first = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    with open(first.cert_path, "w", encoding="utf-8") as fh:
        fh.write("not a certificate")
    again = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    assert again.issued is True


def test_a_key_that_does_not_match_the_certificate_is_replaced(tmp_path):
    remotecert.ensure_certificate(str(tmp_path / "a"), ["192.168.1.23"], "DJPC", now=NOW)
    other = remotecert.ensure_certificate(str(tmp_path / "b"), ["192.168.1.23"], "DJPC", now=NOW)
    with open(other.key_path, "rb") as fh:
        stray = fh.read()
    with open(os.path.join(str(tmp_path / "a"), remotecert.KEY_FILE), "wb") as fh:
        fh.write(stray)
    again = remotecert.ensure_certificate(str(tmp_path / "a"), ["192.168.1.23"], "DJPC", now=NOW)
    assert again.issued is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_key_is_readable_by_its_owner_only(tmp_path):
    info = remotecert.ensure_certificate(str(tmp_path), ["192.168.1.23"], "DJPC", now=NOW)
    assert stat.S_IMODE(os.stat(info.key_path).st_mode) == 0o600


def test_home_addresses_keeps_private_ipv4_default_route_first():
    out = remotecert.home_addresses(
        _default=lambda: "192.168.1.23",
        _others=lambda: ["169.254.3.3", "100.101.1.2", "10.0.0.5", "8.8.8.8",
                         "127.0.0.1", "192.168.1.23", "not an ip", "172.20.0.9"])
    assert out == ["192.168.1.23", "10.0.0.5", "172.20.0.9"]


def test_home_addresses_survives_no_network():
    assert remotecert.home_addresses(_default=lambda: "", _others=lambda: []) == []
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_remotecert.py -q`
Expected: `ImportError` / `AttributeError`: no module `cratebuilder.remotecert`.

- [ ] **Step 3: Write `cratebuilder/remotecert.py`**

```python
"""The Home network mode's own TLS certificate: made once, reissued only when it must be."""
import datetime
import ipaddress
import os
import re
import socket
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_FILE = "cratebuilder_remote.crt"
KEY_FILE = "cratebuilder_remote.key"

# 397 days is the longest server-certificate lifetime every major browser
# accepts; Task 0's spike may raise it for Chrome on Android. Renewal starts a
# month early so a phone never meets an expired certificate.
VALIDITY_DAYS = 397
RENEW_WITHIN_DAYS = 30

# How long Settings keeps saying "your phones will warn once more" after a
# reissue — long enough to cover a phone that is only used at weekends.
EXPECT_WARNING_DAYS = 14

_TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class CertInfo:
    cert_path: str
    key_path: str
    fingerprint: str
    not_after: float
    issued: bool


def _is_home(ip):
    return (ip.version == 4 and ip.is_private and not ip.is_loopback
            and not ip.is_link_local and ip not in _TAILSCALE_RANGE)


def _default_route_address():
    # A UDP connect sends nothing; it only asks the OS which interface would
    # carry traffic off this machine. 192.0.2.1 is TEST-NET-1, never routed.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _all_addresses():
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def home_addresses(_default=None, _others=None):
    """This PC's home-network IPv4 addresses, the default-route one first."""
    found = []
    candidates = [(_default or _default_route_address)()]
    candidates += list((_others or _all_addresses)())
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(str(raw))
        except ValueError:
            continue
        if _is_home(ip) and str(ip) not in found:
            found.append(str(ip))
    return found


def _paths(folder):
    return os.path.join(folder, CERT_FILE), os.path.join(folder, KEY_FILE)


def _load(cert_path, key_path):
    """The stored certificate, or None if it is missing, unreadable, or its
    key is not the key it was made with."""
    try:
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        with open(key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        return None
    return cert


def _covers(cert, address):
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return False
    return ipaddress.ip_address(address) in ext.value.get_values_for_type(x509.IPAddress)


def _still_good(cert, addresses, now):
    if cert.not_valid_after_utc - now <= datetime.timedelta(days=RENEW_WITHIN_DAYS):
        return False
    # Only the address phones are sent to matters; with no network at all
    # there is nothing to check against, so the certificate is kept rather
    # than churned into a new warning for nothing.
    return not addresses or _covers(cert, addresses[0])


def _write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _write_public(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _issue(cert_path, key_path, addresses, hostname, now):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DJ-CrateBuilder")])
    alt = [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    if hostname and _DNS_LABEL.match(hostname):
        alt += [x509.DNSName(hostname), x509.DNSName(f"{hostname}.local")]
    start = now - datetime.timedelta(hours=1)          # a phone's clock may lag
    builder = (x509.CertificateBuilder()
               .subject_name(name).issuer_name(name)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(start)
               .not_valid_after(start + datetime.timedelta(days=VALIDITY_DAYS))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                              critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                              critical=False))
    if alt:
        builder = builder.add_extension(x509.SubjectAlternativeName(alt),
                                        critical=False)
    cert = builder.sign(key, hashes.SHA256())
    # Key first: a certificate on disk without its key would be reloaded,
    # fail the key match, and be reissued anyway — never the other way round.
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _write_public(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert


def ensure_certificate(folder, addresses, hostname, now=None):
    """Reuse the stored certificate if it still fits, otherwise make a new one."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    os.makedirs(folder, exist_ok=True)
    cert_path, key_path = _paths(folder)
    addresses = list(addresses or [])
    cert = _load(cert_path, key_path)
    issued = cert is None or not _still_good(cert, addresses, now)
    if issued:
        cert = _issue(cert_path, key_path, addresses, hostname, now)
    return CertInfo(cert_path=cert_path, key_path=key_path,
                    fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
                    not_after=cert.not_valid_after_utc.timestamp(),
                    issued=issued)
```

If Task 0 recorded that 825 days works, set `VALIDITY_DAYS = 825` and adjust the comment above it.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_remotecert.py -q`
Expected: all pass (one skipped on Windows).

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/remotecert.py tests/test_remotecert.py
git commit -m "feat(remote): make and reissue the Home network certificate"
```

---

### Task 4: Live listener lifecycle

**Model:** `opus`. Multi-file; it decides what is exposed on the network.

**Files:**
- Create: `cratebuilder/remotemount.py`
- Modify: `cratebuilder/server.py` (replace `bind_host` L66-77 with `listen_plan`)
- Modify: `cratebuilder/service.py` (`__init__` near L925: `self.remote_mount = None`; `_apply_remote_mode`)
- Modify: `web_window.py` (`start_remote_mount` L599-635; `main` L1244-1249 and after L1327; module docstring L11)
- Modify: `web_server.py` (`main` L82-137; `announce_pairing` L64-79; the `LAN_REFUSED` / `DISABLED_NOTE` constants)
- Test: create `tests/test_remotemount.py`; update `tests/test_server.py` (bind-rule test), `tests/test_web_window.py` (L17-18, L95-180), `tests/test_remote_parked.py` (`test_lan_bind_is_refused_while_parked`)

**Interfaces:**
- Consumes: `RemoteState.mode()`, `note_certificate()`, `path` (Task 2); `remotecert.ensure_certificate`, `home_addresses` (Task 3).
- Produces:
  - `server.listen_plan(mode) -> (host, tls) | None`
  - `remotemount.REMOTE_PORT = 8770`
  - `RemoteMount(service, port=8770, make_server=None, addresses=None, hostname=None, tailscale=None)`:
    - `.apply(mode) -> dict` (raises on failure, leaving nothing listening)
    - `.stop() -> dict`
    - `.status() -> dict` with keys `mode, listening, host, port, tls, addresses, public_url, tailscale, error`
    - `.wait()`
  - `CrateBuilderService.remote_mount` (None or a `RemoteMount`)
  - the event `remote.mode {"mode": str}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remotemount.py`:

```python
"""RemoteMount: the listener follows the Remote Access mode, live."""
import threading

import pytest

from cratebuilder import server
from cratebuilder.remoteauth import MODE_HOME, MODE_INTERNET, MODE_OFF, RemoteState
from cratebuilder.remotemount import RemoteMount
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


class FakeServer:
    def __init__(self, app, host, port, ssl, log):
        self.host, self.port, self.ssl = host, port, ssl
        self.should_exit = False
        self.started = False
        log.append(self)

    def run(self):
        self.started = True
        while not self.should_exit:
            threading.Event().wait(0.01)


@pytest.fixture
def made():
    return []


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


@pytest.fixture
def mount(service, made):
    m = RemoteMount(service, port=8799,
                    make_server=lambda app, host, port, ssl: FakeServer(app, host, port, ssl, made),
                    addresses=lambda: ["192.168.1.23", "10.0.0.5"],
                    hostname=lambda: "DJPC")
    service.remote_mount = m
    yield m
    m.stop()


def test_the_plan_per_mode():
    assert server.listen_plan(MODE_OFF) is None
    assert server.listen_plan(MODE_HOME) == (server.ANY_INTERFACE, True)
    assert server.listen_plan(MODE_INTERNET) == (server.LOOPBACK, False)


def test_off_listens_nowhere(mount, made):
    status = mount.apply(MODE_OFF)
    assert made == [] and status["listening"] is False


def test_home_listens_on_the_network_with_the_certificate(mount, made, service):
    status = mount.apply(MODE_HOME)
    assert status["listening"] is True and status["tls"] is True
    assert made[-1].host == server.ANY_INTERFACE and made[-1].port == 8799
    assert made[-1].ssl["ssl_certfile"].endswith("cratebuilder_remote.crt")
    assert made[-1].ssl["ssl_keyfile"].endswith("cratebuilder_remote.key")
    assert status["addresses"] == ["192.168.1.23", "10.0.0.5"]
    assert len(service.remote_state.certificate_info()["fingerprint"]) == 64


def test_internet_listens_on_this_machine_only_without_tls(mount, made):
    status = mount.apply(MODE_INTERNET)
    assert made[-1].host == server.LOOPBACK and made[-1].ssl == {}
    assert status["tls"] is False


def test_a_change_stops_the_old_listener_first(mount, made):
    mount.apply(MODE_HOME)
    mount.apply(MODE_INTERNET)
    assert made[0].should_exit is True
    assert made[1].should_exit is False
    mount.apply(MODE_OFF)
    assert made[1].should_exit is True


def test_a_listener_that_cannot_start_raises_and_leaves_nothing(service, made):
    class Dies(FakeServer):
        def run(self):
            raise SystemExit(1)     # what uvicorn does when the port is taken

    m = RemoteMount(service, port=8799,
                    make_server=lambda app, host, port, ssl: Dies(app, host, port, ssl, made),
                    addresses=lambda: ["192.168.1.23"], hostname=lambda: "DJPC")
    with pytest.raises(OSError):
        m.apply(MODE_INTERNET)
    assert m.status()["listening"] is False


def test_the_setting_re_plans_the_listener(service, mount, made):
    events = []
    service.events.subscribe(lambda t, p: events.append((t, p)))
    service.call("settings.set", {"key": "remote_mode", "value": "Home network"})
    assert mount.status()["mode"] == MODE_HOME and mount.status()["listening"]
    service._emit.flush()           # events leave through the coalescer
    assert ("remote.mode", {"mode": MODE_HOME}) in events


def test_a_mode_that_cannot_start_falls_back_to_off(service, made):
    class Dies(FakeServer):
        def run(self):
            raise SystemExit(1)

    service.remote_mount = RemoteMount(
        service, port=8799,
        make_server=lambda app, host, port, ssl: Dies(app, host, port, ssl, made),
        addresses=lambda: ["192.168.1.23"], hostname=lambda: "DJPC")
    with pytest.raises(CBError, match="switched off"):
        service.call("settings.set", {"key": "remote_mode", "value": "Home network"})
    assert service.remote_state.mode() == MODE_OFF
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_remotemount.py -q`
Expected: `ModuleNotFoundError: cratebuilder.remotemount`.

- [ ] **Step 3: Replace `bind_host` with `listen_plan` in `server.py`**

Replace the whole `bind_host` function (L66-77) with:

```python
def listen_plan(mode):
    """(host, tls) the remote mount listens on in *mode*, or None for Off.

    One rule for both entry points. Home is the only mode that reaches the
    network, and only with TLS; Internet stays on this machine and lets
    Tailscale carry it; Off listens nowhere at all.
    """
    if mode == MODE_HOME:
        return ANY_INTERFACE, True
    if mode == MODE_INTERNET:
        return LOOPBACK, False
    return None
```

Add `MODE_HOME, MODE_INTERNET` to the `from cratebuilder.remoteauth import (...)` block at L30.

- [ ] **Step 4: Write `cratebuilder/remotemount.py`**

```python
"""The remote mount's lifecycle: start, stop and restart the listener as the Remote Access mode changes."""
import os
import socket
import threading
import time

from cratebuilder import remotecert
from cratebuilder.remoteauth import MODE_OFF
from cratebuilder.server import create_app, listen_plan, uvicorn_kwargs

REMOTE_PORT = 8770
START_WAIT_SECONDS = 5
STOP_WAIT_SECONDS = 5


def _uvicorn_server(app, host, port, ssl):
    import uvicorn                    # deferred: the window works without it
    return uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                         log_level="warning", **ssl,
                                         **uvicorn_kwargs()))


class RemoteMount:
    """One listener for the remote transport, re-planned on every mode change.

    Owned by the process that owns the service — the desktop window, or
    web_server.py headless — and reached from Settings through
    `service.remote_mount`, so a mode change takes effect on the spot instead
    of at the next launch. Everything that touches the network is injectable
    so the tests never open a socket.
    """

    def __init__(self, service, port=REMOTE_PORT, make_server=None,
                 addresses=None, hostname=None, tailscale=None):
        self._service = service
        self._state = service.remote_state
        self._port = port
        self._make_server = make_server or _uvicorn_server
        self._addresses = addresses or remotecert.home_addresses
        self._hostname = hostname or socket.gethostname
        self._tailscale = tailscale
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._status = self._blank(MODE_OFF)

    def _blank(self, mode):
        return {"mode": mode, "listening": False, "host": None,
                "port": self._port, "tls": False, "addresses": [],
                "public_url": None, "tailscale": None, "error": None}

    def status(self):
        with self._lock:
            return dict(self._status)

    def apply(self, mode):
        """Stop whatever is listening, then start what *mode* needs.

        Raises when the listener cannot start, with nothing left listening —
        the caller decides what the stored mode becomes.
        """
        with self._lock:
            self._stop_locked()
            status = self._blank(mode)
            self._status = status
            plan = listen_plan(mode)
            if plan is None:
                return dict(status)
            host, tls = plan
            ssl = {}
            if tls:
                addresses = list(self._addresses())
                folder = os.path.dirname(self._state.path) or "."
                info = remotecert.ensure_certificate(folder, addresses,
                                                     self._hostname())
                self._state.note_certificate(info.fingerprint)
                ssl = {"ssl_certfile": info.cert_path,
                       "ssl_keyfile": info.key_path}
                status["addresses"] = addresses
            try:
                self._start_locked(host, ssl)
            except Exception as exc:
                status["error"] = str(exc)
                raise
            status.update(listening=True, host=host, tls=tls)
            return dict(status)

    def stop(self):
        return self.apply(MODE_OFF)

    def wait(self):
        """Block until the listener ends — web_server.py's foreground. Joined
        in short slices: a bare join() never sees Ctrl+C on Windows."""
        thread = self._thread
        while thread is not None and thread.is_alive():
            thread.join(0.5)

    def _start_locked(self, host, ssl):
        app = create_app(self._service, self._state)
        server = self._make_server(app, host, self._port, ssl)
        thread = threading.Thread(target=server.run, daemon=True,
                                  name="cratebuilder-remote")
        thread.start()
        # uvicorn binds inside run(); a port already taken ends the thread
        # instead of raising here, so wait to see which of the two happens.
        deadline = time.monotonic() + START_WAIT_SECONDS
        while not getattr(server, "started", False):
            if not thread.is_alive():
                raise OSError(f"Could not listen on port {self._port} — is "
                              "another program using it?")
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
        self._server, self._thread = server, thread

    def _stop_locked(self):
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(STOP_WAIT_SECONDS)
```

- [ ] **Step 5: Wire the mount into the service**

In `CrateBuilderService.__init__`, next to `self.on_open_howto = None` (L925), add:

```python
        # The listener behind Settings ▸ Remote Access (a RemoteMount). Set by
        # the process that owns one — web_window.py or web_server.py — and
        # None in tests and anywhere nothing listens.
        self.remote_mount = None
```

Replace `_apply_remote_mode` (from Task 2) with:

```python
    def _apply_remote_mode(self, mode):
        """Store the mode, re-plan the listener, and tell every card.

        A mode whose listener cannot start is switched back to Off rather
        than left claiming to be on: Settings must never say phones can
        connect when nothing is listening.
        """
        before = self.remote_state.mode()
        self.remote_state.set_mode(mode)
        if before != mode:
            self.emit("control.holder", {})
        mount = self.remote_mount
        if mount is not None:
            try:
                mount.apply(self.remote_state.mode())
            except Exception as exc:
                self.remote_state.set_mode(remoteauth.MODE_OFF)
                try:
                    mount.apply(remoteauth.MODE_OFF)
                except Exception:
                    pass
                self.emit("remote.mode", {"mode": remoteauth.MODE_OFF})
                raise CBError(f"Remote Access could not start, so it has been "
                              f"switched off: {exc}")
        self.emit("remote.mode", {"mode": self.remote_state.mode()})
```

- [ ] **Step 6: Rewrite `start_remote_mount` and its call in `web_window.py`**

Replace `start_remote_mount` (L599-635) with:

```python
def start_remote_mount(service, port=REMOTE_PORT, host_allow=None):
    """Build the one RemoteMount this window owns and bring it up in the
    stored mode.

    Settings re-plans it live through `service.remote_mount`, so nothing here
    decides the bind — cratebuilder/server.listen_plan does, for both entry
    points. A mount that cannot start never blocks the window: the mode goes
    back to Off and the Remote Access card says so when it is next opened.

    *host_allow* names this host should also answer to — the public name of a
    proxy or tunnel. Merged into the store, so naming one once configures it
    for good.
    """
    from cratebuilder.remoteauth import MODE_OFF
    from cratebuilder.remotemount import RemoteMount

    state = service.remote_state
    if host_allow:
        state.add_extra_hosts(host_allow)
    mount = RemoteMount(service, port=port)
    service.remote_mount = mount
    try:
        mount.apply(state.mode())
    except Exception as exc:                         # never block the window
        print(f"Remote mount could not start: {exc}", file=sys.stderr)
        state.set_mode(MODE_OFF)
    return mount
```

Task 8 adds the Tailscale half (`tailscale=tailscale`) once `cratebuilder.tailscale` exists.

Drop the now-unused `import uvicorn` / `bind_host` lines this function used to hold. In `main()`, replace L1244-1249

```python
    if service.remote_state.is_enabled():
        try:
            start_remote_mount(service, lan="--lan" in sys.argv,
                               host_allow=host_allow_args(sys.argv))
        except Exception as exc:                     # never block the window
            print(f"Remote mount could not start: {exc}", file=sys.stderr)
```

with:

```python
    mount = start_remote_mount(service, host_allow=host_allow_args(sys.argv))
```

After `webview.start(started, private_mode=False)` (L1327), add:

```python
    # The window is gone. Take the listener down on the way out so an
    # Internet-mode Tailscale entry is removed rather than left pointing at a
    # port nothing answers on; the stored mode is untouched, so the next
    # launch brings it straight back.
    try:
        mount.stop()
    except Exception:
        pass
```

Update the module docstring near L11, which describes "binds 127.0.0.1 unless `--lan` is passed", to say the mount follows the Remote Access mode in Settings.

- [ ] **Step 7: Rewrite the headless entry point `web_server.py`**

Replace `LAN_REFUSED` and `DISABLED_NOTE` with:

```python
OFF_REFUSED = (
    "Remote Access is set to Off, so there is nothing to serve.\n"
    "Choose Home network or Over the internet in Settings > Remote Access on "
    "the host, then start this again.")
```

Change `announce_pairing(state, host, port, force=False)` to `announce_pairing(state, url, force=False)`, and its last print to `print(f"  Open {url} on the device and enter it.\n")`. The caller now hands it the whole address, `https://…` in Home mode and Tailscale's own address in Internet mode (Task 8).

Remove the `--lan` argument from the parser. Replace everything from `host = bind_host(state, lan=args.lan)` to the end of `main` with:

```python
    mode = state.mode()
    if mode == MODE_OFF:
        sys.exit(OFF_REFUSED)
    mount = RemoteMount(service, port=args.port)
    service.remote_mount = mount
    status = mount.apply(mode)
    scheme = "https" if status["tls"] else "http"
    shown = (status["addresses"] or [status["host"]])[0]
    url = status.get("public_url") or f"{scheme}://{shown}:{args.port}/"
    where = args.data_dir or app_dir()
    print(f"DJ-CrateBuilder remote mount  ·  {url}  ({mode})")
    print(f"  data dir     : {where}")
    print(f"  token store  : {os.path.join(where, REMOTE_FILE_NAME)}")
    print(f"  paired       : {state.device_count()} device(s)")
    print(f"  read-only    : {'on' if state.get_flag('read_only') else 'off'}")
    extra = state.extra_hosts()
    answers_to = ", ".join(extra) if extra else "(addresses and this machine only)"
    print(f"  also answers to: {answers_to}")
    announce_pairing(state, url, force=args.pair)
    try:
        mount.wait()
    except KeyboardInterrupt:
        pass
    finally:
        mount.stop()            # Internet mode: take the Tailscale entry down too
```

Fix the imports:

- remove `import uvicorn`, and `bind_host, create_app, uvicorn_kwargs` from the server import, if nothing else uses them;
- add `from cratebuilder.remoteauth import MODE_OFF` and `from cratebuilder.remotemount import RemoteMount`.

Check the module docstring (L7) and replace its `--lan` wording.

- [ ] **Step 8: Update the tests that used `bind_host` or ran `web_server`**

1. **`tests/test_server.py`**: replace `test_the_bind_rule_needs_both_consent_and_intent` with:

   ```python
   def test_the_listen_plan_is_the_mode(state):
       from cratebuilder.server import ANY_INTERFACE, LOOPBACK, listen_plan

       assert listen_plan(MODE_OFF) is None
       assert listen_plan(MODE_HOME) == (ANY_INTERFACE, True)
       assert listen_plan("internet") == (LOOPBACK, False)
   ```

2. **`tests/test_remote_parked.py`**: `test_lan_bind_is_refused_while_parked` body becomes

   ```python
       state = RemoteState(str(tmp_path / "remote.json"))
       state.set_mode(MODE_HOME)
       assert server.listen_plan(state.mode()) is None
   ```

3. **`tests/test_web_window.py`**
   - L17-18: import `MODE_HOME, MODE_OFF, RemoteState` from `cratebuilder.remoteauth`, and drop the `cratebuilder.server` import (nothing else in the file uses it).
   - Replace `test_the_embedded_thread_uses_the_same_bind_rule_as_the_entry_point`, `test_a_disabled_host_does_not_print_a_pairing_code`, `test_host_allow_is_persisted_by_the_entry_point` and `test_the_embedded_thread_refuses_lan_without_consent` with:

     ```python
     class _RecordingMount:
         """Stands in for RemoteMount: remembers the modes it was asked for."""

         def __init__(self, service, port=None, tailscale=None, **kwargs):
             self.applied = []
             self.stopped = False

         def apply(self, mode):
             self.applied.append(mode)
             return {"mode": mode, "tls": mode == MODE_HOME, "host": "0.0.0.0",
                     "addresses": ["192.168.1.23"], "public_url": None}

         def wait(self):
             return None

         def stop(self):
             self.stopped = True


     class _FailingMount(_RecordingMount):
         def apply(self, mode):
             raise OSError("port busy")


     def _window_service(tmp_path):
         class Svc:
             remote_state = RemoteState(str(tmp_path / "remote.json"))
             remote_mount = None
         return Svc()


     def test_the_window_hands_one_mount_to_the_service_in_the_stored_mode(
             tmp_path, monkeypatch):
         from cratebuilder import remotemount

         monkeypatch.setattr(remotemount, "RemoteMount", _RecordingMount)
         svc = _window_service(tmp_path)
         mount = web_window.start_remote_mount(svc, port=0)
         assert svc.remote_mount is mount
         assert mount.applied == [MODE_OFF]
         svc.remote_state.set_mode(MODE_HOME)
         assert web_window.start_remote_mount(svc, port=0).applied == [MODE_HOME]


     def test_a_mount_that_cannot_start_leaves_remote_access_off(
             tmp_path, monkeypatch, capsys):
         from cratebuilder import remotemount

         monkeypatch.setattr(remotemount, "RemoteMount", _FailingMount)
         svc = _window_service(tmp_path)
         svc.remote_state.set_mode(MODE_HOME)
         web_window.start_remote_mount(svc, port=0)      # must not raise
         assert svc.remote_state.mode() == MODE_OFF
         assert "port busy" in capsys.readouterr().err


     def _headless(monkeypatch, state, filled=None):
         import web_server

         monkeypatch.setattr(web_server, "RemoteMount", _RecordingMount)
         monkeypatch.setattr(web_server, "build_service",
                             lambda data_dir: type("Svc", (), {
                                 "remote_state": state,
                                 "remote_mount": None,
                                 "populate_watchlist_from_folders":
                                     lambda self: (filled if filled is not None
                                                   else []).append(1),
                             })())
         return web_server


     def test_an_off_host_refuses_to_serve_and_mints_no_code(tmp_path, monkeypatch):
         state = RemoteState(str(tmp_path / "remote.json"))
         web_server = _headless(monkeypatch, state)
         with pytest.raises(SystemExit) as exc:
             web_server.main(["--port", "0", "--pair", "--data-dir", str(tmp_path)])
         assert "set to Off" in str(exc.value.code)
         assert state.active_code() is None


     def test_a_home_host_prints_its_https_address_and_a_code(tmp_path, capsys,
                                                             monkeypatch):
         state = RemoteState(str(tmp_path / "remote.json"))
         state.set_mode(MODE_HOME)
         web_server = _headless(monkeypatch, state)
         web_server.main(["--port", "0", "--pair", "--data-dir", str(tmp_path)])
         printed = capsys.readouterr().out
         assert "Pairing code" in printed
         assert "https://192.168.1.23:0/" in printed


     def test_host_allow_is_persisted_by_the_entry_point(tmp_path, capsys,
                                                         monkeypatch):
         state = RemoteState(str(tmp_path / "remote.json"))
         state.set_mode(MODE_HOME)
         filled = []
         web_server = _headless(monkeypatch, state, filled)
         web_server.main(["--port", "0", "--data-dir", str(tmp_path),
                          "--host-allow", "https://cb.example.com/",
                          "--host-allow", "booth.tailnet.ts.net"])
         assert state.extra_hosts() == ["cb.example.com", "booth.tailnet.ts.net"]
         # The headless host owes the same first-run fill the window does.
         assert filled == [1]
         assert "cb.example.com" in capsys.readouterr().out
     ```

   - In `test_the_console_banners_survive_a_cp1252_terminal`, the loop becomes `for name in ("OFF_REFUSED",):`.

- [ ] **Step 9: Run the tests**

Run: `python -m pytest -q tests/test_remotemount.py tests/test_remote_modes.py tests/test_remote_parked.py tests/test_server.py tests/test_web_window.py`
Expected: all pass.

Run `graft grep "bind_host|--lan|LAN_REFUSED|DISABLED_NOTE"`. Expected: no hits outside this plan and the spec.

- [ ] **Step 10: Commit**

```bash
git add cratebuilder/remotemount.py cratebuilder/server.py cratebuilder/service.py \
  web_window.py web_server.py tests/test_remotemount.py tests/test_server.py \
  tests/test_web_window.py tests/test_remote_parked.py
git commit -m "feat(remote): start, stop and restart the listener live on mode change"
```

---

### Task 5: `remote.connect_info` and the QR code

**Model:** `opus`. It's a new host method on the security boundary, and must be local-only.

**Files:**
- Modify: `cratebuilder/service.py` (`LOCAL_ONLY` L118-119; `_methods` near L1369; new methods after `remote_revoke`)
- Test: create `tests/test_remote_connect.py`

**Interfaces:**
- Consumes: `RemoteMount.status()` (Task 4); `RemoteState.certificate_info()` (Task 2); `remotecert.EXPECT_WARNING_DAYS` (Task 3).
- Produces:
  - `service.qr_data_url(text) -> "data:image/svg+xml;base64,..."`
  - method `remote.connect_info` → `{"mode", "listening", "error", "url", "alternatives": [str], "qr", "expect_warning": bool, "tailscale": dict | None}`. It is local-only.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remote_connect.py`:

```python
"""remote.connect_info: what the Connect a phone panel draws, and who may ask."""
import base64
import time

import pytest

from cratebuilder import remoteauth
from cratebuilder.remoteauth import MODE_HOME, MODE_INTERNET, MODE_OFF
from cratebuilder.service import CBError, CrateBuilderService, qr_data_url
from cratebuilder.settings import Settings


class StubMount:
    def __init__(self, status):
        self._status = status

    def status(self):
        return dict(self._status)


@pytest.fixture
def service(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


def _home(service, addresses=("192.168.1.23", "10.0.0.5")):
    service.remote_state.set_mode(MODE_HOME)
    service.remote_mount = StubMount({
        "mode": MODE_HOME, "listening": True, "port": 8770, "tls": True,
        "addresses": list(addresses), "public_url": None, "tailscale": None,
        "error": None})


def test_off_has_nothing_to_show(service):
    info = service.call("remote.connect_info", {})
    assert info["mode"] == MODE_OFF
    assert info["url"] is None and info["qr"] is None


def test_home_gives_the_https_address_first_and_the_rest_as_fallbacks(service):
    _home(service)
    info = service.call("remote.connect_info", {})
    assert info["url"] == "https://192.168.1.23:8770/"
    assert info["alternatives"] == ["https://10.0.0.5:8770/"]
    assert info["listening"] is True and info["error"] is None


def test_the_qr_is_an_svg_image_of_the_address(service):
    _home(service)
    info = service.call("remote.connect_info", {})
    prefix = "data:image/svg+xml;base64,"
    assert info["qr"].startswith(prefix)
    svg = base64.b64decode(info["qr"][len(prefix):]).decode("utf-8")
    assert "<svg" in svg and "<script" not in svg


def test_a_recent_reissue_is_announced_and_an_old_one_is_not(service):
    _home(service)
    state = service.remote_state
    state.note_certificate("a" * 64)
    state.note_certificate("b" * 64)
    assert service.call("remote.connect_info", {})["expect_warning"] is True
    state._data["certificate"]["expect_warning_since"] = int(time.time()) - 15 * 86400
    assert service.call("remote.connect_info", {})["expect_warning"] is False


def test_the_mount_error_is_passed_through(service):
    _home(service)
    service.remote_mount = StubMount({"mode": MODE_HOME, "listening": False,
                                      "port": 8770, "addresses": [],
                                      "error": "port busy"})
    info = service.call("remote.connect_info", {})
    assert info["error"] == "port busy" and info["url"] is None


def test_internet_uses_the_published_address(service):
    service.remote_state.set_mode(MODE_INTERNET)
    service.remote_mount = StubMount({
        "mode": MODE_INTERNET, "listening": True, "port": 8770,
        "public_url": "https://djpc.tail1234.ts.net/",
        "tailscale": {"installed": True}, "error": None})
    info = service.call("remote.connect_info", {})
    assert info["url"] == "https://djpc.tail1234.ts.net/"
    assert info["tailscale"] == {"installed": True}
    assert info["qr"].startswith("data:image/svg+xml;base64,")


def test_a_remote_browser_may_not_ask(service):
    with pytest.raises(CBError):
        service.call("remote.connect_info", {}, transport="remote")


def test_it_is_never_a_read_method():
    assert "remote.connect_info" not in remoteauth.READ_METHODS


def test_qr_data_url_is_deterministic():
    assert qr_data_url("https://x/") == qr_data_url("https://x/")
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_remote_connect.py -q`
Expected: `ImportError: cannot import name 'qr_data_url'`.

- [ ] **Step 3: Implement**

Module level in `service.py`, near the other remote constants:

```python
def qr_data_url(text):
    """A QR code of *text* as an SVG data URL. Black on white with a quiet
    zone whatever the theme, because a phone camera needs that contrast; an
    image (not inline markup) so the page never parses host-made SVG."""
    import segno                     # deferred: only Settings ever asks
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=3,
                                     dark="#000000", light="#ffffff",
                                     xmldecl=False)
    return ("data:image/svg+xml;base64,"
            + base64.b64encode(buf.getvalue()).decode("ascii"))
```

`base64`, `io` and `time` are already imported at the top of `service.py`.

Extend `LOCAL_ONLY`:

```python
LOCAL_ONLY = ("update.", "fs.", "cookies.howto_window", "app.quit",
              "app.close_seen", "remote.connect_info")
```

Register it in `_methods()` beside `"remote.config"`:

```python
            "remote.connect_info": lambda p: self.remote_connect_info(),
```

Add after `remote_revoke`:

```python
    def remote_connect_info(self):
        """The Connect a phone panel: where a phone should go, as text and as
        a QR code, in the mode the host is actually listening in. Local only —
        a paired phone has no business re-reading the door it came through."""
        self._require_local_remote_admin()
        state = self.remote_state
        mode = state.mode()
        mount = self.remote_mount
        status = mount.status() if mount is not None else {}
        out = {"mode": mode, "listening": bool(status.get("listening")),
               "error": status.get("error"), "url": None, "alternatives": [],
               "qr": None, "expect_warning": False,
               "tailscale": status.get("tailscale")}
        if mode == remoteauth.MODE_HOME and out["listening"]:
            port = status.get("port") or 8770
            urls = [f"https://{a}:{port}/" for a in status.get("addresses") or []]
            if urls:
                out["url"], out["alternatives"] = urls[0], urls[1:]
            since = state.certificate_info().get("expect_warning_since") or 0
            out["expect_warning"] = bool(since) and (
                time.time() - since < remotecert.EXPECT_WARNING_DAYS * 86400)
        elif mode == remoteauth.MODE_INTERNET:
            out["url"] = status.get("public_url")
        if out["url"]:
            out["qr"] = qr_data_url(out["url"])
        return out
```

Add `from cratebuilder import remotecert` to the imports.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest -q tests/test_remote_connect.py tests/test_remote_modes.py tests/test_server.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/service.py tests/test_remote_connect.py
git commit -m "feat(remote): connect info with a QR code for the Settings card"
```

---

### Task 6: Connect-a-phone panel (Home network)

**Model:** `opus`. It's `web/app.js`.

**Files:**
- Modify: `web/app.js` (new top-level `paintRemoteConnect` placed directly before `/* Mark a control that reads rather than writes` ≈L5499; new constants beside `REMOTE_MODE_HINTS`; the Remote Access card builder ≈L5644-5800)
- Modify: `web/app.css` (Remote Access card rules)
- Test: create `tests/test_web_remote_client.py`

**Interfaces:**
- Consumes: `remote.connect_info` (Task 5); the event `remote.mode` (Task 4).
- Produces:
  - `paintRemoteConnect(box, info, actions)`: a top-level function in `app.js` that Task 9 extends;
  - the DOM ids `remote-connect`, `remote-qr`, `remote-url`, `remote-alts`, `remote-rule`, `remote-firewall`, `remote-expect`, `remote-error`;
  - the constants `REMOTE_WARNING_RULE`, `REMOTE_FIREWALL_NOTE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_remote_client.py`:

```python
"""web/app.js: the Remote Access card's Connect a phone panel, client-side.

Same method as tests/test_web_about_client.py: the real function is sliced
out of app.js verbatim and run in Node against a stub DOM.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")


@pytest.fixture(scope="module")
def app_js():
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _slice(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


def _run_node(tmp_path, source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / "remote.cjs"
    script.write_text(source, encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


_HARNESS = """
function makeEl(tag) {
  return {
    tag, children: [], attrs: {}, style: {}, listeners: {},
    className: '', textContent: '', id: '', hidden: false, src: '', alt: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    replaceChildren(...cs) { this.children = cs; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(n, fn) { this.listeners[n] = fn; },
  };
}
const document = { createElement: makeEl };
function tagNode(text, cls) { const s = makeEl('span'); s.textContent = text; s.className = 'cb-tag ' + cls; return s; }
%(slices)s
function draw(info) {
  const clicked = [];
  const box = makeEl('div');
  paintRemoteConnect(box, info, {
    onGuide() { clicked.push('guide'); },
    onCheck() { clicked.push('check'); },
  });
  const byId = {};
  const rows = [];
  (function walk(el) {
    if (el.id) byId[el.id] = el;
    if (String(el.className).includes('cb-remote-step')) {
      rows.push(el.children.map((c) => c.textContent).join(' '));
    }
    el.children.forEach(walk);
  })(box);
  // Task 9's checklist buttons, pressed in order, if this drawing has them.
  ['remote-ts-guide', 'remote-ts-check'].forEach((id) => {
    if (byId[id] && byId[id].listeners.click) byId[id].listeners.click();
  });
  return { hidden: box.hidden, ids: Object.keys(byId).sort(), rows, clicked,
           text: Object.fromEntries(Object.entries(byId).map(([k, v]) => [k, v.textContent])),
           qr: byId['remote-qr'] ? byId['remote-qr'].src : null };
}
const HOME = { mode: 'home', listening: true, error: null,
               url: 'https://192.168.1.23:8770/', alternatives: ['https://10.0.0.5:8770/'],
               qr: 'data:image/svg+xml;base64,AAAA', expect_warning: false, tailscale: null };
console.log(JSON.stringify({
  off: draw({ mode: 'off' }),
  none: draw(null),
  home: draw(HOME),
  expect: draw(Object.assign({}, HOME, { expect_warning: true })),
  broken: draw({ mode: 'home', listening: false, error: 'port busy', url: null,
                 alternatives: [], qr: null, expect_warning: false }),
}));
"""


def _slices(app_js):
    # The rule text, the firewall note (and, from Task 9, the Tailscale
    # checklist) sit directly above paintRemoteConnect, so one cut carries
    # every name it uses.
    return _slice(app_js, "  const REMOTE_WARNING_RULE =",
                  "  /* Mark a control that reads rather than writes")


def test_off_and_no_info_hide_the_panel(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    assert r["off"]["hidden"] is True and r["off"]["ids"] == []
    assert r["none"]["hidden"] is True


def test_home_draws_the_qr_address_fallbacks_and_the_rule(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    home = r["home"]
    assert home["hidden"] is False
    assert home["qr"] == "data:image/svg+xml;base64,AAAA"
    assert home["text"]["remote-url"] == "https://192.168.1.23:8770/"
    assert "https://10.0.0.5:8770/" in home["text"]["remote-alts"]
    assert "Tap through once" in home["text"]["remote-rule"]
    assert "click Allow" in home["text"]["remote-firewall"]
    assert "remote-expect" not in home["ids"]


def test_a_reissue_is_announced(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    assert "warn once more" in r["expect"]["text"]["remote-expect"]


def test_a_listener_that_failed_says_why(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    assert r["broken"]["text"]["remote-error"] == "port busy"
    assert "remote-qr" not in r["broken"]["ids"]


def test_the_card_asks_for_connect_info_and_follows_the_mode(app_js):
    assert "call('remote.connect_info')" in app_js
    assert "cbApi.on('remote.mode'" in app_js
    assert "syncSettingControls('remote_mode')" in app_js
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_web_remote_client.py -q`
Expected: FAIL with `ValueError: substring not found` (the function does not exist yet).

- [ ] **Step 3: Add the constants and `paintRemoteConnect`**

Insert directly before `  /* Mark a control that reads rather than writes, so the read-only sweep in`:

```js
  /* The one rule that makes the Home network mode's own certificate safe:
     a phone warns once, and any warning the panel has not announced is the
     user's signal to stop (spec D3). */
  const REMOTE_WARNING_RULE = 'The first time, your phone will warn that the ' +
    'connection isn\u2019t private. Tap through once. If you ever see that ' +
    'warning again and this panel hasn\u2019t told you to expect it, don\u2019t.';
  const REMOTE_FIREWALL_NOTE = 'If Windows asks whether to allow ' +
    'DJ-CrateBuilder on your network, click Allow (Private networks).';

  /* Connect a phone: everything a phone needs to reach this host in the mode
     it is in. Drawn from remote.connect_info alone, so the card never shows
     an address the host is not actually listening on. */
  function paintRemoteConnect(box, info, actions) {
    actions = actions || {};
    box.replaceChildren();
    box.hidden = !info || info.mode === 'off';
    if (box.hidden) return;
    const add = (tag, cls, text, id) => {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (id) el.id = id;
      if (text != null) el.textContent = text;
      box.appendChild(el);
      return el;
    };
    if (info.error) add('div', 'cb-warnbox', info.error, 'remote-error');
    if (info.mode === 'internet' && !info.url) {
      add('div', 'cb-mut', 'Tailscale is not ready yet.', 'remote-tailscale');
      return;
    }
    if (info.url) {
      const qr = add('img', 'cb-remote-qr', null, 'remote-qr');
      qr.src = info.qr;
      qr.alt = 'QR code for ' + info.url;
      add('div', 'cb-mono cb-remote-url', info.url, 'remote-url');
    }
    if (info.alternatives && info.alternatives.length) {
      add('div', 'cb-mut cb-remote-note',
        'Not working? Try: ' + info.alternatives.join('  \u00b7  '), 'remote-alts');
    }
    if (info.mode === 'home') {
      if (info.expect_warning) {
        add('div', 'cb-warnbox', 'Your phones will warn once more \u2014 this ' +
          'is expected. This PC\u2019s certificate changed (a new network ' +
          'address, or its yearly renewal).', 'remote-expect');
      }
      add('div', 'cb-mut cb-remote-note', REMOTE_WARNING_RULE, 'remote-rule');
      add('div', 'cb-mut cb-remote-note', REMOTE_FIREWALL_NOTE, 'remote-firewall');
    }
  }

```

- [ ] **Step 4: Put the panel in the card, and follow the mode**

In the Remote Access card builder, after `card.appendChild(hint);`, add:

```js
      const connectBox = document.createElement('div');
      connectBox.className = 'cb-remote-connect';
      connectBox.id = 'remote-connect';
      connectBox.hidden = true;
      card.appendChild(connectBox);
      const connectActions = {};
      /* Local window only: the address and QR are how a new phone gets in,
         which a paired phone has no business reading back. */
      function paintConnect() {
        if (!local || parked) { paintRemoteConnect(connectBox, null); return; }
        call('remote.connect_info')
          .then((info) => paintRemoteConnect(connectBox, info, connectActions))
          .catch(() => {});
      }
```

Replace

```js
      remoteCard.off = cbApi.on('remote.devices', () => {
        call('remote.config').then(paint).catch(() => {});
      });
```

with:

```js
      const offDevices = cbApi.on('remote.devices', () => {
        call('remote.config').then(paint).catch(() => {});
      });
      /* A mode change re-plans the host's listener, and the address, QR and
         warning rule all follow it — pushed, so the card never polls. The
         Mode select follows too: a mode that could not start comes back as
         Off, which save() alone would not show. */
      const offMode = cbApi.on('remote.mode', (p) => {
        const label = REMOTE_MODE_LABELS[(p && p.mode) || 'off'];
        if (label && state.settings) {
          state.settings.remote_mode = label;
          syncSettingControls('remote_mode');
        }
        call('remote.config').then(paint).catch(() => {});
        paintConnect();
      });
      remoteCard.off = () => { offDevices(); offMode(); };
```

After the card's initial `call('remote.config').then(paint)…` chain, add a first `paintConnect();` call.

- [ ] **Step 5: Style it**

Add to `web/app.css` directly after the existing Remote Access rules (`.cb-remote-parked` at L547, `.cb-card.is-parked .cb-parked-dim` at L551):

```css
/* Connect a phone (Settings ▸ Remote Access). The QR image carries its own
   white quiet zone (made host-side), so it needs no themed background. */
.cb-remote-connect { display: grid; gap: 8px; justify-items: start; margin-top: 4px; }
.cb-remote-connect[hidden] { display: none; }
.cb-remote-qr { width: 176px; height: 176px; border-radius: var(--cb-radius); }
.cb-remote-url { font-size: 13px; color: var(--cb-text); user-select: all; }
.cb-remote-note { font-size: 11.5px; line-height: 1.6; max-width: 62ch; }
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest -q tests/test_web_*_client.py`
Expected: all pass, including `tests/test_web_remote_client.py`.

- [ ] **Step 7: Verify it visually in both themes**

The preview harness has to lift the kill switch without touching shipped code. Its launcher sets `remoteauth.REMOTE_ACCESS_AVAILABLE = True` and calls `state.set_mode("home")` before `web_server.main([...])`. It must **not** call the old `set_flag("enabled", True)`, which no longer exists.

On the local page, set Settings ▸ Remote Access ▸ Mode = *Home network*. Confirm:
- the QR, the `https://…:8770/` address and the rule all appear;
- switching to *Off* hides the panel.

Screenshot both light and dark.

- [ ] **Step 8: Commit**

```bash
git add web/app.js web/app.css tests/test_web_remote_client.py
git commit -m "feat(remote): Connect a phone panel with QR code in Settings"
```

---

### Task 7: The Tailscale module

**Model:** Step 1 is the controller with the maintainer, since it needs Tailscale installed. Steps 2 onward are `opus` (it runs an external CLI with network consequences).

**Files:**
- Create: `cratebuilder/tailscale.py`
- Test: create `tests/test_tailscale.py`
- Modify: spec §8 (record the verified CLI syntax)

**Interfaces:**
- Produces:
  - `tailscale.find_cli(_which=shutil.which, _exists=os.path.isfile, _platform=sys.platform) -> str | None`
  - `tailscale.status(cli=None, _run=None) -> dict` with keys `installed, running, signed_in, magicdns, https, dns_name, error`. It never raises.
  - `tailscale.ready(st) -> bool`
  - `tailscale.publish(port, st, cli=None, _run=None) -> str` (the https URL); raises `TailscaleError`
  - `tailscale.unpublish(port, st, cli=None, _run=None) -> None`. It never raises.
  - `class TailscaleError(Exception)`

- [ ] **Step 1: The maintainer installs Tailscale, and the controller verifies the CLI**

The maintainer:
1. Installs Tailscale on this PC from https://tailscale.com/download and signs in.
2. In https://login.tailscale.com/admin/dns, turns on **MagicDNS** and **HTTPS Certificates**.

The controller runs each command, pasting the JSON shapes (with names redacted) into spec §8:

```
"C:\Program Files\Tailscale\tailscale.exe" version
"C:\Program Files\Tailscale\tailscale.exe" status --json
"C:\Program Files\Tailscale\tailscale.exe" serve --help
"C:\Program Files\Tailscale\tailscale.exe" serve --bg --https=443 http://127.0.0.1:8799
"C:\Program Files\Tailscale\tailscale.exe" serve status --json
"C:\Program Files\Tailscale\tailscale.exe" serve --https=443 off
"C:\Program Files\Tailscale\tailscale.exe" serve status --json
```

Confirm each of these, and change the constants and parsing in Step 3 to match anything that differs:
- `status --json` has `BackendState`, `Self.DNSName`, `CurrentTailnet.MagicDNSEnabled` and `CertDomains`;
- the `serve` syntax above is accepted;
- `serve status --json` shows the proxy under `Web["<dns-name>:443"].Handlers["/"].Proxy`;
- `serve --https=443 off` removes it.

Commit the spec note: `docs(specs): record the verified tailscale CLI shapes`.

- [ ] **Step 2: Write the failing tests (fake CLI, no real Tailscale)**

Create `tests/test_tailscale.py`:

```python
"""tailscale: find the CLI, read its status, publish the app through it."""
import json
import subprocess

import pytest

from cratebuilder import tailscale

READY = {"BackendState": "Running",
         "Self": {"DNSName": "djpc.tail1234.ts.net."},
         "CurrentTailnet": {"MagicDNSEnabled": True},
         "CertDomains": ["djpc.tail1234.ts.net"]}


class FakeCli:
    """Answers `tailscale <args>` from a table; records every call."""

    def __init__(self, status=READY, serve=None, fail=()):
        self.status = status
        self.serve = serve if serve is not None else {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, cli, args):
        self.calls.append(list(args))
        key = args[0] if args[0] != "serve" else " ".join(args[:2])
        if key in self.fail:
            return subprocess.CompletedProcess(args, 1, "", "nope")
        if args[:2] == ["status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.status), "")
        if args[:3] == ["serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.serve), "")
        if args[0] == "serve" and args[-1] == "off":
            self.serve = {}
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "serve":
            self.serve = {"Web": {"djpc.tail1234.ts.net:443": {"Handlers": {
                "/": {"Proxy": args[-1]}}}}}
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unknown")


def test_find_cli_prefers_the_windows_install():
    assert tailscale.find_cli(_which=lambda n: None, _exists=lambda p: True,
                              _platform="win32") == tailscale.WINDOWS_CLI
    assert tailscale.find_cli(_which=lambda n: "/usr/bin/tailscale",
                              _exists=lambda p: False,
                              _platform="linux") == "/usr/bin/tailscale"
    assert tailscale.find_cli(_which=lambda n: None, _exists=lambda p: False,
                              _platform="win32") is None


def test_not_installed():
    st = tailscale.status(cli=None, _find=lambda: None)
    assert st["installed"] is False and tailscale.ready(st) is False


def test_a_ready_tailnet():
    st = tailscale.status(cli="ts", _run=FakeCli())
    assert st == {"installed": True, "running": True, "signed_in": True,
                  "magicdns": True, "https": True,
                  "dns_name": "djpc.tail1234.ts.net", "error": None}
    assert tailscale.ready(st) is True


def test_signed_out_and_settings_off():
    st = tailscale.status(cli="ts", _run=FakeCli(status={"BackendState": "NeedsLogin"}))
    assert st["installed"] is True and st["signed_in"] is False
    assert st["magicdns"] is False and st["https"] is False
    st = tailscale.status(cli="ts", _run=FakeCli(status={
        **READY, "CurrentTailnet": {"MagicDNSEnabled": False}, "CertDomains": None}))
    assert st["magicdns"] is False and st["https"] is False
    assert tailscale.ready(st) is False


def test_a_cli_that_fails_or_prints_junk_never_raises():
    assert tailscale.status(cli="ts", _run=FakeCli(fail={"status"}))["error"]
    junk = lambda cli, args: subprocess.CompletedProcess(args, 0, "{not json", "")
    assert tailscale.status(cli="ts", _run=junk)["running"] is False


def test_publish_points_https_at_the_loopback_listener():
    fake = FakeCli()
    st = tailscale.status(cli="ts", _run=fake)
    url = tailscale.publish(8770, st, cli="ts", _run=fake)
    assert url == "https://djpc.tail1234.ts.net/"
    assert ["serve", "--bg", "--https=443", "http://127.0.0.1:8770"] in fake.calls


def test_publish_again_is_a_no_op():
    fake = FakeCli()
    st = tailscale.status(cli="ts", _run=fake)
    tailscale.publish(8770, st, cli="ts", _run=fake)
    before = len(fake.calls)
    tailscale.publish(8770, st, cli="ts", _run=fake)
    assert not [c for c in fake.calls[before:] if c[:2] == ["serve", "--bg"]]


def test_publish_refuses_to_overwrite_something_else():
    fake = FakeCli(serve={"Web": {"djpc.tail1234.ts.net:443": {"Handlers": {
        "/": {"Proxy": "http://127.0.0.1:3000"}}}}})
    st = tailscale.status(cli="ts", _run=fake)
    with pytest.raises(tailscale.TailscaleError, match="already"):
        tailscale.publish(8770, st, cli="ts", _run=fake)


def test_unpublish_removes_only_our_entry():
    fake = FakeCli()
    st = tailscale.status(cli="ts", _run=fake)
    tailscale.publish(8770, st, cli="ts", _run=fake)
    tailscale.unpublish(8770, st, cli="ts", _run=fake)
    assert ["serve", "--https=443", "off"] in fake.calls
    other = FakeCli(serve={"Web": {"djpc.tail1234.ts.net:443": {"Handlers": {
        "/": {"Proxy": "http://127.0.0.1:3000"}}}}})
    tailscale.unpublish(8770, st, cli="ts", _run=other)
    assert ["serve", "--https=443", "off"] not in other.calls
```

- [ ] **Step 3: Write `cratebuilder/tailscale.py`**

```python
"""Tailscale as the Over the internet mode sees it: find the CLI, read its status, publish the app through it."""
import json
import os
import shutil
import subprocess
import sys

WINDOWS_CLI = r"C:\Program Files\Tailscale\tailscale.exe"
TIMEOUT = 8
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_TARGET = "http://127.0.0.1:{port}"


class TailscaleError(Exception):
    """A publish Tailscale refused, in words for the Settings card."""


def find_cli(_which=shutil.which, _exists=os.path.isfile, _platform=sys.platform):
    if _platform == "win32" and _exists(WINDOWS_CLI):
        return WINDOWS_CLI
    return _which("tailscale")


def _subprocess_run(cli, args):
    try:
        return subprocess.run([cli, *args], capture_output=True, text=True,
                              timeout=TIMEOUT, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _json(result):
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def status(cli=None, _run=None, _find=None):
    """The checklist's ticks. Never raises: a missing, stopped or confused
    Tailscale is an answer the card shows, not an error."""
    out = {"installed": False, "running": False, "signed_in": False,
           "magicdns": False, "https": False, "dns_name": "", "error": None}
    cli = cli or (_find or find_cli)()
    if not cli:
        return out
    out["installed"] = True
    data = _json((_run or _subprocess_run)(cli, ["status", "--json"]))
    if data is None:
        out["error"] = "Tailscale did not answer. Is it running?"
        return out
    backend = data.get("BackendState")
    out["running"] = backend == "Running"
    out["signed_in"] = backend in ("Running", "Starting", "Stopped")
    out["dns_name"] = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    out["magicdns"] = bool((data.get("CurrentTailnet") or {}).get("MagicDNSEnabled"))
    out["https"] = bool(data.get("CertDomains"))
    return out


def ready(st):
    return bool(st and st.get("installed") and st.get("running")
                and st.get("signed_in") and st.get("magicdns")
                and st.get("https") and st.get("dns_name"))


def _handlers(cli, st, run):
    config = _json(run(cli, ["serve", "status", "--json"])) or {}
    entry = (config.get("Web") or {}).get(f"{st['dns_name']}:443") or {}
    return entry.get("Handlers") or {}


def _is_ours(handlers, port):
    return (list(handlers) == ["/"]
            and (handlers["/"] or {}).get("Proxy") == _TARGET.format(port=port))


def publish(port, st, cli=None, _run=None):
    """Point this PC's Tailscale https address at the loopback listener."""
    run = _run or _subprocess_run
    cli = cli or find_cli()
    handlers = _handlers(cli, st, run)
    url = f"https://{st['dns_name']}/"
    if handlers and not _is_ours(handlers, port):
        raise TailscaleError(
            "Tailscale is already publishing something else on this PC's "
            "https address. Remove it (tailscale serve reset) or use Home "
            "network mode instead.")
    if handlers:
        return url
    result = run(cli, ["serve", "--bg", "--https=443", _TARGET.format(port=port)])
    if result is None or result.returncode != 0:
        detail = (result.stderr or "").strip() if result is not None else ""
        raise TailscaleError("Tailscale would not publish the app"
                             + (f": {detail}" if detail else "."))
    return url


def unpublish(port, st, cli=None, _run=None):
    """Remove our https entry — and only ours. Never raises."""
    run = _run or _subprocess_run
    cli = cli or find_cli()
    if not cli or not st or not st.get("dns_name"):
        return
    try:
        if _is_ours(_handlers(cli, st, run), port):
            run(cli, ["serve", "--https=443", "off"])
    except Exception:
        pass
```

If Step 1 found a different syntax or JSON shape, adjust `_TARGET`, the `serve` argument lists, `_handlers` and the fake CLI in the tests to match what was recorded.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_tailscale.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/tailscale.py tests/test_tailscale.py
git commit -m "feat(remote): Tailscale status, publish and unpublish"
```

---

### Task 8: Internet mode on the host

**Model:** `opus`. It spans multiple files and decides exposure.

**Files:**
- Modify: `cratebuilder/remotemount.py` (`apply`, `_stop_locked`)
- Modify: `cratebuilder/service.py` (`LOCAL_ONLY`; `_methods`; new `remote_tailscale_check`)
- Modify: `web_window.py` (`start_remote_mount`: pass `tailscale=tailscale`, as written in Task 4 Step 6)
- Modify: `web_server.py` (`RemoteMount(..., tailscale=tailscale)`)
- Test: extend `tests/test_remotemount.py` and `tests/test_remote_connect.py`

**Interfaces:**
- Consumes: `tailscale.status`, `ready`, `publish`, `unpublish`, `TailscaleError` (Task 7).
- Produces:
  - `RemoteMount.status()["tailscale"]` (the status dict) and `["public_url"]`;
  - `RemoteMount.refresh_tailscale() -> dict`: re-reads Tailscale and publishes if it has become ready, **without restarting the listener**, so connected phones stay connected;
  - the method `remote.tailscale_check` → the same shape as `remote.connect_info`. It is local-only.
  - `remote.connect_info` in Internet mode re-checks Tailscale by itself while nothing is published yet. That covers the spec's "opening Settings" moment.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_remotemount.py`:

```python
class FakeTailscale:
    TailscaleError = type("TailscaleError", (Exception,), {})

    def __init__(self, ready=True, conflict=False):
        self._ready, self._conflict = ready, conflict
        self.published, self.unpublished = [], []

    def status(self):
        return {"installed": True, "running": self._ready, "signed_in": True,
                "magicdns": self._ready, "https": self._ready,
                "dns_name": "djpc.tail1234.ts.net", "error": None}

    def ready(self, st):
        return self._ready

    def publish(self, port, st):
        if self._conflict:
            raise self.TailscaleError("already publishing something else")
        self.published.append(port)
        return "https://djpc.tail1234.ts.net/"

    def unpublish(self, port, st):
        self.unpublished.append(port)


def _internet_mount(service, made, ts):
    m = RemoteMount(service, port=8799, tailscale=ts,
                    make_server=lambda app, host, port, ssl: FakeServer(app, host, port, ssl, made),
                    addresses=lambda: [], hostname=lambda: "DJPC")
    service.remote_mount = m
    return m


def test_internet_publishes_through_tailscale_and_trusts_its_name(service, made):
    ts = FakeTailscale()
    m = _internet_mount(service, made, ts)
    status = m.apply(MODE_INTERNET)
    assert ts.published == [8799]
    assert status["public_url"] == "https://djpc.tail1234.ts.net/"
    assert "djpc.tail1234.ts.net" in service.remote_state.extra_hosts()
    m.apply(MODE_OFF)
    assert ts.unpublished == [8799]


def test_internet_without_a_ready_tailscale_publishes_nothing(service, made):
    ts = FakeTailscale(ready=False)
    status = _internet_mount(service, made, ts).apply(MODE_INTERNET)
    assert ts.published == [] and status["public_url"] is None
    assert status["tailscale"]["https"] is False
    assert status["listening"] is True          # loopback only, harmless


def test_a_publish_conflict_is_shown_not_raised(service, made):
    ts = FakeTailscale(conflict=True)
    status = _internet_mount(service, made, ts).apply(MODE_INTERNET)
    assert "already" in status["error"] and status["public_url"] is None


def test_a_refresh_publishes_without_restarting_the_listener(service, made):
    """Check again must not cut a phone that is already connected."""
    ts = FakeTailscale(ready=False)
    m = _internet_mount(service, made, ts)
    m.apply(MODE_INTERNET)
    ts._ready = True
    status = m.refresh_tailscale()
    assert status["public_url"] == "https://djpc.tail1234.ts.net/"
    assert len(made) == 1 and made[0].should_exit is False


def test_a_refresh_outside_internet_mode_does_nothing(service, made):
    ts = FakeTailscale()
    m = _internet_mount(service, made, ts)
    m.apply(MODE_HOME)
    assert m.refresh_tailscale()["public_url"] is None
    assert ts.published == []
```

Append to `tests/test_remote_connect.py`:

```python
class RefreshingMount(StubMount):
    def __init__(self, status, after):
        super().__init__(status)
        self._after = after
        self.refreshed = 0

    def refresh_tailscale(self):
        self.refreshed += 1
        self._status = dict(self._after)
        return dict(self._status)


_NOT_READY = {"mode": MODE_INTERNET, "listening": True, "port": 8770,
              "public_url": None, "tailscale": {"installed": False}, "error": None}
_READY = dict(_NOT_READY, public_url="https://djpc.tail1234.ts.net/",
              tailscale={"installed": True})


def test_tailscale_check_re_reads_tailscale_and_answers_like_connect_info(service):
    service.remote_state.set_mode(MODE_INTERNET)
    mount = RefreshingMount(_NOT_READY, _READY)
    service.remote_mount = mount
    info = service.call("remote.tailscale_check", {})
    assert mount.refreshed >= 1
    assert info["url"] == "https://djpc.tail1234.ts.net/"


def test_opening_settings_re_checks_an_unpublished_tailscale(service):
    service.remote_state.set_mode(MODE_INTERNET)
    mount = RefreshingMount(_NOT_READY, _NOT_READY)
    service.remote_mount = mount
    service.call("remote.connect_info", {})
    assert mount.refreshed == 1


def test_a_published_address_is_not_re_checked_on_every_look(service):
    service.remote_state.set_mode(MODE_INTERNET)
    mount = RefreshingMount(_READY, _READY)
    service.remote_mount = mount
    service.call("remote.connect_info", {})
    assert mount.refreshed == 0


def test_tailscale_check_is_local_only():
    from cratebuilder.service import LOCAL_ONLY
    assert "remote.tailscale_check" in LOCAL_ONLY
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest -q tests/test_remotemount.py tests/test_remote_connect.py`
Expected: the new tests fail: `published == []`, `RemoteMount` has no `refresh_tailscale`, and `remote.tailscale_check` is unknown.

- [ ] **Step 3: Implement the Tailscale half of `RemoteMount`**

In `__init__`, add `self._published = None`.

In `apply`, after `status.update(listening=True, host=host, tls=tls)` and before `return dict(status)`, insert:

```python
            if mode == MODE_INTERNET and self._tailscale is not None:
                self._publish_locked(status)
```

Add this method:

```python
    def _publish_locked(self, status):
        """Publish the loopback listener through Tailscale, once it is ready.

        A Tailscale that is not ready is not an error — the card shows the
        checklist from status["tailscale"]. A publish Tailscale refuses is
        shown too, but the listener stays: it is on loopback, reachable by
        nothing until the user fixes what the message names.
        """
        ts = self._tailscale
        st = ts.status()
        status["tailscale"] = st
        if not ts.ready(st):
            return
        try:
            status["public_url"] = ts.publish(self._port, st)
        except ts.TailscaleError as exc:
            status["error"] = str(exc)
            return
        self._published = st
        self._state.add_extra_hosts([st["dns_name"]])

    def refresh_tailscale(self):
        """Re-read Tailscale and publish if it has become ready — without
        restarting the listener, so a phone already connected stays
        connected. What "Check again" and opening Settings do."""
        with self._lock:
            status = self._status
            if (status["mode"] != MODE_INTERNET or not status["listening"]
                    or self._tailscale is None):
                return dict(status)
            status["error"] = None
            status["public_url"] = None
            self._publish_locked(status)
            return dict(status)
```

`publish()` returns at once when our entry is already there, so a refresh while published only re-reads the status.

The host allow-list is read on every request (`server.create_app`'s `allowed_names`, L273), so a `*.ts.net` name added after the listener started is honoured at once.

At the top of `_stop_locked`, before the server is stopped, add:

```python
        if self._published is not None and self._tailscale is not None:
            self._tailscale.unpublish(self._port, self._published)
        self._published = None
```

Add `MODE_INTERNET` to the `cratebuilder.remoteauth` import.

`FakeTailscale` exposes `TailscaleError` as an attribute, and so does the real module (`tailscale.TailscaleError`). So `ts.TailscaleError` works for both.

- [ ] **Step 4: Add `remote.tailscale_check` to the service**

Extend `LOCAL_ONLY` with `"remote.tailscale_check"`. Register it:

```python
            "remote.tailscale_check": lambda p: self.remote_tailscale_check(),
```

Add after `remote_connect_info`:

```python
    def remote_tailscale_check(self):
        """"Check again" on the Tailscale checklist: re-read Tailscale and
        publish if it is ready now, then answer exactly what
        remote.connect_info would. Never restarts the listener."""
        self._require_local_remote_admin()
        if self.remote_mount is not None:
            self.remote_mount.refresh_tailscale()
        return self.remote_connect_info()
```

In `remote_connect_info`, replace

```python
        status = mount.status() if mount is not None else {}
```

with:

```python
        status = mount.status() if mount is not None else {}
        if (mode == remoteauth.MODE_INTERNET and mount is not None
                and not status.get("public_url")):
            # Opening Settings is one of the moments the checklist re-checks
            # (spec §3): a Tailscale set up since the last look publishes now.
            status = mount.refresh_tailscale()
```

The Task 5 `StubMount` has no `refresh_tailscale`. Task 5's only Internet-mode test already has a `public_url`, so it never reaches this branch.

- [ ] **Step 5: Pass the real module from both entry points**

In `web_window.start_remote_mount`, add `from cratebuilder import tailscale` beside the other deferred imports and change `RemoteMount(service, port=port)` to `RemoteMount(service, port=port, tailscale=tailscale)`. In `web_server.py`, change `RemoteMount(service, port=args.port)` to `RemoteMount(service, port=args.port, tailscale=tailscale)` and add `from cratebuilder import tailscale`. Task 4's banner already prefers `status["public_url"]` when there is one, so the headless console shows the `https://….ts.net/` address with no further change.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest -q tests/test_remotemount.py tests/test_remote_connect.py tests/test_tailscale.py tests/test_web_window.py`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add cratebuilder/remotemount.py cratebuilder/service.py web_window.py web_server.py \
  tests/test_remotemount.py tests/test_remote_connect.py tests/test_web_window.py
git commit -m "feat(remote): publish Internet mode through Tailscale"
```

---

### Task 9: Tailscale checklist and setup guide

**Model:** `opus`. It's `web/app.js`.

**Files:**
- Modify: `web/app.js` (`paintRemoteConnect`'s internet branch; new `paintTailscaleChecklist` directly above it; new `openTailscaleGuide` beside `openRevokeDevices` ≈L5506; `connectActions` in the card builder)
- Modify: `web/app.css`
- Modify: spec §1 item 4, where "a guide window like the cookie guide" becomes "a step-by-step guide dialog"
- Test: extend `tests/test_web_remote_client.py`

**Interfaces:**
- Consumes: `remote.tailscale_check` (Task 8); `fs.open_url` via the existing `openUrl(url)` helper (`graft grep "function openUrl"` confirms its name); `openModal`, `modalNote`, `modalButton`, `tagNode`.
- Produces:
  - `paintTailscaleChecklist(box, ts, actions)`, with ids `remote-tailscale`, `remote-ts-guide`, `remote-ts-check`;
  - `openTailscaleGuide()`;
  - the `actions` contract `{ onGuide(): void, onCheck(): void }`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web_remote_client.py`, the harness from Task 6 already presses the checklist buttons and collects the checklist rows. `_slices` already cuts from `REMOTE_WARNING_RULE` to the `Mark a control` comment, which will also cover `TAILSCALE_STEPS` and `paintTailscaleChecklist` once they sit between those two points. Add these two cases to the object the harness prints, after `broken`:

```js
  tsMissing: draw({ mode: 'internet', listening: true, error: null, url: null,
                    tailscale: { installed: false, running: false, signed_in: false,
                                 magicdns: false, https: false, dns_name: '' } }),
  tsReady: draw({ mode: 'internet', listening: true, error: null,
                  url: 'https://djpc.tail1234.ts.net/', qr: 'data:image/svg+xml;base64,BBBB',
                  alternatives: [], tailscale: { installed: true, running: true,
                  signed_in: true, magicdns: true, https: true,
                  dns_name: 'djpc.tail1234.ts.net' } }),
```

Then add the tests:

```python
def test_internet_without_tailscale_shows_the_checklist_not_a_qr(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    missing = r["tsMissing"]
    assert "remote-qr" not in missing["ids"]
    assert "remote-ts-guide" in missing["ids"] and "remote-ts-check" in missing["ids"]
    assert len(missing["rows"]) == 4
    assert all("To do" in row for row in missing["rows"])


def test_the_checklist_buttons_reach_the_card(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    assert r["tsMissing"]["clicked"] == ["guide", "check"]


def test_a_ready_tailscale_shows_the_qr(app_js, tmp_path):
    r = _run_node(tmp_path, _HARNESS % {"slices": _slices(app_js)})
    ready = r["tsReady"]
    assert ready["qr"] == "data:image/svg+xml;base64,BBBB"
    assert ready["text"]["remote-url"] == "https://djpc.tail1234.ts.net/"
    assert "remote-rule" not in ready["ids"]      # the home-only warning rule


def test_the_guide_opens_the_two_tailscale_pages_through_the_host(app_js):
    body = _slice(app_js, "  function openTailscaleGuide() {", "\n  }\n")
    assert "openUrl('https://tailscale.com/download')" in body
    assert "openUrl('https://login.tailscale.com/admin/dns')" in body
    assert "Google Play" in body
    assert "fetch(" not in body
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_web_remote_client.py -q`
Expected: the new tests fail (no checklist ids).

- [ ] **Step 3: Implement the checklist**

Directly after `REMOTE_FIREWALL_NOTE` and before `paintRemoteConnect`, add:

```js
  /* What Over the internet needs from Tailscale, in the order a user fixes
     it. Ticked from tailscale.status() on the host, never guessed here. */
  const TAILSCALE_STEPS = [
    [(ts) => ts.installed, 'Tailscale is installed on this PC'],
    [(ts) => ts.signed_in && ts.running, 'You\u2019re signed in and connected'],
    [(ts) => ts.magicdns, 'MagicDNS is on in your Tailscale account'],
    [(ts) => ts.https, 'HTTPS certificates are on in your Tailscale account'],
  ];

  function paintTailscaleChecklist(box, ts, actions) {
    const list = document.createElement('div');
    list.className = 'cb-remote-steps';
    list.id = 'remote-tailscale';
    TAILSCALE_STEPS.forEach(([done, label]) => {
      const ok = !!done(ts || {});
      const row = document.createElement('div');
      row.className = 'cb-row cb-remote-step';
      const text = document.createElement('span');
      text.textContent = label;
      row.append(tagNode(ok ? 'Done' : 'To do', ok ? 'cb-tag--ok' : 'cb-tag--attn'), text);
      list.appendChild(row);
    });
    box.appendChild(list);
    const buttons = document.createElement('div');
    buttons.className = 'cb-row';
    const guide = document.createElement('button');
    guide.className = 'cb-btn cb-btn--sm';
    guide.id = 'remote-ts-guide';
    guide.textContent = 'Open setup guide';
    guide.addEventListener('click', () => actions.onGuide && actions.onGuide());
    const check = document.createElement('button');
    check.className = 'cb-btn cb-btn--sm cb-btn--quiet';
    check.id = 'remote-ts-check';
    check.textContent = 'Check again';
    check.addEventListener('click', () => actions.onCheck && actions.onCheck());
    buttons.append(guide, check);
    box.appendChild(buttons);
  }
```

In `paintRemoteConnect`, replace

```js
    if (info.mode === 'internet' && !info.url) {
      add('div', 'cb-mut', 'Tailscale is not ready yet.', 'remote-tailscale');
      return;
    }
```

with:

```js
    if (info.mode === 'internet' && !info.url) {
      paintTailscaleChecklist(box, info.tailscale, actions);
      return;
    }
```

- [ ] **Step 4: Implement the guide and wire the actions**

Beside `openRevokeDevices`, add:

```js
  /* The four things a user does once, outside this app, to make Over the
     internet work. Links go through openUrl, so they open in the host's own
     browser (fs.open_url) — never a fetch from this page. */
  function openTailscaleGuide() {
    openModal({
      title: 'Set up Tailscale',
      width: 520,
      body(bodyEl) {
        bodyEl.append(
          modalNote('1. Install Tailscale on this PC and sign in (a free ' +
            'personal account is enough).'),
          modalButton('Download Tailscale', '',
            () => openUrl('https://tailscale.com/download')),
          modalNote('2. In your Tailscale account\u2019s DNS settings, turn ' +
            'on MagicDNS and HTTPS Certificates.'),
          modalButton('Open Tailscale DNS settings', '',
            () => openUrl('https://login.tailscale.com/admin/dns')),
          modalNote('3. On your Android phone, install Tailscale from ' +
            'Google Play and sign in with the same account.'),
          modalNote('4. Come back here, press Check again, then scan the QR ' +
            'code with your phone.'));
      },
      foot(footEl, api) {
        footEl.append(modalButton('Close', 'cb-btn--quiet', api.close));
      },
    });
  }
```

In the card builder, fill `connectActions`:

```js
      connectActions.onGuide = openTailscaleGuide;
      connectActions.onCheck = () => {
        call('remote.tailscale_check')
          .then((info) => paintRemoteConnect(connectBox, info, connectActions))
          .catch(() => {});
      };
```

Append to `web/app.css`:

```css
.cb-remote-steps { display: grid; gap: 6px; }
.cb-remote-step { gap: 8px; font-size: 12.5px; }
```

Update spec §1 item 4's wording ("a guide window like the cookie guide" → "a step-by-step guide dialog").

- [ ] **Step 5: Run the tests**

Run: `python -m pytest -q tests/test_web_*_client.py`
Expected: all pass.

- [ ] **Step 6: Verify it visually in both themes**

On the preview, set Mode = *Over the internet*.
- Without Tailscale ready: the four rows show "To do", *Open setup guide* opens the dialog, and its buttons open the host browser.
- With the maintainer's Tailscale ready: the QR and the `https://…ts.net/` address appear.

Screenshot both light and dark.

- [ ] **Step 7: Commit**

```bash
git add web/app.js web/app.css tests/test_web_remote_client.py \
  docs/specs/2026-09-23-remote-access-secure-modes-design.md
git commit -m "feat(remote): Tailscale checklist and setup guide in Settings"
```

---

### Task 10: Real-device test, then switch it on

**Model:** Steps 1-3 are the controller with the maintainer (their phone). Steps 4-7 are `opus`, since they touch `web/app.js` and the security switch.

**Files:**
- Modify: `cratebuilder/remoteauth.py:18` (`REMOTE_ACCESS_AVAILABLE`)
- Modify: `web/app.js` (the parked notice and dimming in the Remote Access card ≈L5660-5672; the `parked` hint text)
- Delete or rewrite: `tests/test_remote_parked.py`
- Modify: `tests/conftest.py:102-109` (the `_remote_access_available` fixture becomes unnecessary)
- Modify: spec status line, plus any About FAQ / README / `CONTEXT.md` text about Remote Access (`graft grep -i "remote access"`)

**Interfaces:** none new.

- [ ] **Step 1: Build a test run the maintainer can use**

Run the app from source against a scratch data dir with the switch lifted. That is the preview harness from Task 6 Step 7, but with the desktop window. If the installed app holds the single-instance lock, use `web_server.py` headless via the harness.

- [ ] **Step 2: Home network, on the maintainer's Android phone**

Record PASS or FAIL for each:
1. Settings ▸ Mode = Home network. The QR and address appear. Windows asks once, and they click Allow.
2. The phone scans the QR. Chrome warns once, and they tap through.
3. They pair with the code, press Take control, add a link and start a download. Progress moves live on the phone.
4. They switch Mode to Off. The phone loses the connection at once.
5. They switch back to Home and reload on the phone. There's **no** new warning, and the phone stays paired.
6. (If practical) the PC's address changes. The panel says "warn once more".

- [ ] **Step 3: Over the internet, on the maintainer's Android phone**

1. Tailscale is on the PC and the phone, same account, and MagicDNS + HTTPS are on.
2. Mode = Over the internet. All four checklist rows are Done, and the QR shows `https://…ts.net/`.
3. The phone is on **mobile data** (Wi-Fi off). It scans, gets a padlock with no warning, pairs, takes control, and starts a download. Progress is live.
4. Mode = Off. `tailscale serve status` no longer lists the app.

If any item fails, stop, file it back into the relevant task, and do not continue.

- [ ] **Step 4: Flip the switch**

In `cratebuilder/remoteauth.py`, change `REMOTE_ACCESS_AVAILABLE = False` to `True`, and update the comment above it. The feature has shipped; the switch stays as an emergency off.

- [ ] **Step 5: Retire the parked-state UI and tests**

In the Remote Access card, remove the `is-parked` notice block and the `cb-parked-dim` dimming. Keep `remoteAccessAvailable()`, since the snapshot still carries `remote_available` for an emergency off.

In `tests/test_remote_parked.py`, keep the behavioural tests (they monkeypatch the switch to False, so they still hold). Replace `test_the_switch_is_off_in_the_shipped_module` with:

```python
def test_the_switch_is_on_in_the_shipped_module():
    src = open(remoteauth.__file__, encoding="utf-8").read()
    assert "\nREMOTE_ACCESS_AVAILABLE = True\n" in src
```

Remove the `_remote_access_available` autouse fixture from `tests/conftest.py` if nothing else needs it.

- [ ] **Step 6: Update the user-facing docs**

Run `graft grep -i "remote access"` and update:
- the About FAQ entry, whose data lives in `DJ-CrateBuilder_v2.0.py`;
- `README.md`;
- `CONTEXT.md`.

The text should describe the two modes plainly and mention Tailscale for the internet mode. Set the spec status to "shipped".

`CLAUDE.md` is local-only (gitignored) but still lists `python web_server.py --lan`. Replace that line with a note that the headless server follows the Mode set in Settings. Never stage the file.

- [ ] **Step 7: Run the tests, then commit**

Run: `python -m pytest -q tests/test_remote_parked.py tests/test_remote_modes.py tests/test_remotemount.py tests/test_remote_connect.py tests/test_tailscale.py tests/test_remotecert.py tests/test_server.py tests/test_web_window.py`
Run: `python -m pytest -q tests/test_web_*_client.py`
Expected: all pass. Ask the maintainer whether to run the full suite before the commit (standing order: not without asking).

```bash
git add cratebuilder/remoteauth.py web/app.js tests/test_remote_parked.py tests/conftest.py \
  README.md CONTEXT.md DJ-CrateBuilder_v2.0.py docs/specs/2026-09-23-remote-access-secure-modes-design.md
git commit -m "feat(remote): switch on Remote Access with Home network and Internet modes"
```

Do not push, and do not publish a nightly. Tell the maintainer it is ready for `/build-update` when they choose.

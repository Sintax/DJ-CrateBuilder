# Browser Integration (Receive Side, web UI) Implementation Plan

> **Where this executes:** the work in this plan lands in the **main app repo**,
> `C:\Users\djsin\Documents\GitHub\DJ-CrateBuilder\`. The plan is *stored* here
> because this repo owns the wire contract and SPEC §10 records the app-side
> design. Copy it to that repo's `docs/specs/plans/` when the work begins, per
> its convention.
>
> **Supersedes** `2026-08-23-browser-integration-receive.md`, which targeted
> the tkinter monolith. The app's v2.0 replaced that UI with a web frontend
> (`web/`) in a pywebview window, driven by `cratebuilder/service.py`. Tasks
> 1–3 of the old plan survive almost verbatim; everything UI-facing is
> re-planned here against the service/event/web-page shape.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per the app repo's CLAUDE.md, execute in an isolated worktree (superpowers:using-git-worktrees) — this touches the singleton socket, the DB schema, Settings and the web bundle. The app repo has a `graft/` index: locate code with `graft ask "<question>" --source`, `graft skeleton <file>` and `graft callers <symbol>` before editing; the `file:line` anchors below were taken from `main` on 2026-09-13 and may have drifted.

**Goal:** The app receives `djcrate://` sends from the browser extension — registering the handler, parsing the URI, and acting on it in either "Bring window forward" or "Collect quietly" mode — through the web UI that ships today.

**Architecture:** Pure logic lands in `cratebuilder/` as Tk-free modules (`browserlink.py` parse, `protocolreg.py` registry, a widened `singleton.py` socket, an inbox table on `DownloadsDatabase`). `CrateBuilderService` gains one receive entry point (`browser_receive`) that either emits a `browser.send` event for the page to open the prefilled dialog, or writes an inbox row and emits `browser.inbox`; three `browser.*` RPC methods let the page list/process/remove inbox rows. `web_window.py` gains argv handling, the listener swap and a "bring the window forward" hook. The page (`web/app.js`) gains a handler that opens the existing Add Channel modal or prefills the Downloads URL box, an inbox modal, and two contract-driven Settings rows.

**Tech Stack:** Python 3.10+ stdlib (`winreg`, `socket`, `sqlite3`), pytest; plain JS in `web/app.js` (no bundler), Python-driven frontend tests that slice `app.js` as text and run pieces under `node`.

**Spec:** The send-side repo owns the contract — read both before starting:
`DJ-CrateBuilder-Browser_Extensions/docs/SPEC.md` (§10 is this plan's scope) and
`DJ-CrateBuilder-Browser_Extensions/docs/specs/djcrate-uri-v1.md` (the wire contract; §2 socket payload, §4 error handling). In the app repo, read `.claude/skills/changing-the-web-ui/SKILL.md` before touching `web/`.

## Global Constraints (from the app repo's CLAUDE.md — not optional)

- Work in a **worktree**, branch `feat/browser-integration`.
- Conventional Commits. **Never push, tag, or open a PR without an explicit ask.**
- **Never bump `APP_BUILD` or `APP_VERSION`**; never run `scripts/release.py` or `/build-update`.
- **No tkinter imports in `cratebuilder/`.**
- DB schema change = `SCHEMA_VERSION` bump + migration step in `_init_schema`.
- **`cratebuilder/ui_strings.py` is generated.** Edit `UI-design/ui-contract.json`, then run `python scripts/gen_ui_strings.py`. Never hand-edit it.
- **`web/` talks to the host only through `cbApi`** (`cbApi.call`, `cbApi.on`). Never `fetch()`, never poll. New host actions go in `_methods()` in `cratebuilder/service.py`.
- **Never edit `web/theme.css`.** No new colours — reuse `--cb-*` tokens.
- Frontend tests slice `app.js` **as text** by exact function-header markers (`"  function renderWatchlistToolbar()"` — two-space indent). Do not rename or re-indent existing functions; add new ones.
- Iterate with `python -m pytest -q -m "not gui"` (~8s) and `python -m pytest -q tests/test_web_*_client.py`; "done" for the branch requires the **full** `python -m pytest -q` plus launching `python web_window.py` to eyeball UI changes **in both themes** (Settings ▸ Appearance).
- House style: one-line module docstrings in `cratebuilder/`; comments explain *why*, not *what*; match neighbouring code's voice.
- Line references are anchors against `main` as of 2026-09-13. Each edit quotes surrounding code — re-locate by the quote (or `graft`) if the file has drifted.

---

### Task 1: `cratebuilder/browserlink.py` — URI parsing

**Files:**
- Create: `cratebuilder/browserlink.py`
- Test: `tests/test_browserlink.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_djcrate_uri(uri) → BrowserSend | ParseError`.
  `BrowserSend` is a frozen dataclass `{kind: str, url: str}` (kind ∈ `'channel'|'track'`, url = decoded canonical https URL).
  `ParseError` is a frozen dataclass `{reason: str, message: str}` with reason ∈ `'newer' | 'bad_url' | 'ignore'`; `message` is user-facing text, empty for `'ignore'`.
  Module constants `MSG_NEWER`, `MSG_BAD_URL` (Task 6 surfaces them as notifications).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_browserlink.py
"""Tests for djcrate:// URI parsing (wire contract v1)."""
from cratebuilder.browserlink import (
    parse_djcrate_uri, BrowserSend, ParseError, MSG_NEWER, MSG_BAD_URL)


def test_valid_channel_uri():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fsomeartist")
    assert r == BrowserSend(kind="channel", url="https://soundcloud.com/someartist")


def test_valid_track_uri_with_encoded_query():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=track&url="
        "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ")
    assert r == BrowserSend(
        kind="track", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_parameter_order_is_not_significant():
    r = parse_djcrate_uri(
        "djcrate://add?url=https%3A%2F%2Fsoundcloud.com%2Fa&kind=channel&v=1")
    assert isinstance(r, BrowserSend)


def test_unknown_extra_parameters_are_ignored():
    r = parse_djcrate_uri(
        "djcrate://add?v=1&kind=track&extra=x&url=https%3A%2F%2Fsoundcloud.com%2Fa%2Fb")
    assert isinstance(r, BrowserSend)


def test_newer_version_and_unknown_kind_get_the_update_message():
    for uri in (
        "djcrate://add?v=2&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa",
        "djcrate://add?kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa",   # v absent
        "djcrate://add?v=1&kind=playlist&url=https%3A%2F%2Fsoundcloud.com%2Fa",
        "djcrate://add?v=1&url=https%3A%2F%2Fsoundcloud.com%2Fa",            # kind absent
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="newer", message=MSG_NEWER), uri


def test_bad_urls_get_the_unsupported_message():
    for uri in (
        "djcrate://add?v=1&kind=track",                                       # url absent
        "djcrate://add?v=1&kind=track&url=",                                  # url empty
        "djcrate://add?v=1&kind=track&url=http%3A%2F%2Fsoundcloud.com%2Fa",   # not https
        "djcrate://add?v=1&kind=track&url=https%3A%2F%2Fevil.example%2Fa",    # bad host
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="bad_url", message=MSG_BAD_URL), uri


def test_non_add_verbs_and_junk_are_silently_ignored():
    for uri in (
        "djcrate://frobnicate?v=1",
        "https://soundcloud.com/a",       # not djcrate at all
        "show",
        "",
        None,
        12345,
    ):
        r = parse_djcrate_uri(uri)
        assert r == ParseError(reason="ignore", message=""), repr(uri)


def test_never_raises_on_garbage():
    parse_djcrate_uri("djcrate://add?%%%bad=encoding%")
    parse_djcrate_uri("djcrate://" + "x" * 100_000)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_browserlink.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cratebuilder.browserlink'`.

- [ ] **Step 3: Implement**

```python
# cratebuilder/browserlink.py
"""djcrate:// URI parsing for browser-extension sends (Tk-free).

Wire contract: the extension repo's docs/specs/djcrate-uri-v1.md. Parsing is
total — any input returns a result object, never an exception — because a
malformed URI must never take down the singleton listener thread.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qs

ACCEPTED_HOSTS = {"www.youtube.com", "soundcloud.com"}

MSG_NEWER = ("This send came from a newer version of the CrateBuilder "
             "extension — update DJ-CrateBuilder.")
MSG_BAD_URL = "That link isn't a supported YouTube or SoundCloud URL."


@dataclass(frozen=True)
class BrowserSend:
    kind: str   # 'channel' | 'track'
    url: str    # decoded canonical https URL


@dataclass(frozen=True)
class ParseError:
    reason: str    # 'newer' | 'bad_url' | 'ignore'
    message: str   # user-facing text; '' when reason == 'ignore'


def parse_djcrate_uri(uri):
    """Parse a djcrate:// URI into a BrowserSend, or a ParseError saying how
    to react: 'newer' and 'bad_url' carry a message to show the user,
    'ignore' means drop it silently (wrong scheme or verb — not ours)."""
    try:
        if not isinstance(uri, str):
            return ParseError("ignore", "")
        split = urlsplit(uri.strip())
        if split.scheme != "djcrate" or split.netloc != "add":
            return ParseError("ignore", "")
        params = parse_qs(split.query)
        version = params.get("v", [None])[0]
        kind = params.get("kind", [None])[0]
        if version != "1" or kind not in ("channel", "track"):
            return ParseError("newer", MSG_NEWER)
        url = params.get("url", [None])[0]
        if not url:
            return ParseError("bad_url", MSG_BAD_URL)
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname not in ACCEPTED_HOSTS:
            return ParseError("bad_url", MSG_BAD_URL)
        return BrowserSend(kind=kind, url=url)
    except Exception:
        return ParseError("ignore", "")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_browserlink.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/browserlink.py tests/test_browserlink.py
git commit -m "feat(browserlink): parse djcrate:// URIs per wire contract v1"
```

---

### Task 2: Widen the singleton socket protocol

**Files:**
- Modify: `cratebuilder/singleton.py:55-74` (replace `listen_for_show_requests`)
- Test: `tests/test_singleton.py` (append; the existing tests must keep passing unchanged)

**Interfaces:**
- Consumes: nothing.
- Produces: `forward_add(port, uri, timeout=0.5)`;
  `listen_for_requests(sock, on_show, on_add=None)` dispatching `show` → `on_show()` and `add <uri>` → `on_add(uri)`;
  `listen_for_show_requests(sock, on_show)` kept as a back-compat wrapper (existing tests, `web_window.py` and the retired monolith call it until Task 7 swaps the window's call site).
  Wire behaviour per contract §2: newline-terminated UTF-8 line, 8 KiB cap, unknown verbs ignored, and **a bare/empty connection still means `show`** — preserving today's behaviour where any connection restores the window.

- [ ] **Step 1: Append the failing tests to `tests/test_singleton.py`**

```python
from cratebuilder.singleton import forward_add, listen_for_requests


def test_forward_add_delivers_uri_to_on_add():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        got = []
        event = threading.Event()
        listen_for_requests(holder, on_show=event.set,
                            on_add=lambda u: (got.append(u), event.set()))
        uri = "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa"
        forward_add(port, uri)
        assert event.wait(timeout=2)
        assert got == [uri]
    finally:
        holder.close()


def test_show_still_dispatches_to_on_show_not_on_add():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()
        added = []
        listen_for_requests(holder, on_show=shown.set, on_add=added.append)
        request_show(port)
        assert shown.wait(timeout=2)
        assert added == []
    finally:
        holder.close()


def test_add_without_on_add_handler_is_ignored_not_fatal():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()
        listen_for_requests(holder, on_show=shown.set)   # no on_add
        forward_add(port, "djcrate://add?v=1")
        request_show(port)                               # listener must survive
        assert shown.wait(timeout=2)
    finally:
        holder.close()


def test_oversized_line_is_capped_and_does_not_kill_listener():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        got = []
        event = threading.Event()
        listen_for_requests(holder, on_show=lambda: None,
                            on_add=lambda u: (got.append(u), event.set()))
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.sendall(b"add " + b"x" * 20000 + b"\n")
        forward_add(port, "djcrate://ok")
        assert event.wait(timeout=2)
        assert got[-1] == "djcrate://ok"
    finally:
        holder.close()


def test_a_throwing_on_add_does_not_kill_the_listener():
    port = _free_port()
    holder = acquire_single_instance(port)
    assert holder is not None
    try:
        shown = threading.Event()

        def boom(_uri):
            raise RuntimeError("handler bug")

        listen_for_requests(holder, on_show=shown.set, on_add=boom)
        forward_add(port, "djcrate://add?v=1")
        request_show(port)
        assert shown.wait(timeout=2)
    finally:
        holder.close()


def test_forward_add_is_a_noop_when_nothing_is_listening():
    port = _free_port()
    forward_add(port, "djcrate://add?v=1", timeout=0.2)   # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_singleton.py -q`
Expected: FAIL — `ImportError: cannot import name 'forward_add'`.

- [ ] **Step 3: Edit `cratebuilder/singleton.py`.** Keep the module docstring, `SINGLE_INSTANCE_PORT`, `acquire_single_instance`, and `request_show` exactly as they are. Replace the existing `listen_for_show_requests` function (lines 55–74, the one whose loop does `conn.recv(16)`) with these four:

```python
def forward_add(port, uri, timeout=0.5):
    """Relay a djcrate:// URI to the already-running instance holding *port*.

    Best-effort like request_show: called by a second launch that lost the
    bind race while carrying a protocol-handler argument. Wire format per the
    extension repo's docs/specs/djcrate-uri-v1.md §2: 'add <uri>\\n', UTF-8.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"add " + uri.encode("utf-8") + b"\n")
    except OSError:
        pass


def _read_line(conn, cap=8192):
    """Read one newline-terminated line (or until EOF/cap) from *conn*.

    Replaces the old fixed conn.recv(16): the 'add' verb carries a URI that
    doesn't fit in 16 bytes. The cap stops a hostile local writer growing the
    buffer without bound; the timeout stops a silent connection parking the
    listener thread forever.
    """
    chunks, total = [], 0
    conn.settimeout(1.0)
    try:
        while total < cap:
            data = conn.recv(1024)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if b"\n" in data:
                break
    except OSError:
        pass
    return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def listen_for_requests(sock, on_show, on_add=None):
    """Run on a daemon thread, dispatching one verb per connection accepted on
    *sock*: 'show' (or a bare/legacy connection) calls on_show(); 'add <uri>'
    calls on_add(uri) when a handler was given. Unknown verbs are ignored, and
    a callback that raises is swallowed — nothing a browser sends may stop
    the next connection being served. Callbacks run on this thread, not a UI
    thread. Returns once *sock* is closed.
    """
    def _loop():
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                line = _read_line(conn)
            finally:
                conn.close()
            try:
                if line.startswith("add "):
                    if on_add is not None:
                        on_add(line[4:].strip())
                elif line == "show" or not line:
                    on_show()
                # anything else: unknown verb, ignore (contract §4)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def listen_for_show_requests(sock, on_show):
    """Back-compat alias from the two-verb widening; new callers should use
    listen_for_requests."""
    return listen_for_requests(sock, on_show)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_singleton.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/singleton.py tests/test_singleton.py
git commit -m "feat(singleton): widen socket protocol to show/add verbs"
```

---

### Task 3: `cratebuilder/protocolreg.py` — handler registration

**Files:**
- Create: `cratebuilder/protocolreg.py`
- Test: `tests/test_protocolreg.py`
- Modify: `packaging/deb/dj-cratebuilder.desktop:5`, `install-linux.sh:283-291` (the `.desktop` heredoc)

**Interfaces:**
- Consumes: nothing.
- Produces: `protocol_is_registered() → bool`, `register_protocol() → bool`, `unregister_protocol() → bool`. All degrade gracefully (False / no-op) off-Windows or on registry error, mirroring `cratebuilder/startup.py`.

- [ ] **Step 1: Write the failing tests.** A dict-backed fake `winreg` — real HKCU writes in a test would pollute the developer's registry.

```python
# tests/test_protocolreg.py
"""Tests for djcrate:// protocol registration (fake winreg — no real HKCU)."""
import cratebuilder.protocolreg as pr


class FakeKey:
    def __init__(self, store, path):
        self.store, self.path = store, path


class FakeWinreg:
    """Dict-backed winreg: {key_path: {value_name: value}}."""
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.keys = {}

    def CreateKey(self, root, path):
        self.keys.setdefault(path, {})
        return FakeKey(self.keys, path)

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(self.keys, path)

    def SetValueEx(self, key, name, reserved, vtype, value):
        self.keys[key.path][name] = value

    def QueryValueEx(self, key, name):
        try:
            return (self.keys[key.path][name], self.REG_SZ)
        except KeyError:
            raise FileNotFoundError(name)

    def DeleteKey(self, root, path):
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(k != path and k.startswith(path + "\\") for k in self.keys):
            raise OSError("subkeys exist")
        del self.keys[path]

    def CloseKey(self, key):
        pass


def _with_fake(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setattr(pr, "winreg", fake)
    return fake


def test_register_writes_the_class_tree(monkeypatch):
    fake = _with_fake(monkeypatch)
    assert pr.register_protocol() is True
    root = fake.keys[r"Software\Classes\djcrate"]
    assert root[""] == "URL:DJ-CrateBuilder Protocol"
    assert root["URL Protocol"] == ""
    cmd = fake.keys[r"Software\Classes\djcrate\shell\open\command"][""]
    assert cmd.startswith('"')
    assert cmd.endswith('"%1"')


def test_is_registered_reflects_state(monkeypatch):
    _with_fake(monkeypatch)
    assert pr.protocol_is_registered() is False
    pr.register_protocol()
    assert pr.protocol_is_registered() is True


def test_unregister_removes_everything(monkeypatch):
    fake = _with_fake(monkeypatch)
    pr.register_protocol()
    assert pr.unregister_protocol() is True
    assert r"Software\Classes\djcrate" not in fake.keys
    assert pr.protocol_is_registered() is False


def test_unregister_when_absent_is_ok(monkeypatch):
    _with_fake(monkeypatch)
    assert pr.unregister_protocol() is True


def test_everything_degrades_off_windows(monkeypatch):
    monkeypatch.setattr(pr, "winreg", None)
    assert pr.protocol_is_registered() is False
    assert pr.register_protocol() is False
    assert pr.unregister_protocol() is False


def test_source_command_names_the_entry_script(monkeypatch):
    """From source the handler must launch web_window.py (the shipped entry
    point), not whatever python happens to be on PATH."""
    monkeypatch.setattr(pr.sys, "frozen", False, raising=False)
    monkeypatch.setattr(pr.sys, "argv", [r"C:\src\web_window.py"])
    cmd = pr._handler_command()
    assert cmd.endswith(r'web_window.py" "%1"')
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_protocolreg.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cratebuilder.protocolreg'`.

- [ ] **Step 3: Implement**

```python
# cratebuilder/protocolreg.py
"""Windows djcrate:// protocol registration via HKCU Software\\Classes.

Mirrors startup.py's degrade-gracefully pattern: every function returns False
or no-ops off-Windows and on registry error. Per-user, no admin rights.
Wire contract: the extension repo's docs/specs/djcrate-uri-v1.md. Linux has
no registry — there the .desktop entry's MimeType line does this job.
"""
import os
import sys

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover
    winreg = None

_CLASS_KEY = r"Software\Classes\djcrate"
_COMMAND_KEY = _CLASS_KEY + r"\shell\open\command"


def _handler_command():
    """Quoted command Windows should run for a djcrate:// navigation.

    Frozen, sys.executable IS the app (web_window.py built onedir). From
    source, sys.argv[0] is web_window.py and pythonw keeps a console window
    from flashing up on every send."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else exe
    script = os.path.abspath(sys.argv[0])
    return f'"{runner}" "{script}" "%1"'


def protocol_is_registered():
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _COMMAND_KEY, 0,
                             winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "")
            return True
        finally:
            winreg.CloseKey(key)
    except (FileNotFoundError, OSError):
        return False


def register_protocol():
    """Write the class tree. Returns True on success. Re-registering is the
    supported way to refresh a stale path after the app moves."""
    if winreg is None:
        return False
    try:
        root = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CLASS_KEY)
        winreg.SetValueEx(root, "", 0, winreg.REG_SZ,
                          "URL:DJ-CrateBuilder Protocol")
        winreg.SetValueEx(root, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(root)
        cmd = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _COMMAND_KEY)
        winreg.SetValueEx(cmd, "", 0, winreg.REG_SZ, _handler_command())
        winreg.CloseKey(cmd)
        return True
    except OSError:
        return False


def unregister_protocol():
    """Delete the class tree leaf-first (winreg has no recursive delete).
    Returns True when gone, including when it was never there."""
    if winreg is None:
        return False
    for path in (_COMMAND_KEY,
                 _CLASS_KEY + r"\shell\open",
                 _CLASS_KEY + r"\shell",
                 _CLASS_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_protocolreg.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS.

- [ ] **Step 5: Linux `.desktop` entries.** Two files carry a `[Desktop Entry]`; both need the scheme handler **and** a `%u` on `Exec=`, or the desktop launches the app without passing the URL.

In `packaging/deb/dj-cratebuilder.desktop`, change line 5 and add a line after `Keywords=`:

```
Exec=dj-cratebuilder %u
…
MimeType=x-scheme-handler/djcrate;
```

In `install-linux.sh`, inside the `cat > "$DESKTOP_DIR/dj-cratebuilder.desktop" << EOF` heredoc (line 283 onwards), change `Exec=$BIN_LINK` to `Exec=$BIN_LINK %u` and add `MimeType=x-scheme-handler/djcrate;` after the `Keywords=` line.

This is build-time only and **cannot be verified from Windows.** Say so in the commit body — do not claim it works.

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/protocolreg.py tests/test_protocolreg.py packaging/deb/dj-cratebuilder.desktop install-linux.sh
git commit -m "feat(protocolreg): per-user djcrate:// handler registration

Linux .desktop MimeType + %u added but unverified — no Linux box in this session."
```

---

### Task 4: Settings — schema key, contract rows, bindings, handler toggle

**Files:**
- Modify: `cratebuilder/settings.py:47` (`_schema_defaults`, after `"run_at_startup": False,`)
- Modify: `UI-design/ui-contract.json` — `settings_keys` (after the `dupe_check_enabled` row, line 130) and `tooltips` (after `settings.dupe_check`, line 229)
- Regenerate: `cratebuilder/ui_strings.py` via `python scripts/gen_ui_strings.py`
- Modify: `cratebuilder/service.py:17` (imports), `:484-494` (`SETTINGS_BINDINGS`), `:2180-2258` (`settings_all`, `settings_get`, `settings_set`), new `_set_browser_handler` beside `_set_run_at_startup` (`:2285`)
- Test: `tests/test_settings.py:27-72` (`EXPECTED_DEFAULTS`), new `tests/test_browser_settings.py`

**Interfaces:**
- Consumes: `protocolreg` (Task 3).
- Produces: schema key `browser_receive_mode` storing `'window' | 'quiet'`; contract keys `browser_receive_mode` (enum, display `'Bring window forward' | 'Collect quietly'`) and `browser_handler` (bool, win32-only, backed by the registry not the config file); service constants `RECEIVE_MODE_WINDOW = "window"`, `RECEIVE_MODE_QUIET = "quiet"`, `BROWSER_HANDLER_KEY = "browser_handler"`. Task 6 reads `self._settings.get("browser_receive_mode")`; Task 8's page renders both rows from `SETTINGS_KEYS` with no bespoke markup.

- [ ] **Step 1: Write the failing tests**

Add to `EXPECTED_DEFAULTS` in `tests/test_settings.py` (anywhere in the dict, e.g. after `"run_at_startup": False,`):

```python
    "browser_receive_mode": "window",
```

Create `tests/test_browser_settings.py`:

```python
# tests/test_browser_settings.py
"""Settings ▸ Browser Integration: the receive-mode binding and the
registry-backed handler toggle."""
import pytest

from cratebuilder import protocolreg
from cratebuilder import ui_strings
from cratebuilder.service import (BROWSER_HANDLER_KEY, RECEIVE_MODE_QUIET,
                                  RECEIVE_MODE_WINDOW, CBError,
                                  CrateBuilderService)
from cratebuilder.settings import Settings


@pytest.fixture
def settings(tmp_path):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(tmp_path / "crate"))
    return s


@pytest.fixture
def service(settings, tmp_path):
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


@pytest.fixture
def fake_registry(monkeypatch):
    """protocolreg over a flag instead of HKCU."""
    state = {"registered": False, "fail": False}
    monkeypatch.setattr(protocolreg, "protocol_is_registered",
                        lambda: state["registered"])

    def _register():
        if state["fail"]:
            return False
        state["registered"] = True
        return True

    def _unregister():
        if state["fail"]:
            return False
        state["registered"] = False
        return True

    monkeypatch.setattr(protocolreg, "register_protocol", _register)
    monkeypatch.setattr(protocolreg, "unregister_protocol", _unregister)
    return state


def test_the_contract_draws_both_rows_in_their_own_section():
    rows = {e["key"]: e for e in ui_strings.SETTINGS_KEYS}
    assert rows["browser_handler"]["section"] == "Browser Integration"
    assert rows["browser_handler"]["type"] == "bool"
    assert rows["browser_handler"]["platform"] == "win32"
    assert rows["browser_receive_mode"]["section"] == "Browser Integration"
    assert rows["browser_receive_mode"]["options"] == [
        "Bring window forward", "Collect quietly"]
    for key in ("settings.browser_handler", "settings.browser_receive_mode",
                "settings.browser_integration", "wl.browser_inbox"):
        assert ui_strings.TOOLTIPS.get(key), key


def test_receive_mode_reads_as_display_form(service, settings):
    assert service.settings_get("browser_receive_mode")["value"] == "Bring window forward"
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    assert service.settings_get("browser_receive_mode")["value"] == "Collect quietly"


def test_receive_mode_writes_stored_form(service, settings):
    service.settings_set("browser_receive_mode", "Collect quietly")
    assert settings.get("browser_receive_mode") == RECEIVE_MODE_QUIET
    service.settings_set("browser_receive_mode", "Bring window forward")
    assert settings.get("browser_receive_mode") == RECEIVE_MODE_WINDOW


def test_receive_mode_rejects_an_unknown_value(service):
    with pytest.raises(CBError):
        service.settings_set("browser_receive_mode", "Teleport")


def test_settings_all_serves_both_keys(service, fake_registry):
    values = service.settings_all()
    assert values["browser_receive_mode"] == "Bring window forward"
    assert values[BROWSER_HANDLER_KEY] is False
    fake_registry["registered"] = True
    assert service.settings_all()[BROWSER_HANDLER_KEY] is True


def test_handler_toggle_registers_and_unregisters(service, fake_registry):
    assert service.settings_set(BROWSER_HANDLER_KEY, True) == {
        "key": BROWSER_HANDLER_KEY, "value": True}
    assert fake_registry["registered"] is True
    assert service.settings_set(BROWSER_HANDLER_KEY, False)["value"] is False
    assert fake_registry["registered"] is False


def test_handler_toggle_refuses_when_the_registry_write_fails(service, fake_registry):
    fake_registry["fail"] = True
    with pytest.raises(CBError):
        service.settings_set(BROWSER_HANDLER_KEY, True)
    assert fake_registry["registered"] is False


def test_handler_toggle_is_local_only(settings, tmp_path, fake_registry):
    remote = CrateBuilderService(transport="remote", settings=settings,
                                 db_path=str(tmp_path / "db.sqlite"))
    with pytest.raises(CBError):
        remote.settings_set(BROWSER_HANDLER_KEY, True)
    assert fake_registry["registered"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_browser_settings.py tests/test_settings.py -q`
Expected: FAIL — `ImportError: cannot import name 'BROWSER_HANDLER_KEY'`, and `test_every_schema_default` FAILS with `KeyError: 'browser_receive_mode'`.

- [ ] **Step 3: Schema key.** In `cratebuilder/settings.py::_schema_defaults`, directly after `"run_at_startup": False,`:

```python
        "browser_receive_mode": "window",   # 'window' | 'quiet' — see service.RECEIVE_MODE_*
```

- [ ] **Step 4: Contract rows.** In `UI-design/ui-contract.json`, inside `"settings_keys"`, insert after the `dupe_check_enabled` row and before the blank line that precedes `remote_enabled` (the section order here is the order the Settings screen draws its cards — this puts Browser Integration just above the Remote Access separator):

```json
    { "key": "browser_handler", "label": "Register the djcrate:// link handler for the browser extension", "type": "bool", "default": false, "platform": "win32", "section": "Browser Integration", "source": "new", "tooltip": "settings.browser_handler" },
    { "key": "browser_receive_mode", "label": "When a browser send arrives", "type": "enum", "options": ["Bring window forward", "Collect quietly"], "default": "Bring window forward", "section": "Browser Integration", "source": "new", "tooltip": "settings.browser_receive_mode" },

```

Inside `"tooltips"`, after the `settings.dupe_check` entry:

```json
    "settings.browser_integration": { "text": "The djcrate:// link type the DJ-CrateBuilder browser extension uses to hand this app a channel or a track. It is one-way: the extension can't tell whether the app received a send, so if one seems lost, check the handler toggle here first.", "source": "new", "screen": "3j" },
    "settings.browser_handler": { "text": "Lets the browser extension send YouTube and SoundCloud channels and tracks straight to this app by registering the djcrate:// link type with Windows — per-user, no admin rights. Untick and re-tick after moving or reinstalling the app to refresh the stored path. On Linux the .desktop entry does this instead.", "source": "new", "screen": "3j" },
    "settings.browser_receive_mode": { "text": "What happens when the extension sends a link. Bring window forward opens the matching dialog at once, prefilled. Collect quietly queues it in the Browser Inbox on the Watch List and pings the tray instead, so a run of sends doesn't keep pulling the window up.", "source": "new", "screen": "3j" },
    "wl.browser_inbox": { "text": "Links the browser extension sent while receive mode was Collect quietly. Process opens the same prefilled dialog a send would have opened straight away; Remove discards it.", "source": "new", "screen": "3d" },
```

Then regenerate: `python scripts/gen_ui_strings.py`. It prints counts; `settings keys:` should be two higher than before and the four new tooltip keys must appear in `cratebuilder/ui_strings.py`. **Commit the regenerated file; never edit it by hand.**

- [ ] **Step 5: Service — imports, constants, binding.** At `cratebuilder/service.py:17`, extend the package import:

```python
from cratebuilder import (activitylog, browserlink, debuglog, protocolreg,
                          rebuild, startup, ui_strings, util, ydl)
```

(`browserlink` is used in Task 6; importing it now is harmless.) Beside the other module constants — directly after `JOB_FINISHED = "job.finished"` (line 78):

```python
# Browser-extension sends (djcrate://) — the receive side of the extension
# repo's docs/specs/djcrate-uri-v1.md. Two receive modes, stored as the two
# short words; the Settings screen shows the display strings.
RECEIVE_MODE_WINDOW = "window"
RECEIVE_MODE_QUIET = "quiet"
RECEIVE_MODE_DISPLAY = {RECEIVE_MODE_WINDOW: "Bring window forward",
                        RECEIVE_MODE_QUIET: "Collect quietly"}
# The handler toggle is not a config key at all: its value IS the registry.
BROWSER_HANDLER_KEY = "browser_handler"
BROWSER_SEND = "browser.send"      # {kind, url} — window mode: open the flow
BROWSER_INBOX = "browser.inbox"    # {count, added: {kind, url} | None}
```

Beside the other `_*_to_display` helpers (after `_cookie_method_from_display`, ~line 483):

```python
def _receive_mode_to_display(value):
    return RECEIVE_MODE_DISPLAY.get(value, RECEIVE_MODE_DISPLAY[RECEIVE_MODE_WINDOW])


def _receive_mode_from_display(value):
    for stored, display in RECEIVE_MODE_DISPLAY.items():
        if value in (stored, display):
            return stored
    raise ValueError(f"Unknown receive mode: {value!r}")
```

Add to `SETTINGS_BINDINGS`:

```python
    "browser_receive_mode": _simple_binding("browser_receive_mode", _receive_mode_to_display, _receive_mode_from_display),
```

- [ ] **Step 6: Service — the handler toggle.** The key is served like the `REMOTE_SETTINGS_KEYS` (a value that lives outside `Settings`). In `settings_all`, directly after the `if flag is not None:` block inside the loop:

```python
            if key == BROWSER_HANDLER_KEY:
                out[key] = protocolreg.protocol_is_registered()
                continue
```

In `settings_get`, after the `if flag is not None:` return:

```python
        if key == BROWSER_HANDLER_KEY:
            return {"key": key, "value": protocolreg.protocol_is_registered()}
```

In `settings_set`, directly after `self._refuse_frozen_setting(key)`:

```python
        if key == BROWSER_HANDLER_KEY:
            return self._set_browser_handler(value)
```

Then add the method directly after `_set_run_at_startup`:

```python
    def _set_browser_handler(self, value):
        """Register or unregister the djcrate:// handler — the Run-at-login
        shape: it edits the host's own registry, so local transport only, and
        a failed write is refused rather than echoed back as if it took.
        The reply reads the registry again, so the page shows what is true."""
        if self.transport != LOCAL:
            raise CBError("Browser integration can only be changed from the "
                          "app window on the host machine.")
        ok = (protocolreg.register_protocol() if value
              else protocolreg.unregister_protocol())
        if not ok:
            raise CBError("Could not update the djcrate:// handler in the "
                          "Windows registry.")
        return {"key": BROWSER_HANDLER_KEY,
                "value": protocolreg.protocol_is_registered()}
```

- [ ] **Step 7: Run to verify pass**

Run: `python -m pytest tests/test_browser_settings.py tests/test_settings.py tests/test_settings_bindings.py tests/test_service.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS — including `test_settings_all_serves_every_key_the_settings_screen_draws`, which now demands both new keys be served.

- [ ] **Step 8: Commit**

```bash
git add cratebuilder/settings.py cratebuilder/service.py cratebuilder/ui_strings.py UI-design/ui-contract.json tests/test_settings.py tests/test_browser_settings.py
git commit -m "feat(settings): browser integration receive mode and djcrate:// handler toggle"
```

---

### Task 5: Quiet inbox — persistence

**Files:**
- Modify: `cratebuilder/db.py:84` (`SCHEMA_VERSION`), `:198-262` (the `executescript` in `_init_schema`), migrations comment before `:331`, new methods after `add_watchlist_channel` (`:1057`)
- Test: `tests/test_db.py:288-295`, `:440-447`, `:558-564` (the three `"7"` asserts) + append inbox tests

**Interfaces:**
- Consumes: nothing new.
- Produces on `DownloadsDatabase`: `add_inbox_item(*, url, kind, received_at=None) → bool` (False = coalesced duplicate); `list_inbox() → list[sqlite3.Row]` with columns `id, url, kind, received_at`, oldest first; `get_inbox_item(item_id) → sqlite3.Row | None`; `remove_inbox_item(item_id) → None`; `inbox_count() → int`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_db.py`, change every schema-version assert from 7 to 8: `test_schema_version_is_7` becomes `test_schema_version_is_8` with `row["value"] == "8"` and `SCHEMA_VERSION == 8` (lines 288–295), and the two later `version == "7"` / `row["value"] == "7"` asserts (lines 447 and 564) become `"8"`. Then append, using the file's existing `_new_db(tmp_path)` helper:

```python
# ── browser inbox (schema v8) ────────────────────────────────────────────────

def test_inbox_add_list_get_remove_count(tmp_path):
    db = _new_db(tmp_path)
    assert db.inbox_count() == 0
    assert db.add_inbox_item(url="https://soundcloud.com/a", kind="channel",
                             received_at=100) is True
    assert db.add_inbox_item(url="https://soundcloud.com/a/b", kind="track",
                             received_at=200) is True
    assert db.inbox_count() == 2
    rows = db.list_inbox()
    assert [r["url"] for r in rows] == [
        "https://soundcloud.com/a", "https://soundcloud.com/a/b"]
    assert rows[0]["kind"] == "channel"
    assert db.get_inbox_item(rows[1]["id"])["url"] == "https://soundcloud.com/a/b"
    assert db.get_inbox_item(999999) is None
    db.remove_inbox_item(rows[0]["id"])
    assert db.inbox_count() == 1


def test_inbox_duplicate_url_is_coalesced(tmp_path):
    db = _new_db(tmp_path)
    assert db.add_inbox_item(url="https://soundcloud.com/a", kind="channel") is True
    assert db.add_inbox_item(url="https://soundcloud.com/a", kind="channel") is False
    assert db.inbox_count() == 1


def test_inbox_survives_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    DownloadsDatabase(path).add_inbox_item(
        url="https://soundcloud.com/a", kind="channel")
    assert DownloadsDatabase(path).inbox_count() == 1


def test_an_existing_v7_database_gains_the_inbox_table(tmp_path):
    """The CREATE TABLE IF NOT EXISTS is the migration: a database opened by
    this build gets the table, and its version stamp moves to 8."""
    path = str(tmp_path / "old.db")
    db = DownloadsDatabase(path)
    with db._conn() as conn:
        conn.execute("DROP TABLE browser_inbox")
        conn.execute("UPDATE schema_info SET value = '7' WHERE key = 'version'")
    reopened = DownloadsDatabase(path)
    assert reopened.inbox_count() == 0
    with reopened._conn() as conn:
        assert conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()["value"] == "8"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_db.py -q`
Expected: the four new tests FAIL on missing methods / missing table, and the version tests FAIL on `'7' != '8'` — proving both edits are observed.

- [ ] **Step 3: Implement.** Bump line 84 to `SCHEMA_VERSION = 8`. Inside the `executescript("""…""")` block of `_init_schema`, after the `idx_unavail_channel_url` index and before the closing `"""`:

```sql
                    CREATE TABLE IF NOT EXISTS browser_inbox (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        url         TEXT NOT NULL UNIQUE,
                        kind        TEXT NOT NULL,
                        received_at INTEGER NOT NULL
                    );
```

In the migrations section, directly before the `conn.execute("INSERT OR REPLACE INTO schema_info …` at line 331, add in the house voice:

```python
                # schema v8: browser_inbox — sends the browser extension made
                # while receive mode was "Collect quietly", kept until the
                # user processes or discards them (extension repo, SPEC §10).
                # The CREATE TABLE IF NOT EXISTS above IS the migration: an
                # existing database gains the table on its next open, and
                # url is UNIQUE so a repeated send coalesces at the row.
```

Then add the five methods directly after `add_watchlist_channel` (`:1057`):

```python
    # ── browser inbox ─────────────────────────────────────────────────────────

    def add_inbox_item(self, *, url, kind, received_at=None):
        """Queue a browser-extension send for later processing. A URL already
        pending is silently coalesced (returns False); a fresh row returns
        True, which is what drives the tray ping."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO browser_inbox (url, kind, received_at) "
                "VALUES (?, ?, ?)",
                (url, kind, int(received_at if received_at is not None
                                else time.time())))
            return cur.rowcount == 1

    def list_inbox(self):
        """All pending browser sends, oldest first."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, url, kind, received_at FROM browser_inbox "
                "ORDER BY received_at, id").fetchall()

    def get_inbox_item(self, item_id):
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, url, kind, received_at FROM browser_inbox "
                "WHERE id = ?", (item_id,)).fetchone()

    def remove_inbox_item(self, item_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM browser_inbox WHERE id = ?", (item_id,))

    def inbox_count(self):
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM browser_inbox").fetchone()[0]
```

`time` is already imported at the top of `db.py` (line 7).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_db.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/db.py tests/test_db.py
git commit -m "feat(db): browser_inbox table at schema v8 with URL coalescing"
```

---

### Task 6: `service.browser_receive` — dispatch, events, inbox RPCs, snapshot

**Files:**
- Modify: `cratebuilder/service.py` — `__init__` (after `self.on_open_howto = None`, `:891`), `_methods()` (`:1164-1272`), `snapshot()` (`:1279-1311`), new section after `_set_browser_handler` (Task 4)
- Test: `tests/test_service_browser.py`

**Interfaces:**
- Consumes: `browserlink.parse_djcrate_uri` (Task 1); `RECEIVE_MODE_QUIET`, `BROWSER_SEND`, `BROWSER_INBOX` (Task 4); `DownloadsDatabase` inbox methods (Task 5); existing `self._db()` (None when no file), `self._db_for_write()`, `self.emit`, `self._lock`, `self.transport`, `LOCAL`.
- Produces:
  - `service.browser_receive(uri) → dict` — never raises; `{"action": "opened"|"parked"|"queued"|"rejected"|"ignored", …}`. Task 7 calls it from the socket listener and the entry point.
  - `service.on_bring_forward` — hook attribute (`None` by default); Task 7 binds it to `restore_window`.
  - Snapshot gains `"browser": {"inbox_count": int, "pending": [{kind, url}, …]}`. `pending` is non-empty only on the **local** transport's snapshot and is drained by serving it — Task 8's page acts on it once after boot.
  - RPC methods `browser.inbox_list → [{id, kind, url, received_at}]`, `browser.inbox_take {id} → {kind, url}` (removes the row), `browser.inbox_remove {id} → {count}`. Each inbox change emits `BROWSER_INBOX {count, added}`.
  - Events: `BROWSER_SEND {kind, url}` (window mode, once the page is ready); `BROWSER_INBOX {count, added: {kind,url}|None}`; and a `notification` for a rejected send (`level: "warn"`) or a freshly queued one (`level: "info"`).

**Why the pending list exists:** on a cold start by the protocol handler there is no page yet to receive `browser.send`. `web/app.js`'s `boot()` registers every `cbApi.on` handler *before* it asks for `state.snapshot`, so the first local snapshot is the one moment after which a live event is guaranteed to land. Window-mode sends that arrive earlier are parked and handed over in that snapshot; the flag flips there too, so everything after is live. Quiet-mode sends never park — the inbox row is the durable thing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_service_browser.py
"""browser_receive: window mode, quiet mode, parking before the page is up,
the inbox RPCs, and what each of them emits."""
from urllib.parse import quote

import pytest

from cratebuilder.service import (BROWSER_INBOX, BROWSER_SEND, LOCAL, REMOTE,
                                  RECEIVE_MODE_QUIET, CBError,
                                  CrateBuilderService)
from cratebuilder.settings import Settings


def _uri(kind, url):
    return f"djcrate://add?v=1&kind={kind}&url={quote(url, safe='')}"


CHANNEL = "https://soundcloud.com/someartist"
TRACK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def settings(tmp_path):
    s = Settings(path=str(tmp_path / "config.json"))
    s.set("base_dir", str(tmp_path / "crate"))
    return s


@pytest.fixture
def service(settings, tmp_path):
    svc = CrateBuilderService(settings=settings,
                              db_path=str(tmp_path / "cratebuilder.db"))
    svc.events_seen = []
    svc.events.subscribe(lambda t, p: svc.events_seen.append((t, p)))
    svc.brought_forward = 0

    def _forward():
        svc.brought_forward += 1

    svc.on_bring_forward = _forward
    yield svc
    svc.close()


def _of(service, event_type):
    return [p for t, p in service.events_seen if t == event_type]


def _ready(service):
    """The page's first snapshot, which is what makes live sends deliverable."""
    return service.call("state.snapshot", transport=LOCAL)


# ── window mode ──────────────────────────────────────────────────────────────

def test_a_send_before_the_page_is_up_is_parked_then_handed_over_once(service):
    assert service.browser_receive(_uri("channel", CHANNEL)) == {"action": "parked"}
    assert _of(service, BROWSER_SEND) == []
    assert service.brought_forward == 1
    first = _ready(service)
    assert first["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]
    assert service.brought_forward == 2          # shown again as it is handed over
    second = _ready(service)
    assert second["browser"]["pending"] == []


def test_a_send_after_the_page_is_up_is_emitted_live(service):
    _ready(service)
    assert service.browser_receive(_uri("track", TRACK)) == {"action": "opened"}
    assert _of(service, BROWSER_SEND) == [{"kind": "track", "url": TRACK}]
    assert _ready(service)["browser"]["pending"] == []


def test_a_remote_snapshot_never_drains_the_window_pending_list(service):
    service.browser_receive(_uri("channel", CHANNEL))
    remote = service.call("state.snapshot", transport=REMOTE)
    assert remote["browser"]["pending"] == []
    assert _ready(service)["browser"]["pending"] == [{"kind": "channel", "url": CHANNEL}]


def test_window_mode_leaves_the_inbox_alone(service):
    _ready(service)
    service.browser_receive(_uri("channel", CHANNEL))
    assert service.browser_inbox_count() == 0
    assert _of(service, BROWSER_INBOX) == []


# ── quiet mode ───────────────────────────────────────────────────────────────

def test_quiet_mode_queues_and_pings_without_bringing_the_window_forward(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    _ready(service)
    assert service.browser_receive(_uri("channel", CHANNEL)) == {
        "action": "queued", "fresh": True}
    assert service.brought_forward == 0
    assert _of(service, BROWSER_SEND) == []
    assert _of(service, BROWSER_INBOX) == [
        {"count": 1, "added": {"kind": "channel", "url": CHANNEL}}]
    notes = _of(service, "notification")
    assert notes and notes[-1]["level"] == "info"
    assert service.browser_inbox_count() == 1


def test_quiet_mode_coalesces_a_duplicate_send(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("channel", CHANNEL))
    assert service.browser_receive(_uri("channel", CHANNEL)) == {
        "action": "queued", "fresh": False}
    assert service.browser_inbox_count() == 1
    assert _of(service, BROWSER_INBOX)[-1] == {"count": 1, "added": None}
    assert len(_of(service, "notification")) == 1    # no second ping


def test_quiet_mode_works_before_the_page_is_up(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("track", TRACK))
    snap = _ready(service)
    assert snap["browser"] == {"inbox_count": 1, "pending": []}


# ── the inbox RPCs ───────────────────────────────────────────────────────────

def test_inbox_list_take_and_remove(service, settings):
    settings.set("browser_receive_mode", RECEIVE_MODE_QUIET)
    service.browser_receive(_uri("channel", CHANNEL))
    service.browser_receive(_uri("track", TRACK))
    rows = service.call("browser.inbox_list")
    assert [(r["kind"], r["url"]) for r in rows] == [
        ("channel", CHANNEL), ("track", TRACK)]
    assert set(rows[0]) == {"id", "kind", "url", "received_at"}
    taken = service.call("browser.inbox_take", {"id": rows[0]["id"]})
    assert taken == {"kind": "channel", "url": CHANNEL}
    assert service.browser_inbox_count() == 1
    assert _of(service, BROWSER_INBOX)[-1] == {"count": 1, "added": None}
    assert service.call("browser.inbox_remove", {"id": rows[1]["id"]}) == {"count": 0}
    assert service.call("browser.inbox_list") == []


def test_taking_a_row_that_is_gone_is_a_user_facing_error(service):
    with pytest.raises(CBError):
        service.call("browser.inbox_take", {"id": 12345})


def test_inbox_calls_without_a_database_are_empty_not_errors(service, tmp_path):
    assert service.call("browser.inbox_list") == []
    assert service.call("browser.inbox_remove", {"id": 1}) == {"count": 0}
    assert not (tmp_path / "cratebuilder.db").exists()


# ── contract §4: errors ──────────────────────────────────────────────────────

def test_a_newer_contract_warns_and_brings_the_window_forward(service):
    _ready(service)
    result = service.browser_receive(
        "djcrate://add?v=99&kind=track&url=https%3A%2F%2Fsoundcloud.com%2Fa%2Fb")
    assert result == {"action": "rejected"}
    note = _of(service, "notification")[-1]
    assert note["level"] == "warn" and "newer version" in note["body"]
    assert service.brought_forward == 1
    assert _of(service, BROWSER_SEND) == []


def test_a_bad_url_warns_with_the_unsupported_message(service):
    result = service.browser_receive(
        "djcrate://add?v=1&kind=track&url=https%3A%2F%2Fevil.example%2Fa")
    assert result == {"action": "rejected"}
    assert "supported YouTube or SoundCloud" in _of(service, "notification")[-1]["body"]


def test_junk_is_ignored_silently_and_never_raises(service):
    for junk in ("djcrate://frobnicate", "show", "", None, object()):
        assert service.browser_receive(junk) == {"action": "ignored"}
    assert service.events_seen == []
    assert service.brought_forward == 0


def test_a_throwing_bring_forward_hook_does_not_break_the_receive(service):
    def boom():
        raise RuntimeError("window already closing")
    service.on_bring_forward = boom
    _ready(service)
    assert service.browser_receive(_uri("channel", CHANNEL)) == {"action": "opened"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_service_browser.py -q`
Expected: FAIL — `AttributeError: 'CrateBuilderService' object has no attribute 'browser_receive'` (the `settings` fixture and snapshot still work).

- [ ] **Step 3: `__init__` state.** Directly after `self.on_open_howto = None` (`:891`):

```python
        # The desktop window's "come forward" — web_window.py binds
        # restore_window here. None everywhere else, and a browser send then
        # only emits its event (a remote-only host has no window to raise).
        self.on_bring_forward = None
        # Window-mode browser sends that arrived before the app window had a
        # page to show them on — a cold start by the protocol handler. The
        # page's first snapshot drains them; see _browser_snapshot for why
        # that is the one safe hand-over point.
        self._browser_pending = []
        self._local_page_ready = False
```

- [ ] **Step 4: Dispatch table.** In `_methods()`, after the `"watchlist.import_done"` entry:

```python
            "browser.inbox_list": lambda p: self.browser_inbox_list(),
            "browser.inbox_take": lambda p: self.browser_inbox_take(p.get("id")),
            "browser.inbox_remove":
                lambda p: self.browser_inbox_remove(p.get("id")),
```

- [ ] **Step 5: Snapshot.** In `snapshot()`, after the `"genres": self.genres(),` line:

```python
            "browser": self._browser_snapshot(),
```

- [ ] **Step 6: The receive section.** Add directly after `_set_browser_handler` (Task 4):

```python
    # ── browser extension (djcrate:// sends) ──────────────────────────────────
    # Contract: the extension repo's docs/specs/djcrate-uri-v1.md; design:
    # its docs/SPEC.md §10. Reached from the single-instance listener's
    # thread and from the entry point, never from the page.

    def browser_receive(self, uri):
        """Act on one djcrate:// send. Never raises — the listener thread has
        to survive whatever the browser hands it, and the entry point calls
        this before the window exists.

        Window mode brings the window forward and hands the send to the page,
        live if it has one, otherwise parked for its first snapshot. Quiet
        mode writes the inbox row and says so, and leaves the window alone —
        that is the whole point of the mode. Contract errors surface as a
        warning notification (bell + toast) rather than a dialog: the page
        may not exist yet, and a modal that steals focus for a bad send is
        worse than a note that waits.
        """
        result = browserlink.parse_djcrate_uri(uri)
        if isinstance(result, browserlink.ParseError):
            if not result.message:
                return {"action": "ignored"}
            self.emit("notification", {"level": "warn", "title": "Browser send",
                                       "body": result.message, "at": time.time()})
            self._bring_forward()
            return {"action": "rejected"}
        send = {"kind": result.kind, "url": result.url}
        if self._settings.get("browser_receive_mode") == RECEIVE_MODE_QUIET:
            fresh = self._db_for_write().add_inbox_item(url=result.url,
                                                        kind=result.kind)
            count = self.browser_inbox_count()
            self.emit(BROWSER_INBOX, {"count": count,
                                      "added": send if fresh else None})
            if fresh:
                self.emit("notification", {
                    "level": "info", "title": "Browser send queued",
                    "body": (f"A {result.kind} from your browser is waiting in "
                             f"the Browser Inbox ({count} pending)."),
                    "at": time.time()})
            return {"action": "queued", "fresh": fresh}
        with self._lock:
            ready = self._local_page_ready
            if not ready:
                self._browser_pending.append(send)
        if ready:
            self.emit(BROWSER_SEND, send)
        self._bring_forward()
        return {"action": "opened" if ready else "parked"}

    def _bring_forward(self):
        hook = self.on_bring_forward
        if hook is None:
            return
        try:
            hook()
        except Exception:
            pass                    # a window mid-close; the send still lands

    def _browser_snapshot(self):
        """The inbox count for every page, plus — for the app window only —
        the window-mode sends that arrived before it had a page. Draining
        them here is what makes a cold-start send open its dialog exactly
        once: web/app.js's boot() registers its event handlers BEFORE it asks
        for this snapshot, so from this call on a live browser.send reaches
        the page, and nothing before it could have. A remote page's snapshot
        is not that moment — the send belongs to the desktop the browser is
        on — so it neither drains nor flips the flag."""
        pending = []
        if self.transport == LOCAL:
            with self._lock:
                self._local_page_ready = True
                pending, self._browser_pending = self._browser_pending, []
            if pending:
                # A start-minimised launch has just hidden the window the
                # first bring-forward showed; this one lands after it.
                self._bring_forward()
        return {"inbox_count": self.browser_inbox_count(), "pending": pending}

    def browser_inbox_count(self):
        db = self._db()
        return db.inbox_count() if db is not None else 0

    def browser_inbox_list(self):
        db = self._db()
        if db is None:
            return []
        return [{"id": r["id"], "kind": r["kind"], "url": r["url"],
                 "received_at": r["received_at"]} for r in db.list_inbox()]

    def browser_inbox_take(self, item_id):
        """Hand one queued send to the page to open, and drop it from the
        inbox — quiet mode's per-row Process. The page opens the same flow a
        window-mode send would have."""
        db = self._db()
        row = db.get_inbox_item(item_id) if db is not None else None
        if row is None:
            raise CBError("That browser send is no longer in the inbox.")
        db.remove_inbox_item(item_id)
        self.emit(BROWSER_INBOX, {"count": db.inbox_count(), "added": None})
        return {"kind": row["kind"], "url": row["url"]}

    def browser_inbox_remove(self, item_id):
        db = self._db()
        if db is not None:
            db.remove_inbox_item(item_id)
        count = self.browser_inbox_count()
        self.emit(BROWSER_INBOX, {"count": count, "added": None})
        return {"count": count}
```

- [ ] **Step 7: Run to verify pass**

Run: `python -m pytest tests/test_service_browser.py tests/test_service.py -q` then `python -m pytest -q -m "not gui"`
Expected: PASS. If a snapshot-shape test elsewhere enumerates top-level keys exactly, add `"browser"` to its expected set.

- [ ] **Step 8: Commit**

```bash
git add cratebuilder/service.py tests/test_service_browser.py
git commit -m "feat(service): receive djcrate:// sends — window/quiet modes, inbox RPCs, browser.* events"
```

---

### Task 7: `web_window.py` — argv, listener swap, bring-forward hook, tray balloon

**Files:**
- Modify: `web_window.py:35-36` (singleton import), `:33-34` (service import), `:673-687` (`acquire_or_hand_off`), `:454-473` (`WindowTray._on_event`) + a new `_balloon` beside `_notify` (`:281`), `main()` at `:1150` (lock), `:1195-1198` (hooks), `:1215` (listener)
- Test: `tests/test_web_window.py` (append)

**Interfaces:**
- Consumes: `forward_add`, `listen_for_requests` (Task 2); `service.browser_receive`, `service.on_bring_forward`, `BROWSER_INBOX` (Task 6); existing `restore_window(window)`, `WindowTray._icon`, `WINDOW_TITLE`.
- Produces: `djcrate_uri_arg(argv) → str | None`; `acquire_or_hand_off(port=…, uri=None)` forwarding the URI instead of `show` when it loses the bind; the running window wired so a socket `add` reaches `service.browser_receive`; a tray balloon on a fresh quiet-mode arrival.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_web_window.py`, after the existing `acquire_or_hand_off` tests (`:261-268`):

```python
# ── djcrate:// protocol-handler launches ─────────────────────────────────────

def test_djcrate_uri_arg_finds_the_protocol_argument_anywhere_in_argv():
    uri = "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fa"
    assert web_window.djcrate_uri_arg(["exe", uri]) == uri
    assert web_window.djcrate_uri_arg(["exe", "--screen", "watchlist", uri]) == uri
    assert web_window.djcrate_uri_arg(["exe"]) is None
    assert web_window.djcrate_uri_arg(["exe", "--startup"]) is None
    assert web_window.djcrate_uri_arg(["djcrate://not-argv0"]) is None


def test_a_losing_launch_with_a_uri_forwards_it_instead_of_asking_for_show(monkeypatch):
    forwarded, asked = [], []
    monkeypatch.setattr(web_window, "acquire_single_instance", lambda port: None)
    monkeypatch.setattr(web_window, "forward_add",
                        lambda port, uri: forwarded.append((port, uri)))
    monkeypatch.setattr(web_window, "request_show", asked.append)
    with pytest.raises(SystemExit) as info:
        web_window.acquire_or_hand_off(port=49737, uri="djcrate://add?v=1")
    assert info.value.code == 0
    assert forwarded == [(49737, "djcrate://add?v=1")]
    assert asked == []


def test_a_winning_launch_with_a_uri_keeps_the_lock(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(web_window, "acquire_single_instance", lambda port: sentinel)
    assert web_window.acquire_or_hand_off(port=0, uri="djcrate://add?v=1") is sentinel


def test_main_wires_the_listener_and_the_bring_forward_hook():
    """main() cannot run headless, so pin the wiring as text: the two-verb
    listener with browser_receive as its add handler, the hook, and the
    cold-start receive — each on its own line so a refactor that drops one
    fails here and not in the field."""
    src = inspect.getsource(web_window.main)
    assert "uri = djcrate_uri_arg(sys.argv)" in src
    assert "lock = acquire_or_hand_off(uri=uri)" in src
    assert "service.on_bring_forward = lambda: restore_window(window)" in src
    assert ("listen_for_requests(lock, on_show=lambda: restore_window(window),\n"
            "                        on_add=service.browser_receive)") in src
    assert "if uri:\n" in src and "service.browser_receive(uri)" in src
```

And in the tray section, after `test_a_refused_menu_action_is_logged_and_notified_never_raised`:

```python
def test_a_fresh_quiet_mode_send_raises_a_tray_balloon(make_tray):
    tray, window, service, icons = make_tray()
    tray._ensure()                                   # the icon is up
    service.events.emit(web_window.BROWSER_INBOX,
                        {"count": 2, "added": {"kind": "track", "url": "https://x"}})
    assert icons[-1].notifications == [
        ("Queued a track from your browser — 2 waiting in the Browser Inbox.",
         web_window.WINDOW_TITLE)]


def test_a_coalesced_or_processed_inbox_change_stays_silent(make_tray):
    tray, window, service, icons = make_tray()
    tray._ensure()
    service.events.emit(web_window.BROWSER_INBOX, {"count": 1, "added": None})
    assert icons[-1].notifications == []


def test_no_icon_means_no_balloon_and_no_error(make_tray):
    tray, window, service, icons = make_tray()
    service.events.emit(web_window.BROWSER_INBOX,
                        {"count": 1, "added": {"kind": "channel", "url": "https://x"}})
    assert icons == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_web_window.py -q`
Expected: FAIL — `AttributeError: module 'web_window' has no attribute 'djcrate_uri_arg'` and friends.

- [ ] **Step 3: Imports.** Replace lines 33–36:

```python
from cratebuilder.service import (BROWSER_INBOX, JOB_FINISHED, LOCAL, CBError,
                                  CrateBuilderService, app_icon_path)
from cratebuilder.singleton import (SINGLE_INSTANCE_PORT, acquire_single_instance,
                                    forward_add, listen_for_requests, request_show)
```

- [ ] **Step 4: argv + hand-off.** Directly above `acquire_or_hand_off` add:

```python
def djcrate_uri_arg(argv):
    """The djcrate:// URI a protocol-handler launch carries, or None.

    Hand-parsed like host_allow_args: this entry point has no parser. argv[0]
    is skipped — it is the script or exe, never a payload.
    """
    for token in argv[1:]:
        if token.startswith("djcrate://"):
            return token
    return None
```

Replace `acquire_or_hand_off` with:

```python
def acquire_or_hand_off(port=SINGLE_INSTANCE_PORT, uri=None):
    """Claim the single-instance lock, or hand off to the instance that
    already holds it and exit.

    Shares SINGLE_INSTANCE_PORT with the tkinter app deliberately — both
    own the same database and must never run together. A launch carrying a
    djcrate:// URI (the OS answering a browser send) forwards the payload to
    the running instance instead of asking it to show its window — the
    running app decides whether to surface, per its receive mode. Returns the
    bound lock socket on success; the caller must keep a reference to it for
    the whole process lifetime, or it is garbage-collected and the lock
    silently released.
    """
    lock = acquire_single_instance(port)
    if lock is None:
        if uri:
            forward_add(port, uri)
        else:
            request_show(port)
        sys.exit(0)
    return lock
```

- [ ] **Step 5: `main()` wiring.** Change the lock line (`:1150`) from `lock = acquire_or_hand_off()` to:

```python
    uri = djcrate_uri_arg(sys.argv)
    lock = acquire_or_hand_off(uri=uri)
```

After `service.on_open_howto = howto.open` (`:1198`) add:

```python
    # A browser send in window mode raises the window from the tray or the
    # taskbar before the page opens its dialog. Thread-safe by restore_window's
    # own contract — the listener thread is the usual caller.
    service.on_bring_forward = lambda: restore_window(window)
```

Replace the listener line (`:1215`) `listen_for_show_requests(lock, lambda: restore_window(window))` with:

```python
    listen_for_requests(lock, on_show=lambda: restore_window(window),
                        on_add=service.browser_receive)
    if uri:
        # This launch WAS the protocol-handler invocation and won the bind.
        # Window mode parks the send until the page's first snapshot (the
        # bridge below has nothing to push to yet); quiet mode writes its
        # inbox row right now, before any window exists.
        service.browser_receive(uri)
```

Keep it where the old listener line was — after `window.events.minimized += tray.on_minimized`, before `def started():`.

- [ ] **Step 6: Tray balloon.** In `WindowTray`, directly after `_notify` add:

```python
    def _balloon(self, message):
        """A tray balloon when the icon is up, and nothing otherwise — with
        the window visible the page's own toast and bell carry the news."""
        icon = self._icon
        if icon is not None:
            icon.notify(message, WINDOW_TITLE)
```

In `_on_event`, add a branch before the `elif type == JOB_FINISHED:` line:

```python
        elif type == BROWSER_INBOX:
            added = payload.get("added")
            if added:
                self._balloon(
                    f"Queued a {added.get('kind')} from your browser — "
                    f"{int(payload.get('count') or 0)} waiting in the Browser Inbox.")
```

- [ ] **Step 7: Run + manual verify (house rule for anything the window does)**

Run: `python -m pytest tests/test_web_window.py tests/test_singleton.py -q` then `python -m pytest -q -m "not gui"` — PASS.

Launch `python web_window.py`, then from a second terminal:

```powershell
python web_window.py "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fsomeartist"
```

Expected at this point (the page has no handler yet — Task 8 adds it): the second process exits immediately, the running window comes forward, and the debug/activity logs show nothing alarming. With Settings ▸ Browser Integration set to "Collect quietly" (the row renders already, from Task 4's contract change): the window stays put and, if it was in the tray, a balloon appears.

- [ ] **Step 8: Commit**

```bash
git add web_window.py tests/test_web_window.py
git commit -m "feat(window): receive djcrate:// launches — argv, socket add verb, bring-forward hook, tray balloon"
```

---

### Task 8: Page — window-mode handling and the Settings rows' behaviour

**Files:**
- Modify: `web/app.js` — `openAddChannel()` (`:2397`), `applySettingsDependencies()` (`:4808`, the `run_at_startup` block at `:4864`), `SECTION_TOOLTIPS` (`:5723`), `boot()` (`:7521`), plus a new `subscribeBrowserEvents()` and `handleBrowserSend()`
- Test: `tests/test_web_browser_client.py` (new)

**Interfaces:**
- Consumes: `browser.send` event `{kind, url}`, snapshot `state.browser.pending` (Task 6); existing `show(name)`, `openModal`, `toast`, `$`, `cbApi.transport`.
- Produces: `handleBrowserSend(send)` (Task 9's inbox Process calls it); `openAddChannel(prefill)` where a string prefill fills the URL box (the `#wl-add` click still passes its Event, which is ignored); `drainBrowserPending()`.

**Read `.claude/skills/changing-the-web-ui/SKILL.md` first.** Every function below is new or extended in place — none is renamed, none re-indented.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_browser_client.py
"""web/app.js: browser-extension sends — the prefilled flows a window-mode
send opens, the boot-time drain, and the Settings rows' local-only gate.
Static half pins the wiring; the Node half runs handleBrowserSend against a
stub DOM, sliced out of app.js verbatim like the other client tests."""
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


# ── static: the wiring exists ────────────────────────────────────────────────

def test_the_page_subscribes_to_both_browser_events(app_js):
    assert "cbApi.on('browser.send'" in app_js
    assert "cbApi.on('browser.inbox'" in app_js
    assert "subscribeBrowserEvents();" in _slice(app_js, "  async function boot()", "\n  }\n")


def test_live_sends_are_acted_on_by_the_app_window_only(app_js):
    handler = _slice(app_js, "    cbApi.on('browser.send'", "    });")
    assert "cbApi.transport !== 'local'" in handler


def test_boot_drains_the_parked_sends_after_the_first_screen_is_shown(app_js):
    """After show(): drainBrowserPending navigates to the Watch List or
    Downloads, and boot's own show(hash || 'overview') must not run after it
    and flip the screen back under the dialog it just opened."""
    body = _slice(app_js, "  async function boot()", "\n  }\n")
    assert body.index("show(location.hash.slice(1) || 'overview');") \
        < body.index("drainBrowserPending();")


def test_the_add_channel_modal_takes_a_prefill(app_js):
    body = _slice(app_js, "  function openAddChannel(prefill)", "  /* ── Remove (plain yes/no)")
    assert "typeof prefill === 'string'" in body
    assert "urlEl.value = " in body


def test_the_handler_toggle_is_local_only_like_run_at_startup(app_js):
    body = _slice(app_js, "  function applySettingsDependencies()", "\n  }\n")
    assert "set('browser_handler', remoteMount," in body


def test_the_section_carries_its_help_icon(app_js):
    assert "'Browser Integration': 'settings.browser_integration'" in app_js


# ── node: handleBrowserSend opens the right flow ─────────────────────────────

_HARNESS = """
const shown = [];
function show(name) { shown.push(name); }
const opened = [];
function openAddChannel(prefill) { opened.push(prefill); }
const toasts = [];
function toast(m) { toasts.push(m); }
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, value: '', events: [], focused: false,
    dispatchEvent(e) { this.events.push(e.type); },
    focus() { this.focused = true; } };
  return els[id];
}
global.Event = class { constructor(type) { this.type = type; } };
const state = { browser: { pending: [
  { kind: 'channel', url: 'https://soundcloud.com/a' },
  { kind: 'track', url: 'https://www.youtube.com/watch?v=x' } ] } };
%(fn)s
%(drain)s
handleBrowserSend(null);
handleBrowserSend({ kind: 'track', url: '' });
const untouched = { shown: shown.slice(), opened: opened.slice() };
drainBrowserPending();
console.log(JSON.stringify({ untouched, shown, opened, toasts,
  url: $('#dl-url').value, events: $('#dl-url').events,
  genreFocused: $('#dl-genre').focused }));
"""


def test_a_channel_opens_add_channel_prefilled_and_a_track_prefills_downloads(app_js, tmp_path):
    r = _run_node(tmp_path, "browsersend.mjs", _HARNESS % {
        "fn": _slice(app_js, "  function handleBrowserSend(send)",
                     "  function drainBrowserPending()"),
        "drain": _slice(app_js, "  function drainBrowserPending()",
                        "  function subscribeBrowserEvents()"),
    })
    assert r["untouched"] == {"shown": [], "opened": []}      # empty sends do nothing
    assert r["shown"] == ["watchlist", "downloads"]
    assert r["opened"] == ["https://soundcloud.com/a"]
    assert r["url"] == "https://www.youtube.com/watch?v=x"
    assert r["events"] == ["input"]                           # renderBatch's trigger
    assert r["genreFocused"] is True
    assert any("pick a genre" in t for t in r["toasts"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_web_browser_client.py -q`
Expected: FAIL — `ValueError: substring not found` on every slice.

- [ ] **Step 3: Prefill on Add Channel.** Change the header `function openAddChannel() {` (`:2397`) to `function openAddChannel(prefill) {`, and directly after `urlEl.placeholder = …` inside `body(body)` add:

```js
        /* A browser-extension send arrives with the URL already known; the
           #wl-add click passes its Event here, which is not one. */
        if (typeof prefill === 'string') urlEl.value = prefill;
```

Leave `$('#wl-add').addEventListener('click', openAddChannel);` (`:7251`) exactly as it is.

- [ ] **Step 4: The send handler, the drain, the subscriptions.** Add a new section directly above `  /* ── Remove (plain yes/no) ─…` (`:2447`), i.e. right after `openAddChannel`:

```js
  /* ── browser extension sends ──────────────────────────────────────────────
     A djcrate:// send the host received (the extension repo's
     docs/specs/djcrate-uri-v1.md). Window mode hands it here as a
     `browser.send` event — or, when it arrived before this page existed, in
     the snapshot's browser.pending — and the page opens the flow the user
     would have opened by hand, prefilled. The browser can't know a genre, so
     nothing is added or queued until they pick one here. */
  function handleBrowserSend(send) {
    if (!send || !send.url) return;
    if (send.kind === 'channel') {
      show('watchlist');
      openAddChannel(send.url);
      return;
    }
    show('downloads');
    const box = $('#dl-url');
    box.value = send.url;
    /* The input listener is what lets Start see a pasted link; a value set
       by script does not fire it on its own. */
    box.dispatchEvent(new Event('input'));
    toast('Sent from your browser — pick a genre, then Add to Batch.');
    $('#dl-genre').focus();
  }

  /* Sends the host parked while this window had no page. The host clears
     the list as it serves the snapshot, so a reload never replays them. */
  function drainBrowserPending() {
    const pending = (state && state.browser && state.browser.pending) || [];
    pending.forEach(handleBrowserSend);
  }

  function subscribeBrowserEvents() {
    /* Only the app window acts on a live send: the extension runs on the
       host, and a paired phone should not have a dialog appear because
       someone clicked a button on the desktop. */
    cbApi.on('browser.send', (send) => {
      if (cbApi.transport !== 'local') return;
      handleBrowserSend(send);
    });
    cbApi.on('browser.inbox', (p) => {
      if (!state || !p) return;
      state.browser = Object.assign({}, state.browser, { inbox_count: p.count || 0 });
      renderBrowserInbox();
      renderOverviewAttention();
    });
  }
```

`renderBrowserInbox` is defined in Task 9; until then add a one-line placeholder **immediately below** `subscribeBrowserEvents` so the page does not throw on an inbox event, and Task 9 replaces it:

```js
  function renderBrowserInbox() {}
```

- [ ] **Step 5: Boot.** In `boot()`, add `subscribeBrowserEvents();` after `subscribeCleanupEvents();`, and add `drainBrowserPending();` as the **last** line of `boot()`, after `show(location.hash.slice(1) || 'overview');`.

- [ ] **Step 6: Settings behaviour.** In `applySettingsDependencies`, directly after the `set('run_at_startup', remoteMount, …);` statement:

```js
    // The djcrate:// handler is the host's own registry entry — app window only.
    set('browser_handler', remoteMount,
      'Browser integration can only be changed from the app window on the host machine.');
```

In `SECTION_TOOLTIPS`, add `'Browser Integration': 'settings.browser_integration',` after the `'Downloads Database'` line.

- [ ] **Step 7: Run + look**

Run: `python -m pytest -q tests/test_web_*_client.py` — PASS, including every pre-existing client test (none of the sliced functions changed).

Launch `python web_window.py --screen settings`: a "Browser Integration" card with the toggle, the mode select and a `?` on the card header, sitting above the Remote Access rule. **Check both themes.** Then, with the app running, from a second terminal:

```powershell
python web_window.py "djcrate://add?v=1&kind=channel&url=https%3A%2F%2Fsoundcloud.com%2Fsomeartist"
python web_window.py "djcrate://add?v=1&kind=track&url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ"
```

Expected: channel → the window comes forward on the Watch List with "+ Add Channel" open and the URL filled; track → the Downloads screen with the URL box filled and the genre select focused. Close the app entirely and repeat the channel send → the app launches and the dialog opens once the page is up. Send a `v=99` URI → a warning toast and a bell entry, no dialog.

- [ ] **Step 8: Commit**

```bash
git add web/app.js tests/test_web_browser_client.py
git commit -m "feat(web): open prefilled flows for browser-extension sends; browser integration settings gate"
```

---

### Task 9: Page — the Browser Inbox

**Files:**
- Modify: `web/index.html:281` (after `#wl-import`), `web/app.js` — replace the Task 8 placeholder `renderBrowserInbox`, add `openBrowserInbox()`, wire in `wireWatchlist()` (`:7248`), call from `renderWatchlist()` (`:2125`), add a row to `renderOverviewAttention()` (`:904`)
- Test: `tests/test_web_browser_client.py` (append)

**Interfaces:**
- Consumes: `browser.inbox_list` / `browser.inbox_take` / `browser.inbox_remove` RPCs and `state.browser.inbox_count` (Task 6); `handleBrowserSend` (Task 8); existing `openModal`, `modalButton`, `modalNote`, `tagNode`, `closeModal`, `num`, `ovEmpty`.
- Produces: nothing later tasks consume.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_web_browser_client.py`:

```python
# ── the inbox ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def index_html():
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as fh:
        return fh.read()


def test_the_inbox_button_exists_hidden_and_is_wired(app_js, index_html):
    assert 'id="wl-inbox" hidden' in index_html
    assert 'data-tt="wl.browser_inbox"' in index_html
    assert "$('#wl-inbox').addEventListener('click', openBrowserInbox);" in app_js
    assert "renderBrowserInbox();" in _slice(app_js, "  function renderWatchlist()", "\n  }\n")


def test_the_inbox_modal_processes_through_the_same_send_handler(app_js):
    body = _slice(app_js, "  function openBrowserInbox()", "\n  }\n")
    assert "cbApi.call('browser.inbox_list')" in body
    assert "cbApi.call('browser.inbox_take', { id: row.id })" in body
    assert "cbApi.call('browser.inbox_remove', { id: row.id })" in body
    assert "handleBrowserSend(send)" in body


def test_the_overview_counts_waiting_sends_under_needs_attention(app_js):
    body = _slice(app_js, "  function renderOverviewAttention()", "\n  }\n")
    assert "browserInboxCount()" in body
    assert "Browser Inbox" in body


_INBOX_HARNESS = """
const num = (n) => Number(n || 0).toLocaleString();
const els = {};
function $(sel) {
  const id = sel.slice(1);
  if (!els[id]) els[id] = { id, hidden: false, textContent: '' };
  return els[id];
}
let state = { browser: { inbox_count: 0 } };
%(count)s
%(render)s
renderBrowserInbox();
const empty = { hidden: $('#wl-inbox').hidden, text: $('#wl-inbox').textContent };
state.browser.inbox_count = 3;
renderBrowserInbox();
const three = { hidden: $('#wl-inbox').hidden, text: $('#wl-inbox').textContent };
state = null;
renderBrowserInbox();
const noState = { hidden: $('#wl-inbox').hidden };
console.log(JSON.stringify({ empty, three, noState }));
"""


def test_the_inbox_button_hides_at_zero_and_counts_otherwise(app_js, tmp_path):
    r = _run_node(tmp_path, "inboxbtn.mjs", _INBOX_HARNESS % {
        "count": _slice(app_js, "  function browserInboxCount()",
                        "  function renderBrowserInbox()"),
        "render": _slice(app_js, "  function renderBrowserInbox()",
                         "  function openBrowserInbox()"),
    })
    assert r["empty"]["hidden"] is True
    assert r["three"] == {"hidden": False, "text": "🌐 Browser Inbox (3)"}
    assert r["noState"]["hidden"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_web_browser_client.py -q`
Expected: the new tests FAIL (`substring not found` / `id="wl-inbox"` absent); Task 8's tests still pass.

- [ ] **Step 3: Markup.** In `web/index.html`, directly after the `#wl-import` button (`:281`), inside the same `cb-row`:

```html
        <button class="cb-btn cb-btn--quiet cb-btn--xs" id="wl-inbox" hidden
                data-tt="wl.browser_inbox">🌐 Browser Inbox</button>
```

- [ ] **Step 4: Button + modal.** Replace the Task 8 placeholder `function renderBrowserInbox() {}` with:

```js
  function browserInboxCount() {
    return (state && state.browser && state.browser.inbox_count) || 0;
  }

  /* Hidden at zero: the button is only news once quiet mode has queued
     something, and a toolbar that always showed it would leave users who
     never turned quiet mode on wondering what it is for. */
  function renderBrowserInbox() {
    const btn = $('#wl-inbox');
    if (!btn) return;
    const count = browserInboxCount();
    btn.hidden = !count;
    btn.textContent = `🌐 Browser Inbox (${num(count)})`;
  }

  function openBrowserInbox() {
    let list = null;
    const paint = async () => {
      list.innerHTML = '';
      let rows = [];
      try {
        rows = await cbApi.call('browser.inbox_list');
      } catch (err) {
        list.appendChild(modalNote(err.userFacing ? err.message
          : 'The host could not read the inbox.'));
        return;
      }
      if (!rows.length) { list.appendChild(modalNote('The inbox is empty.')); return; }
      rows.forEach((row) => {
        const line = document.createElement('div');
        line.className = 'cb-row';
        line.style.cssText = 'gap:8px;padding:6px 0;border-bottom:1px solid var(--cb-line-soft)';
        const url = document.createElement('span');
        url.className = 'cb-mono';
        url.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px';
        url.textContent = row.url;
        url.title = row.url;
        /* Process is the send arriving late: the same handler, the same
           prefilled dialog, and the row is gone from the host before the
           dialog opens so a Cancel there does not resurrect it. */
        const process = modalButton('Process', 'cb-btn--fill cb-btn--sm', async () => {
          try {
            const send = await cbApi.call('browser.inbox_take', { id: row.id });
            closeModal();
            handleBrowserSend(send);
          } catch (err) {
            toast(err.userFacing ? err.message
              : 'The host could not hand over that send.', true);
            paint();
          }
        });
        const remove = modalButton('Remove', 'cb-btn--quiet cb-btn--sm', async () => {
          try { await cbApi.call('browser.inbox_remove', { id: row.id }); }
          catch (_) { /* the browser.inbox event re-syncs the count either way */ }
          paint();
        });
        line.append(tagNode(row.kind, 'cb-tag--grey'), url, process, remove);
        list.appendChild(line);
      });
    };
    openModal({
      title: '🌐 Browser Inbox',
      width: 640,
      body(body) {
        body.appendChild(modalNote(
          'Sends collected while receive mode was "Collect quietly". Process ' +
          'opens the same prefilled dialog a send would have opened straight ' +
          'away; Remove discards it.'));
        list = document.createElement('div');
        body.appendChild(list);
        paint();
      },
      foot(foot) {
        const close = modalButton('Close', 'cb-btn--quiet', closeModal);
        close.style.marginLeft = 'auto';
        foot.appendChild(close);
      },
    });
  }
```

- [ ] **Step 5: Wire and render.** In `wireWatchlist()` after `$('#wl-import').addEventListener('click', openImportPicker);`:

```js
    $('#wl-inbox').addEventListener('click', openBrowserInbox);
```

In `renderWatchlist()`, as its first statement:

```js
    renderBrowserInbox();
```

- [ ] **Step 6: Overview.** In `renderOverviewAttention()`, directly before `if (!rows.length) {`:

```js
    const inbox = browserInboxCount();
    if (inbox) {
      rows.push([num(inbox),
        `browser send${inbox === 1 ? '' : 's'} waiting in the Browser Inbox (Watch List)`,
        'cb-tag--attn']);
    }
```

- [ ] **Step 7: Run + look**

Run: `python -m pytest -q tests/test_web_*_client.py` then `python -m pytest -q -m "not gui"` — PASS.

Launch the app, set receive mode to "Collect quietly", minimise to the tray, and run the two second-terminal sends from Task 8 Step 7. Expected: no window comes forward; a tray balloon; when the window is opened, the Overview's "Needs attention" lists the waiting sends and the Watch List toolbar shows "🌐 Browser Inbox (2)". Open it: Process on the channel row closes the inbox and opens Add Channel prefilled, the count drops to 1; Remove on the track row empties it and the button disappears. Send the same channel twice → one row. Restart the app with a row pending → still there. **Both themes.**

- [ ] **Step 8: Commit**

```bash
git add web/index.html web/app.js tests/test_web_browser_client.py
git commit -m "feat(web): Browser Inbox for quiet-mode sends, with an Overview attention row"
```

---

### Task 10: Full-suite gate, manual sweep, branch wrap-up

**Files:** none new.

- [ ] **Step 1: Full suite.** `python -m pytest -q` (both lanes, ~2 min). Every test green. The house rule is explicit: "done" requires this run and its output reported. Fix anything red before proceeding.

- [ ] **Step 2: Launch and eyeball, both themes.** `python web_window.py` — Settings card present and working (toggle on → `reg query "HKCU\Software\Classes\djcrate\shell\open\command"` shows the command ending in `"%1"`; toggle off → key gone; mode change survives a restart), window mode per Task 8 Step 7, quiet mode per Task 9 Step 7, the app-closed cold start for both modes, and the `v=99` warning.

- [ ] **Step 3: Cross-repo E2E.** Hand back to the extension repo's plan, Task 9 — the ten-row checklist at `DJ-CrateBuilder-Browser_Extensions/docs/e2e-checklist.md`, run in both Chrome and Firefox. Row 2's "Main tab prefilled" now reads as the Downloads screen's URL box.

- [ ] **Step 4: Stop.** Merging the branch and any push wait for an explicit ask, per CLAUDE.md. Use superpowers:finishing-a-development-branch to present the integration options.

---

## Self-review notes

- **Contract coverage:** §1/§4 parsing and messages → Task 1; §2 socket framing (line read, 8 KiB cap, `show` back-compat, unknown-verb ignore, throwing handler survives) → Task 2; §3 canonical-host validation → Task 1's `ACCEPTED_HOSTS`. SPEC §10: registry toggle → Tasks 3 + 4 (+ Task 8's local-only gate); receive-mode setting → Task 4; window-up mode with prefilled dialogs → Tasks 6 + 8; quiet inbox (persist across restarts, coalesce, tray ping, visible count, per-row process) → Tasks 5 + 6 + 7 + 9; Linux MimeType (and the `%u` the old plan missed) → Task 3, flagged unverifiable from Windows; "all parsing and dispatch in `cratebuilder/` as pure code, only the dialogs and tray ping in the shell" → the service owns dispatch, `web_window.py` owns the tray, `app.js` owns the dialogs.
- **What changed versus the tkinter plan:** no `_handle_djcrate_send` on a Tk app and no `after()` — the service emits events and the page acts; the cold-start delay is replaced by the parked-until-first-snapshot hand-over, which is exact rather than a 1.5 s guess; Settings rows come from the contract JSON, not hand-built widgets; the "visible count" is an Overview attention row plus a Watch List button; contract errors are a warning notification, not a modal.
- **House rules:** schema bump + migration comment → Task 5; generated `ui_strings.py` regenerated, never hand-edited → Task 4; no tkinter in `cratebuilder/`; no new colours, no `theme.css` edits; `cbApi` only; sliced functions (`renderWatchlistToolbar`, `renderOverview`, `applySettingsDependencies`, `renderSettings`) untouched or extended by insertion only; worktree, no-push, no-`APP_BUILD` → Global Constraints and Task 10.
- **Type consistency:** `browser_receive` return shapes match the Task 6 tests; `BROWSER_INBOX` payload `{count, added}` is what Task 7's tray and Task 8's page read; `browser.inbox_take` returns `{kind, url}`, which is exactly what `handleBrowserSend` takes; the snapshot key is `browser` with `inbox_count` + `pending` in Tasks 6, 8 and 9; the settings key `browser_receive_mode` stores `'window'|'quiet'` (Task 4) and Task 6 compares against `RECEIVE_MODE_QUIET`.
- **Known unknowns, surfaced not hidden:** whether any existing test enumerates `snapshot()`'s top-level keys exactly (Task 6 Step 7 says what to do); whether `gen_ui_strings.py`'s design-index bookkeeping shifts when four contract tooltips are added (Task 4 Step 4 names the counts to check); the exact `.desktop` heredoc lines in `install-linux.sh` (Task 3 quotes the anchor). Each names the check to run rather than guessing.

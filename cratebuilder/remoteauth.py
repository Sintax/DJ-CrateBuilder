"""Remote-access state: device tokens, pairing codes and the single-writer lock."""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

REMOTE_FILE_NAME = "cratebuilder_remote.json"

# HANDOFF §8.1 — a 6-digit code, good for five minutes, exchanged once.
PAIRING_CODE_TTL = 300
PAIRING_CODE_DIGITS = 6

# HANDOFF §8.5 — six digits is 10^6 and a phone can try fast, so the window is
# per client address rather than per code: rotating the code must not hand an
# attacker a fresh budget.
PAIR_ATTEMPT_LIMIT = 5
PAIR_ATTEMPT_WINDOW = 300

# A device that holds the control lock, has gone quiet for this long AND has no
# event socket open can be displaced by another client asking for it — a phone
# that walked out of range must not hold the host hostage until the process
# restarts. A holder with a live socket is never displaced by time alone: it is
# watching, which is exactly what a device driving a long batch is doing.
CONTROL_IDLE_SECONDS = 120

# Past this many distinct client keys, the attempt log is swept of everything
# whose window has closed. Bounded either way — the sweep is O(keys) and keys
# only accumulate between sweeps.
ATTEMPT_KEY_SWEEP_AT = 64

# How long a last_seen touch may sit in memory before it is written out. Every
# authenticated request updates it, and rewriting a JSON file per request would
# turn a progress stream into a disk-bound one.
LAST_SEEN_FLUSH_SECONDS = 60

FLAG_KEYS = ("enabled", "require_pairing", "read_only")

DEFAULTS = {
    # Nothing is reachable off this machine until the user says so. Never
    # flipped on by any code path but an explicit setting write.
    "enabled": False,
    "require_pairing": True,
    "read_only": False,
}

# Methods a remote client may call while it is read-only — because the host is
# in read-only mode, or because another device holds the control lock. The rule
# is "does it mutate state or launch work on the host": everything here answers
# a question and starts nothing.
#
# Deliberately absent, and why:
#   db.maintenance_preview   — pure counts, but it exists only to arm a
#                              destructive run; letting it through opens a
#                              confirm modal whose Confirm is refused.
#   watchlist.resolve_candidates — reads nothing local: it spends the host's
#                              yt-dlp session on a channel search, which is
#                              work, and it is step one of a write flow.
#   logs.download / db.export_csv are present: both only read and hand back
#                              bytes the viewer already has permission to see.
#   logs.watch is present: it starts a tail thread, not a job — without it the
#                              log viewers cannot follow a running batch, which
#                              is the whole point of a read-only session.
READ_METHODS = frozenset({
    "state.snapshot",
    "ui_strings",
    "settings.get",
    "batch.list",
    "watchlist.list",
    "watchlist.details",
    "db.groups",
    "db.query",
    "db.export_csv",
    "db.artwork_preview",
    "logs.tail",
    "logs.search",
    "logs.download",
    "logs.watch",
    "remote.config",
    "remote.devices",
    "remote.session",
})

# Transport-layer methods the server answers itself — they are about the
# connection, not the app, so they never reach CrateBuilderService.call.
CONTROL_METHODS = frozenset({"remote.claim_control", "remote.release_control"})

# What this device is allowed to do. Always answerable — it describes only the
# caller's own connection, so read-only mode has nothing to withhold.
SESSION_METHOD = "remote.session"

READ_ONLY_REASON = (
    "The host is in read-only mode. Remote browsers can watch progress and "
    "read logs, but cannot start, cancel, or change settings.")

UNPAIRED_REASON = "Pair this device to reach the host."

# `remote_enabled` is a live gate, not a startup-time bind decision: switching
# it off has to shut remote clients out of a host that is already listening.
DISABLED_REASON = (
    "Remote access is switched off on this host. Turn on “Allow remote "
    "control over the internet” in Settings ▸ Remote Access on the "
    "host machine to reach it from another device.")

RATE_LIMITED_REASON = (
    "Too many pairing attempts from this device. Wait five minutes and try "
    "again with a fresh code from the host.")

BAD_CODE_REASON = "That pairing code is not valid. Ask the host for a new one."


class PairingRefused(Exception):
    """A pairing attempt the host will not honour, with its HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ControlHeld(Exception):
    """Another device holds the single-writer lock."""

    def __init__(self, holder_name):
        self.holder_name = holder_name or "Another device"
        super().__init__(control_reason(self.holder_name))


def control_reason(holder_name):
    return (f"{holder_name or 'Another device'} has control of this host. "
            "Only one device can drive it at a time — wait for it to be "
            "released, or take control from the host window.")


def token_hash(token):
    """SHA-256 hex of a device token. The only form ever written to disk."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _matches(candidate_hash, stored_hash):
    """Constant-time hash comparison — never a plain ==."""
    return hmac.compare_digest(str(candidate_hash), str(stored_hash))


class RemoteState:
    """Everything the remote transport has to remember, backed by one JSON file.

    The file (`cratebuilder_remote.json`, beside the database) holds the three
    remote-access flags and one row per paired device carrying the SHA-256 of
    its token — never the token itself, so a leaked file cannot be replayed
    against the host. The live pairing code, the per-address attempt log and
    the control lock are in memory only: all three are meaningless across a
    restart, and the code in particular must never be written anywhere.

    *now* is injectable so the five-minute code TTL and the rate-limit window
    can be tested without sleeping.

    ONE instance per app dir. `singleton.py` already makes that true for the
    app (one host process, one database writer) and the desktop window hosts
    the server thread in-process rather than beside it, so the two mounts share
    this object. Two instances over one file would each hold their own copy and
    the last writer would win; `_maybe_flush` refuses to write over a file that
    changed underneath it, which stops the worst of it (a revoke undone by the
    other process's next `last_seen` touch), but the invariant is one instance.
    """

    def __init__(self, path, now=time.time):
        self._path = path
        self._now = now
        self._lock = threading.RLock()
        self._data = self._load()
        self._code = None            # {"code", "expires_at"} — memory only
        self._attempts = {}          # client address -> [attempt timestamps]
        self._control = None         # {"device_id", "name", "seen"}
        # device id -> {connection key: close callback}. Live event sockets,
        # which is what makes "this device is still here" answerable — see
        # register_connection.
        self._connections = {}
        self._conn_seq = 0
        self._last_flush = 0.0
        self._saved_mtime = self._mtime()

    # ── persistence ──────────────────────────────────────────────────────────

    @property
    def path(self):
        return self._path

    def _mtime(self):
        try:
            return os.path.getmtime(self._path)
        except OSError:
            return None

    def _load(self):
        data = {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
        out = dict(DEFAULTS)
        for key in FLAG_KEYS:
            if key in data:
                out[key] = bool(data.get(key))
        devices = []
        for row in data.get("devices") or []:
            if not isinstance(row, dict):
                continue
            digest = str(row.get("token_hash") or "")
            if len(digest) != 64:
                continue        # not a SHA-256 hex digest — drop it
            devices.append({
                "id": str(row.get("id") or secrets.token_hex(8)),
                "name": str(row.get("name") or "Device"),
                "token_hash": digest,
                "paired_at": int(row.get("paired_at") or 0),
                "last_seen": int(row.get("last_seen") or 0),
            })
        out["devices"] = devices
        return out

    def _save(self):
        """Write the file atomically — a half-written token store would lock
        every paired device out at once."""
        payload = {key: bool(self._data.get(key)) for key in FLAG_KEYS}
        payload["devices"] = [dict(row) for row in self._data["devices"]]
        folder = os.path.dirname(self._path)
        if folder:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                pass
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._last_flush = self._now()
        self._saved_mtime = self._mtime()

    # ── flags ────────────────────────────────────────────────────────────────

    def config(self):
        with self._lock:
            return {key: bool(self._data.get(key)) for key in FLAG_KEYS}

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

    # ── devices ──────────────────────────────────────────────────────────────

    def devices(self):
        """Public rows for the Settings card — ids and names, never hashes."""
        with self._lock:
            return [{"id": row["id"], "name": row["name"],
                     "paired_at": row["paired_at"], "last_seen": row["last_seen"]}
                    for row in self._data["devices"]]

    def device_count(self):
        with self._lock:
            return len(self._data["devices"])

    def authenticate(self, token):
        """The device this token belongs to, or None.

        Every stored hash is compared, and always with `hmac.compare_digest`,
        so neither the answer nor the time taken says which device came close.
        """
        if not token:
            return None
        digest = token_hash(token)
        with self._lock:
            for row in self._data["devices"]:
                if _matches(digest, row["token_hash"]):
                    row["last_seen"] = int(self._now())
                    self._maybe_flush()
                    return {"id": row["id"], "name": row["name"],
                            "paired_at": row["paired_at"],
                            "last_seen": row["last_seen"]}
        return None

    def _maybe_flush(self):
        """Persist a last_seen touch at most once a minute (lock held).

        Skipped entirely when the file has changed under us. A `last_seen`
        touch is a nicety; rewriting the whole payload over another process's
        revoke is not, and this is the only writer cheap enough to fire on an
        ordinary request.
        """
        if self._now() - self._last_flush < LAST_SEEN_FLUSH_SECONDS:
            return
        if self._saved_mtime is not None and self._mtime() != self._saved_mtime:
            self._last_flush = self._now()
            return
        try:
            self._save()
        except OSError:
            pass                # a read-only app dir must not break auth

    def revoke(self, target):
        """Drop one device by id (or by token_hash), or every device for
        "all". Returns how many rows went.

        Revocation is the user's only remedy for a lost or stolen device, so it
        has to reach the connections that device already has open — a token
        checked once at handshake and never again would leave the host pushing
        every track title, path and debug line to a revoked browser until the
        process restarted. Each open socket registers a close callback (see
        register_connection); the ones belonging to a dropped device are called
        here, OUTSIDE the state lock, since they hop to an event loop.
        """
        wanted = str(target or "").strip()
        if not wanted:
            return 0
        closers = []
        with self._lock:
            keep, dropped = [], []
            for row in self._data["devices"]:
                if (wanted == "all" or row["id"] == wanted
                        or _matches(wanted, row["token_hash"])):
                    dropped.append(row)
                else:
                    keep.append(row)
            if not dropped:
                return 0
            self._data["devices"] = keep
            for row in dropped:
                closers.extend(cb for cb in
                               self._connections.pop(row["id"], {}).values() if cb)
                if (self._control is not None
                        and self._control["device_id"] == row["id"]):
                    self._control = None
            self._save()
        for close in closers:
            try:
                close()
            except Exception:
                pass            # a socket already gone is not a failed revoke
        return len(dropped)

    # ── live connections ─────────────────────────────────────────────────────
    # Who is actually here, kept apart from who holds the lock. Keeping them in
    # one dict was the bug: `mark_connected` returned early whenever the caller
    # was not the current holder, and a browser opens its socket BEFORE it
    # claims control — so the holder's liveness was never recorded, and the
    # 120-second idle window displaced a device that was sitting there watching.

    def register_connection(self, device_id, on_revoke=None):
        """Record one open event socket. Returns the key to unregister with.

        *on_revoke* is called (from `revoke`, on whatever thread revoked) when
        this device's access is withdrawn and the socket must be closed.
        """
        with self._lock:
            self._conn_seq += 1
            key = self._conn_seq
            self._connections.setdefault(device_id, {})[key] = on_revoke
            if self._control is not None and self._control["device_id"] == device_id:
                self._control["seen"] = self._now()
            return key

    def unregister_connection(self, device_id, key):
        """Drop one socket. True when that released the control lock.

        Ref-counted on purpose: a reload opens the new socket before the old
        one closes, and a second tab is a second socket — neither is the device
        leaving, and neither may drop the lock out from under it.
        """
        with self._lock:
            conns = self._connections.get(device_id)
            if conns is not None:
                conns.pop(key, None)
                if not conns:
                    self._connections.pop(device_id, None)
            if self._connections.get(device_id):
                return False
            if self._control is not None and self._control["device_id"] == device_id:
                self._control = None
                return True
            return False

    def is_connected(self, device_id):
        with self._lock:
            return bool(self._connections.get(device_id))

    def connection_count(self, device_id):
        with self._lock:
            return len(self._connections.get(device_id) or {})

    # ── pairing ──────────────────────────────────────────────────────────────

    def begin_pairing(self, ttl=PAIRING_CODE_TTL):
        """Mint a fresh 6-digit code, replacing any code still live.

        `secrets`, never `random`: the module that seeds from the system CSPRNG
        is the only one whose output an attacker cannot predict from a previous
        code. The code lives in memory and is handed to the host's own UI —
        it is never persisted and never logged.
        """
        with self._lock:
            code = f"{secrets.randbelow(10 ** PAIRING_CODE_DIGITS):0{PAIRING_CODE_DIGITS}d}"
            self._code = {"code": code, "expires_at": self._now() + ttl}
            return {"code": code, "expires_at": self._code["expires_at"],
                    "ttl": ttl}

    def active_code(self):
        """The live code, or None once it has expired."""
        with self._lock:
            if self._code is None:
                return None
            if self._now() >= self._code["expires_at"]:
                self._code = None
                return None
            return dict(self._code)

    def cancel_pairing(self):
        with self._lock:
            self._code = None

    def _rate_limit(self, client):
        """Refuse a client that has spent its attempt budget (lock held)."""
        key = str(client or "unknown")
        now = self._now()
        # Keys were only ever pruned when the same key came back, so a host
        # seeing many addresses accumulated a list per address forever. Sweep
        # the closed windows once the table is big enough to be worth it.
        if len(self._attempts) > ATTEMPT_KEY_SWEEP_AT:
            self._attempts = {
                k: [t for t in stamps if now - t < PAIR_ATTEMPT_WINDOW]
                for k, stamps in self._attempts.items()
                if any(now - t < PAIR_ATTEMPT_WINDOW for t in stamps)}
        recent = [t for t in self._attempts.get(key, ())
                  if now - t < PAIR_ATTEMPT_WINDOW]
        if len(recent) >= PAIR_ATTEMPT_LIMIT:
            self._attempts[key] = recent
            raise PairingRefused(RATE_LIMITED_REASON, status=429)
        recent.append(now)
        self._attempts[key] = recent

    def claim(self, code, device_name=None, client=None):
        """Exchange a pairing code for a long-lived device token.

        The code is single-use — it is cleared before the token is minted, so
        two browsers racing the same code cannot both come away paired. The
        plaintext token is returned exactly once here and never stored; only
        its SHA-256 reaches the file.

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
            token = secrets.token_urlsafe(32)
            row = {
                "id": secrets.token_hex(8),
                "name": (str(device_name or "").strip() or "Paired device")[:60],
                "token_hash": token_hash(token),
                "paired_at": int(self._now()),
                "last_seen": int(self._now()),
            }
            self._data["devices"].append(row)
            self._save()
            self._attempts.pop(str(client or "unknown"), None)
            return {"token": token,
                    "device": {"id": row["id"], "name": row["name"],
                               "paired_at": row["paired_at"],
                               "last_seen": row["last_seen"]}}

    # ── single-writer control lock (HANDOFF §2) ──────────────────────────────
    # The local window is never a contender: it always holds precedence, so it
    # neither claims nor can be displaced. This lock is only ever between
    # remote clients — it is what stops two phones starting two batches against
    # one yt-dlp session.

    def control_holder(self):
        with self._lock:
            if self._control is None:
                return None
            return {"device_id": self._control["device_id"],
                    "name": self._control["name"]}

    def has_control(self, device_id):
        with self._lock:
            return self._control is not None and self._control["device_id"] == device_id

    def claim_control(self, device_id, name=None):
        """Take the lock, or raise ControlHeld naming who has it.

        A holder that is neither connected NOR recently active can be
        displaced: a browser closed without a clean socket teardown must not
        leave the host unusable. Liveness is read from the connection registry
        rather than from a flag on the holder record — the flag could only be
        set by whoever already held the lock, and a browser opens its event
        socket before it claims control, so it was never set for a real holder
        and every holder decayed after two minutes of watching.
        """
        with self._lock:
            holder = self._control
            now = self._now()
            if (holder is not None and holder["device_id"] != device_id
                    and (bool(self._connections.get(holder["device_id"]))
                         or now - holder.get("seen", 0) < CONTROL_IDLE_SECONDS)):
                raise ControlHeld(holder.get("name"))
            self._control = {"device_id": device_id, "name": name or "Device",
                             "seen": now}
            return self.control_holder()

    def release_control(self, device_id):
        """Give the lock up. Returns True when this call actually freed it."""
        with self._lock:
            if self._control is not None and self._control["device_id"] == device_id:
                self._control = None
                return True
            return False

    def touch_control(self, device_id):
        with self._lock:
            if self._control is not None and self._control["device_id"] == device_id:
                self._control["seen"] = self._now()

    # ── authorisation ────────────────────────────────────────────────────────

    def method_allowed(self, method, device_id):
        """(allowed, reason) for one remote call.

        Read-only mode and the control lock are separate gates, checked in that
        order: while the host is read-only nothing writes, so asking for
        control is refused too rather than handing out a lock that grants
        nothing.
        """
        if method in READ_METHODS:
            return True, ""
        with self._lock:
            if self._data.get("read_only"):
                return False, READ_ONLY_REASON
            if method in CONTROL_METHODS:
                return True, ""
            holder = self._control
            if holder is None or holder["device_id"] != device_id:
                if holder is None:
                    return False, ("No device holds control of this host yet. "
                                   "Take control first — only one device can "
                                   "drive it at a time.")
                return False, control_reason(holder.get("name"))
            return True, ""

    def session(self, device_id, device_name=None):
        """What one connected device is allowed to do right now."""
        with self._lock:
            read_only = bool(self._data.get("read_only"))
            holder = self.control_holder()
        has_control = holder is not None and holder["device_id"] == device_id
        if read_only:
            reason = READ_ONLY_REASON
        elif has_control:
            reason = ""
        elif holder is None:
            reason = ("No device holds control of this host yet. Take control "
                      "first — only one device can drive it at a time.")
        else:
            reason = control_reason(holder.get("name"))
        return {"device_id": device_id, "name": device_name or "",
                "read_only": read_only, "has_control": has_control,
                "can_write": not read_only and has_control,
                "holder": holder, "reason": reason}

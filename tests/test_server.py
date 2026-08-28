"""The remote transport: pairing, device tokens, read-only mode, control lock.

Everything runs against a service, database and token store built under the
test's own tmp_path — no test may reach the developer's real cratebuilder.db,
config, or cratebuilder_remote.json. The FastAPI mount is driven through
Starlette's TestClient (in-process, no real socket); the clock is injected
wherever a TTL or a rate-limit window is the thing under test.
"""

import logging

import pytest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cratebuilder import remoteauth
from cratebuilder.remoteauth import (PAIR_ATTEMPT_LIMIT, PAIRING_CODE_TTL,
                                     PairingRefused, RemoteState, token_hash)
from cratebuilder.server import create_app
from cratebuilder.service import CBError, CrateBuilderService
from cratebuilder.settings import Settings


class Clock:
    """A clock the test moves by hand, for the TTL and rate-limit windows."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def state(tmp_path, clock):
    """Remote access ON — the shipped default is off, which every route now
    refuses (see the `enabled` live-gate tests), so the tests that are about
    something else have to switch it on the way the user would."""
    state = RemoteState(str(tmp_path / "cratebuilder_remote.json"), now=clock)
    state.set_flag("enabled", True)
    return state


@pytest.fixture
def service(tmp_path, state):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"),
                               log_path=str(tmp_path / "activity.log"),
                               debug_log_path=str(tmp_path / "debug.log"),
                               remote_state=state)


@pytest.fixture
def client(service, state, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>bundle</h1>", encoding="utf-8")
    # base_url gives every request a Host of 127.0.0.1 rather than TestClient's
    # default "testserver", so these run against the REAL Host allow-list
    # instead of an allowance invented for the tests.
    return TestClient(create_app(service, state, web_dir=str(web)),
                      base_url="http://127.0.0.1")


def pair(client, state, name="Test phone"):
    """Pair one device the way a browser does, returning its token."""
    code = state.begin_pairing()["code"]
    res = client.post("/pair", json={"code": code, "device_name": name})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def auth(token):
    return {"X-CB-Token": token}


# ── authentication ───────────────────────────────────────────────────────────

def test_rpc_without_a_token_is_refused(client):
    res = client.post("/rpc", json={"method": "state.snapshot", "params": {}})
    assert res.status_code == 401


def test_401_says_the_same_thing_for_missing_and_wrong_tokens(client, state):
    """A 401 that distinguished the two would confirm to a prober which of its
    guesses had reached a real device row."""
    missing = client.post("/rpc", json={"method": "state.snapshot"})
    malformed = client.post("/rpc", json={"method": "state.snapshot"},
                            headers=auth("not-a-token"))
    unknown = client.post("/rpc", json={"method": "state.snapshot"},
                          headers=auth("x" * 43))
    assert missing.status_code == malformed.status_code == unknown.status_code == 401
    assert (missing.json()["detail"] == malformed.json()["detail"]
            == unknown.json()["detail"] == remoteauth.UNPAIRED_REASON)


def test_token_auth_passes(client, state):
    token = pair(client, state)
    res = client.post("/rpc", json={"method": "state.snapshot"}, headers=auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["host"]["transport"] == "remote"


def test_revoked_token_is_refused(client, state):
    token = pair(client, state)
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 200
    state.revoke("all")
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 401


def test_the_access_log_never_prints_a_device_token(client, state, caplog):
    """The WebSocket handshake has to carry its token in the query string, and
    uvicorn logs the full path — so the log has to redact it, or an info-level
    run writes every device's long-lived token to the console."""
    from cratebuilder.server import install_log_redaction

    install_log_redaction()
    token = pair(client, state)
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5000",
                    f"/ws?token={token}")
        logger.info('127.0.0.1 - "GET /ws?token=%s HTTP/1.1" 101' % token)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in text
    assert "token=<redacted>" in text


def test_only_the_hash_is_ever_written_to_disk(client, state, tmp_path):
    token = pair(client, state)
    stored = (tmp_path / "cratebuilder_remote.json").read_text(encoding="utf-8")
    assert token not in stored
    assert token_hash(token) in stored


def test_static_bundle_is_reachable_unpaired(client):
    """The bundle IS the pairing screen — an unpaired browser has to load it."""
    res = client.get("/")
    assert res.status_code == 200
    assert "bundle" in res.text


# ── pairing ──────────────────────────────────────────────────────────────────

def test_pairing_happy_path_issues_a_token(client, state):
    code = state.begin_pairing()["code"]
    res = client.post("/pair", json={"code": code, "device_name": "Studio iPad"})
    assert res.status_code == 200
    body = res.json()
    assert body["token"]
    assert body["device"]["name"] == "Studio iPad"
    assert state.device_count() == 1


def test_a_pairing_code_works_exactly_once(client, state):
    code = state.begin_pairing()["code"]
    assert client.post("/pair", json={"code": code}).status_code == 200
    second = client.post("/pair", json={"code": code})
    assert second.status_code == 400
    assert state.device_count() == 1


def test_a_code_expires_after_five_minutes(state, clock):
    code = state.begin_pairing()["code"]
    clock.advance(PAIRING_CODE_TTL - 1)
    assert state.active_code() is not None
    clock.advance(2)
    assert state.active_code() is None
    with pytest.raises(PairingRefused):
        state.claim(code, "late", client="1.2.3.4")


def test_the_wrong_code_is_refused_without_consuming_the_right_one(state):
    code = state.begin_pairing()["code"]
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(PairingRefused):
        state.claim(wrong, "guesser", client="1.2.3.4")
    assert state.active_code()["code"] == code


def test_pairing_attempts_are_rate_limited_per_address(client, state, clock):
    state.begin_pairing()
    bad = {"code": "999999", "device_name": "guesser"}
    for _ in range(PAIR_ATTEMPT_LIMIT):
        assert client.post("/pair", json=bad).status_code == 400
    limited = client.post("/pair", json=bad)
    assert limited.status_code == 429
    assert "Too many pairing attempts" in limited.json()["detail"]


def test_the_rate_limit_window_reopens(state, clock):
    for _ in range(PAIR_ATTEMPT_LIMIT):
        with pytest.raises(PairingRefused):
            state.claim("999999", client="10.0.0.9")
    with pytest.raises(PairingRefused) as exc:
        state.claim("999999", client="10.0.0.9")
    assert exc.value.status == 429
    clock.advance(remoteauth.PAIR_ATTEMPT_WINDOW + 1)
    code = state.begin_pairing()["code"]
    assert state.claim(code, "back again", client="10.0.0.9")["token"]


def test_pair_info_leaks_only_the_one_boolean(client, state):
    state.begin_pairing()
    body = client.get("/pair/info").json()
    assert body == {"require_pairing": True}


def test_pairing_without_a_code_when_the_host_does_not_require_one(client, state):
    state.set_flag("require_pairing", False)
    res = client.post("/pair", json={"device_name": "LAN laptop"})
    assert res.status_code == 200
    assert res.json()["token"]


# ── read-only mode ───────────────────────────────────────────────────────────

def test_read_only_refuses_a_download_but_allows_the_snapshot(client, state):
    token = pair(client, state)
    state.set_flag("read_only", True)
    snapshot = client.post("/rpc", json={"method": "state.snapshot"},
                           headers=auth(token))
    assert snapshot.json()["ok"] is True
    start = client.post("/rpc", json={"method": "download.start"},
                        headers=auth(token))
    assert start.status_code == 200
    assert start.json() == {"ok": False, "error": remoteauth.READ_ONLY_REASON}


def test_read_only_allows_the_log_and_database_reads(client, state):
    token = pair(client, state)
    state.set_flag("read_only", True)
    for method, params in (("logs.tail", {"name": "activity"}),
                           ("logs.search", {"name": "debug", "query": "x"}),
                           ("db.query", {"table": "downloads"}),
                           ("db.groups", {}),
                           ("db.export_csv", {"table": "downloads"}),
                           ("watchlist.list", {}),
                           ("batch.list", {}),
                           ("ui_strings", {})):
        res = client.post("/rpc", json={"method": method, "params": params},
                          headers=auth(token))
        assert res.json()["ok"] is True, (method, res.json())


def test_read_only_refuses_settings_and_maintenance(client, state):
    token = pair(client, state)
    state.set_flag("read_only", True)
    for method, params in (("settings.set", {"key": "bitrate_quality",
                                             "value": "192 kbps"}),
                           ("batch.add", {"url": "https://x/y"}),
                           ("db.rebuild", {}),
                           ("db.maintenance_preview", {"task": "db.rebuild"}),
                           ("watchlist.resolve_candidates", {"channel_id": 1}),
                           ("remote.claim_control", {})):
        body = client.post("/rpc", json={"method": method, "params": params},
                           headers=auth(token)).json()
        assert body["ok"] is False, method
        assert body["error"] == remoteauth.READ_ONLY_REASON, method


# ── control lock ─────────────────────────────────────────────────────────────

def test_a_writer_must_claim_control_first(client, state):
    token = pair(client, state)
    refused = client.post("/rpc", json={"method": "batch.add",
                                        "params": {"url": "https://x/y"}},
                          headers=auth(token)).json()
    assert refused["ok"] is False
    assert "control" in refused["error"]

    claimed = client.post("/rpc", json={"method": "remote.claim_control"},
                          headers=auth(token)).json()
    assert claimed["ok"] is True
    assert claimed["result"]["can_write"] is True

    added = client.post("/rpc", json={"method": "batch.add",
                                      "params": {"url": "https://x/y"}},
                        headers=auth(token)).json()
    assert added["ok"] is True


def test_a_second_client_is_refused_writes_while_the_first_holds_control(client, state):
    first = pair(client, state, "First phone")
    second = pair(client, state, "Second phone")
    assert client.post("/rpc", json={"method": "remote.claim_control"},
                       headers=auth(first)).json()["ok"] is True

    stolen = client.post("/rpc", json={"method": "remote.claim_control"},
                         headers=auth(second)).json()
    assert stolen["ok"] is False
    assert "First phone" in stolen["error"]

    write = client.post("/rpc", json={"method": "batch.add",
                                      "params": {"url": "https://x/y"}},
                        headers=auth(second)).json()
    assert write["ok"] is False
    assert "First phone" in write["error"]

    # …but it can still read.
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(second)).json()["ok"] is True


def test_claiming_control_emits_a_control_holder_event(client, state, service):
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    token = pair(client, state, "Booth laptop")
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    holders = [p for t, p in seen if t == "control.holder"]
    assert holders and holders[-1]["name"] == "Booth laptop"


def test_control_is_released_and_can_then_be_taken(client, state):
    first = pair(client, state, "First")
    second = pair(client, state, "Second")
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(first))
    client.post("/rpc", json={"method": "remote.release_control"}, headers=auth(first))
    taken = client.post("/rpc", json={"method": "remote.claim_control"},
                        headers=auth(second)).json()
    assert taken["ok"] is True
    assert taken["result"]["has_control"] is True


def test_an_idle_holder_with_no_socket_can_be_displaced(state, clock):
    state.claim_control("dev-1", "Gone phone")
    with pytest.raises(remoteauth.ControlHeld):
        state.claim_control("dev-2", "Booth")
    clock.advance(remoteauth.CONTROL_IDLE_SECONDS + 1)
    assert state.claim_control("dev-2", "Booth")["device_id"] == "dev-2"


def test_a_live_holder_cannot_be_displaced_by_going_quiet(state, clock):
    """In the REAL order: a browser opens its event socket first, THEN claims
    control. The old test did it the other way round, which is the only order
    in which the liveness flag was ever set — so it passed while every actual
    holder decayed after two minutes of watching a batch."""
    state.register_connection("dev-1", None)
    state.claim_control("dev-1", "Desk")
    clock.advance(remoteauth.CONTROL_IDLE_SECONDS * 4)
    with pytest.raises(remoteauth.ControlHeld):
        state.claim_control("dev-2", "Booth")


def test_a_holder_whose_socket_closes_loses_the_lock(state, clock):
    key = state.register_connection("dev-1", None)
    state.claim_control("dev-1", "Desk")
    assert state.unregister_connection("dev-1", key) is True
    assert state.control_holder() is None


def test_a_second_socket_closing_does_not_release_the_lock(state, clock):
    """A reload opens the new socket before the old one closes, and a second
    tab is a second socket — neither is the device leaving."""
    first = state.register_connection("dev-1", None)
    state.claim_control("dev-1", "Desk")
    second = state.register_connection("dev-1", None)
    assert state.unregister_connection("dev-1", second) is False
    assert state.control_holder()["device_id"] == "dev-1"
    assert state.connection_count("dev-1") == 1
    # …and it is still held against a rival while that first socket is open.
    clock.advance(remoteauth.CONTROL_IDLE_SECONDS * 4)
    with pytest.raises(remoteauth.ControlHeld):
        state.claim_control("dev-2", "Booth")
    assert state.unregister_connection("dev-1", first) is True
    assert state.control_holder() is None


def test_a_holder_keeps_the_lock_across_a_page_reload(client, state):
    """The reload sequence through the real routes: socket 1 open, claim, then
    socket 2 opens and socket 1 closes. The lock must not move."""
    token = pair(client, state, "Reloading phone")
    with client.websocket_connect(f"/ws?token={token}") as first:
        first.receive_json()
        client.post("/rpc", json={"method": "remote.claim_control"},
                    headers=auth(token))
        assert state.control_holder()["name"] == "Reloading phone"
        with client.websocket_connect(f"/ws?token={token}") as second:
            second.receive_json()
            assert state.connection_count(state.devices()[0]["id"]) == 2
        # The second socket closed; the first is still open.
        assert state.control_holder()["name"] == "Reloading phone"
    assert state.control_holder() is None


# ── transport gating ─────────────────────────────────────────────────────────

def test_updater_and_filesystem_are_refused_through_rpc(client, state):
    token = pair(client, state)
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    for method in ("update.check", "update.apply", "fs.pick_folder",
                   "fs.reveal"):
        body = client.post("/rpc", json={"method": method}, headers=auth(token)).json()
        assert body["ok"] is False, method
        assert "app window on the host machine" in body["error"], method


def test_remote_settings_cannot_be_changed_from_a_remote_client(client, state):
    token = pair(client, state)
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    # False, not True: the interesting attempt is a remote client switching OFF
    # the gates that hold it accountable — remote access itself, and the
    # requirement that the next device present a code.
    for key in ("remote_enabled", "remote_require_pairing", "remote_read_only"):
        body = client.post("/rpc", json={"method": "settings.set",
                                         "params": {"key": key, "value": False}},
                           headers=auth(token)).json()
        assert body["ok"] is False, key
    assert state.get_flag("enabled") is True
    assert state.get_flag("require_pairing") is True


def test_pair_begin_and_revoke_are_local_only(client, state, service):
    token = pair(client, state)
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    for method, params in (("remote.pair_begin", {}),
                           ("remote.revoke", {"device_id": "all"}),
                           ("remote.pair_cancel", {})):
        body = client.post("/rpc", json={"method": method, "params": params},
                           headers=auth(token)).json()
        assert body["ok"] is False, method
    assert state.device_count() == 1
    # The same calls from the local window are fine.
    assert service.call("remote.pair_begin")["code"]


def test_remote_config_never_hands_the_pairing_code_to_a_remote_client(
        client, state, service):
    token = pair(client, state)
    service.call("remote.pair_begin")
    body = client.post("/rpc", json={"method": "remote.config"},
                       headers=auth(token)).json()
    assert body["ok"] is True
    assert "pairing" not in body["result"]
    assert service.call("remote.config")["pairing"]["code"]


# ── NEW-6: base_dir is the trust root, so remote may not move it ─────────────

def test_base_dir_cannot_be_changed_over_the_remote_transport(client, state,
                                                              service, tmp_path):
    token = pair(client, state)
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    before = service.settings_get("base_dir")["value"]
    body = client.post("/rpc", json={"method": "settings.set",
                                     "params": {"key": "base_dir",
                                                "value": str(tmp_path / "elsewhere")}},
                       headers=auth(token)).json()
    assert body["ok"] is False
    assert "app window on the host machine" in body["error"]
    assert service.settings_get("base_dir")["value"] == before


def test_base_dir_is_still_settable_from_the_local_window(service, tmp_path):
    target = str(tmp_path / "new-crate")
    assert service.settings_set("base_dir", target)["value"] == target


# ── per-call transport ───────────────────────────────────────────────────────

def test_one_service_serves_both_transports(service, tmp_path):
    """The HANDOFF diagram's one core, two mounts: the same object answers
    "you may reveal a folder" locally and "you may not" remotely."""
    assert service.call("state.snapshot")["capabilities"] == {
        "update": True, "filesystem": True}
    assert service.call("state.snapshot", transport="remote")["capabilities"] == {
        "update": False, "filesystem": False}
    with pytest.raises(CBError):
        service.call("fs.pick_folder", transport="remote")
    assert service.call("state.snapshot")["capabilities"]["filesystem"] is True


def test_an_unknown_per_call_transport_is_rejected(service):
    with pytest.raises(ValueError):
        service.call("state.snapshot", transport="carrier-pigeon")


# ── log download route ───────────────────────────────────────────────────────

def test_the_log_route_streams_the_file(client, state, service, tmp_path):
    (tmp_path / "activity.log").write_text("DOWNLOADED | one\n", encoding="utf-8")
    token = pair(client, state)
    res = client.get("/logs/activity", headers=auth(token))
    assert res.status_code == 200
    assert "DOWNLOADED | one" in res.text


def test_the_log_route_needs_a_token(client, tmp_path):
    (tmp_path / "activity.log").write_text("x\n", encoding="utf-8")
    assert client.get("/logs/activity").status_code == 401


def test_the_log_route_refuses_a_name_it_does_not_own(client, state):
    token = pair(client, state)
    res = client.get("/logs/..%2F..%2Fconfig.json", headers=auth(token))
    assert res.status_code == 404


# ── events ───────────────────────────────────────────────────────────────────

def test_the_socket_carries_an_emitted_event(client, state, service):
    token = pair(client, state)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        first = ws.receive_json()
        assert first["type"] == "host.status"
        assert first["payload"]["online"] is True
        service.emit("notification", {"level": "info", "title": "Batch",
                                      "body": "done", "at": "now"})
        frame = ws.receive_json()
        assert frame["type"] == "notification"
        assert frame["payload"]["title"] == "Batch"


def test_an_unpaired_socket_is_closed_having_received_nothing(client, service):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?token=nope") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_a_socket_with_no_token_at_all_is_closed(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_an_unpaired_socket_never_subscribes_to_the_bus(client, service):
    """The acceptance rule, stated as a fact about the event bus rather than
    about what happened to arrive: authentication is BEFORE the subscribe, so
    there is no window in which an unpaired listener could see anything."""
    before = len(service.events._subscribers)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=nope") as ws:
            ws.receive_json()
    service.emit("notification", {"title": "secret", "body": "track name"})
    assert len(service.events._subscribers) == before


def test_the_socket_reports_this_devices_session(client, state):
    token = pair(client, state, "Booth")
    state.set_flag("read_only", True)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        payload = ws.receive_json()["payload"]
        assert payload["session"]["read_only"] is True
        assert payload["session"]["can_write"] is False


def test_closing_the_socket_frees_the_control_lock(client, state):
    token = pair(client, state, "Leaving phone")
    client.post("/rpc", json={"method": "remote.claim_control"}, headers=auth(token))
    assert state.control_holder()["name"] == "Leaving phone"
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
    assert state.control_holder() is None


# ── read-only allow-list, as data ────────────────────────────────────────────

def test_every_read_method_exists_in_the_dispatch_table(service):
    """The allow-list must not drift from the dispatch dict — a typo here
    would silently refuse a read the design promises."""
    handled = set(service._methods())
    server_side = {"remote.session"}
    assert remoteauth.READ_METHODS - handled == server_side


# ── S1: the rate limiter's key cannot be chosen by the caller ────────────────

def test_uvicorn_is_configured_not_to_trust_proxy_headers():
    """uvicorn defaults to proxy_headers=True trusting 127.0.0.1, which
    replaces request.client.host with X-Forwarded-For for every request from
    loopback — i.e. exactly the deployment this app documents (behind Caddy or
    a tunnel). That turns the pairing limiter's key into an attacker-chosen
    string, and a 6-digit code into something brute-forceable inside its TTL."""
    from cratebuilder.server import uvicorn_kwargs

    kwargs = uvicorn_kwargs()
    assert kwargs["proxy_headers"] is False
    assert not kwargs["forwarded_allow_ips"]


def test_both_entry_points_pass_the_hardened_uvicorn_settings():
    """A one-line default is only a fix while both call sites use it."""
    import inspect

    import web_server
    import web_window

    for module in (web_server, web_window):
        source = inspect.getsource(module)
        assert "uvicorn.Config(" in source
        assert "**uvicorn_kwargs()" in source, module.__name__


def test_a_rotating_forwarded_for_cannot_buy_extra_pairing_attempts(client, state):
    """The reviewer's reproduction, in-process: 20 wrong codes, each with a
    different X-Forwarded-For. The budget is the socket peer's, so the header
    buys nothing and the limit still trips."""
    state.begin_pairing()
    statuses = []
    for n in range(20):
        res = client.post("/pair", json={"code": "999999"},
                          headers={"X-Forwarded-For": f"10.9.0.{n}"})
        statuses.append(res.status_code)
    assert statuses[:PAIR_ATTEMPT_LIMIT] == [400] * PAIR_ATTEMPT_LIMIT
    assert set(statuses[PAIR_ATTEMPT_LIMIT:]) == {429}


def test_the_attempt_log_does_not_grow_without_bound(state, clock):
    for n in range(remoteauth.ATTEMPT_KEY_SWEEP_AT * 2):
        with pytest.raises(PairingRefused):
            state.claim("999999", client=f"10.0.{n // 256}.{n % 256}")
    clock.advance(remoteauth.PAIR_ATTEMPT_WINDOW + 1)
    with pytest.raises(PairingRefused):
        state.claim("999999", client="10.9.9.9")
    assert len(state._attempts) <= remoteauth.ATTEMPT_KEY_SWEEP_AT + 1


# ── S2: revoking cuts the device's live event socket ─────────────────────────

def test_revoking_a_device_closes_its_open_event_socket(client, state, service):
    """Revocation is the user's only remedy for a lost device. Without this it
    stops /rpc but leaves the host pushing every track title, path and debug
    line to the revoked browser until the process restarts."""
    token = pair(client, state, "Stolen phone")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "host.status"
            assert state.revoke("all") == 1
            service.emit("notification", {"level": "info", "title": "SECRET",
                                          "body": "C:/Music/x"})
            # Whatever arrives next, it is the close — never the notification.
            frame = ws.receive_json()
            assert frame["type"] != "notification", frame
    assert exc.value.code == 4401
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 401


def test_revoking_one_device_leaves_the_others_streaming(client, state, service):
    keep = pair(client, state, "Booth")
    drop = pair(client, state, "Stolen")
    dropped_id = [d["id"] for d in state.devices() if d["name"] == "Stolen"][0]
    with client.websocket_connect(f"/ws?token={keep}") as ws:
        ws.receive_json()
        assert state.revoke(dropped_id) == 1
        service.emit("notification", {"title": "Batch", "body": "done"})
        assert ws.receive_json()["type"] == "notification"
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(keep)).status_code == 200
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(drop)).status_code == 401


# ── S3/S4: `remote enabled` is a live gate, and the bind rule is shared ──────

def test_every_remote_route_refuses_while_remote_access_is_off(client, state):
    token = pair(client, state)
    state.set_flag("enabled", False)
    rpc = client.post("/rpc", json={"method": "state.snapshot"}, headers=auth(token))
    assert rpc.status_code == 403
    assert rpc.json()["detail"] == remoteauth.DISABLED_REASON
    assert client.get("/logs/activity", headers=auth(token)).status_code == 403
    assert client.get("/pair/info").status_code == 403
    assert client.post("/pair", json={"code": "123456"}).status_code == 403


def test_a_disabled_host_will_not_mint_a_token(client, state):
    code = state.begin_pairing()["code"]
    state.set_flag("enabled", False)
    assert client.post("/pair", json={"code": code}).status_code == 403
    assert state.device_count() == 0


def test_turning_remote_access_off_cuts_a_running_mount(client, state):
    """The toggle is a live control, not a startup-time bind decision — the 3j
    card reads as a kill switch and this is what makes it one."""
    token = pair(client, state)
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 200
    state.set_flag("enabled", False)
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 403
    state.set_flag("enabled", True)
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers=auth(token)).status_code == 200


def test_a_disabled_host_closes_the_socket_without_dropping_the_token(client, state):
    token = pair(client, state)
    state.set_flag("enabled", False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
    # 4403, not 4401: the token is still good, so the browser keeps it and
    # keeps retrying rather than demanding a fresh pairing code.
    assert exc.value.code == 4403
    assert state.device_count() == 1


# ── F5: the master switch cuts sockets that are ALREADY open ────────────────
# The per-request gates refuse new work the moment the flag goes down, but a
# socket opened before the flip kept streaming every track title, channel name,
# folder path and log line for as long as the browser stayed open. Revoke has
# always cut its connections; the master switch is the stronger promise of the
# two and must not be the weaker one.

def test_turning_remote_access_off_cuts_a_live_event_socket(client, state,
                                                            service):
    """The executed reproduction, as a test: the frame carrying a track title
    used to arrive AFTER the user switched remote access off."""
    token = pair(client, state, "Booth phone")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "host.status"
            state.set_flag("enabled", False)
            service.emit("progress.current", {"title": "SECRET TRACK TITLE",
                                              "percent": 50, "job": "batch"})
            service._emit.flush()
            frame = ws.receive_json()
            assert False, f"nothing may arrive after the switch: {frame}"
    # 4403, not 4401: the token is still good. The client keeps it and keeps
    # retrying rather than throwing it away and demanding a pairing code.
    assert exc.value.code == 4403
    assert state.device_count() == 1, "disabling is not revoking"


def test_revoke_still_closes_with_4401_after_the_disable_path_was_added(
        client, state):
    """The two kill switches say different things about the client's token,
    so they must keep closing with different codes."""
    token = pair(client, state, "Stolen phone")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            state.revoke("all")
            ws.receive_json()
    assert exc.value.code == 4401


def test_the_master_switch_drops_the_control_lock_with_the_sockets(client,
                                                                   state):
    """Every device is gone, so nobody is left holding the single-writer lock
    — the first device back in after the switch returns claims it fresh."""
    token = pair(client, state)
    client.post("/rpc", json={"method": "remote.claim_control"},
                headers=auth(token))
    assert state.control_holder() is not None
    state.set_flag("enabled", False)
    assert state.control_holder() is None


def test_the_host_window_hears_that_the_holder_went(service, state):
    """The 3j card is still drawing whoever held control; settings.set is the
    path the toggle actually takes, so that is where the event belongs."""
    seen = []
    service.events.subscribe(lambda t, p: seen.append((t, p)))
    service.settings_set("remote_enabled", True)
    state.claim_control("dev-1", "Booth")
    service.settings_set("remote_enabled", False)
    service._emit.flush()
    assert ("control.holder", {}) in seen


# ── F6: a vanished viewer's log tail is handed back ─────────────────────────
# `logs.watch {on:true}` is ref-counted in the service and only decremented by
# the client's own {on:false} — which a closed tab or a phone off Wi-Fi never
# sends. The event socket is the one thing that knows the viewer is gone.

def _watch_count(service, name="activity"):
    watcher = service._log_watchers.get(name)
    return watcher["count"] if watcher else 0


def test_a_vanished_viewer_does_not_leak_its_log_tail(client, state, service):
    token = pair(client, state, "Phone that walks away")
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        body = client.post("/rpc", json={"method": "logs.watch",
                                         "params": {"name": "activity",
                                                    "on": True}},
                           headers=auth(token)).json()
        assert body["ok"] is True
        assert _watch_count(service) == 1
        # The browser is closed — no {on:false} is ever sent.
    assert _watch_count(service) == 0, "the tail thread outlived every viewer"
    watcher = service._log_watchers.get("activity")
    assert watcher is None or watcher["stop"].is_set()


def test_a_second_tab_keeps_the_tail_alive_until_the_last_one_goes(client,
                                                                   state,
                                                                   service):
    """The same ref-count reasoning one layer up: a reload opens the new socket
    before the old one closes, and a second tab is a second socket."""
    token = pair(client, state, "Two tabs")
    with client.websocket_connect(f"/ws?token={token}") as first:
        first.receive_json()
        client.post("/rpc", json={"method": "logs.watch",
                                  "params": {"name": "activity", "on": True}},
                    headers=auth(token))
        with client.websocket_connect(f"/ws?token={token}") as second:
            second.receive_json()
            client.post("/rpc", json={"method": "logs.watch",
                                      "params": {"name": "activity",
                                                 "on": True}},
                        headers=auth(token))
            assert _watch_count(service) == 2
        assert _watch_count(service) == 2, "one tab closing is not the viewer leaving"
    assert _watch_count(service) == 0


def test_a_client_that_closes_its_own_watch_is_not_unwound_twice(client, state,
                                                                 service):
    """The polite path still works: {on:false} decrements, and the socket
    teardown must not then take the count negative or stop someone else's."""
    token = pair(client, state, "Polite phone")
    other = pair(client, state, "Local-ish second device")
    with client.websocket_connect(f"/ws?token={other}") as keeper:
        keeper.receive_json()
        client.post("/rpc", json={"method": "logs.watch",
                                  "params": {"name": "activity", "on": True}},
                    headers=auth(other))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            client.post("/rpc", json={"method": "logs.watch",
                                      "params": {"name": "activity",
                                                 "on": True}},
                        headers=auth(token))
            client.post("/rpc", json={"method": "logs.watch",
                                      "params": {"name": "activity",
                                                 "on": False}},
                        headers=auth(token))
            assert _watch_count(service) == 1
        assert _watch_count(service) == 1, "the other device is still watching"
    assert _watch_count(service) == 0


def test_the_bind_rule_needs_both_consent_and_intent(state):
    from cratebuilder.server import ANY_INTERFACE, LOOPBACK, bind_host

    state.set_flag("enabled", False)
    assert bind_host(state, lan=False) == LOOPBACK
    assert bind_host(state, lan=True) is None      # refused: no consent
    state.set_flag("enabled", True)
    assert bind_host(state, lan=False) == LOOPBACK  # consent alone is not intent
    assert bind_host(state, lan=True) == ANY_INTERFACE


# ── S5: Host / Origin ────────────────────────────────────────────────────────

def test_a_rebinding_host_header_is_refused(client, state):
    token = pair(client, state)
    res = client.post("/rpc", json={"method": "state.snapshot"},
                      headers={**auth(token), "Host": "cratebuilder.evil.test"})
    assert res.status_code == 421
    assert res.json()["detail"] == remoteauth_bad_host()


def test_address_literals_and_localhost_are_accepted(client, state):
    token = pair(client, state)
    for host in ("127.0.0.1", "127.0.0.1:8770", "localhost:8770", "192.168.1.9"):
        res = client.post("/rpc", json={"method": "state.snapshot"},
                          headers={**auth(token), "Host": host})
        assert res.status_code == 200, host


def test_the_socket_refuses_a_cross_origin_upgrade(client, state):
    from cratebuilder.server import origin_is_allowed

    assert origin_is_allowed(None, "127.0.0.1:8770", set()) is True  # non-browser
    assert origin_is_allowed("http://127.0.0.1:8770", "127.0.0.1:8770",
                             set()) is True
    assert origin_is_allowed("https://evil.test", "127.0.0.1:8770",
                             set()) is False
    # An address literal is not blanket-accepted here, unlike in a Host header:
    # a page served from the attacker's own IP would otherwise pass.
    assert origin_is_allowed("http://10.1.2.3", "127.0.0.1:8770", set()) is False

    token = pair(client, state)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws?token={token}",
                                      headers={"Origin": "https://evil.test"}) as ws:
            ws.receive_json()
    # 4421, NOT 4401 — the token is good and the deployment is wrong, so the
    # client must keep it rather than read this as revocation.
    assert exc.value.code == 4421


# ── N1: a proxied or tunnelled deployment names its public host ─────────────

PROXY_NAME = "cratebuilder.example.com"


def test_a_forwarded_host_is_refused_until_it_is_configured(client, state):
    """The shipped allow-list is addresses, localhost and this machine's name —
    which is every reverse-proxy and Tailscale deployment refused, since the
    browser sends the PUBLIC name. Naming it is the user's decision, and that
    is the whole difference between it and an attacker-chosen domain."""
    token = pair(client, state)
    before = client.post("/rpc", json={"method": "state.snapshot"},
                         headers={**auth(token), "Host": PROXY_NAME})
    assert before.status_code == 421

    assert state.add_extra_hosts([PROXY_NAME]) == [PROXY_NAME]

    # Every route, not just /rpc — the bundle and pairing are just as broken
    # by a 421, and they are what a new device reaches first.
    after = client.post("/rpc", json={"method": "state.snapshot"},
                        headers={**auth(token), "Host": PROXY_NAME})
    assert after.status_code == 200
    assert after.json()["ok"] is True
    assert client.get("/pair/info", headers={"Host": PROXY_NAME}).status_code == 200
    assert client.get("/", headers={"Host": PROXY_NAME}).status_code == 200
    assert client.get("/logs/activity",
                      headers={**auth(token), "Host": PROXY_NAME}).status_code in (200, 404)


def test_a_configured_host_takes_effect_without_a_restart(client, state):
    """Read per request, not captured at create_app: adding a name has to work
    on the mount that is already running, the way the enabled flag does."""
    token = pair(client, state)
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers={**auth(token), "Host": "booth.tailnet.ts.net"}
                       ).status_code == 421
    state.add_extra_hosts(["booth.tailnet.ts.net"])
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers={**auth(token), "Host": "booth.tailnet.ts.net"}
                       ).status_code == 200
    state.remove_extra_hosts(["booth.tailnet.ts.net"])
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers={**auth(token), "Host": "booth.tailnet.ts.net"}
                       ).status_code == 421


def test_a_configured_host_also_answers_for_the_socket_origin(client, state):
    """The proxy that rewrites Host to the upstream: the browser still sends
    its real Origin, so same-origin can never hold however right the setup is."""
    token = pair(client, state)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
                f"/ws?token={token}",
                headers={"Origin": f"https://{PROXY_NAME}"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4421

    state.add_extra_hosts([PROXY_NAME])
    with client.websocket_connect(
            f"/ws?token={token}",
            headers={"Origin": f"https://{PROXY_NAME}"}) as ws:
        assert ws.receive_json()["type"] == "host.status"


def test_configured_hosts_survive_a_reload_and_do_not_duplicate(tmp_path, clock):
    path = str(tmp_path / "cratebuilder_remote.json")
    first = RemoteState(path, now=clock)
    # Typed the way a user would, scheme and port and all.
    first.add_extra_hosts(["https://CB.Example.com:443/", "cb.example.com"])
    assert first.extra_hosts() == ["cb.example.com"]
    first.add_extra_hosts(["booth.lan"])
    assert RemoteState(path, now=clock).extra_hosts() == ["cb.example.com",
                                                          "booth.lan"]


def test_an_unnamed_domain_is_still_refused_once_another_is_configured(client, state):
    """Adding one name must not open the door to every name."""
    token = pair(client, state)
    state.add_extra_hosts([PROXY_NAME])
    assert client.post("/rpc", json={"method": "state.snapshot"},
                       headers={**auth(token), "Host": "evil.test"}
                       ).status_code == 421


def test_both_entry_points_expose_host_allow():
    """A configurable allow-list is only a fix if the user can reach it."""
    import inspect

    import web_server
    import web_window

    assert "--host-allow" in inspect.getsource(web_server)
    assert "--host-allow" in inspect.getsource(web_window)
    assert web_window.host_allow_args(
        ["web_window.py", "--host-allow", "a.test", "--lan",
         "--host-allow=b.test"]) == ["a.test", "b.test"]


# ── S8: the paired-device roster is the local window's to know ───────────────

def test_a_remote_client_gets_the_device_count_not_the_roster(client, state, service):
    pair(client, state, "Booth iPad")
    token = pair(client, state, "Studio phone")
    body = client.post("/rpc", json={"method": "remote.config"},
                       headers=auth(token)).json()
    assert body["ok"] is True
    assert body["result"]["devices"] == []
    assert body["result"]["device_count"] == 2
    listed = client.post("/rpc", json={"method": "remote.devices"},
                         headers=auth(token)).json()["result"]
    assert listed["devices"] == []
    # The local window still gets the names it renders on the 3j card.
    assert {d["name"] for d in service.call("remote.config")["devices"]} == {
        "Booth iPad", "Studio phone"}


def remoteauth_bad_host():
    from cratebuilder.server import BAD_HOST_REASON
    return BAD_HOST_REASON


def test_no_write_method_slipped_into_the_read_allow_list():
    writes = {"download.start", "download.cancel", "batch.add", "batch.clear",
              "settings.set", "watchlist.scan", "watchlist.scan_all",
              "watchlist.add", "watchlist.remove", "watchlist.edit",
              "db.rebuild", "db.dedupe", "db.repair_tags", "db.fetch_artwork",
              "db.maintenance_preview", "watchlist.resolve_candidates",
              "remote.revoke", "remote.pair_begin", "fs.reveal", "update.apply"}
    assert not (writes & remoteauth.READ_METHODS)

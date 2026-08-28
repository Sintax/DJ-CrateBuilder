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
    return RemoteState(str(tmp_path / "cratebuilder_remote.json"), now=clock)


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
    return TestClient(create_app(service, state, web_dir=str(web)))


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


def test_an_idle_holder_can_be_displaced(state, clock):
    state.claim_control("dev-1", "Gone phone")
    with pytest.raises(remoteauth.ControlHeld):
        state.claim_control("dev-2", "Booth")
    clock.advance(remoteauth.CONTROL_IDLE_SECONDS + 1)
    assert state.claim_control("dev-2", "Booth")["device_id"] == "dev-2"


def test_a_live_holder_cannot_be_displaced_by_going_quiet(state, clock):
    state.claim_control("dev-1", "Desk")
    state.mark_connected("dev-1", True)
    clock.advance(remoteauth.CONTROL_IDLE_SECONDS * 4)
    with pytest.raises(remoteauth.ControlHeld):
        state.claim_control("dev-2", "Booth")


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
    for key in ("remote_enabled", "remote_require_pairing", "remote_read_only"):
        body = client.post("/rpc", json={"method": "settings.set",
                                         "params": {"key": key, "value": True}},
                           headers=auth(token)).json()
        assert body["ok"] is False, key
    assert state.get_flag("enabled") is False


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


def test_no_write_method_slipped_into_the_read_allow_list():
    writes = {"download.start", "download.cancel", "batch.add", "batch.clear",
              "settings.set", "watchlist.scan", "watchlist.scan_all",
              "watchlist.add", "watchlist.remove", "watchlist.edit",
              "db.rebuild", "db.dedupe", "db.repair_tags", "db.fetch_artwork",
              "db.maintenance_preview", "watchlist.resolve_candidates",
              "remote.revoke", "remote.pair_begin", "fs.reveal", "update.apply"}
    assert not (writes & remoteauth.READ_METHODS)

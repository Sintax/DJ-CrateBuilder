"""Contract tests for cratebuilder.ydl.YdlSession — the read-only yt-dlp
boundary.

Every test injects a fake runner, so yt-dlp is never imported and the network
is never touched. The reachability probe is injected too; the one test that
would otherwise reach the default probe asserts it is never called.
"""
import pytest

from cratebuilder import util, ydl
from cratebuilder.settings import CookieConfig


class FakeRunner:
    """Records (opts, target) per call and replays a queued result. A result
    that is an Exception instance is raised instead of returned."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, opts, target):
        self.calls.append((opts, target))
        result = self.results.pop(0) if self.results else {}
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def opts(self):
        return self.calls[-1][0]

    @property
    def target(self):
        return self.calls[-1][1]


def _never_probed():
    raise AssertionError("network probe must not run for this verdict")


def _session(*results, **kwargs):
    kwargs.setdefault("network_probe", _never_probed)
    runner = FakeRunner(*results)
    return ydl.YdlSession(runner=runner, **kwargs), runner


BASE = {"quiet": True, "no_warnings": True, "skip_download": True}
JS = {"js_runtimes": {"node": {"path": None}},
      "remote_components": ["ejs:github"]}


# ── exact options per intent ────────────────────────────────────────────────

def test_probe_metadata_opts_are_flat_with_js_runtime():
    session, runner = _session({"title": "T"})
    assert session.probe_metadata("https://yt/v1") == {"title": "T"}
    assert runner.opts == {**BASE, "extract_flat": "in_playlist", **JS}
    assert runner.target == "https://yt/v1"


def test_probe_formats_opts_have_no_extract_flat_but_do_have_js():
    session, runner = _session({"formats": [{"vcodec": "none", "abr": 128}]})
    assert session.probe_formats("https://yt/v1") == [
        {"vcodec": "none", "abr": 128}]
    assert runner.opts == {**BASE, **JS}
    assert "extract_flat" not in runner.opts


def test_probe_thumbnail_opts_ignore_no_formats_and_carry_js():
    session, runner = _session({"thumbnail": "https://img/1.jpg"})
    assert session.probe_thumbnail("https://yt/v1") == "https://img/1.jpg"
    assert runner.opts == {**BASE, "ignore_no_formats_error": True, **JS}


def test_probe_thumbnail_returns_none_when_absent():
    session, _ = _session({"thumbnail": ""})
    assert session.probe_thumbnail("https://yt/v1") is None


def test_probe_identity_asks_for_the_header_only_and_skips_js():
    session, runner = _session({"channel_id": "UC1", "uploader_id": "@a",
                                "channel_url": "https://yt/c/UC1",
                                "title": "Artist"})
    ident = session.probe_identity("https://yt/@a")
    assert runner.opts == {**BASE, "extract_flat": "in_playlist",
                           "playlist_items": "0"}
    assert "js_runtimes" not in runner.opts
    assert (ident.channel_id, ident.handle) == ("UC1", "@a")
    assert ident.channel_url == "https://yt/c/UC1"
    assert ident.display_name == "Artist"
    assert ident.raw["channel_id"] == "UC1"


def test_probe_identity_blank_answer_is_not_an_error():
    session, _ = _session({})
    ident = session.probe_identity("https://yt/@a")
    assert (ident.channel_id, ident.handle, ident.channel_url,
            ident.display_name) == ("", "", "", "")


def test_list_channel_opts_are_flat_and_lazy_without_js():
    session, runner = _session({"entries": [{"id": "v1"}, {"id": "v2"}]})
    assert session.list_channel("https://yt/c/UC1/videos") == [
        {"id": "v1"}, {"id": "v2"}]
    assert runner.opts == {**BASE, "extract_flat": "in_playlist",
                           "lazy_playlist": True}
    assert "js_runtimes" not in runner.opts


def test_list_channel_ignore_no_formats_flag_adds_exactly_one_key():
    session, runner = _session({"entries": []})
    session.list_channel("https://yt/c/UC1", ignore_no_formats=True)
    assert runner.opts == {**BASE, "extract_flat": "in_playlist",
                           "lazy_playlist": True,
                           "ignore_no_formats_error": True}


def test_list_channel_flattens_one_tab_level():
    session, _ = _session({"entries": [
        {"id": "tab", "entries": [{"id": "v1"}, {"id": "v2"}]},
        {"id": "v3"}]})
    assert session.list_channel("https://yt/@a") == [
        {"id": "v1"}, {"id": "v2"}, {"id": "v3"}]


def test_list_channel_returns_empty_list_when_yt_dlp_returns_nothing():
    session, _ = _session(None)
    assert session.list_channel("https://yt/@a") == []


def test_search_channels_opts_use_extract_flat_true_and_item_range():
    session, runner = _session({"entries": []})
    session.search_channels("Some Artist", max_results=5)
    assert runner.opts == {**BASE, "extract_flat": True,
                           "playlist_items": "1-5"}
    assert runner.target == ("https://www.youtube.com/results?"
                             "search_query=Some%20Artist&sp=EgIQAg%3D%3D")


def test_search_soundcloud_tracks_opts_and_query():
    session, runner = _session({"entries": []})
    session.search_soundcloud_tracks("some artist", limit=7)
    assert runner.opts == {**BASE, "extract_flat": True}
    assert runner.target == "scsearch7:some artist"


# ── one auth policy, applied to every intent ───────────────────────────────

_COOKIES_ON = CookieConfig(use_cookies=True, cookie_method="Browser",
                           cookies_browser="Firefox", cookies_profile="dj",
                           cookie_file="")
_COOKIES_OFF = CookieConfig(use_cookies=False, cookie_method="Browser",
                            cookies_browser="Firefox", cookies_profile="dj",
                            cookie_file="")


def _exercise_every_intent(session):
    session.probe_metadata("u")
    session.probe_formats("u")
    session.probe_thumbnail("u")
    session.probe_identity("u")
    session.list_channel("u")
    session.search_channels("name")
    session.search_soundcloud_tracks("name")


def test_every_intent_including_the_searches_gets_the_session_auth():
    session, runner = _session(*[{}] * 7, cookies=_COOKIES_ON)
    _exercise_every_intent(session)
    assert len(runner.calls) == 7
    for opts, _ in runner.calls:
        assert opts["cookiesfrombrowser"] == ("firefox", "dj")


def test_cookies_off_means_no_cookie_keys_on_any_intent():
    session, runner = _session(*[{}] * 7, cookies=_COOKIES_OFF)
    _exercise_every_intent(session)
    for opts, _ in runner.calls:
        assert "cookiesfrombrowser" not in opts
        assert "cookiefile" not in opts


def test_no_cookie_config_at_all_means_no_cookie_keys():
    session, runner = _session({}, cookies=None)
    session.probe_metadata("u")
    assert "cookiesfrombrowser" not in runner.opts
    assert "cookiefile" not in runner.opts


def test_cookie_file_method_uses_the_file_when_it_exists(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookies = CookieConfig(use_cookies=True, cookie_method="Cookie File",
                           cookies_browser="Firefox", cookies_profile="",
                           cookie_file=f"  {jar}  ")
    session, runner = _session({}, cookies=cookies)
    session.list_channel("u")
    assert runner.opts["cookiefile"] == str(jar)


def test_cookie_file_method_with_missing_file_stays_unauthenticated(tmp_path):
    cookies = CookieConfig(use_cookies=True, cookie_method="Cookie File",
                           cookies_browser="Firefox", cookies_profile="",
                           cookie_file=str(tmp_path / "nope.txt"))
    session, runner = _session({}, cookies=cookies)
    session.list_channel("u")
    assert "cookiefile" not in runner.opts


def test_auth_policy_is_snapshotted_once_per_session(tmp_path):
    """A session decides its auth once; a later change to the file on disk
    cannot make one intent authenticate differently from another."""
    jar = tmp_path / "cookies.txt"
    jar.write_text("x", encoding="utf-8")
    cookies = CookieConfig(use_cookies=True, cookie_method="Cookie File",
                           cookies_browser="Firefox", cookies_profile="",
                           cookie_file=str(jar))
    session, runner = _session({}, {}, cookies=cookies)
    session.probe_metadata("u")
    jar.unlink()
    session.list_channel("u")
    assert runner.calls[0][0]["cookiefile"] == str(jar)
    assert runner.calls[1][0]["cookiefile"] == str(jar)


# ── typed errors + the captive-portal rule ─────────────────────────────────

def test_transient_message_raises_offline():
    session, _ = _session(RuntimeError("Unable to download webpage: timed out"))
    with pytest.raises(ydl.YdlOffline) as exc:
        session.list_channel("u")
    assert "timed out" in exc.value.message
    assert exc.value.intent == "list_channel"
    assert exc.value.target == "u"


def test_permanent_message_with_network_up_raises_permanent():
    session, _ = _session(RuntimeError("This channel does not exist"),
                          network_probe=lambda: True)
    with pytest.raises(ydl.YdlPermanent) as exc:
        session.list_channel("u")
    assert exc.value.message == "This channel does not exist"


def test_permanent_message_with_network_down_is_downgraded_to_offline():
    """The captive-portal rule: a hotel portal answers everything with its own
    404, which says nothing about the channel."""
    session, _ = _session(RuntimeError("HTTP Error 404: Not Found"),
                          network_probe=lambda: False)
    with pytest.raises(ydl.YdlOffline) as exc:
        session.list_channel("u")
    assert exc.value.message == "HTTP Error 404: Not Found"


def test_unrecognised_message_raises_unclassified():
    session, _ = _session(RuntimeError("something we have never seen"))
    with pytest.raises(ydl.YdlUnclassified):
        session.probe_metadata("u")


def test_every_typed_error_is_a_ydl_error():
    for cls in (ydl.YdlOffline, ydl.YdlPermanent, ydl.YdlUnclassified):
        assert issubclass(cls, ydl.YdlError)


def test_transient_verdict_never_consults_the_network():
    """_never_probed raises if called — a transient message is already a
    definite verdict, so probing would be a pointless extra timeout."""
    session, _ = _session(RuntimeError("connection reset by peer"))
    with pytest.raises(ydl.YdlOffline):
        session.probe_identity("u")


def test_the_captive_portal_rule_protects_every_intent():
    for call in (lambda s: s.probe_metadata("u"),
                 lambda s: s.probe_formats("u"),
                 lambda s: s.probe_thumbnail("u"),
                 lambda s: s.probe_identity("u"),
                 lambda s: s.list_channel("u"),
                 lambda s: s.search_channels("n"),
                 lambda s: s.search_soundcloud_tracks("n")):
        session, _ = _session(RuntimeError("Video unavailable: is private"),
                              network_probe=lambda: False)
        with pytest.raises(ydl.YdlOffline):
            call(session)


def test_error_verdicts_agree_with_the_scan_classifier():
    """One home for the markers: ydl and sidecar read the same verdict."""
    from cratebuilder import sidecar
    pairs = (("HTTP Error 404", ydl.YdlPermanent, "needs_resolve"),
             ("connection refused", ydl.YdlOffline, "offline"),
             ("who knows", ydl.YdlUnclassified, "error"))
    for message, error_type, status in pairs:
        session, _ = _session(RuntimeError(message), network_probe=lambda: True)
        with pytest.raises(error_type):
            session.list_channel("u")
        assert sidecar.classify_scan_error(message) == status


# ── result shaping ─────────────────────────────────────────────────────────

def test_search_channels_drops_non_uc_rows_and_shapes_the_rest():
    session, _ = _session({"entries": [
        {"id": "abc123", "title": "A video row"},
        {"channel_id": "UCaaa", "title": "Real Channel",
         "uploader_id": "@real", "channel_follower_count": 1234},
        {"id": "UCbbb", "channel": "Fallback Title"},
    ]})
    out = session.search_channels("Query Name", max_results=5)
    assert out == [
        {"title": "Real Channel", "channel_id": "UCaaa",
         "url": "https://www.youtube.com/channel/UCaaa/videos",
         "handle": "@real", "followers": 1234},
        {"title": "Fallback Title", "channel_id": "UCbbb",
         "url": "https://www.youtube.com/channel/UCbbb/videos",
         "handle": "", "followers": None},
    ]


def test_search_channels_falls_back_to_the_query_name_for_a_titleless_row():
    session, _ = _session({"entries": [{"channel_id": "UCaaa"}]})
    assert session.search_channels("Query Name")[0]["title"] == "Query Name"


def test_search_channels_stops_at_max_results():
    session, _ = _session({"entries": [
        {"channel_id": f"UC{n}", "title": f"C{n}"} for n in range(6)]})
    assert len(session.search_channels("n", max_results=2)) == 2


def test_search_soundcloud_tracks_url_fallback_chain():
    session, _ = _session({"entries": [
        {"url": "https://sc/a/one", "uploader": " Artist A "},
        {"permalink_url": "https://sc/b/two", "channel": "Artist B"},
        {"webpage_url": "https://sc/c/three"},
        {"uploader": "No URL At All"},
        {"url": "", "permalink_url": "", "webpage_url": ""},
    ]})
    assert session.search_soundcloud_tracks("q") == [
        {"url": "https://sc/a/one", "title": "Artist A"},
        {"url": "https://sc/b/two", "title": "Artist B"},
        {"url": "https://sc/c/three", "title": ""},
    ]


def test_search_soundcloud_tracks_prefers_url_over_the_later_fields():
    session, _ = _session({"entries": [
        {"url": "https://sc/first", "permalink_url": "https://sc/second",
         "webpage_url": "https://sc/third"}]})
    assert session.search_soundcloud_tracks("q")[0]["url"] == "https://sc/first"


def test_probe_formats_returns_an_empty_list_when_there_are_none():
    session, _ = _session({"formats": None})
    assert session.probe_formats("u") == []


# ── debug logging ──────────────────────────────────────────────────────────

class RecordingLogger:
    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)


def test_every_intent_logs_one_line_labelled_with_its_own_name():
    log = RecordingLogger()
    runner = FakeRunner(*[{}] * 7)
    session = ydl.YdlSession(runner=runner, debug=log,
                             network_probe=_never_probed)
    _exercise_every_intent(session)
    labels = [line.split("(")[1].split(")")[0] for line in log.lines]
    assert labels == ["probe_metadata", "probe_formats", "probe_thumbnail",
                      "probe_identity", "list_channel", "search_channels",
                      "search_soundcloud_tracks"]


def test_debug_log_redacts_the_browser_cookie_source():
    log = RecordingLogger()
    runner = FakeRunner({})
    session = ydl.YdlSession(cookies=_COOKIES_ON, runner=runner, debug=log,
                             network_probe=_never_probed)
    session.probe_metadata("u")
    assert "<redacted>" in log.lines[0]
    assert "firefox" not in log.lines[0]
    assert "dj" not in log.lines[0]
    assert runner.opts["cookiesfrombrowser"] == ("firefox", "dj")


def test_debug_log_redacts_the_cookie_file_path(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("x", encoding="utf-8")
    cookies = CookieConfig(use_cookies=True, cookie_method="Cookie File",
                           cookies_browser="Firefox", cookies_profile="",
                           cookie_file=str(jar))
    log = RecordingLogger()
    session = ydl.YdlSession(cookies=cookies, runner=FakeRunner({}), debug=log,
                             network_probe=_never_probed)
    session.list_channel("u")
    assert "<redacted>" in log.lines[0]
    assert str(jar) not in log.lines[0]


def test_a_plain_callable_works_as_the_debug_sink():
    lines = []
    session = ydl.YdlSession(runner=FakeRunner({}), debug=lines.append,
                             network_probe=_never_probed)
    session.probe_formats("u")
    assert lines[0].startswith("YDL OPTS (probe_formats) | ")


def test_no_debug_sink_means_no_logging_and_no_crash():
    session, _ = _session({})
    session.probe_metadata("u")


def test_logged_line_is_a_snapshot_not_the_live_opts_dict():
    log = RecordingLogger()
    runner = FakeRunner({})
    session = ydl.YdlSession(runner=runner, debug=log,
                             network_probe=_never_probed)
    session.list_channel("u")
    assert "'extract_flat': 'in_playlist'" in log.lines[0]


# ── the default injections stay out of the way ─────────────────────────────

def test_the_default_runner_is_the_yt_dlp_one():
    assert ydl.YdlSession()._runner is ydl.extract_info


def test_the_default_network_probe_is_the_tcp_one():
    assert ydl.YdlSession()._network_probe is ydl.network_is_reachable


def test_network_is_reachable_is_false_when_every_connect_fails(monkeypatch):
    tried = []

    def _refuse(address, timeout=None):
        tried.append(address)
        raise OSError("connection refused")

    monkeypatch.setattr(ydl.socket, "create_connection", _refuse)
    assert ydl.network_is_reachable(timeout=0.01) is False
    assert tried == list(ydl._REACHABILITY_ENDPOINTS)


def test_network_is_reachable_is_true_on_the_first_success(monkeypatch):
    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ydl.socket, "create_connection",
                        lambda address, timeout=None: _Sock())
    assert ydl.network_is_reachable(timeout=0.01) is True


def test_read_only_intents_never_set_download_only_options():
    session, runner = _session(*[{}] * 7, cookies=_COOKIES_ON)
    _exercise_every_intent(session)
    for opts, _ in runner.calls:
        for key in ("http_headers", "geo_bypass", "sleep_interval",
                    "max_sleep_interval", "postprocessors", "outtmpl",
                    "progress_hooks", "format"):
            assert key not in opts
        assert opts["skip_download"] is True


def test_util_owns_the_markers_and_ydl_reads_them():
    assert util.classify_ydl_error("HTTP Error 404") == "permanent"
    assert util.classify_ydl_error("getaddrinfo failed") == "transient"
    assert util.classify_ydl_error("") == "unknown"
    assert util.classify_ydl_error(None) == "unknown"
    assert not hasattr(ydl, "PERMANENT_ERROR_MARKERS")

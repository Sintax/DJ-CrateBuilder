"""The monolith's read-only yt-dlp sites go through YdlSession, never yt-dlp.

Every test injects a recording runner, so no test imports yt_dlp or touches the
network. What is asserted at each site: the intent it asks for, the options that
intent really built (auth policy, JS runtime), and the caller-side handling of a
typed failure.
"""
import pytest

from cratebuilder import util as cb_util
from cratebuilder import ydl as cb_ydl
from cratebuilder.settings import CookieConfig


# Every Tk variable a test in this file flips. Nothing here touches the DB or
# the crate folders, so one shared app serves the whole file — each test just
# starts from the fresh-app value of these vars.
_COOKIE_VARS = ("_use_cookies", "_cookie_method", "_cookies_browser",
                "_cookies_profile", "_cookie_file")


@pytest.fixture(scope="module")
def _cookie_defaults(shared_app):
    return {name: getattr(shared_app, name).get() for name in _COOKIE_VARS}


@pytest.fixture
def app(shared_app, _cookie_defaults):
    """This file's `app` IS the module-shared app, cookie vars reset."""
    for name, value in _cookie_defaults.items():
        getattr(shared_app, name).set(value)
    return shared_app


class RecordingRunner:
    """A YdlSession runner that records every (opts, target) it is handed and
    replays a canned answer — or raises one."""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def __call__(self, opts, target):
        self.calls.append((dict(opts), target))
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def opts(self):
        return self.calls[-1][0]

    @property
    def target(self):
        return self.calls[-1][1]


@pytest.fixture
def fake_ydl(app, monkeypatch):
    """Replace the app's session factory with one whose runner is a stub.

    Returns a function: install(result=…, error=…) -> RecordingRunner. The
    session itself is the real YdlSession built from the real _cookie_config(),
    so option-building and error typing are exercised for real; only yt-dlp and
    the reachability probe are faked.
    """
    def install(result=None, error=None, reachable=True):
        runner = RecordingRunner(result=result, error=error)
        monkeypatch.setattr(
            app, "_ydl_session",
            lambda **kw: cb_ydl.YdlSession(
                cookies=app._cookie_config(), debug=app._dbg, runner=runner,
                network_probe=lambda: reachable, **kw))
        return runner
    return install


# ── the factory itself ────────────────────────────────────────────────────────

def test_ydl_session_carries_the_live_tk_cookie_policy(app):
    """_ydl_session() snapshots the cookie vars as they are right now, so a
    setting the user just changed applies to the next operation."""
    app._use_cookies.set(True)
    app._cookie_method.set("Browser")
    app._cookies_browser.set("Firefox")
    app._cookies_profile.set("work")
    runner = RecordingRunner()
    app._ydl_session(runner=runner).probe_identity("https://x/y")
    assert runner.opts["cookiesfrombrowser"] == ("firefox", "work")

    app._use_cookies.set(False)
    runner2 = RecordingRunner()
    app._ydl_session(runner=runner2).probe_identity("https://x/y")
    assert "cookiesfrombrowser" not in runner2.opts


class RecordingDebug:
    """Stands in for the app's debug logger."""

    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)


def test_ydl_session_logs_every_intent_to_the_debug_log(app, monkeypatch):
    """Redacted-options logging is the session's job now — it happens for
    intents that never logged anything before, and the auth value never
    reaches the log."""
    dbg = RecordingDebug()
    monkeypatch.setattr(app, "_dbg", dbg)
    app._use_cookies.set(True)
    app._cookie_method.set("Browser")
    app._cookies_browser.set("Chrome")
    app._cookies_profile.set("secret-profile")
    app._ydl_session(runner=RecordingRunner()).search_soundcloud_tracks("x")
    logged = "\n".join(dbg.lines)
    assert "YDL OPTS (search_soundcloud_tracks)" in logged
    assert "<redacted>" in logged
    assert "secret-profile" not in logged


# ── site 6 — channel search behind the Fix Link dialog ────────────────────────

def test_resolve_channel_via_search_uses_the_search_channels_intent(fake_ydl,
                                                                    app):
    runner = fake_ydl(result={"entries": [
        {"id": "UCaaa", "title": "Chan A", "uploader_id": "@a",
         "channel_follower_count": 7},
        {"id": "notachannel", "title": "A video"},
    ]})
    out = app._resolve_channel_via_search("Chan A", max_results=3)
    assert [c["channel_id"] for c in out] == ["UCaaa"]
    assert "UCaaa" in out[0]["url"]
    assert runner.opts["playlist_items"] == "1-3"
    assert runner.opts["extract_flat"] is True
    assert "search_query=Chan%20A" in runner.target


def test_resolve_channel_via_search_now_authenticates(fake_ydl, app):
    """Settled behaviour change: the search used to run unauthenticated."""
    app._use_cookies.set(True)
    app._cookie_method.set("Browser")
    app._cookies_browser.set("Edge")
    app._cookies_profile.set("")
    runner = fake_ydl(result={"entries": []})
    app._resolve_channel_via_search("whoever")
    assert runner.opts["cookiesfrombrowser"] == ("edge",)


def test_resolve_channel_via_search_still_raises_by_design(fake_ydl, app):
    """The Fix Link dialog catches the failure itself to show 'Search failed:
    …', so this call site must keep propagating."""
    fake_ydl(error=RuntimeError("HTTP Error 429: Too Many Requests"))
    with pytest.raises(cb_ydl.YdlError) as excinfo:
        app._resolve_channel_via_search("whoever")
    assert "429" in str(excinfo.value)


# ── site 8 — SoundCloud track search ──────────────────────────────────────────

def test_sc_track_search_uses_the_soundcloud_search_intent(fake_ydl, app):
    runner = fake_ydl(result={"entries": [
        {"url": "https://soundcloud.com/artist/track", "uploader": "Artist"},
        {"title": "no url here"},
    ]})
    assert app._sc_track_search("Artist", limit=5) == [
        {"url": "https://soundcloud.com/artist/track", "title": "Artist"}]
    assert runner.target == "scsearch5:Artist"


def test_sc_track_search_still_raises_by_design(fake_ydl, app):
    fake_ydl(error=RuntimeError("Unable to download API page"))
    with pytest.raises(cb_ydl.YdlError):
        app._sc_track_search("Artist")


# ── site 2 — artwork backfill channel listing ─────────────────────────────────

@pytest.fixture
def artfill(app, cb_mod, monkeypatch):
    """An _ArtworkBackfillSession over no rows, with sidecar/tag reads stubbed
    so the yt-dlp path is the only thing under test."""
    monkeypatch.setattr(cb_mod, "read_channel_sidecar",
                        lambda folder: {"channel_url": "https://yt/c/x",
                                        "platform": "YouTube",
                                        "display_name": "Chan"})
    monkeypatch.setattr(cb_mod, "read_source_url",
                        lambda path: "https://soundcloud.com/a/b")
    return cb_mod._ArtworkBackfillSession(app, [], "square")


def test_artwork_listing_asks_for_a_channel_list_that_ignores_no_formats(
        artfill, fake_ydl):
    runner = fake_ydl(result={"entries": [{"id": "v1", "title": "Track One"}]})
    index = artfill._fetch_channel_index("C:/crate/Chan")
    assert "v1" in index.values()
    assert runner.opts["ignore_no_formats_error"] is True
    assert runner.opts["extract_flat"] == "in_playlist"
    assert runner.opts["lazy_playlist"] is True


def test_artwork_listing_flattens_a_tabbed_channel_response(artfill, fake_ydl):
    """A bare channel URL answers with tabs; the index must still see videos."""
    fake_ydl(result={"entries": [
        {"entries": [{"id": "v1", "title": "Track One"}]}]})
    assert "v1" in artfill._fetch_channel_index("C:/crate/Chan").values()


def test_artwork_listing_caches_an_empty_index_on_failure(artfill, fake_ydl):
    """Returning {} is what makes _recover_video_id cache the failure, so a
    dead channel costs one lookup for the whole run rather than one per track."""
    fake_ydl(error=RuntimeError("404 Not Found"))
    assert artfill._fetch_channel_index("C:/crate/Chan") == {}


# ── site 3 — thumbnail from the ID3 source tag ────────────────────────────────

def test_thumbnail_from_source_tag_uses_the_thumbnail_intent(artfill,
                                                             fake_ydl):
    runner = fake_ydl(result={"thumbnail": "https://img/a.jpg"})
    assert artfill._thumbnail_from_source_tag("C:/x.mp3") == \
        "https://img/a.jpg"
    assert runner.opts["ignore_no_formats_error"] is True
    assert "extract_flat" not in runner.opts


def test_thumbnail_probe_now_gets_the_js_runtime(artfill, fake_ydl):
    """The one documented JS-runtime rule violation: this site extracts real
    formats and used to run without a runtime. Routing it through the session
    fixes it."""
    runner = fake_ydl(result={"thumbnail": "https://img/a.jpg"})
    artfill._thumbnail_from_source_tag("C:/x.mp3")
    assert runner.opts["js_runtimes"] == {"node": {"path": None}}


def test_thumbnail_from_source_tag_returns_none_on_failure(artfill, fake_ydl):
    fake_ydl(error=RuntimeError("Video unavailable"))
    assert artfill._thumbnail_from_source_tag("C:/x.mp3") is None


# ── site 11 — Watch List scan verdicts ────────────────────────────────────────

def test_wl_scan_verdict_map_covers_every_typed_ydl_error(cb_mod):
    """The scan reads its verdict off the error's type, so every concrete
    YdlError subclass must be in the map or a scan would fall through to
    re-reading the message YdlSession already judged."""
    assert cb_mod.WL_SCAN_VERDICT_BY_YDL_ERROR == {
        cb_ydl.YdlPermanent:    "needs_resolve",
        cb_ydl.YdlOffline:      "offline",
        cb_ydl.YdlUnclassified: "error",
    }

    def every_subclass(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from every_subclass(sub)

    # Walks the whole tree, not just direct subclasses: a type derived from
    # one of these must still resolve to a verdict.
    for error_type in every_subclass(cb_ydl.YdlError):
        assert cb_mod.wl_scan_verdict_for(error_type("boom")) is not None


def test_a_failed_metadata_fetch_names_its_cause_from_the_error_type(cb_mod):
    """The Main tab's fetch failure reads the same way the Watch List scan
    does — off the type, because YdlSession has already applied the
    captive-portal rule and re-reading the message here would undo it."""
    for error_type, lead in (
            (cb_ydl.YdlOffline,      "Can't reach the site"),
            (cb_ydl.YdlPermanent,    "Link is gone"),
            (cb_ydl.YdlUnclassified, "Fetch failed")):
        kind = cb_mod.fetch_failure_kind_for(error_type("boom"))
        assert cb_util.describe_fetch_failure(kind, "boom").startswith(lead)

    # Anything that isn't a typed read failure still gets a sentence rather
    # than falling through to None and formatting as "None".
    assert cb_mod.fetch_failure_kind_for(RuntimeError("boom")) == "unknown"


@pytest.mark.parametrize("message,expected", [
    ("ERROR: This channel does not exist", "needs_resolve"),
    ("HTTP Error 503: Service Unavailable", "offline"),
    ("something nobody has ever seen before", "error"),
])
def test_scan_verdict_matches_the_old_message_classifier(app, cb_mod,
                                                         message, expected):
    """The typed errors must land on exactly the verdicts classify_scan_error
    produced from the same text, with the network up."""
    session = cb_ydl.YdlSession(
        runner=RecordingRunner(error=RuntimeError(message)),
        network_probe=lambda: True)
    with pytest.raises(cb_ydl.YdlError) as excinfo:
        session.list_channel("https://yt/c/x")
    assert cb_mod.WL_SCAN_VERDICT_BY_YDL_ERROR[type(excinfo.value)] == expected


def test_captive_portal_downgrades_a_dead_looking_channel_to_offline(app,
                                                                    cb_mod):
    """A hotel portal answers 404 for everything. With no route to the network
    the scan must park the row as offline and keep its link, not send the user
    to Fix Link — and that rule now lives in the session, for every intent."""
    session = cb_ydl.YdlSession(
        runner=RecordingRunner(error=RuntimeError("HTTP Error 404: Not Found")),
        network_probe=lambda: False)
    with pytest.raises(cb_ydl.YdlOffline) as excinfo:
        session.list_channel("https://yt/c/x")
    assert cb_mod.WL_SCAN_VERDICT_BY_YDL_ERROR[type(excinfo.value)] == "offline"


# ── reachability has one home ─────────────────────────────────────────────────

def test_app_reachability_probe_delegates_to_the_ydl_module(app, monkeypatch):
    seen = []
    monkeypatch.setattr(cb_ydl, "network_is_reachable",
                        lambda timeout=2.0: seen.append(timeout) or True)
    assert app._network_is_reachable(0.5) is True
    assert seen == [0.5]


# ── the download site shares the read-only site's option helpers ──────────────

def test_the_download_path_authenticates_through_the_shared_helpers(app):
    """What _apply_cookie_opts / _apply_js_runtime used to guard, at the seam
    that replaced them.

    The download options now come out of cratebuilder.download's one builder,
    fed the same CookieConfig snapshot a YdlSession authenticates with — so the
    authenticated attempt carries the user's cookies and the JS runtime, and the
    unauthenticated age-gate attempt differs by the cookies alone."""
    from cratebuilder import download as cb_download

    app._use_cookies.set(True)
    app._cookie_method.set("Browser")
    app._cookies_browser.set("Brave")
    app._cookies_profile.set("")
    plan = cb_download.TrackPlan(url="https://yt/watch?v=x", title="T",
                                 save_dir="C:/crate", genre="DnB",
                                 platform="YouTube")
    opts_for = cb_download.download_opts_builder(
        plan, cookies=app._cookie_config())

    authed = opts_for(True)
    assert authed["cookiesfrombrowser"] == ("brave",)
    assert authed["js_runtimes"] == {"node": {"path": None}}

    anon = opts_for(False)
    assert "cookiesfrombrowser" not in anon
    assert anon["js_runtimes"] == {"node": {"path": None}}


def test_the_monolith_builds_no_yt_dlp_options_dict_at_all(cb_mod):
    """Zero YoutubeDL constructions in the app file: every read-only question
    goes through cratebuilder.ydl and the download goes through
    cratebuilder.download."""
    import inspect
    source = inspect.getsource(cb_mod)
    assert source.count("yt_dlp.YoutubeDL(") == 0
    assert "progress_hooks" not in source
    assert "FFmpegExtractAudio" not in source


def test_the_downloader_is_constructed_once_per_track(cb_mod):
    """Inside the per-entry loop, not above it.

    The policy and cookie snapshots are read at construction, so building it
    per URL silently narrows "Keep original format", geo-bypass and use_cookies
    from per-track to per-URL: a setting the user changes mid-channel would
    wait for the next URL instead of the next track, which is not what the
    pre-TrackDownloader code did."""
    import inspect
    lines = inspect.getsource(cb_mod.MP3DownloaderApp._process_one_url).splitlines()
    loop = next(i for i, ln in enumerate(lines)
                if "for idx, entry in enumerate(entries)" in ln)
    built = [i for i, ln in enumerate(lines)
             if "cb_download.TrackDownloader(" in ln]
    assert len(built) == 1
    assert built[0] > loop


def test_a_cancelled_track_leaves_a_settled_queue_row(cb_mod):
    """The cancelled branch breaks out of the loop, so it has to render the row
    on the way past — otherwise the in-flight row stays ST_ACTIVE and spins for
    the rest of the session."""
    import inspect
    source = inspect.getsource(cb_mod.MP3DownloaderApp._process_one_url)
    body = source.split('if outcome.kind == "cancelled":', 1)[1]
    head = body.split("break", 1)[0]
    assert "_set_row_state" in head
    assert "ST_SKIPPED" in head


def test_cookie_config_snapshot_is_what_the_session_authenticates_with(app):
    """The worker-facing snapshot, not Settings.cookie_config() — a session must
    authenticate with what the user currently sees in Settings."""
    app._use_cookies.set(True)
    app._cookie_method.set("Cookie File")
    app._cookie_file.set("  ")
    snapshot = app._cookie_config()
    assert isinstance(snapshot, CookieConfig)
    runner = RecordingRunner()
    app._ydl_session(runner=runner).probe_metadata("https://x/y")
    # "Cookie File" with a blank path stays silently unauthenticated.
    assert "cookiefile" not in runner.opts

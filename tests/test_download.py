"""Contract tests for cratebuilder.download.TrackDownloader — one track, end
to end.

Every test injects a fake runner, so yt-dlp is never imported, nothing is ever
downloaded and the network is never touched. The Canceller is injected too, so
no test ever waits out a real backoff. All disk writes go to tmp_path.
"""
import pytest

from cratebuilder.crate import CrateLayout
from cratebuilder.db import DownloadsDatabase
from cratebuilder.download import (
    MAX_ATTEMPTS, NullSink, Outcome, REASON_WIDTH, TrackDownloader, TrackPlan,
    classify_download_failure, condense_error, download_opts_builder,
    looks_age_restricted, parse_percent, rung_kbps, strip_ansi,
)
from cratebuilder.settings import CookieConfig, DownloadPolicy


# ── doubles ──────────────────────────────────────────────────────────────────
class FakeRunner:
    """Records (opts, url) per call and replays one queued result per call.

    An Exception instance is raised, a callable is invoked with (opts, url) so
    it can drive the progress hook, anything else is returned as the info dict.
    Running out of results is an error — a test that under-queues is a test
    whose ladder ran a rung it should not have."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, opts, url):
        self.calls.append((opts, url))
        assert self.results, f"runner called {len(self.calls)}x, unqueued"
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(opts, url)
        return result

    @property
    def opts(self):
        return self.calls[-1][0]


class RecordingSink:
    """Every Sink event, in order, as a tuple."""

    def __init__(self):
        self.events = []

    def started(self, title):
        self.events.append(("started", title))

    def progress(self, percent=None, speed_text=""):
        self.events.append(("progress", percent, speed_text))

    def bitrate_detected(self, source_abr, target_kbps):
        self.events.append(("bitrate_detected", source_abr, target_kbps))

    def title_corrected(self, title):
        self.events.append(("title_corrected", title))

    def finished(self):
        self.events.append(("finished",))

    def kinds(self):
        return [e[0] for e in self.events]


class FakeCanceller:
    """Records every backoff it is asked to wait out. Reports cancellation on
    the *cancel_on*-th wait (1-based); never, when that is None. Never sleeps.
    `now` is the immediate signal is_set() reports — the hook's mid-download
    abort and the pre-attempt check read it."""

    def __init__(self, cancel_on=None):
        self.waits = []
        self.cancel_on = cancel_on
        self.now = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.now:
            return True
        return self.cancel_on is not None and len(self.waits) >= self.cancel_on

    def is_set(self):
        return self.now


class RecordingLog:
    """Stand-in for the app's activity logger and debug logger."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def error(self, message):
        self.lines.append(("error", message))

    def debug(self, message):
        self.lines.append(("debug", message))

    def at(self, level):
        return [m for lvl, m in self.lines if lvl == level]


class FakeDb:
    """Records the two DB calls a download makes, with their kwargs."""

    def __init__(self):
        self.downloads = []
        self.unavailable = []

    def add_download(self, **kwargs):
        self.downloads.append(kwargs)

    def record_unavailable(self, **kwargs):
        self.unavailable.append(kwargs)
        return True


def _policy(**over):
    """A real DownloadPolicy snapshot, so the field names a download reads are
    pinned against the declared record rather than a loose stub."""
    fields = dict(
        skip_existing=True, skip_mode="In Folder Only", limit_enabled=False,
        limit_minutes=8, bitrate_quality="192", bitrate_auto_upgrade=False,
        no_conversion=False,
        sleep_enabled=True, sleep_mode="Auto", sleep_preset="Light  (1–5 s)",
        sleep_min=1, sleep_max=5, geo_bypass=True, rotate_ua=True,
        cover_art_enabled=True, cover_art_mode="crop")
    fields.update(over)
    return DownloadPolicy(**fields)


def _cookies(use_cookies=True):
    return CookieConfig(use_cookies=use_cookies, cookie_method="Browser",
                        cookies_browser="Firefox", cookies_profile="",
                        cookie_file="")


def _plan(tmp_path, **over):
    fields = dict(
        url="https://youtu.be/v1", title="Track 1",
        save_dir=str(tmp_path), genre="DnB", platform="YouTube",
        video_id="v1", expected_path=str(tmp_path / "Track 1.mp3"),
        target_kbps="192")
    fields.update(over)
    return TrackPlan(**fields)


def _downloader(runner, **over):
    kwargs = dict(runner=runner, db=FakeDb(), policy=_policy(),
                  canceller=FakeCanceller())
    kwargs.update(over)
    return TrackDownloader(**kwargs)


def _hook_driver(info, *hook_dicts):
    """A runner result that feeds *hook_dicts* to the opts' progress hook (as
    yt-dlp would) and then returns *info*."""
    def run(opts, url):
        for d in hook_dicts:
            for hook in opts["progress_hooks"]:
                hook(d)
        return info
    return run


AGE_ERROR = RuntimeError("Content warning: this video may be inappropriate "
                         "for some users. Confirm your age")
TRANSIENT = "[WinError 10054] An existing connection was forcibly closed"
# yt-dlp's commonest transient wrapper. The retired bare-"age" substring test
# read this as an age gate, because "age" sits inside "webpage".
TRANSIENT_WEBPAGE = "Unable to download webpage: The read operation timed out"


# ── success path, end to end ─────────────────────────────────────────────────
def test_success_writes_file_tags_db_row_and_outcome(tmp_path):
    save_dir = tmp_path / "crate"
    save_dir.mkdir()
    mp3 = save_dir / "Real Track.mp3"
    mp3.write_bytes(b"ID3 audio")
    art = tmp_path / "art.jpg"
    art.write_bytes(b"jpg")

    db = DownloadsDatabase(str(tmp_path / "cb.db"))
    tagged, remembered = [], []
    runner = FakeRunner(_hook_driver(
        {"title": "Real Track", "id": "vid9", "upload_date": "20240102",
         "thumbnail": "https://img/9.jpg",
         "requested_downloads": [{"filepath": str(mp3)}]},
        {"status": "downloading", "total_bytes": 200, "downloaded_bytes": 100,
         "info_dict": {"abr": 160, "title": "Real Track"}}))

    dl = TrackDownloader(
        runner=runner, db=db, policy=_policy(), canceller=FakeCanceller(),
        tag=lambda path, title, url, genre=None: tagged.append(
            (path, title, url, genre)),
        harvest_art=lambda path, vid, title, source_url=None, genre=None: (
            str(art), True, path),
        remember=remembered.append)

    plan = _plan(tmp_path, title="Real Track", save_dir=str(save_dir),
                 video_id="", channel_name="UKF", channel_url="https://yt/c",
                 channel_id="UC1", thumbnail_url="",
                 expected_path=str(save_dir / "Real Track.mp3"))
    outcome = dl.run(plan, NullSink())

    assert outcome == Outcome(
        kind="downloaded", title="Real Track", path=str(mp3),
        bitrate_text="160k → 192k",
        quality_text="160 kbps src → 192 kbps MP3",
        artwork_path=str(art), artwork_embedded=True)
    assert tagged == [(str(mp3), "Real Track", plan.url, "DnB")]
    assert remembered == [str(mp3)]

    rows = db.get_all_downloads()
    assert len(rows) == 1
    row = rows[0]
    assert row["video_id"] == "vid9"
    assert row["title"] == "Real Track"
    assert row["file_path"] == str(mp3)
    assert row["genre"] == "DnB"
    assert row["bitrate"] == "160 kbps src → 192 kbps MP3"
    assert row["upload_date"] == "20240102"
    assert row["thumbnail_url"] == "https://img/9.jpg"
    assert row["channel_name"] == "UKF"
    assert row["artwork_path"] == str(art)


def test_success_without_source_bitrate_reports_target_only(tmp_path):
    runner = FakeRunner({"title": "T"})
    dl = _downloader(runner)
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.bitrate_text == "→ 192k"
    assert outcome.quality_text == "192 kbps MP3"


# ── the age-gate ladder: one builder, one difference ─────────────────────────
def _age_gate_run(tmp_path, sink=None, **over):
    """Fail the authenticated rung with an age error, succeed unauthenticated.
    Returns (outcome, first_opts, second_opts)."""
    runner = FakeRunner(AGE_ERROR, {"title": "T"})
    kwargs = dict(cookies=_cookies(), ffmpeg_dir=str(tmp_path / "ffmpeg"),
                  policy=_policy(bitrate_auto_upgrade=True),
                  probe_formats=lambda url: [{"vcodec": "none", "abr": 256}])
    kwargs.update(over)
    dl = _downloader(runner, **kwargs)
    plan = _plan(tmp_path, sleep_range=(1, 5), session_ua="UA/1",
                 cover_art=True)
    outcome = dl.run(plan, sink or NullSink())
    assert len(runner.calls) == 2
    return outcome, runner.calls[0][0], runner.calls[1][0]


def test_age_retry_opts_differ_by_exactly_auth_and_derived_bitrate(tmp_path):
    outcome, first, second = _age_gate_run(tmp_path)
    assert outcome.kind == "downloaded"

    # The two differences that are allowed to exist.
    assert first.pop("cookiesfrombrowser") == ("firefox",)
    assert "cookiesfrombrowser" not in second
    assert first.pop("postprocessors") == [{
        "key": "FFmpegExtractAudio", "preferredcodec": "mp3",
        "preferredquality": "256"}]
    assert second.pop("postprocessors") == [{
        "key": "FFmpegExtractAudio", "preferredcodec": "mp3",
        "preferredquality": "192"}]

    # Nothing else may drift. This is the assertion the phase exists for.
    assert first == second


def test_age_retry_reports_the_bitrate_it_actually_encoded(tmp_path):
    outcome, _, _ = _age_gate_run(tmp_path)
    assert outcome.quality_text == "192 kbps MP3"


def test_ffmpeg_location_is_present_on_both_rungs(tmp_path):
    _, first, second = _age_gate_run(tmp_path)
    expected = str(tmp_path / "ffmpeg")
    assert first["ffmpeg_location"] == expected
    assert second["ffmpeg_location"] == expected


def test_no_ffmpeg_location_key_when_running_from_source(tmp_path):
    _, first, second = _age_gate_run(tmp_path, ffmpeg_dir=None)
    assert "ffmpeg_location" not in first
    assert "ffmpeg_location" not in second


def test_keep_original_format_is_honoured_on_both_rungs(tmp_path):
    _, first, second = _age_gate_run(
        tmp_path, policy=_policy(no_conversion=True))
    assert "postprocessors" not in first
    assert "postprocessors" not in second
    # With no conversion the bitrate is not expressed at all, so the rungs
    # differ by the auth key alone.
    assert first.pop("cookiesfrombrowser") == ("firefox",)
    assert first == second


def test_sleep_throttle_is_applied_on_both_rungs(tmp_path):
    _, first, second = _age_gate_run(tmp_path)
    for opts in (first, second):
        assert opts["sleep_interval"] == 1
        assert opts["max_sleep_interval"] == 5


def test_cover_art_and_user_agent_and_geo_are_on_both_rungs(tmp_path):
    _, first, second = _age_gate_run(tmp_path)
    for opts in (first, second):
        assert opts["writethumbnail"] is True
        assert opts["http_headers"] == {"User-Agent": "UA/1"}
        assert opts["geo_bypass"] is True


def test_js_runtime_is_on_both_rungs_from_the_shared_helper(tmp_path):
    _, first, second = _age_gate_run(tmp_path)
    for opts in (first, second):
        assert opts["js_runtimes"] == {"node": {"path": None}}
        assert opts["remote_components"] == ["ejs:github"]


def test_unauthenticated_first_rung_never_climbs_the_age_rung(tmp_path):
    runner = FakeRunner(AGE_ERROR)
    dl = _downloader(runner, cookies=_cookies(use_cookies=False))
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert len(runner.calls) == 1
    assert outcome.kind == "failed"


def test_non_age_failure_never_climbs_the_age_rung(tmp_path):
    runner = FakeRunner(RuntimeError("Video unavailable"))
    dl = _downloader(runner, cookies=_cookies())
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert len(runner.calls) == 1
    assert outcome == Outcome(kind="failed", reason="unavailable",
                              title="Track 1")


def test_bitrate_probe_only_runs_authenticated(tmp_path):
    probed = []
    runner = FakeRunner({"title": "T"})
    dl = _downloader(runner, cookies=_cookies(use_cookies=False),
                     policy=_policy(bitrate_auto_upgrade=True),
                     probe_formats=lambda url: probed.append(url) or [])
    dl.run(_plan(tmp_path), NullSink())
    assert probed == []
    assert runner.opts["postprocessors"][0]["preferredquality"] == "192"


def test_cookie_settings_that_produce_no_options_are_not_authentication(
        tmp_path):
    """"Cookie File" with a path that is not on disk yields {} — so cookies are
    switched on and yet the attempt is anonymous.

    Treating use_cookies alone as authentication spent a probe round-trip that
    could never see an authenticated ladder, and climbed an age rung whose
    options were byte-identical to the one that just failed."""
    probed = []
    runner = FakeRunner(AGE_ERROR)
    cookies = CookieConfig(use_cookies=True, cookie_method="Cookie File",
                           cookies_browser="Firefox", cookies_profile="",
                           cookie_file=str(tmp_path / "not-here.txt"))
    dl = _downloader(runner, cookies=cookies,
                     policy=_policy(bitrate_auto_upgrade=True),
                     probe_formats=lambda url: probed.append(url) or [
                         {"vcodec": "none", "abr": 256}])
    outcome = dl.run(_plan(tmp_path), NullSink())

    assert "cookiefile" not in runner.opts
    assert "cookiesfrombrowser" not in runner.opts
    assert probed == []
    assert len(runner.calls) == 1
    assert outcome == Outcome(kind="failed", reason="age-restricted",
                              title="Track 1")


def test_bitrate_probe_is_opt_in(tmp_path):
    """With bitrate_auto_upgrade off (the default), the probe never runs even
    on a fully authenticated session — it is a ~4s network round-trip per
    track that a free-tier account can never benefit from."""
    probed = []
    runner = FakeRunner({"title": "T"})
    dl = _downloader(runner, cookies=_cookies(),
                     probe_formats=lambda url: probed.append(url) or [
                         {"vcodec": "none", "abr": 256}])
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "downloaded"
    assert probed == []
    assert runner.opts["postprocessors"][0]["preferredquality"] == "192"


def test_format_selector_never_falls_back_to_muxed_best(tmp_path):
    """No muxed-"best" tier: with no audio-only stream on offer the track must
    fail as "format unavailable" (staying pending for a retry), not silently
    download the entire video to extract the same audio."""
    runner = FakeRunner({"title": "T"})
    _downloader(runner).run(_plan(tmp_path), NullSink())
    assert runner.opts["format"] == "bestaudio[abr>=160]/bestaudio"


def test_bitrate_probe_failure_falls_back_to_configured_bitrate(tmp_path):
    def boom(url):
        raise RuntimeError("probe exploded")

    runner = FakeRunner({"title": "T"})
    dbg = RecordingLog()
    dl = _downloader(runner, cookies=_cookies(),
                     policy=_policy(bitrate_auto_upgrade=True),
                     probe_formats=boom, debug=dbg)
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "downloaded"
    assert runner.opts["postprocessors"][0]["preferredquality"] == "192"
    assert any("BITRATE PROBE FAIL" in m for m in dbg.at("warning"))


# ── transient retry ladder ───────────────────────────────────────────────────
def test_transient_error_retries_then_succeeds_waiting_the_backoff(tmp_path):
    runner = FakeRunner(RuntimeError(TRANSIENT), {"title": "T"})
    canceller = FakeCanceller()
    log = RecordingLog()
    dl = _downloader(runner, canceller=canceller, logger=log)
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "downloaded"
    assert len(runner.calls) == 2
    assert canceller.waits == [2]
    assert any("NET RETRY" in m for m in log.at("info"))


def test_transient_error_gives_up_after_max_attempts(tmp_path):
    runner = FakeRunner(*[RuntimeError(TRANSIENT)] * MAX_ATTEMPTS)
    canceller = FakeCanceller()
    dl = _downloader(runner, canceller=canceller)
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert len(runner.calls) == MAX_ATTEMPTS
    assert canceller.waits == [2, 4]
    assert outcome.kind == "failed"


def test_age_rung_gets_its_own_transient_retry_ladder(tmp_path):
    runner = FakeRunner(AGE_ERROR, RuntimeError(TRANSIENT), {"title": "T"})
    canceller = FakeCanceller()
    dl = _downloader(runner, canceller=canceller, cookies=_cookies())
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "downloaded"
    assert len(runner.calls) == 3
    assert canceller.waits == [2]


def test_cancel_during_backoff_is_an_outcome_not_an_error(tmp_path):
    runner = FakeRunner(RuntimeError(TRANSIENT), RuntimeError(TRANSIENT))
    canceller = FakeCanceller(cancel_on=1)
    log = RecordingLog()
    errors = []
    db = FakeDb()
    dl = _downloader(runner, canceller=canceller, db=db, logger=log,
                     log_error=lambda *a: errors.append(a))
    outcome = dl.run(_plan(tmp_path), NullSink())

    assert outcome == Outcome(kind="cancelled", title="Track 1")
    assert len(runner.calls) == 1
    assert canceller.waits == [2]
    assert errors == []
    assert log.at("error") == []
    assert db.downloads == [] and db.unavailable == []


# ── failure recording ────────────────────────────────────────────────────────
def test_permanent_failure_is_remembered_as_unavailable(tmp_path):
    runner = FakeRunner(RuntimeError("ERROR: HTTP Error 404: Not Found"))
    db = FakeDb()
    dl = _downloader(runner, db=db)
    plan = _plan(tmp_path, channel_url="",
                 suppress_channel_url="https://youtube.com/@ukf/")
    outcome = dl.run(plan, NullSink())
    assert outcome == Outcome(kind="unavailable", reason="Removed",
                              title="Track 1")
    assert db.unavailable == [dict(
        platform="YouTube", video_id="v1",
        channel_url="https://youtube.com/@ukf", title="Track 1",
        reason="Removed")]


def test_deferred_failure_is_remembered_nowhere(tmp_path):
    runner = FakeRunner(RuntimeError("This live event will begin in 3 hours"))
    db = FakeDb()
    errors = []
    dl = _downloader(runner, db=db, log_error=lambda *a: errors.append(a))
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome == Outcome(kind="deferred", reason="Not out yet",
                              title="Track 1")
    assert db.unavailable == []
    assert errors == [("Track 1", "https://youtu.be/v1", "Not out yet")]


def test_failure_logs_the_full_error_once_to_the_activity_log(tmp_path):
    runner = FakeRunner(RuntimeError("\x1b[0;31mERROR:\x1b[0m Video "
                                     "unavailable"))
    log = RecordingLog()
    dl = _downloader(runner, logger=log)
    dl.run(_plan(tmp_path), NullSink())
    assert len(log.at("error")) == 1
    assert log.at("error")[0].startswith("ERROR       | Title: Track 1 | ")


# ── the classification ladder ────────────────────────────────────────────────
@pytest.mark.parametrize("error_text,kind,reason", [
    ("This video premieres in 5 hours", "deferred", "Not out yet"),
    ("Only DRM-protected formats are available", "unavailable",
     "DRM-protected"),
    ("ERROR: HTTP Error 404: Not Found", "unavailable", "Removed"),
    ("Video not available from your location", "unavailable", "Geo-blocked"),
    ("ffmpeg not found; install it", "failed", "FFmpeg missing"),
    ("Sign in to confirm you are not a bot", "failed", "login required"),
    ("Video unavailable", "failed", "unavailable"),
    ("This is a private video", "failed", "private"),
    ("Blocked on copyright grounds", "failed", "copyright claim"),
    ("This video is available to members only", "failed", "members only"),
    ("This video has been removed by the uploader", "failed", "removed"),
    ("The uploader has blocked it in your country", "failed", "blocked"),
    ("Requested format is not available", "failed", "format unavailable"),
])
def test_classification_ladder_branches(error_text, kind, reason):
    failure = classify_download_failure(error_text)
    assert (failure.kind, failure.reason) == (kind, reason)


def test_deferred_beats_permanent():
    # A premiere whose message also carries a 404. Read as "Removed" it would
    # be filed in the permanently-unavailable memory and buried for good.
    failure = classify_download_failure(
        "ERROR: HTTP Error 404: Not Found - this video premieres in 2 days")
    assert (failure.kind, failure.reason) == ("deferred", "Not out yet")


def test_is_age_is_sticky_and_sits_below_ffmpeg_and_sign_in():
    # The retry's own error says nothing about age, but the first failure did.
    assert classify_download_failure(
        "Requested format is not available", is_age=True).reason \
        == "age-restricted"
    # Order is preserved: both of these outrank the sticky age verdict.
    assert classify_download_failure(
        "ffmpeg exited with code 1", is_age=True).reason == "FFmpeg missing"
    assert classify_download_failure(
        "Sign in to confirm your age", is_age=True).reason == "login required"


# The three raw messages that were reaching the queue verbatim, taken from a
# real batch run's activity.log: yt-dlp's format check failing to make a temp
# file, googlevideo refusing the media URL, and a read timeout arriving as a
# bare "ERROR:" glued to yt-dlp's own progress-line wrapper by a carriage
# return. Pinned as they were logged, ANSI already stripped.
SEEN_RAW = [
    (r"[Errno 13] Permission denied: 'C:\WINDOWS\System32\tmpr1yl51yu.tmp'",
     "write blocked"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden",
     "refused (403)"),
    ("ERROR: \r[download] Got error: HTTPSConnectionPool("
     "host='rr4---sn-p5qlsn7d.googlevideo.com', port=443): "
     "Read timed out. (read timeout=20.0)", "timed out"),
]


@pytest.mark.parametrize("error_text,reason", SEEN_RAW)
def test_errors_seen_dumped_raw_into_the_queue_now_have_labels(
        error_text, reason):
    failure = classify_download_failure(error_text)
    assert (failure.kind, failure.reason) == ("failed", reason)


@pytest.mark.parametrize("error_text,kind,reason", [
    ("HTTP Error 429: Too Many Requests", "failed", "rate-limited"),
    ("HTTP Error 503: Service Unavailable", "failed", "server error"),
    ("[Errno 28] No space left on device", "failed", "disk full"),
    ("ConnectionResetError(10054, 'An existing connection was forcibly "
     "closed')", "failed", "network error"),
])
def test_condition_markers_outrank_the_track_level_ones(
        error_text, kind, reason):
    """A 503 carries the word "unavailable" and a reset carries none of the
    track-level markers at all. Read by the older ladder the first became
    "unavailable" — the track blamed for the server's wobble."""
    failure = classify_download_failure(error_text)
    assert (failure.kind, failure.reason) == (kind, reason)


def test_every_reason_the_ladder_can_produce_fits_the_queue_column():
    """The queue row is one fixed-width line in a Text widget that neither
    wraps nor scrolls sideways, so a label wider than the column is off the
    edge of the window and unreadable rather than merely untidy.

    Two hand-written labels predate the column budget and are deliberately left
    alone — they are readable phrases rather than raw error text, "format
    unavailable" is named in the comment on the format selector, and at one and
    four characters over they still land inside the window at any size the app
    opens at. Everything else, the condensed fallback included, fits."""
    grandfathered = {"copyright claim", "format unavailable"}
    texts = ["This video premieres in 5 hours", "Only DRM-protected formats",
             "ERROR: HTTP Error 404: Not Found",
             "not available from your location", "ffmpeg not found",
             "Sign in to confirm", "Video unavailable", "a private video",
             "copyright grounds", "members only", "removed by the uploader",
             "blocked in your country"]
    texts += [t for t, _ in SEEN_RAW]
    texts += ["Kaboom " + "x" * 100, "", "ERROR: ", "\x1b[0;31mERROR:\x1b[0m ",
              "Requested format is not available"]
    for text in texts:
        reason = classify_download_failure(strip_ansi(text)).reason
        if reason in grandfathered:
            continue
        assert len(reason) <= REASON_WIDTH, (text, reason)


def test_condense_error_strips_yt_dlp_decoration():
    # Doubled severity prefix, extractor stamp, CLI advice and the wiki link:
    # all of it true, and none of it sayable in fourteen characters.
    assert condense_error(
        "ERROR: [youtube] TmSjSbCqeJo: Something odd happened. "
        "Use --cookies-from-browser for the authentication. "
        "See  https://github.com/yt-dlp/yt-dlp/wiki") == "Something odd…"


def test_condense_error_on_pure_decoration_says_failed():
    # yt-dlp's read-timeout give-up raises with an empty message, so the whole
    # string is its own "ERROR:" prefix. "" in the column would read as a row
    # that never finished rendering.
    for text in ("", None, "ERROR:", "ERROR: ERROR:   ", "[youtube] abc123:"):
        assert condense_error(text) == "failed"


def test_condense_error_keeps_a_clipped_second_word_over_a_bare_first_one():
    # Breaking on the space after "nsig" leaves a label that says nothing.
    assert condense_error(
        "nsig extraction failed: Some formats may be missing") \
        == "nsig extracti…"
    # But a word ending late enough is worth breaking on.
    assert condense_error("database is locked") == "database is…"


def test_unclassified_error_is_condensed_to_the_column_width():
    text = "Kaboom " + "x" * 100
    failure = classify_download_failure(text)
    assert failure.kind == "failed"
    assert len(failure.reason) <= REASON_WIDTH
    assert failure.reason.startswith("Kaboom")
    assert failure.reason.endswith("…")


def test_sticky_age_survives_a_second_rung_with_a_different_error(tmp_path):
    runner = FakeRunner(AGE_ERROR, RuntimeError("Requested format is not "
                                                "available"))
    dl = _downloader(runner, cookies=_cookies())
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome == Outcome(kind="failed", reason="age-restricted",
                              title="Track 1")


def test_looks_age_restricted_matches_what_youtube_actually_says():
    assert looks_age_restricted("Confirm your age to continue")
    assert looks_age_restricted("Sign in to verify your age")
    assert looks_age_restricted(
        "This video may be inappropriate for some users")
    assert looks_age_restricted("This video is age-restricted")
    assert looks_age_restricted("This is ADULT content")
    assert not looks_age_restricted("Video unavailable")


def test_ordinary_words_containing_age_are_not_an_age_gate():
    """The retired test was a bare `"age" in text` match.

    It fired on every error carrying "webpage", "message" or "package", which
    both mislabelled network trouble as an age restriction in the user's log and
    bought it a second attempt ladder."""
    for innocent in (TRANSIENT_WEBPAGE,
                     "Unable to download webpage: HTTP Error 500",
                     "Got error: message from server",
                     "package not found"):
        assert not looks_age_restricted(innocent)


def test_a_transient_webpage_timeout_never_climbs_the_age_rung(tmp_path):
    """A flaky connection must not buy itself a second attempt ladder.

    Two independent conditions stop it now — the text is no longer read as an
    age gate, and the call site also requires a non-transient failure — so this
    stays pinned end to end rather than trusting either one alone. When only the
    substring test guarded it, every timed-out track paid 6 attempts and 12s of
    backoff instead of 3 and 6s."""
    assert not looks_age_restricted(TRANSIENT_WEBPAGE)

    runner = FakeRunner(*[RuntimeError(TRANSIENT_WEBPAGE)] * MAX_ATTEMPTS)
    canceller = FakeCanceller()
    dl = _downloader(runner, canceller=canceller, cookies=_cookies())
    outcome = dl.run(_plan(tmp_path), NullSink())

    assert len(runner.calls) == MAX_ATTEMPTS
    assert canceller.waits == [2, 4]
    assert all("cookiesfrombrowser" in opts for opts, _ in runner.calls)
    assert outcome.kind == "failed"
    # And it is reported as the network problem it is. The sticky age verdict
    # used to relabel it "age-restricted", sending the user looking for a
    # cookie problem they did not have.
    assert outcome.reason == "timed out"


# ── sink events ──────────────────────────────────────────────────────────────
def test_sink_event_stream_for_a_successful_download(tmp_path):
    sink = RecordingSink()
    runner = FakeRunner(_hook_driver(
        {"title": "Real Title"},
        {"status": "downloading", "total_bytes": 200, "downloaded_bytes": 50,
         "_speed_str": "\x1b[0;32m1.20MiB/s\x1b[0m", "_eta_str": "00:42",
         "_percent_str": " 25.0%",
         "info_dict": {"abr": 160, "title": "Real Title"}},
        {"status": "finished"}))
    dl = _downloader(runner)
    dl.run(_plan(tmp_path), sink)

    assert sink.events == [
        ("started", "Track 1"),
        ("bitrate_detected", 160, "192"),
        ("progress", 25.0, "1.20MiB/s  00:42"),
        ("title_corrected", "Real Title"),
        ("finished",),
    ]


def test_progress_prefers_bytes_and_falls_back_to_the_percent_string(tmp_path):
    sink = RecordingSink()
    runner = FakeRunner(_hook_driver(
        {"title": "T"},
        {"status": "downloading", "total_bytes": 200, "downloaded_bytes": 100,
         "_percent_str": " 99.9%"},
        {"status": "downloading", "downloaded_bytes": 100,
         "_percent_str": " 42.5%"},
        {"status": "downloading", "downloaded_bytes": 100,
         "_percent_str": " N/A%"}))
    _downloader(runner).run(_plan(tmp_path), sink)
    progress = [e for e in sink.events if e[0] == "progress"]
    assert progress == [("progress", 50.0, ""), ("progress", 42.5, "")]


def test_bitrate_detected_fires_once_for_the_first_source_bitrate(tmp_path):
    sink = RecordingSink()
    downloading = {"status": "downloading", "total_bytes": 4,
                   "downloaded_bytes": 1, "info_dict": {"tbr": 128}}
    runner = FakeRunner(_hook_driver({"title": "T"}, downloading,
                                     dict(downloading, downloaded_bytes=2)))
    _downloader(runner).run(_plan(tmp_path), sink)
    assert sink.kinds().count("bitrate_detected") == 1
    assert ("bitrate_detected", 128, "192") in sink.events


def test_bitrate_detected_fires_again_on_the_second_rung(tmp_path):
    """The source bitrate is per rung, not per track.

    The authenticated rung reported 256k and then age-failed; the label, the
    queue row and the downloads row's bitrate column all used to attribute that
    to the unauthenticated rung's actual 128k download."""
    def abr_then_age_fail(opts, url):
        for hook in opts["progress_hooks"]:
            hook({"status": "downloading", "total_bytes": 4,
                  "downloaded_bytes": 1, "info_dict": {"abr": 256}})
        raise AGE_ERROR

    sink = RecordingSink()
    db = FakeDb()
    runner = FakeRunner(abr_then_age_fail, _hook_driver(
        {"title": "T"},
        {"status": "downloading", "total_bytes": 4, "downloaded_bytes": 1,
         "info_dict": {"abr": 128}}))
    dl = _downloader(
        runner, db=db, cookies=_cookies(),
        policy=_policy(bitrate_auto_upgrade=True),
        probe_formats=lambda url: [{"vcodec": "none", "abr": 256}])
    outcome = dl.run(_plan(tmp_path), sink)

    assert outcome.kind == "downloaded"
    assert [e for e in sink.events if e[0] == "bitrate_detected"] == [
        ("bitrate_detected", 256, "256"),
        ("bitrate_detected", 128, "192"),
    ]
    # Everything recorded describes the rung that actually downloaded.
    assert outcome.quality_text == "128 kbps src → 192 kbps MP3"
    assert outcome.bitrate_text == "128k → 192k"
    assert db.downloads[0]["bitrate"] == "128 kbps src → 192 kbps MP3"


def test_title_corrected_replaces_the_queue_write_after_the_download(tmp_path):
    sink = RecordingSink()
    runner = FakeRunner({"title": "The Real Name"})
    outcome = _downloader(runner).run(_plan(tmp_path), sink)
    assert ("title_corrected", "The Real Name") in sink.events
    assert outcome.title == "The Real Name"


def test_title_corrected_does_not_repeat_a_title_the_hook_reported(tmp_path):
    sink = RecordingSink()
    runner = FakeRunner(_hook_driver(
        {"title": "Real Title"},
        {"status": "downloading", "total_bytes": 2, "downloaded_bytes": 1,
         "info_dict": {"title": "Real Title"}}))
    _downloader(runner).run(_plan(tmp_path), sink)
    assert sink.kinds().count("title_corrected") == 1


def test_unchanged_title_reports_no_correction(tmp_path):
    sink = RecordingSink()
    runner = FakeRunner({"title": "Track 1"})
    _downloader(runner).run(_plan(tmp_path), sink)
    assert "title_corrected" not in sink.kinds()


def test_run_without_a_sink_is_silent_not_fatal(tmp_path):
    runner = FakeRunner(_hook_driver(
        {"title": "T"},
        {"status": "downloading", "total_bytes": 2, "downloaded_bytes": 1},
        {"status": "finished"}))
    assert _downloader(runner).run(_plan(tmp_path)).kind == "downloaded"


# ── "Never raises" is a guarantee the batch driver leans on ──────────────────
def test_a_raise_from_the_db_row_is_one_failed_track_not_a_dead_batch(tmp_path):
    """A raise out of run() returns no counts at all, and the batch driver
    reads that as a fatal error and breaks out of every remaining URL. So
    everything after the download — the DB row included — has to end as an
    Outcome."""
    class ExplodingDb(FakeDb):
        def add_download(self, **kwargs):
            raise RuntimeError("database is locked")

    dbg = RecordingLog()
    runner = FakeRunner({"title": "T"})
    outcome = _downloader(runner, db=ExplodingDb(), debug=dbg).run(
        _plan(tmp_path), NullSink())

    assert outcome.kind == "failed"
    # Condensed for the queue column, but still recognisably about the database
    # — and the untouched text is in the debug log below.
    assert outcome.reason.startswith("database")
    assert len(outcome.reason) <= REASON_WIDTH
    assert any("database is locked" in m for m in dbg.at("error"))
    assert any("DOWNLOAD ABORT" in m for m in dbg.at("error"))


def test_a_raise_from_the_activity_log_is_also_only_one_failed_track(tmp_path):
    def boom(*a, **kw):
        raise OSError("activity.log is read-only")

    runner = FakeRunner({"title": "T"})
    outcome = _downloader(runner, log_download=boom).run(
        _plan(tmp_path), NullSink())
    assert outcome == Outcome(kind="failed", reason="activity.log…")


# ── the pure pieces ─────────────────────────────────────────────────────────
def test_rung_kbps_states_the_bitrate_rule_once(tmp_path):
    plan = _plan(tmp_path, target_kbps="192")
    assert rung_kbps(plan, True, "256") == "256"
    assert rung_kbps(plan, False, "256") == "192"
    assert rung_kbps(plan, True, None) == "192"


def test_one_builder_yields_two_dicts_differing_only_by_auth(tmp_path):
    plan = _plan(tmp_path, target_kbps="320")
    build = download_opts_builder(plan, cookies=_cookies(),
                                  no_conversion=True)
    authed, anon = build(True), build(False)
    assert authed.pop("cookiesfrombrowser") == ("firefox",)
    assert authed == anon


def test_strip_ansi_and_parse_percent():
    assert strip_ansi("\x1b[0;32mgo\x1b[0m") == "go"
    assert parse_percent(" \x1b[0m12.5%") == 12.5
    assert parse_percent(" N/A%") is None
    assert parse_percent("") is None


def test_expected_path_is_used_when_the_info_dict_names_no_file(tmp_path):
    save_dir = tmp_path / "crate"
    save_dir.mkdir()
    runner = FakeRunner({"title": "Track 1"})
    db = FakeDb()
    dl = _downloader(runner, db=db)
    plan = _plan(tmp_path, save_dir=str(save_dir),
                 expected_path=str(save_dir / "Track 1.mp3"))
    outcome = dl.run(plan, NullSink())
    assert outcome.path == str(save_dir / "Track 1.mp3")
    assert db.downloads[0]["file_path"] == str(save_dir / "Track 1.mp3")


def test_real_file_on_disk_wins_over_the_guessed_name(tmp_path):
    save_dir = tmp_path / "crate"
    save_dir.mkdir()
    real = save_dir / CrateLayout.track_file_name("Real Track")
    real.write_bytes(b"audio")
    runner = FakeRunner({"title": "Real Track"})
    dl = _downloader(runner)
    plan = _plan(tmp_path, save_dir=str(save_dir),
                 expected_path=str(save_dir / "Track 1.mp3"))
    assert dl.run(plan, NullSink()).path == str(real)


# ── immediate abort — Skip/Cancel mid-download ───────────────────────────────
def test_a_tripped_canceller_aborts_the_download_from_inside_the_hook(
        tmp_path):
    """The one interruption point yt-dlp offers: the progress hook raises the
    moment the canceller reads set, and the rung reports cancelled — never an
    error, never a logged failure."""
    canceller = FakeCanceller()
    log = RecordingLog()
    errors = []
    db = FakeDb()

    def runner(opts, url):
        canceller.now = True                     # Skip lands mid-transfer
        opts["progress_hooks"][0]({"status": "downloading",
                                   "downloaded_bytes": 10, "total_bytes": 100})
        raise AssertionError("the hook must abort before this")

    dl = _downloader(runner, canceller=canceller, db=db, logger=log,
                     log_error=lambda *a: errors.append(a))
    outcome = dl.run(_plan(tmp_path), NullSink())

    assert outcome == Outcome(kind="cancelled", title="Track 1")
    assert errors == [] and log.at("error") == []
    assert db.downloads == [] and db.unavailable == []


def test_a_canceller_already_set_never_starts_the_attempt(tmp_path):
    canceller = FakeCanceller()
    canceller.now = True
    calls = []
    dl = _downloader(lambda opts, url: calls.append(url), canceller=canceller)
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "cancelled"
    assert calls == []


def test_an_error_landing_after_cancel_is_cancelled_not_an_error(tmp_path):
    """A connection torn down BY the abort raises whatever it raises; with the
    canceller set, that noise must not be recorded as a track failure."""
    canceller = FakeCanceller()
    errors = []

    def runner(opts, url):
        canceller.now = True
        raise RuntimeError("connection reset by peer")

    dl = _downloader(runner, canceller=canceller,
                     log_error=lambda *a: errors.append(a))
    outcome = dl.run(_plan(tmp_path), NullSink())
    assert outcome.kind == "cancelled"
    assert errors == []


# ── SkipOrCancel — the composite Canceller ───────────────────────────────────
def test_skip_or_cancel_is_set_by_either_source():
    import threading
    from cratebuilder.download import SkipOrCancel
    event = threading.Event()
    flag = {"skip": False}
    c = SkipOrCancel(event, lambda: flag["skip"])
    assert c.is_set() is False
    flag["skip"] = True
    assert c.is_set() is True
    flag["skip"] = False
    event.set()
    assert c.is_set() is True


def test_skip_or_cancel_wait_is_interrupted_by_the_predicate():
    import threading, time as _time
    from cratebuilder.download import SkipOrCancel
    flag = {"skip": False}
    c = SkipOrCancel(threading.Event(), lambda: flag["skip"])
    timer = threading.Timer(0.05, lambda: flag.update(skip=True))
    timer.start()
    t0 = _time.monotonic()
    assert c.wait(5) is True                     # 5 s backoff, cut short
    assert _time.monotonic() - t0 < 2
    timer.cancel()


def test_skip_or_cancel_wait_times_out_quietly_when_nothing_cancels():
    import threading
    from cratebuilder.download import SkipOrCancel
    c = SkipOrCancel(threading.Event(), lambda: False)
    assert c.wait(0.05) is False
    assert c.wait(None) is False                 # no timeout = a plain poll

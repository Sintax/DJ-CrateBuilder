"""Crate path building + ownership, as the app reaches them.

Written as characterization tests BEFORE the CrateLayout / ChannelCrate
extraction and kept as the regression net after it. Two behaviours changed on
purpose and say so at the assertion: a whitespace-only genre now normalises to
the no-genre folder on both path builders, and the 40-char prefix tier that
used to claim an original as owned because a remix of it existed is retired.

No network, no real config file, no real library — every path lives under a
tmp_path via the app fixtures. Read-only tests ride the module-shared
`shared_app`; anything that mutates app state, the DB, or the crate folders
gets its own `app`.
"""
import os

import pytest

import yt_dlp
from cratebuilder import crate
from cratebuilder.util import normalize_track_key, safe_filename

try:
    from yt_dlp.utils import sanitize_filename as ytdl_sanitize
except ImportError:                                   # pragma: no cover
    ytdl_sanitize = None


def _plat(app, platform="YouTube"):
    return app._platform_dir(platform)


# ══════════════════════════════════════════════════════════════════════════════
# MP3DownloaderApp._channel_save_path — base/Platform/[Genre|_No Genre]/[Channel]
# ══════════════════════════════════════════════════════════════════════════════
# ── genre component ───────────────────────────────────────────────────────────
def test_save_path_real_genre(shared_app):
    app = shared_app
    assert app._channel_save_path("Drum & Bass", platform="YouTube") == \
        os.path.join(_plat(app), "Drum & Bass")


@pytest.mark.parametrize("genre", [None, "", "(none)"])
def test_save_path_no_genre_sentinels_all_become_no_genre_dir(shared_app,
                                                              genre):
    # The three in-app "no genre" spellings collapse to the one on-disk name.
    assert shared_app._channel_save_path(genre, platform="YouTube") == \
        os.path.join(_plat(shared_app), "_No Genre")


def test_save_path_whitespace_genre_becomes_no_genre_dir(shared_app):
    app = shared_app
    # THE ONE INTENDED DIVERGENCE (inventory DECISION 3). Before the
    # CrateLayout extraction the app builder treated "  " as a real genre and
    # built a folder component made of spaces — which Win32 then trimmed to
    # nothing, so _resolve_save_dir failed outright and the whole URL reported a
    # fatal error (see test_resolve_save_dir_normalises_a_whitespace_genre).
    # Now both builders agree with the viewer's older, stricter reading: a
    # whitespace-only genre is no genre.
    assert app._channel_save_path("  ", platform="YouTube") == \
        os.path.join(_plat(app), "_No Genre")
    assert app._channel_save_path("  ", "Some Channel", platform="YouTube") == \
        os.path.join(_plat(app), "_No Genre", "Some Channel")

def test_save_path_keeps_a_padded_real_genre_verbatim(shared_app):
    app = shared_app
    # A padded genre names a real folder: _scan_genres offers whatever
    # os.listdir returned, and that value is what gets stored in the DB and the
    # sidecars. Trimming it here would send the download to a NEW sibling
    # folder and leave the app unable to see the existing library. Only the
    # whitespace-ONLY case normalises (above).
    assert app._channel_save_path("  DnB  ", platform="YouTube") == \
        os.path.join(_plat(app), "  DnB  ")


# ── channel component ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("channel", [None, ""])
def test_save_path_empty_channel_stops_at_the_genre(shared_app, channel):
    assert shared_app._channel_save_path("DnB", channel, platform="YouTube") \
        == os.path.join(_plat(shared_app), "DnB")


def test_save_path_channel_is_sanitised_and_stripped(shared_app):
    app = shared_app
    raw = '  UKF: Drum\\Bass / Mix*Q? "x" <y> |z|  '
    got = app._channel_save_path("DnB", raw, platform="YouTube")
    assert got == os.path.join(_plat(app), "DnB",
                               'UKF_ Drum_Bass _ Mix_Q_ _x_ _y_ _z_')
    # ...which is exactly safe_filename(name, strip=True) — not yt-dlp's
    # sanitiser. Folder names use the app's own regex form.
    assert os.path.basename(got) == safe_filename(raw, strip=True)


def test_save_path_channel_that_sanitises_to_empty_is_dropped(shared_app):
    app = shared_app
    # Whitespace-only survives the `if channel_name:` truth test but
    # safe_filename(strip=True) empties it, so no channel component is added.
    assert safe_filename("   ", strip=True) == ""
    assert app._channel_save_path("DnB", "   ", platform="YouTube") == \
        os.path.join(_plat(app), "DnB")


def test_save_path_channel_of_only_illegal_chars_is_kept_as_underscores(
        shared_app):
    app = shared_app
    # safe_filename maps each illegal char to "_", so the result is truthy and
    # a folder named "___" IS created. Not dropped.
    assert app._channel_save_path("DnB", "?:|", platform="YouTube") == \
        os.path.join(_plat(app), "DnB", "___")


# ── platform component ────────────────────────────────────────────────────────
def test_save_path_explicit_platform_selects_the_subdir(shared_app):
    app = shared_app
    assert app._channel_save_path("DnB", "Chan", platform="SoundCloud") == \
        os.path.join(app._base_dir, "SoundCloud", "DnB", "Chan")
    assert app._channel_save_path("DnB", "Chan", platform="YouTube") == \
        os.path.join(app._base_dir, "YouTube", "DnB", "Chan")


def test_save_path_platform_none_falls_back_to_the_main_tab_variable(app):
    assert app._platform_var.get() == "YouTube"
    assert app._channel_save_path("DnB", "Chan") == \
        os.path.join(app._base_dir, "YouTube", "DnB", "Chan")
    app._platform_var.set("SoundCloud")
    assert app._channel_save_path("DnB", "Chan") == \
        os.path.join(app._base_dir, "SoundCloud", "DnB", "Chan")


def test_save_path_unknown_platform_raises(shared_app):
    app = shared_app
    # The app builder has no platform guard: an unrecognised name is a KeyError
    # out of PLATFORMS. The viewer's twin swallows it instead (see below).
    with pytest.raises(KeyError):
        app._channel_save_path("DnB", "Chan", platform="Bandcamp")


# ── purity / the makedirs wrapper ─────────────────────────────────────────────
def test_resolve_save_dir_normalises_a_whitespace_genre(app):
    # The payoff of the divergence above. This call used to raise
    # FileNotFoundError [WinError 3] — Win32 trimmed the trailing spaces off the
    # "  " component, leaving an empty directory name for makedirs — which
    # inside _process_one_url escaped to the outer handler and failed the whole
    # URL. It now lands in the no-genre bucket and creates cleanly.
    made = app._resolve_save_dir("  ", "Chan", platform="YouTube")
    assert made == os.path.join(_plat(app), "_No Genre", "Chan")
    assert os.path.isdir(made)


def test_save_path_is_pure_and_resolve_save_dir_creates(app, tmp_path):
    path = app._channel_save_path("DnB", "Chan", platform="YouTube")
    assert not os.path.exists(path)
    made = app._resolve_save_dir("DnB", "Chan", platform="YouTube")
    assert made == path and os.path.isdir(made)
    # Everything stayed inside the test sandbox.
    assert str(tmp_path) in made


# ══════════════════════════════════════════════════════════════════════════════
# DatabaseViewerWindow._wl_channel_folder — the same shape from a row dict
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def viewer(cb_mod, shared_app, tmp_path_factory):
    """A real viewer over an empty throwaway DB, parented to the isolated app —
    how production reaches _wl_channel_folder (App._open_db_viewer).

    Module-scoped over shared_app: every test below only READS through
    _wl_channel_folder, so one viewer serves the file."""
    db_dir = tmp_path_factory.mktemp("viewer_db")
    db = cb_mod.DownloadsDatabase(str(db_dir / "viewer.db"))
    v = cb_mod.DatabaseViewerWindow(shared_app, db)
    v.update()
    yield v
    try:
        v.destroy()
    except Exception:
        pass


def _row(**kw):
    row = {"platform": "YouTube", "genre": "DnB", "display_name": "Chan"}
    row.update(kw)
    return row


def test_wl_folder_matches_the_app_builder_for_the_normal_case(viewer,
                                                               shared_app):
    assert viewer._wl_channel_folder(_row()) == \
        shared_app._channel_save_path("DnB", "Chan", platform="YouTube")


@pytest.mark.parametrize("genre", [None, "", "(none)"])
def test_wl_folder_no_genre_sentinels_all_become_no_genre_dir(viewer,
                                                              shared_app,
                                                              genre):
    assert viewer._wl_channel_folder(_row(genre=genre)) == \
        os.path.join(_plat(shared_app), "_No Genre", "Chan")


def test_wl_folder_whitespace_genre_becomes_no_genre_dir(viewer, shared_app):
    app = shared_app
    # DIVERGENCE from the app builder: the viewer .strip()s genre first, so
    # "  " becomes "" and lands in "_No Genre". This is the behaviour Phase 4
    # adopts for both builders.
    assert viewer._wl_channel_folder(_row(genre="  ")) == \
        os.path.join(_plat(app), "_No Genre", "Chan")


def test_wl_folder_strips_platform_and_name_but_not_the_genre(viewer,
                                                              shared_app):
    app = shared_app
    # Platform and channel name are still stripped (the platform has to match
    # the PLATFORMS table, and the channel name is sanitised anyway), but the
    # genre is now used verbatim — which is what makes the viewer's Folder
    # column point at the folder the download actually writes to.
    got = viewer._wl_channel_folder(_row(platform="  YouTube  ",
                                         genre="  DnB  ",
                                         display_name="  Chan  "))
    assert got == os.path.join(_plat(app), "  DnB  ", "Chan")
    assert got == app._channel_save_path("  DnB  ", "  Chan  ",
                                         platform="YouTube")


@pytest.mark.parametrize("name", [None, "", "   "])
def test_wl_folder_empty_channel_stops_at_the_genre(viewer, shared_app, name):
    assert viewer._wl_channel_folder(_row(display_name=name)) == \
        os.path.join(_plat(shared_app), "DnB")


def test_wl_folder_sanitises_the_channel_name(viewer, shared_app):
    got = viewer._wl_channel_folder(_row(display_name='A: B / C?'))
    assert got == os.path.join(_plat(shared_app), "DnB", "A_ B _ C_")


def test_wl_folder_soundcloud_row(viewer, shared_app):
    assert viewer._wl_channel_folder(_row(platform="SoundCloud")) == \
        os.path.join(shared_app._base_dir, "SoundCloud", "DnB", "Chan")


@pytest.mark.parametrize("platform", [None, "", "   ", "Bandcamp", "youtube"])
def test_wl_folder_unknown_or_blank_platform_returns_empty(viewer, platform):
    # DIVERGENCE: the viewer guards `platform not in PLATFORMS` and returns "".
    # Note the guard is case-sensitive — "youtube" is NOT recognised. The app
    # builder raises KeyError for all of these instead.
    assert viewer._wl_channel_folder(_row(platform=platform)) == ""


def test_wl_folder_swallows_any_failure_and_returns_empty(viewer, monkeypatch):
    # DIVERGENCE: wrapped in `except Exception: return ""`, so an unreachable
    # parent app degrades to a blank Folder column rather than raising into the
    # tree rebuild.
    monkeypatch.setattr(viewer, "_parent", None)
    assert viewer._wl_channel_folder(_row()) == ""


def test_wl_folder_is_pure(viewer):
    path = viewer._wl_channel_folder(_row(display_name="Never Created"))
    assert path and not os.path.exists(path)


# ══════════════════════════════════════════════════════════════════════════════
# CrateLayout.find_existing — two EXACT tiers, .mp3 only
#
# This is what _file_exists_on_disk became. It answers only "which file did the
# download just write?" (post-success path resolution, for the tag and artwork
# pass); deciding OWNERSHIP is ChannelCrate's job now, so the third tier — the
# 40-char case-insensitive prefix scan — is retired. The tests below that used
# to pin tier 3 are kept, inverted, so the retirement is asserted rather than
# merely absent.
# ══════════════════════════════════════════════════════════════════════════════
def _mp3(directory, name):
    p = directory / f"{name}.mp3"
    p.write_bytes(b"\x00")
    return str(p)


def _owns_title(directory, title):
    """The path a ChannelCrate over *directory* claims for *title*, or ""."""
    return crate.ChannelCrate(
        str(directory), is_downloaded=lambda _vid: False).track_path(title)


def test_file_exists_tier1_prefers_the_ytdlp_sanitised_name(tmp_path):
    if ytdl_sanitize is None:                          # pragma: no cover
        pytest.skip("yt_dlp.utils.sanitize_filename unavailable")
    d = tmp_path / "chan"
    d.mkdir()
    title = "DnB Mix?"
    ytdl_name = ytdl_sanitize(title, restricted=False)     # 'DnB Mix？'
    legacy_name = safe_filename(title, strip=True)         # 'DnB Mix_'
    assert ytdl_name != legacy_name
    ytdl_path = _mp3(d, ytdl_name)
    _mp3(d, legacy_name)
    # Both candidates exist; tier 1 wins.
    assert crate.CrateLayout.find_existing(str(d), title) == ytdl_path


def test_file_exists_tier2_finds_the_legacy_regex_name(tmp_path):
    d = tmp_path / "chan"
    d.mkdir()
    title = "DnB Mix?"
    legacy_path = _mp3(d, safe_filename(title, strip=True))
    assert crate.CrateLayout.find_existing(str(d), title) == legacy_path


def test_file_exists_tier2_uses_the_stripped_form(tmp_path):
    # yt-dlp keeps surrounding spaces, safe_filename(strip=True) removes them,
    # so a padded title is found only by tier 2.
    d = tmp_path / "chan"
    d.mkdir()
    stripped_path = _mp3(d, "Padded Title")
    assert crate.CrateLayout.find_existing(
        str(d), "  Padded Title  ") == stripped_path


def test_both_exact_tiers_are_reachable_through_the_track_key_index(tmp_path):
    # Why retiring tier 3 does not weaken ownership: the normalized track key
    # reconciles BOTH exact spellings, so a ChannelCrate claims either vintage
    # of file. The tiers only survive for path resolution, where the actual
    # name on disk matters.
    d = tmp_path / "ytdl-vintage"
    d.mkdir()
    ytdl = _mp3(d, ytdl_sanitize("DnB Mix?", restricted=False)
                if ytdl_sanitize else safe_filename("DnB Mix?"))
    assert _owns_title(d, "DnB Mix?") == ytdl

    e = tmp_path / "legacy-vintage"
    e.mkdir()
    legacy = _mp3(e, safe_filename("  Padded Title  ", strip=True))
    assert _owns_title(e, "  Padded Title  ") == legacy


def test_retired_tier3_no_longer_claims_a_truncated_variant(tmp_path):
    # WAS: the prefix scan returned this file for a query that diverged after
    # char 40 (flat vs full extraction disagreeing about a title's tail). Both
    # halves of that trade are now gone — the near-miss is not found, and
    # neither is the remix false-match below. Exactness is the whole point.
    d = tmp_path / "chan"
    d.mkdir()
    _mp3(d, "Aurora Skyline Sessions Vol 3 Extended Journey [Official Audio]")
    query = "Aurora Skyline Sessions Vol 3 Extended Journey"
    assert crate.CrateLayout.find_existing(str(d), query) is None
    assert _owns_title(d, query) == ""


def test_retired_tier3_no_longer_false_matches_a_remix(tmp_path):
    # THE BUG THE RETIREMENT FIXES. The 40-char prefix scan used to return the
    # Cutline Remix when asked about the ORIGINAL, because both share the first
    # 40 characters — so on the main tab with skip-existing on, the original was
    # silently never downloaded. Now the original is unowned and the remix is
    # owned, by exact normalized key.
    d = tmp_path / "chan"
    d.mkdir()
    remix = _mp3(d, "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade "
                    "(Cutline Remix)")
    original = "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade"
    assert len(original) > 40
    assert crate.CrateLayout.find_existing(str(d), original) is None
    assert normalize_track_key(original) != \
        normalize_track_key(os.path.basename(remix))
    assert _owns_title(d, original) == ""
    assert _owns_title(d, os.path.splitext(os.path.basename(remix))[0]) == remix


def test_lookup_is_no_longer_case_insensitive_across_different_titles(tmp_path):
    # The prefix scan lowercased both sides, so a SHOUTED longer title matched a
    # lowercase query. Exact keys are case-insensitive by normalisation, but
    # only for the SAME title — a different title no longer matches at all.
    d = tmp_path / "chan"
    d.mkdir()
    _mp3(d, "AURORA SKYLINE SESSIONS VOL 3 EXTENDED JOURNEY LIVE")
    query = "aurora skyline sessions vol 3 extended journey"
    assert crate.CrateLayout.find_existing(str(d), query) is None
    assert _owns_title(d, query) == ""
    # Same title, different case, still owned.
    assert _owns_title(
        d, "Aurora Skyline Sessions Vol 3 Extended Journey Live").endswith(
        "LIVE.mp3")


def test_file_exists_ignores_non_mp3_audio(tmp_path):
    # "Keep original format" writes .webm/.m4a — invisible to both tiers and to
    # the ownership index, exactly as before.
    d = tmp_path / "chan"
    d.mkdir()
    (d / "Some Track.webm").write_bytes(b"\x00")
    (d / "Some Track.m4a").write_bytes(b"\x00")
    assert crate.CrateLayout.find_existing(str(d), "Some Track") is None
    assert _owns_title(d, "Some Track") == ""


def test_file_exists_returns_none_when_nothing_matches(tmp_path):
    d = tmp_path / "chan"
    d.mkdir()
    _mp3(d, "Completely Different Track")
    assert crate.CrateLayout.find_existing(str(d), "Some Track") is None


def test_file_exists_missing_directory_is_none_not_an_error(tmp_path):
    assert crate.CrateLayout.find_existing(
        str(tmp_path / "nope"), "Some Track") is None
    assert _owns_title(tmp_path / "nope", "Some Track") == ""


def test_retired_tier3_no_longer_claims_a_short_title_prefix(tmp_path):
    # The worst case of the retired tier: for titles under 40 chars the
    # "prefix" was the whole sanitised title, so ANY longer file starting with
    # it was claimed as owned — "Cascade" was owned because a VIP edit existed.
    d = tmp_path / "chan"
    d.mkdir()
    longer = _mp3(d, "Cascade (VIP Edit)")
    assert crate.CrateLayout.find_existing(str(d), "Cascade") is None
    assert _owns_title(d, "Cascade") == ""
    assert _owns_title(d, "Cascade (VIP Edit)") == longer


# ══════════════════════════════════════════════════════════════════════════════
# The ownership asymmetry — scan buckets it, the download path attempts it
# ══════════════════════════════════════════════════════════════════════════════
class _FailingYdl:
    """yt_dlp.YoutubeDL stand-in: records the attempt, then fails with a
    message that is neither transient, deferred, nor permanently unavailable,
    so the entry lands in the plain error tally."""

    def __init__(self, attempts, opts):
        self._attempts = attempts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        self._attempts.append(url)
        raise RuntimeError("simulated download attempt")


class _WritingYdl:
    """yt_dlp.YoutubeDL stand-in that actually writes the .mp3 its outtmpl
    names, so a batch can be observed noticing its own output. *titles* maps a
    URL to the title yt-dlp reports (and therefore the file name it writes)."""

    def __init__(self, attempts, titles, opts):
        self._attempts = attempts
        self._titles = titles
        self._outtmpl = opts.get("outtmpl") or ""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        self._attempts.append(url)
        title = self._titles[url]
        path = self._outtmpl.replace("%(title)s", title).replace(
            "%(ext)s", "mp3")
        with open(path, "wb") as fh:
            fh.write(b"\x00")
        return {"title": title, "id": url.rsplit("=", 1)[-1],
                "requested_downloads": [{"filepath": path}]}


class _StubSession:
    """YdlSession stand-in answering the one intent _process_one_url uses."""

    def __init__(self, info):
        self._info = info

    def probe_metadata(self, url):
        return self._info


def test_scan_withholds_upcoming_and_suppressed_but_download_attempts_them(
        cb_mod, app, tmp_path, monkeypatch):
    """The scan and the Watch List download disagree about two buckets.

    "Mirror the scan EXACTLY" (the comment in _process_one_url) covers only the
    DB-id and exact-folder-key tests. Suppression and upcoming have no
    download-side pre-flight, so the same entry the scan withheld is downloaded
    anyway — caught, if at all, by the post-failure backstop. Pinned as
    behaviour: Phase 4's Ownership/skip_decision must keep it.
    """
    db = app._db
    owned = {"id": "vowned", "title": "Owned Track",
             "url": "https://yt/watch?v=vowned"}
    soon = {"id": "vsoon", "title": "Premiere Track",
            "url": "https://yt/watch?v=vsoon", "live_status": "is_upcoming"}
    gone = {"id": "vgone", "title": "Gone Track",
            "url": "https://yt/watch?v=vgone"}
    entries = [owned, soon, gone]

    db.add_download(video_id="vowned", title="Owned Track",
                    channel_name="Chan", channel_url="https://yt/c",
                    platform="YouTube", genre="DnB",
                    file_path=str(tmp_path / "gone-from-disk.mp3"),
                    upload_date="", bitrate="")
    # "Removed" suppresses only from the second recorded failure onward.
    for _ in range(2):
        db.record_unavailable(platform="YouTube", video_id="vgone",
                              channel_url="https://yt/c", title="Gone Track",
                              reason="Removed")
    assert db.get_suppressed_reasons("YouTube") == {"vgone": "Removed"}

    # ── the scan's verdict ────────────────────────────────────────────────
    out = crate.classify_scan_entries(
        entries, is_downloaded=db.is_video_downloaded, folder_keys={},
        limit_sec=None, platform="YouTube",
        is_unavailable=db.get_suppressed_reasons("YouTube").get)
    assert [e["id"] for e in out["upcoming"]] == ["vsoon"]
    assert [e["id"] for e in out["unavailable"]] == ["vgone"]
    assert out["new"] == [] and out["on_disk"] == []

    # ── the Watch List download's verdict for the very same entries ───────
    attempts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL",
                        lambda opts: _FailingYdl(attempts, opts))
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        {"_type": "playlist", "title": "Chan", "entries": entries}))
    app._wl_download_active = True
    app._grand_dl = app._grand_sk = app._grand_er = 0

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan")

    # Only the DB-owned track was skipped pre-flight.
    assert (downloaded, skipped, errors) == (0, 1, 2)
    assert attempts == ["https://yt/watch?v=vsoon", "https://yt/watch?v=vgone"]
    # Neither pre-flight oracle was consulted: the suppression memory is
    # unchanged (the simulated failure is not a permanent cause) and the
    # premiere was attempted rather than deferred.
    assert db.get_suppressed_reasons("YouTube") == {"vgone": "Removed"}
    assert not db.is_video_downloaded("vsoon")


def test_a_batch_sees_the_file_it_just_wrote(cb_mod, app, monkeypatch):
    """Two entries in one batch whose titles normalise alike: the second must
    be skipped, not downloaded over the first.

    The channel folder is indexed once per batch, so a file written mid-batch
    is invisible unless the crate is told about it. Without that, both entries
    download and the second files a duplicate downloads row against the SAME
    file_path — which Folders Cleanup and Rebuild-DB both key on.
    """
    first = {"id": "vone", "title": "Cascade",
             "url": "https://yt/watch?v=vone"}
    second = {"id": "vtwo", "title": "Cascade",
              "url": "https://yt/watch?v=vtwo"}
    titles = {first["url"]: "Cascade", second["url"]: "Cascade"}

    attempts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL",
                        lambda opts: _WritingYdl(attempts, titles, opts))
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        {"_type": "playlist", "title": "Chan", "entries": [first, second]}))
    app._skip_existing.set(True)
    app._skip_mode.set("In Folder Only")
    app._wl_download_active = False
    app._grand_dl = app._grand_sk = app._grand_er = 0

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan")

    assert (downloaded, skipped, errors) == (1, 1, 0)
    assert attempts == [first["url"]]
    # One row, not two pointing at the same file.
    rows = [r for r in app._db.get_all_downloads()
            if r["video_id"] in ("vone", "vtwo")]
    assert len(rows) == 1


def test_download_side_skips_an_exact_folder_key_match(cb_mod, app,
                                                       monkeypatch):
    """The half of "mirror the scan" that IS implemented: an exact normalized
    key already in the channel folder skips, and a long-prefix sibling of a
    different track does not (no tier-3 fallback on the Watch List path)."""
    save_dir = app._resolve_save_dir("DnB", "Chan", platform="YouTube")
    owned_file = os.path.join(
        save_dir,
        "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade "
        "(Cutline Remix).mp3")
    with open(owned_file, "wb") as fh:
        fh.write(b"\x00")

    remix = {"id": "vremix",
             "title": "Aurora Skyline Sessions Vol 3 Extended Journey - "
                      "Cascade (Cutline Remix)",
             "url": "https://yt/watch?v=vremix"}
    original = {"id": "vorig",
                "title": "Aurora Skyline Sessions Vol 3 Extended Journey - "
                         "Cascade",
                "url": "https://yt/watch?v=vorig"}
    entries = [remix, original]

    attempts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL",
                        lambda opts: _FailingYdl(attempts, opts))
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        {"_type": "playlist", "title": "Chan", "entries": entries}))
    app._wl_download_active = True
    app._grand_dl = app._grand_sk = app._grand_er = 0

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan")

    assert (downloaded, skipped, errors) == (0, 1, 1)
    # The original was attempted — the prefix false-match that
    # _file_exists_on_disk would have produced is deliberately absent here.
    assert attempts == ["https://yt/watch?v=vorig"]


def test_the_main_tab_now_matches_the_scan_exactly_too(cb_mod, app,
                                                       monkeypatch):
    """The user-visible payoff of retiring tier 3.

    Same folder and same listing as the Watch List test above, run through the
    MAIN tab with skip-existing on. This used to skip the original as well,
    because the remix shared its first 40 characters — so a DJ who owned a
    remix could never download the original. Both paths now decide ownership
    the same way, by exact normalized track key."""
    save_dir = app._resolve_save_dir("DnB", "Chan", platform="YouTube")
    with open(os.path.join(
            save_dir,
            "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade "
            "(Cutline Remix).mp3"), "wb") as fh:
        fh.write(b"\x00")

    entries = [
        {"id": "vremix",
         "title": "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade "
                  "(Cutline Remix)",
         "url": "https://yt/watch?v=vremix"},
        {"id": "vorig",
         "title": "Aurora Skyline Sessions Vol 3 Extended Journey - Cascade",
         "url": "https://yt/watch?v=vorig"},
    ]

    attempts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL",
                        lambda opts: _FailingYdl(attempts, opts))
    monkeypatch.setattr(app, "_ydl_session", lambda **kw: _StubSession(
        {"_type": "playlist", "title": "Chan", "entries": entries}))
    app._wl_download_active = False
    app._skip_existing.set(True)
    app._skip_mode.set("In Folder Only")
    app._grand_dl = app._grand_sk = app._grand_er = 0

    downloaded, skipped, errors = app._process_one_url(
        "https://yt/c", "DnB", "YouTube", cb_mod.PLATFORMS["YouTube"],
        channel_name_override="Chan")

    assert (downloaded, skipped, errors) == (0, 1, 1)
    assert attempts == ["https://yt/watch?v=vorig"]


# ══════════════════════════════════════════════════════════════════════════════
# The Watch List scan, through the same ChannelCrate
# ══════════════════════════════════════════════════════════════════════════════
class _ListingSession:
    """YdlSession stand-in answering the one intent a scan uses."""

    def __init__(self, entries):
        self._entries = entries

    def list_channel(self, url):
        return list(self._entries)


def _scan_now(app, monkeypatch, cid, entries):
    """Run one Watch List scan synchronously against a stubbed listing."""
    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))
    monkeypatch.setattr(app, "_ydl_session",
                        lambda **kw: _ListingSession(entries))
    app._watchlist_scan_channel(cid)
    return app._db.get_watchlist_channel(cid)


def _watched(app, **kw):
    row = dict(url="https://www.youtube.com/channel/UCscan/videos",
               display_name="Scan Chan", platform="YouTube", genre="DnB")
    row.update(kw)
    return app._db.add_watchlist_channel(**row)


def test_the_scan_buckets_a_mixed_listing_through_the_crate(app, monkeypatch,
                                                            tmp_path):
    """One scan, all four verdicts, driven for real: the DB-owned track is
    dropped, the on-disk one is backfilled and hidden, the premiere and the
    suppressed track are withheld, and only the fresh one becomes pending."""
    db = app._db
    cid = _watched(app)
    save_dir = app._resolve_save_dir("DnB", "Scan Chan", platform="YouTube")
    on_disk = os.path.join(save_dir, "Legacy Track.mp3")
    with open(on_disk, "wb") as fh:
        fh.write(b"\x00")

    db.add_download(video_id="vowned", title="Owned Track",
                    channel_name="Scan Chan", channel_url="https://yt/c",
                    platform="YouTube", genre="DnB",
                    file_path=str(tmp_path / "owned.mp3"),
                    upload_date="", bitrate="")
    for _ in range(2):
        db.record_unavailable(platform="YouTube", video_id="vgone",
                              channel_url="https://yt/c", title="Gone Track",
                              reason="Removed")

    row = _scan_now(app, monkeypatch, cid, [
        {"id": "vowned", "title": "Owned Track"},
        {"id": "vdisk", "title": "Legacy Track", "upload_date": "20250101"},
        {"id": "vgone", "title": "Gone Track"},
        {"id": "vsoon", "title": "Premiere Track", "live_status": "is_upcoming"},
        {"id": "vnew", "title": "Fresh Track", "upload_date": "20260101"},
    ])

    import json
    pending = json.loads(row["pending_entries_json"])
    assert [e["id"] for e in pending] == ["vnew"]
    assert row["pending_new_count"] == 1
    assert app._wl_upcoming_counts[cid] == 1
    # The on-disk legacy track was backfilled with the path the crate found, so
    # the next scan is exact-by-id.
    assert db.is_video_downloaded("vdisk")
    backfilled = next(r for r in db.get_all_downloads()
                      if r["video_id"] == "vdisk")
    assert backfilled["file_path"] == on_disk


def test_the_scan_still_creates_the_channel_folder(app, monkeypatch):
    """A side effect callers rely on: scanning a channel that has never
    downloaded anything gives it a home on disk."""
    cid = _watched(app, display_name="Never Downloaded")
    expected = app._channel_save_path("DnB", "Never Downloaded",
                                      platform="YouTube")
    assert not os.path.exists(expected)

    _scan_now(app, monkeypatch, cid,
              [{"id": "vnew", "title": "Fresh Track"}])

    assert os.path.isdir(expected)


def test_the_scan_normalises_a_whitespace_genre_like_the_download_does(
        app, monkeypatch):
    """The divergence, end to end: a row whose genre is whitespace used to make
    the scan's _resolve_save_dir raise on Win32 (swallowed as an empty folder
    index) and the download fail outright. Both now land in the no-genre
    bucket, and the scan creates it."""
    cid = _watched(app, genre="  ", display_name="Blank Genre")
    row = _scan_now(app, monkeypatch, cid,
                    [{"id": "vnew", "title": "Fresh Track"}])

    assert row["status"] == "found"
    assert os.path.isdir(os.path.join(_plat(app), "_No Genre", "Blank Genre"))


def test_the_scan_reads_the_downloaded_ids_once_not_once_per_entry(
        app, monkeypatch):
    """What made a Scan All lag. Asking per entry opened a SQLite connection
    per video, all through the one lock the UI thread needs to redraw a card —
    seconds of it on a real channel. The whole answer is one query."""
    cid = _watched(app, display_name="Bulk Read")
    bulk = []
    per_entry = []
    real_bulk = app._db.get_downloaded_video_ids
    monkeypatch.setattr(app._db, "get_downloaded_video_ids",
                        lambda: (bulk.append(1), real_bulk())[1])
    monkeypatch.setattr(app._db, "is_video_downloaded",
                        lambda vid: per_entry.append(vid) or False)

    _scan_now(app, monkeypatch, cid, [
        {"id": f"v{i}", "title": f"Track {i}"} for i in range(50)])

    assert len(bulk) == 1
    assert per_entry == []


def test_the_scan_still_hides_a_track_the_database_already_owns(app,
                                                                monkeypatch):
    """The snapshot has to answer exactly what the per-id check did, or a
    scan starts offering tracks that are already downloaded."""
    cid = _watched(app, display_name="Owned Check")
    app._db.add_download(video_id="vowned", title="Owned", channel_name="C",
                         channel_url="https://yt/c", platform="YouTube",
                         genre="DnB", file_path="/x/owned.mp3",
                         upload_date="", bitrate="")

    row = _scan_now(app, monkeypatch, cid, [
        {"id": "vowned", "title": "Owned"},
        {"id": "vfresh", "title": "Fresh"},
    ])

    import json
    pending = json.loads(row["pending_entries_json"])
    assert [e["id"] for e in pending] == ["vfresh"]


def test_scan_all_runs_one_channel_at_a_time(cb_mod):
    """Concurrent scans do not finish sooner — a yt-dlp flat-extraction is
    pure-Python work holding the GIL, so running three only starves the thread
    painting the window. Measured as how late a 15 ms Tk callback fires: one
    scan thread 47 ms (17 fps), three 139-175 ms (6 fps)."""
    assert cb_mod.WATCHLIST_MAX_CONCURRENT_SCANS == 1


# ══════════════════════════════════════════════════════════════════════════════
# normalize_track_key — the reconciler between a title and yt-dlp's filename
# ══════════════════════════════════════════════════════════════════════════════
_ROUND_TRIP_TITLES = [
    "Artist - Track (Original Mix)",
    'Artist: Track / Mix * Q? "x" <y> |z| \\ w',
    "1788-L - ÆTHERSUIT",
    "  Padded Title  ",
    "DnB Mix?",
    "Track [Official Audio] 2026",
]


@pytest.mark.parametrize("title", _ROUND_TRIP_TITLES)
def test_normalize_track_key_round_trips_the_ytdlp_filename(title):
    if ytdl_sanitize is None:                          # pragma: no cover
        pytest.skip("yt_dlp.utils.sanitize_filename unavailable")
    filename = ytdl_sanitize(title, restricted=False) + ".mp3"
    assert normalize_track_key(title) == normalize_track_key(filename)
    assert normalize_track_key(title) != ""


@pytest.mark.parametrize("title", _ROUND_TRIP_TITLES)
def test_normalize_track_key_round_trips_the_legacy_filename(title):
    # The same key reconciles files written before yt-dlp's sanitiser was
    # adopted, which is why the folder key index can index either vintage.
    filename = safe_filename(title) + ".mp3"
    assert normalize_track_key(title) == normalize_track_key(filename)


def test_normalize_track_key_is_empty_for_punctuation_only_titles():
    # Consequence worth pinning: a title with no alphanumerics keys to "", and
    # every folder-key lookup guards on `if key`, so such a track can never be
    # matched on disk and is re-offered on every scan.
    assert normalize_track_key("???") == ""
    if ytdl_sanitize is not None:
        assert normalize_track_key(ytdl_sanitize("???", restricted=False)) == ""

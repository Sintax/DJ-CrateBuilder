import re
import datetime as _dt

from cratebuilder import util


def test_normalize_strips_audio_extensions():
    assert util.normalize_track_key("My Track.mp3") == util.normalize_track_key("My Track")
    for ext in ("m4a", "opus", "webm", "wav", "flac", "aac"):
        assert util.normalize_track_key(f"Song.{ext}") == "song"


def test_normalize_collapses_punctuation_and_case():
    assert util.normalize_track_key("Drum & Bass!! (2024)") == "drumbass2024"
    assert util.normalize_track_key("A_B-C") == "abc"


def test_normalize_handles_empty_and_none():
    assert util.normalize_track_key("") == ""
    assert util.normalize_track_key(None) == ""


def test_today_yyyymmdd_format():
    # today_yyyymmdd() returns date.strftime("%Y%m%d"), i.e. a compact 8-digit
    # YYYYMMDD with NO dashes (e.g. "20260530"), not an ISO "YYYY-MM-DD" string.
    val = util.today_yyyymmdd()
    assert re.fullmatch(r"\d{8}", val)
    # parses as a real date
    _dt.datetime.strptime(val, "%Y%m%d")


def test_detect_platform():
    from cratebuilder.util import detect_platform
    assert detect_platform("https://soundcloud.com/artist") == "SoundCloud"
    assert detect_platform("https://www.youtube.com/@chan") == "YouTube"
    assert detect_platform("") == "YouTube"  # default
    assert detect_platform(None) == "YouTube"


def test_push_mru_prepends_new_value():
    assert util.push_mru(["a", "b", "c"], "d", 6) == ["d", "a", "b", "c"]
    assert util.push_mru([], "a", 6) == ["a"]


def test_push_mru_dedupes_and_moves_to_front():
    assert util.push_mru(["a", "b", "c"], "b", 6) == ["b", "a", "c"]
    assert util.push_mru(["a", "b"], "a", 6) == ["a", "b"]


def test_push_mru_caps_at_limit():
    assert util.push_mru(["a", "b", "c", "d", "e", "f"], "g", 6) == \
        ["g", "a", "b", "c", "d", "e"]


def test_push_mru_does_not_mutate_input():
    items = ["a", "b"]
    util.push_mru(items, "c", 6)
    assert items == ["a", "b"]


def test_push_mru_handles_none_list():
    assert util.push_mru(None, "a", 6) == ["a"]


def test_redact_ydl_opts_redacts_cookiefile():
    out = util.redact_ydl_opts({"cookiefile": r"C:\Users\djsin\cookies.txt"})
    assert out["cookiefile"] == "<redacted>"


def test_redact_ydl_opts_redacts_cookiesfrombrowser():
    out = util.redact_ydl_opts({"cookiesfrombrowser": ("chrome", "Default", None, None)})
    assert out["cookiesfrombrowser"] == "<redacted>"


def test_redact_ydl_opts_keeps_falsy_auth_values():
    # An unset cookie value must stay falsy so the log still shows cookies
    # were NOT configured — redacting None/"" would imply they were.
    out = util.redact_ydl_opts({"cookiefile": None, "cookiesfrombrowser": ""})
    assert out["cookiefile"] is None
    assert out["cookiesfrombrowser"] == ""


def test_redact_ydl_opts_summarizes_progress_hooks():
    out = util.redact_ydl_opts({"progress_hooks": [lambda d: None, lambda d: None]})
    assert out["progress_hooks"] == "[2 hook(s)]"


def test_redact_ydl_opts_passes_through_other_keys():
    opts = {"format": "bestaudio", "outtmpl": "%(title)s.%(ext)s", "quiet": True}
    out = util.redact_ydl_opts(opts)
    assert out["format"] == "bestaudio"
    assert out["outtmpl"] == "%(title)s.%(ext)s"
    assert out["quiet"] is True


def test_redact_ydl_opts_does_not_mutate_input():
    opts = {"cookiefile": "secret.txt", "format": "bestaudio"}
    util.redact_ydl_opts(opts)
    assert opts == {"cookiefile": "secret.txt", "format": "bestaudio"}


def test_redact_ydl_opts_handles_none():
    assert util.redact_ydl_opts(None) == {}


def test_build_cookie_opts_file_method_existing(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    out = util.build_cookie_opts("Cookie File", str(f), "Firefox", "")
    assert out == {"cookiefile": str(f)}


def test_build_cookie_opts_file_method_missing_file():
    out = util.build_cookie_opts("Cookie File", r"C:\nope\cookies.txt", "Firefox", "")
    assert out == {}


def test_build_cookie_opts_file_method_empty_path():
    assert util.build_cookie_opts("Cookie File", "", "Firefox", "") == {}


def test_build_cookie_opts_browser_with_profile():
    out = util.build_cookie_opts("From Browser", "", "Chrome", "Default")
    assert out == {"cookiesfrombrowser": ("chrome", "Default")}


def test_build_cookie_opts_browser_without_profile():
    out = util.build_cookie_opts("From Browser", "", "Firefox", "")
    assert out == {"cookiesfrombrowser": ("firefox",)}


def test_build_cookie_opts_delegator(cb):
    # The monolith's _apply_cookie_opts uses the same extracted helper, so the
    # metadata / probe / download / scan cookie blocks share one source.
    assert cb.build_cookie_opts is util.build_cookie_opts

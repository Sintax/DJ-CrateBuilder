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

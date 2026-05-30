import re
import datetime as _dt


def test_normalize_strips_audio_extensions(cb):
    assert cb.normalize_track_key("My Track.mp3") == cb.normalize_track_key("My Track")
    for ext in ("m4a", "opus", "webm", "wav", "flac", "aac"):
        assert cb.normalize_track_key(f"Song.{ext}") == "song"


def test_normalize_collapses_punctuation_and_case(cb):
    assert cb.normalize_track_key("Drum & Bass!! (2024)") == "drumbass2024"
    assert cb.normalize_track_key("A_B-C") == "abc"


def test_normalize_handles_empty_and_none(cb):
    assert cb.normalize_track_key("") == ""
    assert cb.normalize_track_key(None) == ""


def test_today_yyyymmdd_format(cb):
    # CORRECTED from plan: today_yyyymmdd() returns date.strftime("%Y%m%d"),
    # i.e. a compact 8-digit YYYYMMDD with NO dashes (e.g. "20260530"), not an
    # ISO "YYYY-MM-DD" string. The test characterizes the real behaviour.
    val = cb.today_yyyymmdd()
    assert re.fullmatch(r"\d{8}", val)
    # parses as a real date
    _dt.datetime.strptime(val, "%Y%m%d")

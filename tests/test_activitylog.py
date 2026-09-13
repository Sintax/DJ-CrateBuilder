"""The UPDATED activity.log line: which build, which components changed."""
from cratebuilder import activitylog


def _row(label, have, will, state):
    return {"key": label.lower(), "label": label, "installed": have,
            "offered": will, "state": state}


def test_updated_names_only_the_rows_the_build_changes():
    line = activitylog.updated(82, 83, [
        _row("Python", "3.14.5", "3.14.5", "same"),
        _row("yt-dlp", "2026.8.19", "2026.9.2", "newer"),
        _row("certifi", "2026.7.22", "2026.9.1", "newer"),
        _row("pystray", None, "0.19.5", "missing"),
    ])
    assert line == ("UPDATED     | Build: 82 -> 83 | Components: "
                    "yt-dlp 2026.8.19 -> 2026.9.2, certifi 2026.7.22 -> 2026.9.1")


def test_updated_says_not_listed_when_the_build_has_no_block():
    line = activitylog.updated(82, 83, [
        _row("Python", "3.14.5", None, "unknown"),
        _row("yt-dlp", "2026.8.19", None, "unknown"),
    ])
    assert line.endswith("| Components: not listed")


def test_updated_says_none_when_every_row_is_current():
    line = activitylog.updated(82, 83, [
        _row("Python", "3.14.5", "3.14.5", "same"),
        _row("yt-dlp", "2026.8.19", "2026.8.19", "same"),
    ])
    assert line.endswith("| Components: none")


def test_updated_line_never_trips_the_viewer_filters():
    line = activitylog.updated(82, 83, [])
    assert "DOWNLOADED" not in line
    assert "SKIPPED" not in line
    assert "ERROR" not in line

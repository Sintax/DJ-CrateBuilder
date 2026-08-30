"""CrateBuilderService: logs.tail / logs.search / logs.download / logs.watch."""

import threading

import pytest

from cratebuilder.service import (CBError, CrateBuilderService,
                                  MAX_LOG_SEARCH_MATCHES)
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    """A service pointed entirely at tmp_path, with a fast watch interval so
    tests never wait a real second for the tail thread to notice growth."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(
        settings=settings, db_path=str(tmp_path / "cratebuilder.db"),
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"),
        log_watch_interval=0.02)


def _write_lines(path, lines):
    """Write bytes directly (not write_text) so the on-disk line endings are
    exactly \\n regardless of platform — write_text would translate to CRLF
    on Windows and throw off the byte-offset math this test file checks."""
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _wait_until(condition, tries=200, step=0.01):
    for _ in range(tries):
        if condition():
            return True
        threading.Event().wait(step)
    return condition()


# ── logs.tail: missing file / unknown name ──────────────────────────────────

def test_tail_missing_file_returns_empty_result_never_raises(service):
    result = service.logs_tail("activity")
    assert result["lines"] == []
    assert result["offset"] == 0
    assert result["start"] == 0
    assert result["size"] == 0
    assert result["total_lines"] == 0
    assert result["path"].endswith("activity.log")


def test_tail_unknown_name_raises_cberror(service):
    with pytest.raises(CBError, match="Unknown log"):
        service.logs_tail("nope")


def test_search_missing_file_returns_empty_never_raises(service):
    assert service.logs_search("activity", "anything") == {"matches": [], "total": 0}


def test_search_unknown_name_raises_cberror(service):
    with pytest.raises(CBError, match="Unknown log"):
        service.logs_search("nope", "x")


def test_download_unknown_name_raises_cberror(service):
    with pytest.raises(CBError, match="Unknown log"):
        service.logs_download("nope")


def test_watch_unknown_name_raises_cberror(service):
    with pytest.raises(CBError, match="Unknown log"):
        service.logs_watch("nope", True)


# ── logs.tail: last-N-lines mode ─────────────────────────────────────────────

def test_tail_last_n_lines_mode(service, tmp_path):
    lines = [f"line-{i}" for i in range(10)]
    _write_lines(tmp_path / "activity.log", lines)

    result = service.logs_tail("activity", offset=None, limit=3)

    assert result["lines"] == ["line-7", "line-8", "line-9"]
    expected_start = sum(len((f"line-{i}\n").encode("utf-8")) for i in range(7))
    assert result["start"] == expected_start
    assert result["offset"] == result["size"]
    assert result["size"] == sum(len((l + "\n").encode("utf-8")) for l in lines)


def test_tail_last_n_lines_mode_debug_log(service, tmp_path):
    lines = ["a", "b", "c"]
    _write_lines(tmp_path / "debug.log", lines)
    result = service.logs_tail("debug", offset=None, limit=2)
    assert result["lines"] == ["b", "c"]


def test_tail_limit_larger_than_file_returns_everything(service, tmp_path):
    lines = ["one", "two"]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_tail("activity", offset=None, limit=100)
    assert result["lines"] == lines
    assert result["start"] == 0


# ── logs.tail: forward offsets / windowing ───────────────────────────────────

def test_tail_jump_to_top(service, tmp_path):
    lines = [f"line-{i}" for i in range(10)]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_tail("activity", offset=0, limit=3)
    assert result["lines"] == ["line-0", "line-1", "line-2"]
    assert result["start"] == 0
    assert result["offset"] < result["size"]


def test_tail_forward_pagination_reconstructs_the_whole_file(service, tmp_path):
    lines = [f"row {i:03d} of the synthetic log" for i in range(137)]
    _write_lines(tmp_path / "activity.log", lines)

    collected = []
    offset = 0
    for _ in range(200):
        page = service.logs_tail("activity", offset=offset, limit=10)
        if not page["lines"]:
            break
        collected.extend(page["lines"])
        offset = page["offset"]
        if offset >= page["size"]:
            break
    else:
        pytest.fail("pagination never reached end of file")

    assert collected == lines
    assert offset == service.logs_tail("activity")["size"]


def test_tail_offset_beyond_end_of_file_returns_nothing(service, tmp_path):
    lines = ["a", "b"]
    _write_lines(tmp_path / "activity.log", lines)
    size = service.logs_tail("activity")["size"]
    result = service.logs_tail("activity", offset=size + 500, limit=10)
    assert result["lines"] == []
    assert result["start"] == size
    assert result["offset"] == size


def test_tail_unlimited_limit_returns_the_entire_file(service, tmp_path):
    lines = [f"line-{i}" for i in range(50)]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_tail("activity", offset=0, limit=0)
    assert result["lines"] == lines


def test_tail_unlimited_limit_with_null_offset_is_also_the_whole_file(service, tmp_path):
    lines = [f"line-{i}" for i in range(50)]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_tail("activity", offset=None, limit=0)
    assert result["lines"] == lines


def test_tail_a_window_start_matches_the_offset_that_produced_it(service, tmp_path):
    """logs.tail(offset=<some response's start>) reloads the same window —
    the property the web viewer's jump-to-match relies on."""
    lines = [f"line-{i}" for i in range(40)]
    _write_lines(tmp_path / "activity.log", lines)
    first = service.logs_tail("activity", offset=None, limit=5)
    reloaded = service.logs_tail("activity", offset=first["start"], limit=5)
    assert reloaded["lines"] == first["lines"]


# ── logs.tail: total_lines (whole-file, regardless of window) ───────────────

def test_tail_reports_total_lines_for_the_whole_file_regardless_of_window(service, tmp_path):
    lines = [f"line-{i}" for i in range(50)]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_tail("activity", offset=None, limit=5)
    assert len(result["lines"]) == 5
    assert result["total_lines"] == 50


def test_tail_total_lines_is_the_same_across_every_window_of_the_same_file(service, tmp_path):
    lines = [f"line-{i}" for i in range(50)]
    _write_lines(tmp_path / "activity.log", lines)
    a = service.logs_tail("activity", offset=0, limit=5)
    b = service.logs_tail("activity", offset=None, limit=5)
    assert a["total_lines"] == b["total_lines"] == 50


# ── logs.tail: backward windowing (before=True) ──────────────────────────────

def test_tail_backward_at_offset_zero_returns_nothing(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["a", "b", "c"])
    result = service.logs_tail("activity", offset=0, limit=10, before=True)
    assert result["lines"] == []
    assert result["start"] == 0
    assert result["offset"] == 0


def test_tail_backward_from_a_forward_windows_start_yields_the_preceding_lines(service, tmp_path):
    lines = [f"line-{i}" for i in range(40)]
    _write_lines(tmp_path / "activity.log", lines)
    tail_window = service.logs_tail("activity", offset=None, limit=5)   # last 5 lines
    assert tail_window["lines"] == lines[-5:]

    before_window = service.logs_tail(
        "activity", offset=tail_window["start"], limit=5, before=True)
    assert before_window["lines"] == lines[-10:-5]
    # The backward window's own end lines up exactly with the forward
    # window's start — contiguous, no gap and no overlap.
    assert before_window["offset"] == tail_window["start"]


def test_tail_backward_pagination_reconstructs_the_whole_file(service, tmp_path):
    lines = [f"row {i:03d} of the synthetic log" for i in range(137)]
    _write_lines(tmp_path / "activity.log", lines)

    collected = []
    offset = service.logs_tail("activity")["size"]
    for _ in range(200):
        page = service.logs_tail("activity", offset=offset, limit=10, before=True)
        if not page["lines"]:
            break
        collected = page["lines"] + collected
        offset = page["start"]
        if offset <= 0:
            break
    else:
        pytest.fail("backward pagination never reached the start of file")

    assert collected == lines
    assert offset == 0


def test_tail_backward_limit_larger_than_available_returns_everything_before(service, tmp_path):
    lines = ["one", "two", "three"]
    _write_lines(tmp_path / "activity.log", lines)
    size = service.logs_tail("activity")["size"]
    result = service.logs_tail("activity", offset=size, limit=100, before=True)
    assert result["lines"] == lines
    assert result["start"] == 0


def test_tail_before_is_ignored_when_offset_is_none(service, tmp_path):
    lines = [f"line-{i}" for i in range(10)]
    _write_lines(tmp_path / "activity.log", lines)
    forward = service.logs_tail("activity", offset=None, limit=3, before=False)
    backward = service.logs_tail("activity", offset=None, limit=3, before=True)
    assert forward["lines"] == backward["lines"] == lines[-3:]


def test_tail_before_param_reachable_through_call(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["a", "b", "c"])
    whole = service.call("logs.tail", {"name": "activity"})
    result = service.call(
        "logs.tail",
        {"name": "activity", "offset": whole["start"], "before": True, "limit": 10})
    assert result["lines"] == []
    assert result["start"] == 0


# ── logs.search ───────────────────────────────────────────────────────────────

def test_search_is_case_insensitive_by_default(service, tmp_path):
    lines = ["all fine here", "an ERROR happened", "another Error too", "still fine"]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_search("activity", "error")
    assert result["total"] == 2
    assert [m["line_no"] for m in result["matches"]] == [2, 3]


def test_search_offsets_point_at_the_start_of_the_matching_line(service, tmp_path):
    lines = ["no match", "TARGET is here", "no match again"]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_search("activity", "TARGET")
    assert result["total"] == 1
    offset = result["matches"][0]["offset"]
    window = service.logs_tail("activity", offset=offset, limit=1)
    assert window["lines"] == ["TARGET is here"]


def test_search_regex_mode(service, tmp_path):
    lines = ["code 200 ok", "code 404 missing", "code 500 error"]
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_search("activity", r"code (4|5)\d\d", regex=True)
    assert result["total"] == 2


def test_search_invalid_regex_raises_cberror(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["hello"])
    with pytest.raises(CBError, match="Not a valid search pattern"):
        service.logs_search("activity", "(unclosed", regex=True)


def test_search_empty_query_returns_no_matches_not_everything(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["a", "b", "c"])
    assert service.logs_search("activity", "") == {"matches": [], "total": 0}


def test_search_caps_returned_matches_but_reports_the_true_total(service, tmp_path):
    lines = ["HIT"] * (MAX_LOG_SEARCH_MATCHES + 37)
    _write_lines(tmp_path / "activity.log", lines)
    result = service.logs_search("activity", "HIT")
    assert len(result["matches"]) == MAX_LOG_SEARCH_MATCHES
    assert result["total"] == MAX_LOG_SEARCH_MATCHES + 37


# ── encoding: errors="replace" must never crash ──────────────────────────────

def test_mojibake_does_not_crash_tail_or_search(service, tmp_path):
    path = tmp_path / "activity.log"
    path.write_bytes(b"\xff\xfe not valid utf-8\ngood line\n")

    tail = service.logs_tail("activity")
    assert "�" in tail["lines"][0] or tail["lines"][0] != ""
    assert tail["lines"][-1] == "good line"

    search = service.logs_search("activity", "good line")
    assert search["total"] == 1


# ── logs.download ─────────────────────────────────────────────────────────────

def test_download_returns_the_absolute_path(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["a"])
    result = service.logs_download("activity")
    assert result == {"path": service._log_path}


def test_download_works_on_the_remote_transport_too(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    remote = CrateBuilderService(
        transport="remote", settings=settings,
        db_path=str(tmp_path / "cratebuilder.db"),
        log_path=str(tmp_path / "activity.log"),
        debug_log_path=str(tmp_path / "debug.log"))
    # Not gated by LOCAL_ONLY — call() must actually reach the handler.
    result = remote.call("logs.download", {"name": "activity"})
    assert result["path"].endswith("activity.log")


# ── logs.watch: tail thread lifecycle ────────────────────────────────────────

def test_watch_starts_and_cleanly_stops_the_thread(service, tmp_path):
    (tmp_path / "activity.log").write_text("start\n", encoding="utf-8")

    service.logs_watch("activity", True)
    watcher = service._log_watchers["activity"]
    assert _wait_until(lambda: watcher["thread"].is_alive())

    service.logs_watch("activity", False)
    assert _wait_until(lambda: not watcher["thread"].is_alive())


def test_watch_emits_log_append_on_growth(service, tmp_path):
    path = tmp_path / "activity.log"
    path.write_text("start\n", encoding="utf-8")

    received = []
    service.events.subscribe(lambda t, p: received.append((t, p)))
    service.logs_watch("activity", True)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("new line one\nnew line two\n")

        assert _wait_until(lambda: any(t == "log.append" for t, _p in received))
        event_type, payload = next(e for e in received if e[0] == "log.append")
        assert payload["name"] == "activity"
        assert payload["lines"] == ["new line one", "new line two"]
        assert payload["offset"] == path.stat().st_size
    finally:
        service.logs_watch("activity", False)


def test_watch_is_refcounted_across_multiple_open_screens(service, tmp_path):
    (tmp_path / "activity.log").write_text("start\n", encoding="utf-8")

    service.logs_watch("activity", True)
    service.logs_watch("activity", True)
    watcher = service._log_watchers["activity"]
    assert _wait_until(lambda: watcher["thread"].is_alive())

    service.logs_watch("activity", False)   # one screen closes
    threading.Event().wait(0.05)
    assert watcher["thread"].is_alive(), "second client's watch was still open"

    service.logs_watch("activity", False)   # the last screen closes
    assert _wait_until(lambda: not watcher["thread"].is_alive())


def test_watch_survives_a_file_that_does_not_exist_yet(service, tmp_path):
    path = tmp_path / "activity.log"
    assert not path.exists()

    received = []
    service.events.subscribe(lambda t, p: received.append((t, p)))
    service.logs_watch("activity", True)
    try:
        threading.Event().wait(0.05)   # a couple of poll cycles against nothing
        path.write_text("first line\n", encoding="utf-8")
        assert _wait_until(lambda: any(t == "log.append" for t, _p in received))
    finally:
        service.logs_watch("activity", False)


def test_watch_resyncs_without_emitting_stale_content_on_shrink(service, tmp_path):
    path = tmp_path / "activity.log"
    path.write_text("a" * 500 + "\n", encoding="utf-8")

    received = []
    service.logs_watch("activity", True)
    try:
        watcher = service._log_watchers["activity"]
        assert _wait_until(lambda: watcher["thread"].is_alive())
        threading.Event().wait(0.05)   # let the watcher observe the initial size

        service.events.subscribe(lambda t, p: received.append((t, p)))
        path.write_text("post-trim\n", encoding="utf-8")   # shrinks the file

        threading.Event().wait(0.08)
        assert not any(t == "log.append" for t, _p in received), \
            "a shrink must resync silently, not emit a bogus delta"

        with open(path, "a", encoding="utf-8") as handle:
            handle.write("grew again\n")
        assert _wait_until(lambda: any(t == "log.append" for t, _p in received))
        _t, payload = next(e for e in received if e[0] == "log.append")
        assert payload["lines"] == ["grew again"]
    finally:
        service.logs_watch("activity", False)


# ── dispatch: logs.* reachable through call() ────────────────────────────────

def test_logs_methods_reachable_through_call(service, tmp_path):
    _write_lines(tmp_path / "activity.log", ["hello world"])
    tail = service.call("logs.tail", {"name": "activity"})
    assert tail["lines"] == ["hello world"]
    search = service.call("logs.search", {"name": "activity", "query": "hello"})
    assert search["total"] == 1
    download = service.call("logs.download", {"name": "activity"})
    assert download["path"].endswith("activity.log")
    watch = service.call("logs.watch", {"name": "activity", "on": True})
    assert watch == {"watching": True}
    service.call("logs.watch", {"name": "activity", "on": False})

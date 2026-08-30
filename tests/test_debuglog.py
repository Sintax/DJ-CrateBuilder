"""cratebuilder.debuglog: the head-trimming handler and the logger factory.

The web service writes debug.log through these; v2.0 shipped with the viewer
reading a file nothing ever appended to, which is what this module closed.
"""

import logging
import os
import re

import pytest

from cratebuilder import debuglog


@pytest.fixture
def close_loggers():
    """Close every handler the test opened.

    Loggers are process-wide singletons and a FileHandler holds its file open —
    a test that leaves one behind keeps a handle on tmp_path and leaks lines
    into the next test's log.
    """
    built = []
    yield built.append
    for logger in built:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _logger(track, path, max_bytes=0, name=None):
    logger = debuglog.build_debug_logger(
        str(path), max_bytes=max_bytes,
        name=name or f"test.debuglog.{id(path)}")
    track(logger)
    return logger


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


# ── the formatter ────────────────────────────────────────────────────────────

def test_lines_carry_the_timestamp_level_and_message_the_monolith_writes(
        tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path)
    logger.info("DOWNLOAD | starting")

    line = _lines(path)[0]
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \| INFO  \| "
                    r"DOWNLOAD \| starting$", line), line


def test_debug_level_messages_are_written_not_filtered(tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path)
    logger.debug("YDL OPTS | format=bestaudio")
    assert "YDL OPTS | format=bestaudio" in path.read_text(encoding="utf-8")


def test_the_logger_does_not_propagate_to_the_root_handler(tmp_path,
                                                           close_loggers):
    """Otherwise every debug line also lands wherever the root logger points —
    the console, in a frozen build with no console at all."""
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path)
    assert logger.propagate is False


# ── head trimming ────────────────────────────────────────────────────────────

def test_growing_past_the_cap_drops_the_oldest_lines_not_the_newest(
        tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    cap = 4096
    logger = _logger(close_loggers, path, max_bytes=cap)
    for i in range(400):
        logger.info(f"LINE {i:04d}")

    assert os.path.getsize(path) <= cap
    lines = _lines(path)
    assert "LINE 0399" in lines[-1]
    assert not any("LINE 0000" in line for line in lines)


def test_a_trim_leaves_the_file_starting_on_a_line_boundary(tmp_path,
                                                            close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path, max_bytes=2048)
    for i in range(400):
        logger.info(f"LINE {i:04d}")

    first = _lines(path)[0]
    assert re.match(r"^\d{4}-\d\d-\d\d ", first), first


def test_a_pre_existing_oversized_file_is_trimmed_when_it_is_opened(
        tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    path.write_bytes(b"".join(b"OLD %04d\n" % i for i in range(2000)))
    assert os.path.getsize(path) > 4096

    _logger(close_loggers, path, max_bytes=4096)
    assert os.path.getsize(path) <= 4096
    assert "OLD 1999" in path.read_text(encoding="utf-8")


def test_an_unlimited_cap_never_trims(tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path, max_bytes=0)
    for i in range(500):
        logger.info(f"LINE {i:04d}")
    assert "LINE 0000" in path.read_text(encoding="utf-8")


def test_set_max_bytes_re_caps_a_live_logger_and_trims_at_once(tmp_path,
                                                               close_loggers):
    """The Settings log-size change has to reach the handler already holding
    the file open, exactly as the tkinter app's _autosave_log_limit does."""
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path, max_bytes=0)
    for i in range(500):
        logger.info(f"LINE {i:04d}")
    assert os.path.getsize(path) > 4096

    debuglog.set_max_bytes(logger, 4096)
    assert os.path.getsize(path) <= 4096
    assert "LINE 0499" in path.read_text(encoding="utf-8")


# ── never raises into the caller ─────────────────────────────────────────────

def test_a_log_file_that_cannot_be_opened_leaves_the_logger_silent(
        tmp_path, close_loggers):
    """Built in the service constructor: a diagnostic that stops the app from
    starting is worse than a missing diagnostic."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file", encoding="utf-8")

    logger = _logger(close_loggers, blocked / "debug.log")
    logger.info("nothing to write this to")   # must not raise


def test_a_broken_stream_never_raises_out_of_emit(tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path, max_bytes=512)
    handler = logger.handlers[0]

    class Exploding:
        def tell(self):
            raise OSError("the file went away")

        def write(self, *_a):
            pass

        def flush(self):
            pass

    handler.stream = Exploding()
    logger.info("x" * 600)      # the cap check hits tell(); still no raise


def test_trimming_an_unreadable_file_never_raises(tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path, max_bytes=256)
    handler = logger.handlers[0]
    handler.baseFilename = str(tmp_path / "gone" / "debug.log")
    handler.maybe_trim()        # missing directory — must be a quiet no-op
    handler._trim()


# ── re-initialising ──────────────────────────────────────────────────────────

def test_rebuilding_the_logger_never_doubles_its_output(tmp_path,
                                                        close_loggers):
    name = "test.debuglog.rebuild"
    first = tmp_path / "one.log"
    second = tmp_path / "two.log"
    _logger(close_loggers, first, name=name)
    logger = _logger(close_loggers, second, name=name)
    logger.info("ONLY ONCE")

    assert len(logger.handlers) == 1
    assert second.read_text(encoding="utf-8").count("ONLY ONCE") == 1
    assert "ONLY ONCE" not in first.read_text(encoding="utf-8")


# ── the session banner ───────────────────────────────────────────────────────

def test_the_banner_names_the_run_between_two_rules(tmp_path, close_loggers):
    path = tmp_path / "debug.log"
    logger = _logger(close_loggers, path)
    debuglog.session_banner(logger, version="2.0")

    text = path.read_text(encoding="utf-8")
    assert text.count(debuglog.RULE) == 2
    assert "SESSION START  —  DJ-CrateBuilder v2.0" in text
    assert "yt-dlp version:" in text
    assert "Python:" in text


def test_the_banner_never_raises_on_a_logger_that_cannot_write(close_loggers):
    class Broken(logging.Logger):
        def info(self, *a, **kw):
            raise OSError("disk full")

    debuglog.session_banner(Broken("test.debuglog.broken"))

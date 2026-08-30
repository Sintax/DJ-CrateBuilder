"""Debug-log plumbing: a head-trimming file handler and the logger it feeds."""

import logging
import os
import sys

# Deliberately NOT the monolith's "CrateBuilder.debug". Both can be alive in one
# process, and a logger is a process-wide singleton — whichever owner initialised
# second would clear the other's handler off it and silence that half.
SERVICE_LOGGER_NAME = "CrateBuilder.debug.service"

FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# What a trim leaves behind, as a fraction of the cap.
TRIM_RATIO = 0.9

RULE = "═" * 80


class HeadTrimFileHandler(logging.FileHandler):
    """FileHandler that caps the log at *max_bytes* by removing the oldest lines
    from the top once the file grows past the cap. ``max_bytes <= 0`` means
    unlimited (never trims). After a trim the file is left at ~90% of the cap so
    we don't rewrite on every subsequent line.

    The headless twin of DJ-CrateBuilder_v2.0.py's `_HeadTrimFileHandler`: a
    duplicate rather than an import, for the same reason cratebuilder.watchrun
    keeps its own copy of the audio-extension set — this package never imports
    the monolith."""

    def __init__(self, filename, max_bytes=0, encoding="utf-8"):
        self.max_bytes = max_bytes
        super().__init__(filename, encoding=encoding)
        self.maybe_trim()   # trim a pre-existing oversized file on open

    def emit(self, record):
        super().emit(record)
        try:
            if self.max_bytes > 0 and self.stream is not None \
                    and self.stream.tell() >= self.max_bytes:
                self._trim()
        except Exception:
            pass   # logging must never raise into the app

    def maybe_trim(self):
        """Trim now if the file already exceeds the cap (e.g. on open or after
        the cap is lowered at runtime). Safe no-op when unlimited or small."""
        try:
            if self.max_bytes > 0 and os.path.exists(self.baseFilename) \
                    and os.path.getsize(self.baseFilename) > self.max_bytes:
                self._trim()
        except Exception:
            pass

    def _trim(self):
        # logging handlers use a reentrant lock, so acquiring here is safe even
        # when called from inside emit() (which already holds it).
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            target = max(1, int(self.max_bytes * TRIM_RATIO))
            with open(self.baseFilename, "rb") as f:
                data = f.read()
            if len(data) > target:
                data = data[-target:]
                # Drop the partial first line so the file starts on a boundary.
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1:]
                with open(self.baseFilename, "wb") as f:
                    f.write(data)
            self.stream = self._open()
        except Exception:
            pass
        finally:
            self.release()


def build_debug_logger(path, max_bytes=0, name=SERVICE_LOGGER_NAME):
    """A DEBUG-level logger writing *path* through a HeadTrimFileHandler.

    Handlers are cleared (and closed) first, so re-initialising never doubles
    every line or leaks a file handle onto the previous log. A log file that
    cannot be opened at all leaves the logger silent rather than raising: this
    is built in the service constructor, and a diagnostic that stops the app
    from starting is worse than a missing diagnostic."""
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        handler = HeadTrimFileHandler(path, max_bytes=max_bytes,
                                      encoding="utf-8")
    except Exception:
        logger.addHandler(logging.NullHandler())
        return logger
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATEFMT))
    logger.addHandler(handler)
    return logger


def set_max_bytes(logger, max_bytes):
    """Re-cap a live logger's file and trim it if it already exceeds the new
    limit — the Settings log-size change, applied without a restart."""
    for handler in list(getattr(logger, "handlers", [])):
        if isinstance(handler, HeadTrimFileHandler):
            handler.max_bytes = max_bytes
            handler.maybe_trim()


def ytdlp_version():
    """The installed yt-dlp's version string, or "unknown"."""
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


def session_banner(logger, app_name="DJ-CrateBuilder", version=None):
    """Open the log with the run's identity — app, platform, interpreter and
    yt-dlp version. Mirrors the monolith's SESSION START block; the tkinter-only
    temp-dir guard it also writes stays with the monolith."""
    try:
        logger.info(RULE)
        logger.info(f"SESSION START  —  {app_name}"
                    + (f" v{version}" if version else ""))
        logger.info(f"Platform: {sys.platform}  |  "
                    f"Python: {sys.version.split()[0]}")
        logger.info(f"yt-dlp version: {ytdlp_version()}")
        logger.info(f"CWD | {os.getcwd()}")
        logger.info(RULE)
    except Exception:
        pass

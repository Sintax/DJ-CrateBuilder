"""The activity.log line format: one writer, one set of column widths."""
import time

from cratebuilder.crate import CrateLayout

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def downloaded(title, path, url, platform, genre, quality="192 kbps MP3"):
    """One DOWNLOADED entry. The column padding is load-bearing: the log is
    read as fixed-width text, and the Activity Log viewer's filters key off the
    leading verb."""
    genre_str = genre if genre and genre != CrateLayout.NO_GENRE_VALUE else "—"
    return (f"DOWNLOADED  | Platform: {platform:<11}| "
            f"Genre: {genre_str:<18}| Title: {title} | File: {path} | "
            f"URL: {url} | Quality: {quality}")


def skipped(title, path, reason="already exists"):
    """One SKIPPED entry."""
    return f"SKIPPED     | Reason: {reason:<20}| Title: {title} | File: {path}"


def error(title, url, message):
    """One ERROR entry."""
    return f"ERROR       | Title: {title} | URL: {url} | Error: {message}"


def separator(label=""):
    """The centred rule that opens and closes a batch."""
    if not label:
        return "═" * 80
    pad = max(0, 74 - len(label))
    return f"{'═' * (pad // 2)}  {label}  {'═' * (pad - pad // 2)}"


def over_limit(duration_sec, limit_minutes):
    """The SKIPPED reason for a track the Time Limiter turned away."""
    total = int(duration_sec)
    return (f"exceeds limit ({total // 60}:{total % 60:02d} > "
            f"{limit_minutes}:00)")


def append(path, text, now=None):
    """Append one timestamped line to the activity log at *path*. Never raises:
    a log failure must not fail a download."""
    stamp = time.strftime(TIMESTAMP_FORMAT, time.localtime(now)) if now \
        else time.strftime(TIMESTAMP_FORMAT)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} | {text}\n")
        return True
    except OSError:
        return False

"""Folders Cleanup ‹Smart› — pure matching core (no Tk / DB / network / FS).

Decides which downloaded files in a channel folder no longer correspond to a
video/track on that channel's live listing. See
docs/superpowers/specs/2026-06-17-folders-cleanup-smart-design.md.
"""
from cratebuilder.util import normalize_track_key


def is_scan_trustworthy(scan_count, folder_count):
    """Guard against a bad scan flagging a whole folder for deletion.

    Returns False (caller should SKIP the channel) when the scan returned no
    videos at all, or suspiciously few relative to how many files are on disk —
    the signature of a partial extraction, rate-limit, or yt-dlp breakage. A
    channel that legitimately pruned up to half its catalogue still passes. The
    max(..., 5) floor avoids over-triggering on very small folders."""
    if scan_count <= 0:
        return False
    if folder_count <= 0:
        return True
    return scan_count >= max(folder_count // 2, 5)

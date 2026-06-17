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


def classify_local_files(scan_entries, folder_files, db_video_id_by_path):
    """Bucket a channel folder's files into keep vs flag-for-deletion.

    Pure: all I/O is done by the caller and passed in.

    Args:
        scan_entries: list of yt-dlp flat entries ({"id", "title", ...}).
        folder_files: list of (filename, full_path, size_bytes, mtime) tuples,
            one per .mp3 in the channel folder.
        db_video_id_by_path: dict full_path -> video_id (or None) from the
            downloads table for this channel. Absent path == no DB row.

    A file is FLAGGED only when BOTH signals miss: its DB video_id is not in the
    scan AND its normalised title (from the filename) is not in the scan.
        - strong: the file had a DB video_id -> we know it was ours and it is
          gone from the channel (pre-checked in the UI).
        - weak:   no DB video_id -> no proof it was ever on the channel
          (shown but not pre-checked).

    Returns a list of FlaggedFile dicts:
        {filename, full_path, size_bytes, mtime, video_id, confidence, reason}
    """
    scan_ids = {e.get("id") for e in scan_entries if e.get("id")}
    scan_keys = {normalize_track_key(e.get("title") or "")
                 for e in scan_entries}
    scan_keys.discard("")

    flagged = []
    for filename, full_path, size_bytes, mtime in folder_files:
        vid = db_video_id_by_path.get(full_path)
        if vid and vid in scan_ids:
            continue                      # exact id match — keep
        if normalize_track_key(filename) in scan_keys:
            continue                      # title still on channel — keep
        if vid:
            confidence = "strong"
            reason = "In your library, no longer on the channel"
        else:
            confidence = "weak"
            reason = "No record this was ever on the channel"
        flagged.append({
            "filename":   filename,
            "full_path":  full_path,
            "size_bytes": size_bytes,
            "mtime":      mtime,
            "video_id":   vid,
            "confidence": confidence,
            "reason":     reason,
        })
    return flagged

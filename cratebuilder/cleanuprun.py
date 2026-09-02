"""Folders Cleanup ‹Smart›, headless: scan, classify, review, trash."""

import os
import threading
from datetime import datetime

from cratebuilder.batchresolve import platform_dir
from cratebuilder.cleanup import (classify_local_files, is_scan_trustworthy,
                                  partition_trash)
from cratebuilder.crate import CrateLayout
from cratebuilder.service import MAINTENANCE_JOB, CBError
from cratebuilder.sidecar import watch_fetch_url
from cratebuilder.ydl import YdlSession

TASK = "db.cleanup"
TITLE = "Folders Cleanup"

# How long a channel's review may sit unanswered before the run treats it as
# cancelled — a browser closed mid-review must not hold the maintenance slot
# for ever.
DECISION_TIMEOUT = 15 * 60.0

DECISIONS = ("confirm", "skip", "cancel")


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


class CleanupOps:
    """The web port of the monolith's _FoldersCleanupSession, its review
    window and _apply_cleanup_deletions, with each dialog replaced by an
    event and the review answered over an RPC.

    One run at a time is the job registry's guarantee — it holds the
    maintenance slot, so nothing else rewrites the downloads table while
    files leave the disk. Per channel: scan the live listing, gather the
    folder, classify (cratebuilder.cleanup), then either move on — the scan
    failed, the scan is untrustworthy, nothing flagged — or publish
    `cleanup.review` and block until `decide()` answers it: confirm (with the
    paths to trash, checked against what was offered), skip, or cancel. A
    review nobody answers within DECISION_TIMEOUT is a cancel.

    Nothing here trusts a caller's path: `decide` keeps only the paths the
    pending review itself offered, so a client can never name a file of its
    own to delete.
    """

    task = TASK

    def __init__(self, settings, db_factory, emit, *, log_line=None,
                 debug=None, session_factory=YdlSession, trash=None,
                 decision_timeout=DECISION_TIMEOUT, now=datetime.now):
        self._settings = settings
        self._db_factory = db_factory
        self._emit = emit
        self._log_line = log_line or (lambda text: None)
        self._debug = debug
        self._session_factory = session_factory
        self._trash = trash
        self._decision_timeout = decision_timeout
        self._now = now
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._answered = threading.Event()
        self._pending = None
        self._decision = None
        self._running = False

    # ── controls ─────────────────────────────────────────────────────────────

    @property
    def running(self):
        with self._lock:
            return self._running

    def cancel(self):
        """Stop after the channel in flight. Deletions already confirmed stay
        — they are in the Recycle Bin. Safe when nothing is running."""
        self._cancel.set()
        self._answered.set()
        return {"cancelled": True}

    def pending_review(self):
        """The review waiting on an answer, for a page that reloaded mid-run."""
        with self._lock:
            return dict(self._pending) if self._pending else None

    def decide(self, action, paths=None):
        """Answer the pending review."""
        action = str(action or "").strip().lower()
        if action not in DECISIONS:
            raise CBError(f"Unknown cleanup decision: {action!r}")
        with self._lock:
            if self._pending is None:
                raise CBError("No channel is waiting for a decision.")
            offered = {f["full_path"] for f in self._pending["flagged"]}
            chosen = [p for p in (paths or []) if p in offered]
            self._decision = {"action": action, "paths": chosen}
            self._answered.set()
        return {"accepted": True, "action": action, "paths": len(chosen)}

    # ── the run ──────────────────────────────────────────────────────────────

    def run(self, cids):
        with self._lock:
            self._running = True
            self._pending = None
            self._decision = None
        self._cancel.clear()
        self._answered.clear()
        removed_total = cleaned = skipped = 0
        cancelled = False
        cids = [int(c) for c in cids]
        total = len(cids)
        try:
            for index, cid in enumerate(cids):
                if self._cancel.is_set():
                    cancelled = True
                    break
                ch = self._db().get_watchlist_channel(cid)
                if not ch:
                    skipped += 1
                    continue
                name = (ch.get("display_name") or ch.get("url")
                        or f"channel {cid}")
                self._channel(index, total, cid, name, "scanning")
                entries, err = self._scan(ch)
                if self._cancel.is_set():
                    cancelled = True
                    break
                if err is not None:
                    self._dbg(f"CLEANUP SCAN FAIL | {name} | {err}")
                    self._log_channel(ch, removed=0, kept=0, errors=0,
                                      note=f"skipped (scan error: {err})")
                    skipped += 1
                    self._channel(index, total, cid, name, "skipped",
                                  note=f"scan error: {err}")
                    continue
                folder = self._folder(ch)
                folder_files, db_map = self._gather_folder(folder)
                if not is_scan_trustworthy(len(entries), len(folder_files)):
                    self._dbg(f"CLEANUP SKIP | {name} | scan={len(entries)} "
                              f"folder={len(folder_files)} (untrusted)")
                    self._log_channel(
                        ch, removed=0, kept=len(folder_files), errors=0,
                        note="skipped (scan returned too few videos)")
                    skipped += 1
                    self._channel(index, total, cid, name, "skipped",
                                  note="scan returned too few videos")
                    continue
                flagged = classify_local_files(entries, folder_files, db_map)
                if not flagged:
                    self._log_channel(ch, removed=0, kept=len(folder_files),
                                      errors=0, note="clean (nothing to remove)")
                    self._channel(index, total, cid, name, "clean",
                                  note="nothing to remove")
                    continue
                decision = self._review(index, total, ch, name, folder,
                                        flagged, len(folder_files))
                if decision is None or decision["action"] == "cancel":
                    cancelled = True
                    break
                if decision["action"] == "skip":
                    self._log_channel(ch, removed=0, kept=len(folder_files),
                                      errors=0, note="skipped by user")
                    skipped += 1
                    self._channel(index, total, cid, name, "skipped",
                                  note="skipped by user")
                    continue
                removed, errors = self._apply(ch, decision["paths"],
                                              len(folder_files))
                removed_total += removed
                cleaned += 1
                self._channel(index, total, cid, name, "done",
                              removed=removed,
                              kept=len(folder_files) - removed - errors,
                              errors=errors)
        finally:
            with self._lock:
                self._running = False
                self._pending = None
            self._finish(removed_total, cleaned, skipped, cancelled)
        return {"removed": removed_total, "channels": cleaned,
                "skipped": skipped, "cancelled": cancelled}

    # ── the steps ────────────────────────────────────────────────────────────

    def _db(self):
        return self._db_factory()

    def _dbg(self, text):
        if self._debug is not None:
            try:
                self._debug.info(text)
            except Exception:
                pass

    def _scan(self, ch):
        """The channel's live listing, or the error that stood in for it —
        as the monolith's _scan_worker: a failed scan skips the channel, it
        never flags anything."""
        platform = ch.get("platform") or "YouTube"
        url = watch_fetch_url(platform, ch.get("url") or "")
        try:
            session = self._session_factory(
                cookies=self._settings.cookie_config())
            return list(session.list_channel(url)), None
        except Exception as exc:
            return [], str(exc)[:160]

    def _folder(self, ch):
        platform = ch.get("platform") or "YouTube"
        return CrateLayout.channel_dir(
            platform_dir(self._settings.get("base_dir"), platform),
            ch.get("genre"), ch.get("display_name"))

    def _gather_folder(self, folder):
        """(folder_files, db_video_id_by_path) for *folder* — the monolith's
        _gather_folder: every .mp3 with its size and mtime, and the downloads
        rows that live in this folder keyed by the exact paths built here."""
        folder_files = []
        try:
            for fn in os.listdir(folder):
                if not fn.lower().endswith(CrateLayout.AUDIO_EXT):
                    continue
                full = os.path.join(folder, fn)
                try:
                    st = os.stat(full)
                    folder_files.append((fn, full, st.st_size,
                                         int(st.st_mtime)))
                except OSError:
                    folder_files.append((fn, full, 0, 0))
        except OSError:
            pass
        norm_folder = os.path.normpath(folder)
        db_map = {}
        for d in self._db().get_all_downloads():
            fp = d.get("file_path")
            if fp and os.path.dirname(os.path.normpath(fp)) == norm_folder:
                db_map[os.path.normpath(fp)] = d.get("video_id")
        remap = {}
        for _fn, full, _size, _mtime in folder_files:
            nf = os.path.normpath(full)
            if nf in db_map:
                remap[full] = db_map[nf]
        return folder_files, remap

    def _review(self, index, total, ch, name, folder, flagged, folder_count):
        """Publish the review and wait for `decide`. None means cancelled —
        by the RPC, or by nobody answering in time."""
        review = {"index": index, "total": total, "id": ch.get("id"),
                  "name": name, "folder": folder,
                  "folder_count": folder_count,
                  "flagged": [dict(f) for f in flagged]}
        with self._lock:
            self._pending = review
            self._decision = None
            self._answered.clear()
        if self._cancel.is_set():
            return None
        self._emit("cleanup.review", dict(review))
        answered = self._answered.wait(self._decision_timeout)
        with self._lock:
            decision, self._decision = self._decision, None
            self._pending = None
        if self._cancel.is_set() or not answered:
            return None
        return decision

    def _apply(self, ch, paths, folder_count):
        """Send the confirmed files to the Recycle Bin and drop their rows —
        _apply_cleanup_deletions. Returns (removed, errors)."""
        if not paths:
            self._log_channel(ch, removed=0, kept=folder_count, errors=0,
                              note="confirmed, nothing ticked")
            return 0, 0
        trash = self._trash
        if trash is None:
            try:
                from send2trash import send2trash as trash
            except Exception:
                self._log_channel(ch, removed=0, kept=folder_count, errors=0,
                                  note="aborted (send2trash missing)")
                raise CBError("This feature needs the 'send2trash' package. "
                              "Install it with:  pip install send2trash")
        trashed, errors = partition_trash(paths, trash)
        for p in trashed:
            self._dbg(f"CLEANUP TRASH | {p}")
        for p, exc in errors:
            self._dbg(f"CLEANUP TRASH FAIL | {p} | {exc}")
        removed_rows = self._db().delete_downloads_by_paths(trashed)
        self._dbg(f"CLEANUP DB | removed {removed_rows} download row(s)")
        # Errored files remain on disk but are reported under `errors`, not
        # `kept`, so removed + kept + errors == folder_count in the log line.
        self._log_channel(ch, removed=len(trashed),
                          kept=folder_count - len(trashed) - len(errors),
                          errors=len(errors))
        return len(trashed), len(errors)

    # ── what the run says ────────────────────────────────────────────────────

    def _channel(self, index, total, cid, name, phase, **extra):
        self._emit("cleanup.channel", dict(
            {"index": index, "total": total, "id": cid, "name": name,
             "phase": phase, "job": MAINTENANCE_JOB, "task": TASK}, **extra))

    def _log_channel(self, ch, *, removed, kept, errors, note=""):
        plat = ch.get("platform") or "YouTube"
        genre = ch.get("genre") or CrateLayout.NO_GENRE_VALUE
        name = ch.get("display_name") or ""
        tail = f" — {note}" if note else ""
        self._log_line(f"Folder Cleanup | {plat} / {genre} / {name}: "
                       f"{removed} removed, {kept} kept, {errors} errors{tail}")

    def _finish(self, removed, cleaned, skipped, cancelled):
        """The closing summary — _finish_folders_cleanup's box, as the
        notification the maintenance dialog settles on."""
        extra = (f" {_plural(skipped, 'channel')} skipped (see activity log)."
                 if skipped else "")
        body = (f"{_plural(removed, 'file')} removed across "
                f"{_plural(cleaned, 'channel')}.{extra}")
        self._emit("cleanup.finished", {
            "removed": removed, "channels": cleaned, "skipped": skipped,
            "cancelled": bool(cancelled), "job": MAINTENANCE_JOB,
            "task": TASK})
        self._emit("notification", {
            "level": "warn" if (cancelled or skipped) else "info",
            "title": f"{TITLE} cancelled" if cancelled else f"{TITLE} complete",
            "body": body,
            "at": self._now().isoformat(timespec="seconds"),
            "job": MAINTENANCE_JOB,
            "task": TASK,
            "cancelled": bool(cancelled),
        })

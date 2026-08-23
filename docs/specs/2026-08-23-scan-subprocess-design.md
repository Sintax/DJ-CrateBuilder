# Scan worker: the Watch List listing in a subprocess

## Problem

A Watch List scan is a yt-dlp flat-extraction — megabytes of JSON and regex,
pure-Python work that holds the GIL. Run on a thread, it starves the thread
painting the window: measured as how late a 15 ms Tk callback fires, idle is
0.6 ms (63 fps), one scan thread is 47 ms (17 fps), and the old three
concurrent scan threads were 139–175 ms (6 fps). Dropping the concurrency cap
to 1 (build 57) took the lag from "broken" to "choppy"; nothing run *in this
process* can take it further.

## Design

`cratebuilder/scanproc.py` holds both halves of one seam:

- **Parent** — `list_channel_isolated(url, cookies, should_cancel, debug)`:
  same contract as `YdlSession.list_channel` (raw yt-dlp entry dicts out, the
  same typed errors raised), answered by a child process. The cancel
  predicate is polled four times a second and kills the child mid-listing
  (`ScanCancelled`) — a capability a thread never had. A worker that cannot
  answer raises `ScanWorkerError`, which the scan's existing generic handling
  treats as transient (status only; the link and pending list survive).
- **Child** — `worker_main()`: reads one JSON request from stdin, builds a
  real `YdlSession` from the request's `CookieConfig` snapshot, answers one
  listing on stdout, exits. `sys.stdout` is repointed at stderr before any
  work so nothing yt-dlp prints can corrupt the protocol. The session's debug
  line and any yt-dlp noise go to stderr, which the parent relays into
  debug.log as `SCAN WORKER |` lines.
- **Codec** — request/result/error encode+decode, shared by both sides so
  they cannot drift. Typed errors carry their kind across the pipe and are
  re-raised as the same `YdlOffline` / `YdlPermanent` / `YdlUnclassified`
  classes, so `wl_scan_verdict_for` keeps judging by type and the
  captive-portal rule runs where it must — in the child, whose network view
  is the one the failure happened in.

Entry points: `python -m cratebuilder.scanproc` from source (cwd = repo root
so the package resolves); `DJ-CrateBuilder.exe --scan-worker` frozen —
intercepted at the very top of the monolith's `__main__` block, before the
single-instance guard (which would otherwise make the worker poke the
running window and exit answerless) and before any Tk root exists.

The app's `_scan_list_channel(url, cid)` is the one caller; it snapshots the
live cookie vars (same as `_ydl_session()`) and composes Cancel All with the
per-card ✕ into the predicate. If the worker cannot even start (`OSError`
from the spawn — blocked exe, broken install) it falls back to the
in-process listing: one laggy scan beats a Watch List that cannot scan.

## Deliberately not

- **Not a persistent worker pool.** One child per channel scan; spawn cost
  (~1–3 s frozen, less from source) is dwarfed by the listing and buys a
  stateless protocol with no lifetime management.
- **Not the other intents.** Metadata probes, searches and the artwork
  backfill stay in-process; the scan is the one that runs unattended over
  the whole Watch List. The protocol already carries `ignore_no_formats`
  should the artwork listing ever want the same treatment.
- **Not multiprocessing.** `spawn` re-executes the frozen entry anyway;
  explicit argv + JSON pipes are the same cost with none of the pickling or
  `freeze_support` edge cases.

## Measured

Tk heartbeat (15 ms target) while a scan-shaped child burns CPU flat out:
p50 0.5 ms, p95 1.1 ms, max 9.6 ms — indistinguishable from idle.

## Packaging

`--collect-submodules cratebuilder` already brings `scanproc` into the
frozen build; no spec change. The worker adds no new third-party imports.

# ADR 0001 — No injected clock for the automation timers

- **Status:** Accepted
- **Date:** 2026-08-16
- **Context:** architecture review of v1.3

## Context

The Watch List automation timers — the auto-download schedule, the startup
scan and the update check — are driven by Tk `after(...)` callbacks that read
`time.time()` inline (`_reschedule_auto_download`, `_auto_download_tick`,
`_auto_download_after_scan`, `_reschedule_update_check`). None of the
scheduling behaviour is covered by tests: only `interval_label_to_seconds`,
the label parser, is.

The obvious remedy is a clock/scheduler seam — an injected clock that tests
advance manually, with Tk `after` as the production adapter. This is the
refactor an architecture review will suggest on sight, so the reasoning for
not doing it is recorded here.

## Decision

**Do not introduce an injected clock or scheduler abstraction.**

Instead, extract the *decisions* those timers make as pure functions in
`cratebuilder/util.py`, each taking the current time as an ordinary argument:

- next-run delay from the last run, the interval, and now — including the
  "already overdue, fire in 1 second" rule
- the busy check: skip this tick and retry in 60s when a scan or download is
  already running
- the give-up cap while waiting for scans to settle (~5 minutes at 2s polls)
- the "Next auto-download: …" label text

## Consequences

- The arithmetic and the label wording become testable immediately, with no
  new machinery and no change to how the app keeps time.
- The *sequence* — that a tick actually fires after the interval elapses, that
  the poll loop terminates — stays untested. Accepted: it is thin glue around
  the extracted decisions, and Tk owns the firing either way.
- Should a second real timekeeping adapter ever appear (a headless/CLI mode, a
  service that runs without Tk), this decision should be revisited: the seam
  would then have two genuine adapters instead of one plus a test double.

## Rejected alternative

An injected clock with a manual test clock. Rejected because its only
beneficiary is the test suite: Tk `after` would remain the sole production
adapter, making the seam hypothetical rather than real. One adapter is a
hypothetical seam; two are a real one.

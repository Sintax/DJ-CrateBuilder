# Watch List sharing: pick what you share, and what to do on a clash

**Date:** 2026-09-13
**Builds on:** the one-shot Export / Import from build 76 (`cratebuilder/watchlist_share.py`).

## What changed for the user

- **Export** opens a picker over the Watch List. Nothing is ticked to start;
  `Select All | None` sits above the list. Only the ticked channels go into
  the file. A channel whose link was never resolved is listed greyed-out — it
  has no link to share.
- **Import** opens the file first, then the same picker over the channels the
  file holds. Rows that clash with an entry already in the Watch List carry an
  `⚠ already tracked` tag so the question is not a surprise.
- Each ticked channel lands **one at a time**. When one clashes, a prompt shows
  *Yours* and *In the file* side by side (name + link, the matching half
  highlighted) and offers **Skip / Overwrite / New / Stop import**.

## The rules, and why

**A clash is the same link *or* the same name.** Link identity is the existing
`util.find_matching_watchlist_row` tiering (channel id, exact link, any
spelling of one link). Name identity is case-folded and whitespace-collapsed
because the channel's name is also its folder name on a case-insensitive
filesystem.

**Overwrite re-points the link only.** The existing row's `url` + `channel_id`
take the file's values; `display_name` and `genre` are untouched. Both of
those are folder coordinates — renaming or re-genring here would move (or
strand) music on disk from inside an import, and the Edit dialog already owns
those moves with their own guards. No network probe either: import is
designed to work offline, and the file carries the channel id.

**New is hidden when the *link* clashes.** `watchlist.url` is `UNIQUE`; one
channel can be tracked once. New is offered only for a name-only clash, and the
typed name must itself be unused (checked against every row, same
normalisation) — refused names reopen the prompt with the reason.

**The host judges the clash on every call, not the picker.** Earlier answers
change the list (an Overwrite frees a name; a New takes one), so a clash
computed up front from the picker's snapshot could be wrong by the time that
entry's turn comes. The picker's `⚠` tags are a preview; `import_entry` is the
authority and writes nothing until it has an answer.

**"Apply this choice to every remaining conflict" carries a warning.** It
answers later clashes unseen, so the prompt says in plain words that
Overwrite-for-all re-points every matching channel. It never remembers *New*,
which needs a name per entry. It resets per import.

**Escape / ✕ on the prompt means Skip.** Every exit from the prompt is a
no-write; Stop ends the loop and leaves the rest of the file alone.

## Where it lives

- `cratebuilder/watchlist_share.py` — `name_key`, `name_is_taken`,
  `find_conflict` (pure; `plan_import` retired).
- `cratebuilder/watchrun.py` — `WatchlistOps.import_entry(entry, resolve)`
  replaces `add_imported`.
- `cratebuilder/service.py` — `fs.watchlist_export {ids}`,
  `fs.watchlist_import_read`, `watchlist.import_entry`, `watchlist.import_done`.
  The two file-dialog methods stay `fs.`-prefixed (LOCAL_ONLY); landing an
  entry is an ordinary Watch List write.
- `web/app.js` — `wlShareListModal`, `wlConflictModal`, `wlImportRun`,
  `openExportPicker`, `openImportPicker`; styles under "Watch List sharing" in
  `web/app.css`; tooltip keys `wl.share_*` / `wl.conflict_*` in
  `UI-design/ui-contract.json`.

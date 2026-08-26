"""One queue row's URL turned into the tracks it holds, and who they belong to."""
import os
from dataclasses import dataclass, field

from cratebuilder import util
from cratebuilder.crate import CrateLayout
from cratebuilder.sidecar import channel_url_from_id, write_channel_sidecar
from cratebuilder.ydl import YdlOffline, YdlPermanent, YdlUnclassified

# ── Platform facts ────────────────────────────────────────────────────────────
# The two per-platform values the download path reads out of the monolith's
# PLATFORMS table. Restated here rather than imported: PLATFORMS carries Tk
# colours and widget copy, and this package may not import the monolith.
PLATFORM_SUBDIR = {"YouTube": "YouTube", "SoundCloud": "SoundCloud"}
ITEM_WORD = {"YouTube": "video", "SoundCloud": "track"}

FETCH_FAILURE_KIND = {
    YdlPermanent: "permanent",
    YdlOffline: "offline",
    YdlUnclassified: "unknown",
}


@dataclass(frozen=True)
class TrackSpec:
    """One resolved track: everything the per-track loop needs without asking
    the network again.

    *entry* is the raw yt-dlp flat entry the limiter, the premiere check and
    ChannelCrate ownership all read, so a caller that already holds resolved
    entries (the Watch List) can feed BatchRunner.run_tracks directly."""
    row_id: object
    url: str
    title: str
    save_dir: str
    genre: str
    platform: str
    entry: dict = field(default_factory=dict)
    channel_name: str = ""
    channel_url: str = ""
    channel_id: str | None = None
    suppress_channel_url: str = ""


@dataclass(frozen=True)
class ResolvedRow:
    """What one queue row turned out to hold: its tracks, the folder they are
    going into, and the Watch List row now tracking the channel (None when the
    row is a one-off track or auto-add is off)."""
    tracks: list
    save_dir: str = ""
    channel_name: str = ""
    watchlist_id: object = None


# ── Pure helpers ──────────────────────────────────────────────────────────────
def entry_url(entry, platform):
    """The watch URL for one flat-playlist entry — the monolith's per-platform
    url_builder, headless."""
    url = entry.get("url") or entry.get("webpage_url") or ""
    if url:
        return url
    video_id = entry.get("id") or ""
    if platform == "SoundCloud":
        return video_id
    return f"https://www.youtube.com/watch?v={video_id}"


def platform_dir(base_dir, platform):
    """The crate's platform root: base/YouTube or base/SoundCloud."""
    return os.path.join(base_dir, PLATFORM_SUBDIR.get(platform, platform))


def fetch_failure_reason(exc):
    """A failed probe as the one sentence the queue row shows, keyed off the
    verdict YdlSession already reached rather than re-reading the message."""
    kind = "unknown"
    for error_type, named in FETCH_FAILURE_KIND.items():
        if isinstance(exc, error_type):
            kind = named
            break
    return util.describe_fetch_failure(kind, str(exc))


# ── RowResolver ───────────────────────────────────────────────────────────────
class RowResolver:
    """The session-facing half of a batch: probe a queue row's URL, expand a
    collection into its entries, name the folder they belong in, and stamp the
    channel's identity where the Watch List will look for it later.

    Nothing here downloads anything — it hands BatchRunner a ResolvedRow and
    stops. Kept apart from the track loop because the two halves face opposite
    collaborators: this one only ever talks to a YdlSession, the database's
    Watch List tables and the sidecar file; the loop only ever talks to a
    TrackDownloader."""

    def __init__(self, settings, db, session_factory, *, write_sidecar=None):
        self._settings = settings
        self._db = db
        self._session_factory = session_factory
        self._write_sidecar = write_sidecar or write_channel_sidecar

    def resolve(self, row):
        """Turn one queue row into a ResolvedRow. Raises the typed YdlError a
        failed probe produced — the caller reports it as that row's failure."""
        url = (row.get("url") or "").strip()
        genre = row.get("genre") or CrateLayout.NO_GENRE_VALUE
        platform = row.get("platform") or util.detect_platform(url)
        session = self._session_factory(cookies=self._settings.cookie_config())
        info = session.probe_metadata(url)
        if not info:
            # An intent that answers nothing is a failed fetch, not a URL with
            # one blank track on it.
            raise YdlUnclassified("yt-dlp returned no metadata",
                                  intent="probe_metadata", target=url)

        is_collection = info.get("_type") in ("playlist", "channel")
        channel_id = info.get("channel_id") or ""
        handle = info.get("uploader_id") or ""
        channel_url = (channel_url_from_id(channel_id)
                       or info.get("channel_url")
                       or info.get("uploader_url") or "")
        if is_collection:
            entries = [e for e in session.list_channel(url)
                       if isinstance(e, dict)]
            collection_name = util.derive_collection_name(info)
        else:
            entries = [info]
            collection_name = ""

        # A Watch List run passes its own channel name and is tracked by
        # definition, so it is left out of both the stamp and the auto-add.
        override = row.get("channel_name")
        channel_sub = override or (collection_name if is_collection else None)
        save_dir = CrateLayout.channel_dir(
            platform_dir(self._settings.get("base_dir"), platform),
            genre, channel_sub)
        os.makedirs(save_dir, exist_ok=True)

        watchlist_id = None
        if is_collection:
            self._stamp_channel(save_dir, platform, channel_id, channel_url,
                                handle, channel_sub or collection_name, genre)
            if not override:
                watchlist_id = self._auto_add(url, collection_name, genre,
                                              channel_id, platform)

        word = ITEM_WORD.get(platform, "item")
        tracks = [
            TrackSpec(
                row_id=row.get("id"),
                url=entry_url(entry, platform),
                title=entry.get("title") or f"{word.capitalize()} {i + 1}",
                save_dir=save_dir, genre=genre, platform=platform, entry=entry,
                channel_name=override or collection_name,
                channel_url=url if is_collection else "",
                channel_id=channel_id or None,
                # The downloads row keys on the collection URL; the
                # permanently-unavailable memory keys on the channel a one-off
                # track came from. For a collection they agree.
                suppress_channel_url=url if is_collection else channel_url,
            )
            for i, entry in enumerate(entries)
        ]
        return ResolvedRow(tracks=tracks, save_dir=save_dir,
                           channel_name=channel_sub or "",
                           watchlist_id=watchlist_id)

    def _stamp_channel(self, save_dir, platform, channel_id, channel_url,
                       handle, display_name, genre):
        """Stamp the channel folder with its canonical identity so a later
        Watch List scan never has to guess the handle from the folder name.
        Only meaningful for a YouTube collection, which is the only case that
        carries a UC id."""
        if platform != "YouTube" or not channel_id or not save_dir:
            return False
        return bool(self._write_sidecar(
            save_dir, channel_id=channel_id, channel_url=channel_url,
            handle=handle, display_name=display_name, platform=platform,
            genre=genre or CrateLayout.NO_GENRE_VALUE))

    def _auto_add(self, url, display_name, genre, channel_id, platform):
        """Track this channel on the Watch List if auto-add is on.

        The card goes up as the channel STARTS, not once it finishes: a full
        channel can run for an hour, and a run cancelled part-way should still
        leave the channel tracked — the user pointed the app at it
        deliberately. A channel already tracked under any of its URL forms
        (@handle vs /channel/UC… vs …/videos) has its row backfilled rather
        than duplicated, and a channel that could not be named is never
        inserted as a blank card. Returns the Watch List row id now tracking
        this URL, or None."""
        if not self._settings.automation_config().auto_add_to_watchlist:
            return None
        cid = (channel_id or "").strip()
        name = (display_name or "").strip()
        existing = util.find_matching_watchlist_row(
            self._db.get_all_watchlist_channels(), url,
            channel_id=cid, platform=platform)
        if existing is not None:
            fields = {}
            if cid and not (existing.get("channel_id") or "").strip():
                fields["channel_id"] = cid
            if name and not (existing.get("display_name") or "").strip():
                fields["display_name"] = name
            if fields:
                self._db.update_watchlist_channel_fields(existing["id"],
                                                         **fields)
            return existing["id"]
        if not name:
            return None
        return self._db.add_watchlist_channel(
            url=url, display_name=name, platform=platform,
            genre=genre or CrateLayout.NO_GENRE_VALUE,
            auto_added=True, channel_id=cid or None)

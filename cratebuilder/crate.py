"""Crate layout and per-channel ownership: where a track lives, and whether we have it."""
import enum
import os
import time
from dataclasses import dataclass

from cratebuilder.util import normalize_track_key, safe_filename


# ── CrateLayout ───────────────────────────────────────────────────────────────
class CrateLayout:
    """The single answer to "where does this track live?".

    A namespace, not a state holder: everything here is a class/static method
    and nothing has instance state. Naming is PURE — it never creates a
    directory, and only find_existing() touches the disk at all (a lookup, not
    a write). Callers still makedirs.

    This is the ONLY translator between the in-app no-genre value "(none)" and
    the on-disk folder name "_No Genre", in both directions. The in-app value
    is written into downloads.genre, watchlist.genre, every channel sidecar and
    the links-file key, so it stays "(none)" everywhere; the folder name is a
    presentation detail of the crate, produced here and nowhere else.

    The platform *directory* is passed in rather than (base_dir, platform):
    resolving a platform to its subdir needs the app's PLATFORMS table and its
    "no platform given → whatever the Main tab shows" Tk fallback, both of which
    are UI state. Keeping that in the app's _platform_dir keeps this module free
    of tkinter and of a config table it has no other use for.
    """

    NO_GENRE_VALUE = "(none)"     # in-app / DB / sidecar / links-key value
    NO_GENRE_DIR   = "_No Genre"  # on-disk folder name
    AUDIO_EXT      = ".mp3"

    @classmethod
    def genre_dir_name(cls, genre):
        """The folder name for an in-app genre value.

        Every "no genre" spelling — None, "", "(none)", or whitespace-only —
        becomes "_No Genre". Whitespace-only counts because Win32 trims a
        padded component when creating the directory, leaving an empty name and
        failing the download outright.

        Anything else is used VERBATIM, padding included. A genre folder can
        legitimately carry leading whitespace (the app's own "_No Genre" leads
        with "_" to force sort order), and such a folder is listed, offered and
        stored by its real name — so trimming it here would name a different
        folder than the one the user picked."""
        if not (genre or "").strip() or genre == cls.NO_GENRE_VALUE:
            return cls.NO_GENRE_DIR
        return genre

    @classmethod
    def genre_value(cls, dir_name):
        """The in-app genre value for a genre folder name — the inverse of
        genre_dir_name. "_No Genre" (and anything blank) reads back as
        "(none)", so a genre discovered on disk can be stored as-is."""
        name = (dir_name or "").strip()
        if not name or name == cls.NO_GENRE_DIR:
            return cls.NO_GENRE_VALUE
        return name

    @staticmethod
    def channel_dir_name(channel_name):
        """The folder name for a channel's display name, or "" when the name
        holds nothing legal (whitespace-only). Illegal characters map to "_",
        so a name of pure punctuation still yields a real folder."""
        return safe_filename(channel_name, strip=True)

    @classmethod
    def channel_dir(cls, platform_dir, genre, channel_name=None):
        """Build <platform_dir>/<Genre>[/<Channel>] — pure naming, no disk.

        The genre component is always present (see genre_dir_name). The channel
        component is omitted when *channel_name* is empty or sanitises to
        nothing. The genre is used verbatim apart from stripping: it is not
        sanitised, matching today's builders."""
        parts = [platform_dir, cls.genre_dir_name(genre)]
        safe = cls.channel_dir_name(channel_name)
        if safe:
            parts.append(safe)
        return os.path.join(*parts)

    @classmethod
    def track_file_name(cls, title):
        """The file name the app *expects* yt-dlp to write for *title*.

        A guess, not the truth: yt-dlp's own sanitiser names the real file, and
        it differs from ours over trailing dots, control characters, unicode and
        reserved names. Surrounding whitespace is deliberately NOT stripped here
        — that matches the expected_path the download loop builds today, and
        find_existing() is what reconciles the two spellings afterwards."""
        return safe_filename(title) + cls.AUDIO_EXT

    @classmethod
    def track_path(cls, directory, title):
        """The expected path of *title* inside *directory*. Pure naming."""
        return os.path.join(directory, cls.track_file_name(title))

    @classmethod
    def find_existing(cls, directory, title):
        """Return the existing .mp3 in *directory* holding *title*, or None.

        Two EXACT candidates, in order: the name yt-dlp's sanitiser produces
        (what a download actually writes today), then the legacy
        safe_filename(strip=True) name (files saved before that sanitiser was
        adopted — and the only tier that finds a padded title, since yt-dlp
        keeps surrounding spaces). Both are single existence checks; the
        directory is never listed.

        The old third tier — a case-insensitive scan for any .mp3 whose first 40
        characters matched — is RETIRED. For any title shorter than 40
        characters the "prefix" was the whole title, so an original was claimed
        as owned by any remix or edit extending it. Deciding ownership is
        ChannelCrate's job, by exact normalized track key; this helper exists
        only to find the file a download just wrote."""
        ytdl_safe = _ytdl_safe_name(title)
        exact = os.path.join(directory, ytdl_safe + cls.AUDIO_EXT)
        if os.path.exists(exact):
            return exact
        legacy = os.path.join(directory,
                              safe_filename(title, strip=True) + cls.AUDIO_EXT)
        if legacy != exact and os.path.exists(legacy):
            return legacy
        return None


def _ytdl_safe_name(title):
    """yt-dlp's own filename sanitisation of *title*, falling back to ours when
    yt-dlp is unavailable (the package is optional at import time here)."""
    try:
        from yt_dlp.utils import sanitize_filename as ytdl_sanitize
    except ImportError:
        return safe_filename(title)
    return ytdl_sanitize(title, restricted=False)


# ── Ownership ─────────────────────────────────────────────────────────────────
class OwnershipKind(str, enum.Enum):
    """The headline verdict a ChannelCrate states about one entry."""
    OWNED_BY_ID   = "owned-by-id"
    OWNED_ON_DISK = "owned-on-disk"
    NEW           = "new"
    TOO_LONG      = "too-long"
    UPCOMING      = "upcoming"
    UNAVAILABLE   = "unavailable"


@dataclass(frozen=True)
class Ownership:
    """What a ChannelCrate knows about one entry: a headline *kind* plus the
    evidence behind it.

    The evidence stays separate from the verdict on purpose. *in_db* and *path*
    are filled in whenever they are true, even for an entry whose headline is
    upcoming or unavailable, because the download-side skip modes are defined
    over exactly those two facts ("in the database", "in the folder") while the
    scan's buckets are defined over the headline. One fact, two readers, no
    second implementation.

    kind    — the bucket the scan would put this entry in.
    path    — the .mp3 in the channel folder that already holds this title,
              or "" when there is none.
    reason  — why the track is permanently unavailable ("Removed", …), or "".
    in_db   — the downloads table already has this video id.
    """
    kind: OwnershipKind
    path: str = ""
    reason: str = ""
    in_db: bool = False

    @property
    def is_owned(self):
        """True when we already have this track by either evidence."""
        return bool(self.in_db or self.path)

    @classmethod
    def owned_by_id(cls, path=""):
        return cls(OwnershipKind.OWNED_BY_ID, path=path, in_db=True)

    @classmethod
    def owned_on_disk(cls, path):
        return cls(OwnershipKind.OWNED_ON_DISK, path=path)

    @classmethod
    def new(cls):
        return cls(OwnershipKind.NEW)

    @classmethod
    def too_long(cls, path=""):
        return cls(OwnershipKind.TOO_LONG, path=path)

    @classmethod
    def upcoming(cls, path=""):
        return cls(OwnershipKind.UPCOMING, path=path)

    @classmethod
    def unavailable(cls, reason, path=""):
        return cls(OwnershipKind.UNAVAILABLE, reason=reason, path=path)


class SkipMode(str, enum.Enum):
    """How a download batch decides whether an owned track is skipped. The
    first three are the user's Skip-Existing setting; WATCH_LIST is the Watch
    List's own rule, which ignores that setting entirely."""
    DB_AND_FOLDER = "In Database ~ In Folder"
    FOLDER_ONLY   = "In Folder Only"
    DB_ONLY       = "In Database Only"
    WATCH_LIST    = "Watch List"


class SkipDecision(str, enum.Enum):
    SKIP               = "skip"
    DOWNLOAD           = "download"
    CONFIRM_REDOWNLOAD = "confirm-redownload"


def skip_decision(ownership, mode):
    """What a download batch in *mode* should do about *ownership*. Pure.

    Reads only the evidence (in_db / path), never the headline kind. That is
    what makes UPCOMING and UNAVAILABLE map to DOWNLOAD: the download path has
    no pre-flight suppression or premiere check today — it attempts the track
    and relies on classify_deferred_failure / classify_permanent_failure
    afterwards — and this preserves that exactly. The asymmetry with the scan,
    which withholds both buckets, is deliberate and lives here in one place
    instead of being an accident of two separate implementations.

    CONFIRM_REDOWNLOAD means "the database says we have it but the file is
    gone" — the caller asks the user. Only the two DB-consulting modes can
    produce it; the Watch List never does, so a batch never blocks mid-run. An
    unrecognised mode (including "skip checking is off") is DOWNLOAD."""
    in_db = bool(ownership.in_db)
    on_disk = bool(ownership.path)

    if mode == SkipMode.WATCH_LIST:
        return SkipDecision.SKIP if ownership.is_owned else SkipDecision.DOWNLOAD

    if mode == SkipMode.DB_AND_FOLDER:
        should_skip = on_disk or in_db
    elif mode == SkipMode.FOLDER_ONLY:
        should_skip = on_disk
    elif mode == SkipMode.DB_ONLY:
        should_skip = in_db
    else:
        return SkipDecision.DOWNLOAD

    if not should_skip:
        return SkipDecision.DOWNLOAD
    if in_db and not on_disk:
        return SkipDecision.CONFIRM_REDOWNLOAD
    return SkipDecision.SKIP


# ── Scheduled-but-not-out-yet entries ─────────────────────────────────────────
# yt-dlp live_status values meaning "no finished file exists to fetch yet".
# is_upcoming: a scheduled premiere or stream. is_live: mid-broadcast, so a
# download would capture a partial set. post_live: just ended, still being
# processed into a VOD. 'was_live' is deliberately absent — a finished
# archived stream is an ordinary downloadable video.
UNRELEASED_LIVE_STATUSES = frozenset(("is_upcoming", "is_live", "post_live"))


def is_unreleased_entry(entry, now=None):
    """True when a flat-playlist *entry* is scheduled but not downloadable yet.

    Judged on yt-dlp's live_status, falling back to a release_timestamp still
    in the future. Deliberately NOT judged on two tempting signals: the title
    (SoundCloud uses "Premiere:" for ordinary released tracks, so matching it
    would hide real music) and a missing duration (plenty of flat entries
    simply omit it). Unparseable timestamps count as released — this filter
    hides tracks, so it errs toward showing them."""
    if (entry.get("live_status") or "") in UNRELEASED_LIVE_STATUSES:
        return True
    ts = entry.get("release_timestamp")
    if ts:
        try:
            return float(ts) > (now if now is not None else time.time())
        except (TypeError, ValueError):
            return False
    return False


# ── ChannelCrate ──────────────────────────────────────────────────────────────
class ChannelCrate:
    """The single answer to "do we already have this?" for one channel folder.

    Built per channel folder per operation. The folder's normalized track-key
    index is SNAPSHOTTED at construction — one listdir, not one per entry — so
    a long batch keeps asking the same question of the same folder; build a new
    crate to see files written since. The DB and suppression memories are
    injected as oracles, so this class is testable without either.

    Matching is by exact normalized track key (see normalize_track_key). There
    is no prefix fallback anywhere: the 40-char one CrateLayout used to carry
    claimed an original as owned because a remix of it was on disk.

    Both callers cross this one interface — the scan through classify(), the
    download loop through owns() — which is what keeps the download side from
    skipping a track the scan just surfaced as new.
    """

    def __init__(self, folder, *, is_downloaded, platform="YouTube",
                 suppressed_reason=None, limit_sec=None, folder_keys=None):
        """*folder* is the channel directory (missing is fine — it indexes
        empty). *is_downloaded(video_id) -> bool* is the downloads-table
        memory; *suppressed_reason(video_id) -> reason|falsy* the
        permanently-unavailable memory (None means "never suppressed");
        *limit_sec* the Time-Limiter ceiling in seconds, or None to not judge
        duration; *platform* is used only to build a watch URL for an entry
        that carries none.

        *folder_keys* is the seam for a caller that already holds the index
        (key -> full path): given, it is taken as the snapshot and the folder is
        not read. Everyone else lets construction take the snapshot."""
        self.folder = folder or ""
        self.platform = platform
        self.folder_keys = (dict(folder_keys) if folder_keys is not None
                            else self.index_folder(self.folder))
        self._is_downloaded = is_downloaded
        self._suppressed_reason = suppressed_reason
        self._limit_sec = limit_sec

    @staticmethod
    def index_folder(folder):
        """Map every .mp3 in *folder* by normalized track key to its full path.

        First name wins, so a re-scan is stable. Keys that normalise to "" (a
        title with no alphanumerics) are dropped — they would match every other
        such title. A missing or unreadable folder indexes empty rather than
        raising: an un-downloaded-to channel simply owns nothing. Only .mp3 is
        indexed, matching what a download writes; "Keep original format" files
        are invisible here exactly as they are everywhere else today."""
        keys = {}
        if not folder:
            return keys
        try:
            for name in os.listdir(folder):
                if not name.lower().endswith(CrateLayout.AUDIO_EXT):
                    continue
                key = normalize_track_key(name)
                if key:
                    keys.setdefault(key, os.path.join(folder, name))
        except OSError:
            pass
        return keys

    def track_path(self, title):
        """The path of the file already holding *title* in this folder, or ""
        — an exact normalized-key lookup against the construction snapshot."""
        key = normalize_track_key(title)
        return self.folder_keys.get(key, "") if key else ""

    def remember(self, path):
        """Record a file the caller just wrote into this folder.

        The index is snapshotted once so a batch does one directory listing,
        which means a file written *during* the batch would otherwise stay
        invisible — and two entries in one batch whose titles normalise alike
        would both download, the second one filing a duplicate downloads row
        against the same file. First name still wins."""
        key = normalize_track_key(os.path.basename(path or ""))
        if key:
            self.folder_keys.setdefault(key, path)

    def owns(self, entry, now=None):
        """State the Ownership fact about one yt-dlp flat-playlist *entry*.

        Verdicts are tested in the scan's order, which is the canonical one:
        the downloads table first (owning a track outranks every other story
        about it), then the permanently-unavailable memory, then a premiere or
        in-progress stream, then the duration ceiling, and only then the
        folder. Evidence (in_db, path) is filled in regardless of which verdict
        won, so a caller reading the evidence rather than the verdict — see
        skip_decision — is unaffected by that ordering.

        *now* is the epoch seconds to judge a release_timestamp against
        (defaults to the wall clock; injectable for tests)."""
        vid = entry.get("id") or ""
        path = self.track_path(entry.get("title") or "")

        if vid and self._is_downloaded(vid):
            return Ownership.owned_by_id(path=path)
        if vid and self._suppressed_reason is not None:
            reason = self._suppressed_reason(vid)
            if reason:
                return Ownership.unavailable(reason, path=path)
        if is_unreleased_entry(entry, now):
            return Ownership.upcoming(path=path)
        if self._limit_sec is not None:
            dur = entry.get("duration")
            if dur and dur > self._limit_sec:
                return Ownership.too_long(path=path)
        if path:
            return Ownership.owned_on_disk(path)
        return Ownership.new()

    def classify(self, entries, now=None):
        """Bucket yt-dlp flat-playlist *entries* for a Watch List scan.

        The scan's four-bucket read of owns(): a track already in the DB or
        over the duration ceiling is dropped entirely (it is neither news nor a
        problem), and the rest are reported so the UI can count them.

        Returns {"new": [...], "on_disk": [...], "unavailable": [...],
        "upcoming": [...]} where each new item is {id, title, url,
        upload_date}, each on_disk item is {id, title, upload_date, file_path}
        (a legacy file to backfill then hide), each unavailable item is
        {id, title, reason} and each upcoming item is
        {id, title, release_timestamp}. The id is "" when the entry has none.

        Nothing is remembered about an upcoming track: once it airs, the next
        scan sees an ordinary entry and offers it normally."""
        new_entries = []
        on_disk = []
        unavailable = []
        upcoming = []
        for e in entries:
            own = self.owns(e, now=now)
            kind = own.kind
            if kind is OwnershipKind.OWNED_BY_ID or kind is OwnershipKind.TOO_LONG:
                continue
            vid_id = e.get("id")
            title = e.get("title") or ""
            if kind is OwnershipKind.UNAVAILABLE:
                unavailable.append({"id": vid_id, "title": title,
                                    "reason": own.reason})
            elif kind is OwnershipKind.UPCOMING:
                upcoming.append({
                    "id":                vid_id or "",
                    "title":             title,
                    "release_timestamp": e.get("release_timestamp") or None,
                })
            elif kind is OwnershipKind.OWNED_ON_DISK:
                on_disk.append({
                    "id":          vid_id or "",
                    "title":       title,
                    "upload_date": e.get("upload_date") or "",
                    "file_path":   own.path,
                })
            else:
                new_entries.append({
                    "id":          vid_id or "",
                    "title":       title,
                    "url":         (e.get("url") or e.get("webpage_url")
                                    or (f"https://www.youtube.com/watch?v={vid_id}"
                                        if self.platform == "YouTube" else "")),
                    "upload_date": e.get("upload_date") or "",
                })
        return {"new": new_entries, "on_disk": on_disk,
                "unavailable": unavailable, "upcoming": upcoming}


def classify_scan_entries(entries, *, is_downloaded, folder_keys, limit_sec,
                          platform, is_unavailable=None, now=None,
                          _crate_class=None):
    """Bucket yt-dlp flat-playlist *entries* into new vs already-owned tracks.

    The pure kernel of the Watch List scan, kept as a function for callers that
    already hold a folder index: a ChannelCrate over *folder_keys* answering
    classify(). Pure (no DB / tkinter / filesystem) — the DB membership check is
    injected as *is_downloaded(video_id) -> bool*, *folder_keys* maps a
    normalised track key to the path of the matching .mp3 already on disk, and
    *is_unavailable(video_id)* returns a reason string ("DRM-protected",
    "Removed", "Geo-blocked") for a track that can never be downloaded, or a
    falsy value otherwise (None means "never suppressed").

    *limit_sec* is the Time-Limiter ceiling in seconds, or None to disable
    duration filtering (a value of 0 preserves the original loop's degenerate
    behaviour of dropping every video with a positive duration). Entries with
    no/zero duration are kept (the download step re-filters as a backstop).

    See ChannelCrate.owns for the order the rules are applied in and
    ChannelCrate.classify for the buckets. *_crate_class* exists so a test can
    prove this really delegates rather than holding a second copy of the rule."""
    return (_crate_class or ChannelCrate)(
        "", is_downloaded=is_downloaded, platform=platform,
        suppressed_reason=is_unavailable, limit_sec=limit_sec,
        folder_keys=folder_keys,
    ).classify(entries, now=now)

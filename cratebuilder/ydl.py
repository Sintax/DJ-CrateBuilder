"""YdlSession: the single read-only yt-dlp boundary — probe, list, search."""
import socket
import urllib.parse
from dataclasses import dataclass

from cratebuilder import util
from cratebuilder.sidecar import channel_url_from_id


class YdlError(Exception):
    """A read-only yt-dlp call failed.

    Always carries yt-dlp's original *message* untouched, plus the intent name
    and target that produced it, so callers can log or surface the real text
    instead of a paraphrase. Never raised directly — one of the three
    subclasses below always is, so `except YdlError` catches everything while
    a caller that cares about durability can catch the specific type."""

    def __init__(self, message, intent=None, target=None):
        super().__init__(message)
        self.message = message or ""
        self.intent = intent
        self.target = target


class YdlOffline(YdlError):
    """The call failed for a reason that may well clear up on its own: no
    network, a throttled or blocked request, a server-side wobble. Also what a
    permanent-looking failure is downgraded to when the network turns out to be
    unreachable — see YdlSession._classify."""


class YdlPermanent(YdlError):
    """The target itself is gone, private, or was never a valid URL. Asking
    again changes nothing; the link has to be fixed."""


class YdlUnclassified(YdlError):
    """The failure matched no known marker. Deliberately its own type rather
    than being lumped in with either verdict — guessing is what turns one bad
    scan into a wrongly-flagged channel."""


_ERROR_TYPES = {
    "permanent": YdlPermanent,
    "transient": YdlOffline,
    "unknown": YdlUnclassified,
}

# yt-dlp handed back something that isn't an info dict. Unclassified on
# purpose: it is a failed fetch, not a verdict about the channel, so callers
# must keep what they already know rather than treat it as an empty answer.
_NO_ANSWER = "yt-dlp returned no answer"

# Stable, high-availability endpoints for the reachability probe. Mixed hosts
# and ports so a single blocked port or poisoned DNS entry can't make a live
# connection look dead.
_REACHABILITY_ENDPOINTS = (
    ("www.youtube.com", 443),
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
)


def network_is_reachable(timeout=2.0):
    """Best-effort TCP reachability probe. Tries a couple of stable, high-
    availability endpoints and returns True on the first successful connect,
    False if none answer. Never raises."""
    for host, port in _REACHABILITY_ENDPOINTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def extract_info(opts, url):
    """The default runner: hand *opts* to yt-dlp and extract *url* without
    downloading. Imported lazily so importing this module (and every test that
    injects its own runner) never needs yt-dlp installed."""
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


@dataclass(frozen=True)
class ChannelIdentity:
    """Who a URL turned out to be, read from a header-only probe.

    *channel_id* is what yt-dlp reported and nothing more — deriving an id from
    the URL when yt-dlp has none is channel-identity work, not a yt-dlp
    question, so it stays with the caller (sidecar.channel_id_from_url).
    *raw* is the full info dict, since one caller names the channel from it."""

    channel_id: str
    handle: str
    channel_url: str
    title: str
    display_name: str
    raw: dict


def _cookie_opts(cookies):
    """The session's one auth policy as yt-dlp options: empty when there is no
    CookieConfig or the user has cookies switched off."""
    if cookies is None or not cookies.use_cookies:
        return {}
    return util.build_cookie_opts(
        cookies.cookie_method,
        (cookies.cookie_file or "").strip(),
        cookies.cookies_browser,
        (cookies.cookies_profile or "").strip(),
    )


def _flatten_tabs(entries):
    """Flatten one level of yt-dlp's tab shape: a bare channel URL comes back
    as a list of tabs, each carrying its own entries list, and callers want the
    videos. Entries that are already videos pass through untouched."""
    out = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("entries"), list):
            out.extend(entry["entries"])
        else:
            out.append(entry)
    return out


class YdlSession:
    """The single module through which the app asks yt-dlp anything read-only.

    Built once per operation from a CookieConfig snapshot, then asked one or
    more *intents* — named questions, never hand-built options dicts. Three
    rules the callers used to keep (badly) live in here instead:

      Auth — one policy for the whole session, applied to every intent. A
      session either authenticates or it doesn't; no intent gets to be the
      quiet exception.

      JS runtime — enabled only on intents that extract real formats
      (probe_metadata, probe_formats, probe_thumbnail). Without it YouTube
      answers the "n" signature challenge with storyboard formats only; with
      it on a flat listing it buys nothing. Node >= 22 plus yt-dlp-ejs are
      needed, and yt-dlp falls back gracefully when they're absent.

      Captive portal — a permanent-looking failure is never trusted while the
      network is unreachable. A hotel portal answers every request with its own
      404, which would otherwise mark healthy channels dead.

    The runner, the debug sink and the reachability probe are all injectable,
    so tests never import yt-dlp and never touch the network."""

    def __init__(self, cookies=None, runner=None, debug=None,
                 network_probe=None):
        self._cookie_opts = _cookie_opts(cookies)
        self._runner = runner or extract_info
        self._debug = debug
        self._network_probe = network_probe or network_is_reachable

    def probe_metadata(self, url):
        """Fetch what the app needs to queue *url*: the info dict for a single
        track, or a flat playlist/channel one with its entries. Returns the raw
        yt-dlp info dict ({} if yt-dlp returned nothing)."""
        return self._run("probe_metadata", url,
                         self._opts(js_runtime=True,
                                    extract_flat="in_playlist"))

    def probe_formats(self, url):
        """List the formats really on offer for one track, for the bitrate
        upgrade decision. Returns the raw yt-dlp format dicts; picking a
        bitrate out of them is download policy, not a yt-dlp question."""
        info = self._run("probe_formats", url, self._opts(js_runtime=True))
        return [f for f in (info.get("formats") or []) if isinstance(f, dict)]

    def probe_thumbnail(self, url):
        """The thumbnail URL for one track, or None. ignore_no_formats_error:
        format selection runs even though only metadata is wanted, and a track
        with no servable formats would otherwise abort a lookup its thumbnail
        would have survived."""
        info = self._run("probe_thumbnail", url,
                         self._opts(js_runtime=True,
                                    ignore_no_formats_error=True))
        return info.get("thumbnail") or None

    def probe_identity(self, url):
        """Ask who *url* belongs to, reading the playlist header only
        (playlist_items "0" fetches no entries at all). Returns a
        ChannelIdentity, whose fields are empty strings when yt-dlp said
        nothing — the intent never raises just for a blank answer."""
        info = self._run("probe_identity", url,
                         self._opts(extract_flat="in_playlist",
                                    playlist_items="0"))
        return ChannelIdentity(
            channel_id=info.get("channel_id") or "",
            handle=info.get("uploader_id") or "",
            channel_url=info.get("channel_url") or "",
            title=info.get("title") or "",
            display_name=util.derive_collection_name(info),
            raw=info,
        )

    def list_channel(self, url, ignore_no_formats=False):
        """Enumerate a channel's uploads, lazily and flat.

        Returns the RAW yt-dlp entry dicts on purpose: the scan classifier, the
        cleanup partitioner and the artwork index all read yt-dlp's own fields,
        so the schema is part of this interface. *ignore_no_formats* is for the
        artwork backfill, which wants the listing even from a channel whose
        videos serve no formats."""
        extra = {"extract_flat": "in_playlist", "lazy_playlist": True}
        if ignore_no_formats:
            extra["ignore_no_formats_error"] = True
        return self._run(
            "list_channel", url, self._opts(**extra), require_answer=True,
            shape=lambda info: _flatten_tabs(info.get("entries") or []))

    def search_channels(self, name, max_results=3):
        """Search YouTube for a channel by display name. Returns up to
        *max_results* candidate dicts {title, channel_id, url, handle,
        followers}. Rows whose id is not a real UC channel id are dropped —
        YouTube's channel-filtered results still carry the odd video row."""
        query = urllib.parse.quote(name)
        # sp=EgIQAg%3D%3D  →  YouTube's "Channel" search-results filter.
        search_url = (f"https://www.youtube.com/results?search_query={query}"
                      f"&sp=EgIQAg%3D%3D")
        info = self._run("search_channels", search_url,
                         self._opts(extract_flat=True,
                                    playlist_items=f"1-{max_results}"))
        out = []
        for entry in (info.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            cid = entry.get("channel_id") or entry.get("id") or ""
            if not str(cid).startswith("UC"):
                continue
            out.append({
                "title": entry.get("title") or entry.get("channel") or name,
                "channel_id": cid,
                "url": channel_url_from_id(cid),
                "handle": entry.get("uploader_id") or "",
                "followers": entry.get("channel_follower_count"),
            })
            if len(out) >= max_results:
                break
        return out

    def search_soundcloud_tracks(self, name, limit=20):
        """Search SoundCloud tracks by name. Returns {url, title} dicts, where
        the title is the uploader — each permalink's first path segment is the
        artist handle, which is what the caller is really after. Entries with
        no usable URL are dropped rather than yielding a blank candidate."""
        info = self._run("search_soundcloud_tracks", f"scsearch{limit}:{name}",
                         self._opts(extract_flat=True))
        out = []
        for entry in (info.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or entry.get("permalink_url")
                   or entry.get("webpage_url") or "")
            if not url:
                continue
            out.append({
                "url": url,
                "title": (entry.get("uploader")
                          or entry.get("channel") or "").strip(),
            })
        return out

    def _opts(self, js_runtime=False, **extra):
        """Build one intent's options: the read-only base, the intent's own
        keys, then the session's single auth policy."""
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        opts.update(extra)
        opts.update(self._cookie_opts)
        if js_runtime:
            opts["js_runtimes"] = {"node": {"path": None}}
            # Fallback for machines without the local yt-dlp-ejs package: let
            # yt-dlp fetch the solver scripts from GitHub on demand.
            opts.setdefault("remote_components", ["ejs:github"])
        return opts

    def _run(self, intent, target, opts, shape=None, require_answer=False):
        """Log, call the runner, shape the result, and turn any failure into a
        typed error.

        *shape* reads the fields an intent wants, and runs inside the same
        guard as the call: a lazily paginated listing does its real fetching
        while being enumerated, so shaping it anywhere else would let a
        mid-pagination failure escape untyped.

        Returns {} when yt-dlp answers with a non-dict, so intents can read
        fields off the result without guarding. *require_answer* refuses that
        instead — for intents where "no answer" and "nothing to report" must
        not look alike, such as a channel listing whose emptiness would be
        read as the channel having no uploads."""
        self._log(intent, opts)
        try:
            info = self._runner(opts, target)
        except Exception as exc:
            raise self._classify(exc, intent, target) from exc
        if not isinstance(info, dict):
            if require_answer:
                raise YdlUnclassified(_NO_ANSWER, intent=intent, target=target)
            info = {}
        if shape is None:
            return info
        try:
            return shape(info)
        except Exception as exc:
            raise self._classify(exc, intent, target) from exc

    def _classify(self, exc, intent, target):
        """Pick the typed error for a failure, applying the captive-portal
        rule: a permanent verdict reached while the network is unreachable is
        downgraded to offline, because a portal's canned 404 is evidence about
        the portal, not the channel."""
        message = str(exc)
        verdict = util.classify_ydl_error(message)
        if verdict == "permanent" and not self._network_probe():
            verdict = "transient"
        return _ERROR_TYPES[verdict](message, intent=intent, target=target)

    def _log(self, intent, opts):
        """Write one redacted options line per intent to the debug sink, which
        may be a logger or a plain callable. No sink, no logging."""
        if self._debug is None:
            return
        line = f"YDL OPTS ({intent}) | {util.redact_ydl_opts(opts)}"
        if callable(self._debug):
            self._debug(line)
        else:
            self._debug.info(line)

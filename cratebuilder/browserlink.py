"""djcrate:// URI parsing for browser-extension sends (Tk-free).

Wire contract: the extension repo's docs/specs/djcrate-uri-v1.md. Parsing is
total — any input returns a result object, never an exception — because a
malformed URI must never take down the singleton listener thread.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qs

ACCEPTED_HOSTS = {"www.youtube.com", "soundcloud.com"}

MSG_NEWER = ("This send came from a newer version of the CrateBuilder "
             "extension — update DJ-CrateBuilder.")
MSG_BAD_URL = "That link isn't a supported YouTube or SoundCloud URL."

THEN_VALUES = ("batch", "download")


@dataclass(frozen=True)
class BrowserSend:
    kind: str   # 'channel' | 'track'
    url: str    # decoded canonical https URL
    then: str | None = None   # 'batch' | 'download' — a track's right-click choice


@dataclass(frozen=True)
class ParseError:
    reason: str    # 'newer' | 'bad_url' | 'ignore'
    message: str   # user-facing text; '' when reason == 'ignore'


def parse_djcrate_uri(uri):
    """Parse a djcrate:// URI into a BrowserSend, or a ParseError saying how
    to react: 'newer' and 'bad_url' carry a message to show the user,
    'ignore' means drop it silently (wrong scheme or verb — not ours)."""
    try:
        if not isinstance(uri, str):
            return ParseError("ignore", "")
        split = urlsplit(uri.strip())
        if split.scheme != "djcrate" or split.netloc != "add":
            return ParseError("ignore", "")
        params = parse_qs(split.query)
        version = params.get("v", [None])[0]
        kind = params.get("kind", [None])[0]
        if version != "1" or kind not in ("channel", "track"):
            return ParseError("newer", MSG_NEWER)
        url = params.get("url", [None])[0]
        if not url:
            return ParseError("bad_url", MSG_BAD_URL)
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname not in ACCEPTED_HOSTS:
            return ParseError("bad_url", MSG_BAD_URL)
        then = params.get("then", [None])[0]
        # Contract §1.1: only a track carries a choice, and a value this
        # build does not know is a plain send — the link itself is valid.
        if kind != "track" or then not in THEN_VALUES:
            then = None
        return BrowserSend(kind=kind, url=url, then=then)
    except Exception:
        return ParseError("ignore", "")

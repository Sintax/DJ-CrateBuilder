"""Anonymised bug-report bundles: log tails, scrubbing, and the GitHub issue link."""
import os
import re
import zipfile
from urllib.parse import quote, urlencode

TAIL_BYTES = 512 * 1024
BODY_LIMIT = 6000
_TOKEN = re.compile(r"(token=)[^\s&\"']+", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# The local part may only start where a word run begins, or a long blob
# (base64, a JWT) is re-scanned from every position inside it.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")
# Logs carry paths three ways: as typed, repr()-escaped (doubled backslashes),
# and URL-encoded (%5C / %2F) — one separator pattern has to cover all three.
# No quantifier inside the group: a nested + backtracks exponentially. A
# leading separator may only start where a run begins, or a long run is
# re-scanned from every position inside it.
_SEP = r"(?:[\\/]|%5c|%2f)+"
_LEAD_SEP = r"(?<![\\/])(?<!%5c)(?<!%2f)" + _SEP
_WORD_START = r"(?:(?<![\w-])|(?<=%5c)|(?<=%2f))"
_WORD_END = r"(?:(?![\w-])|(?=%5c)|(?=%2f))"


def tail_bytes(path, limit=TAIL_BYTES):
    """The last *limit* bytes of *path* as text, starting on a whole line."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
    except OSError:
        return ""
    if size > limit:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl != -1 else data
    return data.decode("utf-8", errors="replace")


def _segments(path):
    """The non-empty folder names of *path*, whichever slash it uses."""
    return [p for p in re.split(r"[\\/]+", (path or "").strip()) if p]


def _segment_pattern(segment):
    """A regex matching one folder name as typed or URL-encoded."""
    return "(?:%s|%s)" % (re.escape(segment), re.escape(quote(segment, safe="")))


def _path_pattern(path):
    """A regex matching *path* in any of the forms a log can carry it."""
    lead = _LEAD_SEP if re.match(r"\s*[\\/]", path or "") else ""
    return lead + _SEP.join(_segment_pattern(p) for p in _segments(path))


def _word_pattern(word):
    """A regex matching *word* on its own, including between path separators."""
    return _WORD_START + re.escape(word) + _WORD_END


def scrub_text(text, *, home, base_dir, username, cookie_file=None):
    """Replace anything that identifies the machine or person. Longest
    paths first so <LIBRARY> wins over <HOME> where they nest, and longest
    names first so a short login cannot leave the rest of a folder name
    behind; e-mails before names so a name in the local part cannot expose
    the domain."""
    out = text or ""
    for value, tag in ((cookie_file, "<COOKIE_FILE>"),
                       (base_dir, "<LIBRARY>"), (home, "<HOME>")):
        if _segments(value):
            out = re.sub(_path_pattern(value), tag, out, flags=re.IGNORECASE)
    out = _EMAIL.sub("<EMAIL>", out)
    names = {n.strip().lower(): n.strip()
             for n in (username, *_segments(home)[-1:]) if n and n.strip()}
    for name in sorted(names.values(), key=len, reverse=True):
        out = re.sub(_word_pattern(name), "<USER>", out, flags=re.IGNORECASE)
    out = _TOKEN.sub(r"\1<redacted>", out)
    out = _IPV4.sub("<IP>", out)
    return out


def system_block(*, app_version, app_build, platform, python, transport):
    """The fixed header of a report: app build, OS, Python and window type."""
    return (f"App: DJ-CrateBuilder {app_version} build {app_build}\n"
            f"OS: {platform}\nPython: {python}\nWindow: {transport}\n")


def issue_url(base_url, title, body):
    """A prefilled new-issue link; the body is capped so the URL stays openable."""
    body = (body or "")[:BODY_LIMIT] + "\n\n[log bundle attached]"
    return base_url + "?" + urlencode({"title": title or "", "body": body})


def build_bundle(zip_path, files):
    """Write each {name: text} into a deflated zip at *zip_path*."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text or "")

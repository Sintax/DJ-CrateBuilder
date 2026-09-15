"""Anonymised bug-report bundles: log tails, scrubbing, and the GitHub issue link."""
import os
import re
import zipfile
from urllib.parse import urlencode

TAIL_BYTES = 512 * 1024
BODY_LIMIT = 6000
_TOKEN = re.compile(r"(token=)[^\s&\"']+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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


def _both_slashes(path):
    """A regex matching *path* with either slash in every separator."""
    parts = re.split(r"[\\/]+", path.strip())
    return r"[\\/]".join(re.escape(p) for p in parts if p)


def scrub_text(text, *, home, base_dir, username, cookie_file=None):
    """Replace anything that identifies the machine or person. Longest
    paths first so <LIBRARY> wins over <HOME> where they nest."""
    out = text or ""
    for value, tag in ((cookie_file, "<COOKIE_FILE>"),
                       (base_dir, "<LIBRARY>"), (home, "<HOME>")):
        if value and value.strip():
            out = re.sub(_both_slashes(value), tag, out, flags=re.IGNORECASE)
    if username and username.strip():
        out = re.sub(r"(?<![\w-])" + re.escape(username) + r"(?![\w-])",
                     "<USER>", out, flags=re.IGNORECASE)
    out = _TOKEN.sub(r"\1<redacted>", out)
    out = _EMAIL.sub("<EMAIL>", out)
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

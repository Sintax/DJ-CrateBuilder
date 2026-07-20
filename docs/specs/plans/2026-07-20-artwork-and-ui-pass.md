# Artwork + UI Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cover art work when "Keep original format" is on, stop Rebuild Database from Files orphaning and duplicating artwork, and land five UI cleanups across the Watch List, About, Settings and Main tabs.

**Architecture:** Two new pure-logic units go into `cratebuilder/` (`rebuild.py`, plus new functions in `artwork.py` and `tagging.py`) so the container dispatch and the rebuild resolution rules are headless-testable. The monolith `DJ-CrateBuilder_v1.3.py` keeps all tkinter and threading and is edited in place — no extraction. Cover art gains a per-container dispatcher; WebM is losslessly remuxed to Opus because Matroska attachments are unwritable by mutagen.

**Tech Stack:** Python 3.10+, tkinter, mutagen 1.48.0 (already a runtime dep — `mutagen.mp4`, `mutagen.oggopus`, `mutagen.flac.Picture` all verified importable), Pillow, FFmpeg (bundled in packaged builds, on PATH from source), pytest.

**Spec:** `docs/specs/2026-07-20-artwork-and-ui-pass-design.md`

## Global Constraints

- **Do not bump `APP_VERSION`** — it stays `"1.3"`. **Do not bump `APP_BUILD`** — it is owned by `scripts/release.py`.
- **No tkinter imports in `cratebuilder/`.** That package is a pure-logic boundary keeping tests headless.
- **No `cratebuilder.db` schema change.** All three artwork columns (`artwork_path`, `artwork_embedded`, `thumbnail_url`) already exist from the v4 migration at `cratebuilder/db.py:67-69`. `SCHEMA_VERSION` stays `3` in the constant and is not touched.
- **Never raise from artwork or tagging code.** Every function in `cratebuilder/artwork.py` and `cratebuilder/tagging.py` returns `False`/`None` on failure. An artwork failure must never fail a download.
- **`cratebuilder/` modules use one-line module docstrings.** The monolith uses substantial multi-line docstrings and Unicode box-drawing dividers (`# ══════════…`). Match whichever file you are editing.
- **No new comments for new code** unless the *why* is non-obvious. Do not touch existing docstrings.
- **Do not add dependencies.** Everything needed is already installed.
- **Rebuild must never write or delete an artwork file.** It only reads existing sidecars and re-points DB rows. This is the core guarantee of Task 6 and is asserted by test.
- **Baseline test state: `249 passed, 1 failed`.** `tests/test_settings_vars.py::test_new_settings_defaults` fails at line 21 because `_run_at_startup` reads the Windows registry, which the test's `HOME`/`USERPROFILE` monkeypatching does not isolate. This failure is pre-existing and environmental. Do not fix it, and do not report it as caused by this work.
- **Commit after every task.** Conventional Commits: `type(scope): subject`, imperative, ~70 chars.
- **Never push, tag, or open a PR** without an explicit ask.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `cratebuilder/artwork.py` | Add per-container cover embedding + WebM remux + dispatcher. Existing `embed_cover()` untouched. | 1, 2 |
| `cratebuilder/tagging.py` | Add MP4 and Vorbis-comment text tagging beside the existing ID3 path. | 3 |
| `cratebuilder/rebuild.py` | **New.** Audio extension set, `video_id` recovery from tags, local-only artwork resolution. | 5 |
| `DJ-CrateBuilder_v1.3.py` | Wire the above into the download path and the rebuild button; all five UI changes. | 4, 6, 7, 8, 9 |
| `tests/test_artwork.py` | Extended — per-container round-trips, dispatcher table. | 1, 2 |
| `tests/test_tagging.py` | Extended — MP4/Vorbis text tags. | 3 |
| `tests/test_rebuild.py` | **New.** Recovery, resolution branches, and the no-write/no-delete guarantee. | 5 |

**Task order matters.** Tasks 1-3 are pure-logic and independent of each other. Task 4 depends on 1-3. Task 5 depends on 3 (it reads the tags Task 3 writes). Task 6 depends on 5. Tasks 7-9 are UI-only and independent of everything.

---

## Task 1: Per-container cover embedding (MP4 + Ogg)

**Files:**
- Modify: `cratebuilder/artwork.py` — add after `embed_cover()` (ends line 172)
- Test: `tests/test_artwork.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `embed_cover_mp4(audio_path, jpg_path) -> bool`, `embed_cover_ogg(audio_path, jpg_path) -> bool`. Both mirror `embed_cover`'s contract exactly: return `True` only if the file was changed, `False` on any failure, never raise.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_artwork.py`. Fixtures are generated with FFmpeg because a hand-rolled byte literal cannot produce a valid MP4 or Ogg container.

```python
import shutil
import subprocess

_FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None, reason="FFmpeg not on PATH")


def _make_silent(path, codec, seconds=1):
    """Generate a real, valid audio file so mutagen has a container to tag."""
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(seconds),
         "-c:a", codec, str(path)],
        check=True)
    return str(path)


@requires_ffmpeg
def test_embed_cover_mp4_round_trips(tmp_path):
    from mutagen.mp4 import MP4
    audio = _make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    assert artwork.embed_cover_mp4(audio, jpg) is True

    covers = MP4(audio).tags.get("covr")
    assert covers and len(bytes(covers[0])) > 0


@requires_ffmpeg
def test_embed_cover_ogg_round_trips(tmp_path):
    from mutagen.oggopus import OggOpus
    audio = _make_silent(tmp_path / "t.opus", "libopus")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    assert artwork.embed_cover_ogg(audio, jpg) is True

    assert OggOpus(audio).get("metadata_block_picture")


@requires_ffmpeg
def test_embed_cover_mp4_replaces_rather_than_stacks(tmp_path):
    from mutagen.mp4 import MP4
    audio = _make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    artwork.embed_cover_mp4(audio, jpg)
    artwork.embed_cover_mp4(audio, jpg)

    assert len(MP4(audio).tags.get("covr")) == 1


def test_embed_cover_mp4_rejects_wrong_extension(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    assert artwork.embed_cover_mp4(audio, jpg) is False


def test_embed_cover_ogg_missing_image_is_false(tmp_path):
    assert artwork.embed_cover_ogg(str(tmp_path / "nope.opus"),
                                   str(tmp_path / "nope.jpg")) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_artwork.py -q -k "mp4 or ogg"`
Expected: FAIL with `AttributeError: module 'cratebuilder.artwork' has no attribute 'embed_cover_mp4'`

- [ ] **Step 3: Add the imports**

At the top of `cratebuilder/artwork.py`, after the existing `mutagen.id3` try/except block (lines 13-16), add:

```python
try:
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    MP4 = None

try:
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    Picture = None
```

Add `import base64` to the stdlib import block at the top of the file (beside `import os`), **not** inside the try — it is stdlib and can never fail to import.

- [ ] **Step 4: Implement both functions**

Add after `embed_cover()` (after line 172):

```python
MP4_EXTS = (".m4a", ".mp4", ".m4b")
OGG_EXTS = (".opus", ".ogg", ".oga")


def embed_cover_mp4(audio_path, jpg_path):
    """Embed *jpg_path* as the cover atom on the MP4/M4A at *audio_path*.

    MP4 stores cover art in the `covr` atom of the iTunes metadata box, not in
    an ID3 frame — a completely different mechanism from `embed_cover`. Assigning
    a single-element list replaces any existing art rather than appending, so
    re-embedding never accumulates duplicates.

    Returns True if the file was changed, False otherwise. Never raises.
    """
    if MP4 is None:
        return False
    if not audio_path or not audio_path.lower().endswith(MP4_EXTS):
        return False
    if not os.path.isfile(audio_path):
        return False
    if not jpg_path or not os.path.isfile(jpg_path):
        return False
    try:
        with open(jpg_path, "rb") as fh:
            data = fh.read()
        if not data:
            return False
        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True
    except Exception:
        return False


def embed_cover_ogg(audio_path, jpg_path):
    """Embed *jpg_path* as the cover picture on the Ogg file at *audio_path*.

    Ogg (Opus and Vorbis) carries art as a base64-encoded FLAC picture block in
    the `metadata_block_picture` Vorbis comment. The comment is overwritten
    wholesale, so re-embedding replaces the art.

    Returns True if the file was changed, False otherwise. Never raises.
    """
    if Picture is None:
        return False
    if not audio_path or not audio_path.lower().endswith(OGG_EXTS):
        return False
    if not os.path.isfile(audio_path):
        return False
    if not jpg_path or not os.path.isfile(jpg_path):
        return False
    try:
        with open(jpg_path, "rb") as fh:
            data = fh.read()
        if not data:
            return False

        pic = Picture()
        pic.data = data
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.depth = 24
        # Dimensions are advisory in a FLAC picture block; 0x0 is legal. Reading
        # them needs Pillow, so a stripped install still embeds working art.
        pic.width, pic.height = 0, 0
        if Image is not None:
            try:
                with Image.open(jpg_path) as im:
                    pic.width, pic.height = im.size
            except Exception:
                pass

        opener = OggOpus if audio_path.lower().endswith(".opus") else OggVorbis
        audio = opener(audio_path)
        audio["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
        return True
    except Exception:
        return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_artwork.py -q`
Expected: PASS, all tests in the file green.

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/artwork.py tests/test_artwork.py
git commit -m "feat(artwork): embed cover art into MP4 and Ogg containers"
```

---

## Task 2: WebM remux + container dispatcher

**Files:**
- Modify: `cratebuilder/artwork.py` — add after `embed_cover_ogg()` from Task 1
- Test: `tests/test_artwork.py`

**Interfaces:**
- Consumes: `embed_cover_mp4`, `embed_cover_ogg`, `MP4_EXTS`, `OGG_EXTS` from Task 1; `embed_cover` (existing, line 131).
- Produces:
  - `remux_webm_to_opus(audio_path, ffmpeg_dir=None) -> str | None` — returns the new `.opus` path on success, `None` on failure.
  - `embed_cover_any(audio_path, jpg_path, ffmpeg_dir=None) -> tuple[str, bool]` — returns `(final_audio_path, embedded)`. The path may differ from the input when a remux happened. **Task 4 depends on this exact two-tuple shape.**

- [ ] **Step 1: Write the failing tests**

```python
@requires_ffmpeg
def test_remux_webm_to_opus_produces_opus_and_removes_source(tmp_path):
    webm = _make_silent(tmp_path / "t.webm", "libopus")
    out = artwork.remux_webm_to_opus(webm)
    assert out is not None
    assert out.lower().endswith(".opus")
    assert os.path.isfile(out)
    assert not os.path.exists(webm)


@requires_ffmpeg
def test_embed_cover_any_webm_remuxes_then_embeds(tmp_path):
    from mutagen.oggopus import OggOpus
    webm = _make_silent(tmp_path / "t.webm", "libopus")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")

    path, embedded = artwork.embed_cover_any(webm, jpg)

    assert embedded is True
    assert path.lower().endswith(".opus")
    assert OggOpus(path).get("metadata_block_picture")


@requires_ffmpeg
def test_embed_cover_any_m4a_keeps_path(tmp_path):
    audio = _make_silent(tmp_path / "t.m4a", "aac")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(audio, jpg)
    assert embedded is True
    assert path == audio


def test_embed_cover_any_mp3_uses_apic(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(audio, jpg)
    assert embedded is True
    assert path == audio
    assert ID3(audio).getall("APIC")


def test_embed_cover_any_unknown_extension_is_noop(tmp_path):
    audio = tmp_path / "t.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    jpg = _make_image(tmp_path / "art.jpg", fmt="JPEG")
    path, embedded = artwork.embed_cover_any(str(audio), jpg)
    assert embedded is False
    assert path == str(audio)


def test_embed_cover_any_missing_image_is_noop(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    path, embedded = artwork.embed_cover_any(audio, None)
    assert embedded is False
    assert path == audio
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_artwork.py -q -k "remux or any"`
Expected: FAIL with `AttributeError: module 'cratebuilder.artwork' has no attribute 'remux_webm_to_opus'`

- [ ] **Step 3: Add imports**

At the top of `cratebuilder/artwork.py`, add to the stdlib import block (after `import os`, line 3):

```python
import shutil
import subprocess
import sys
```

- [ ] **Step 4: Implement remux and dispatcher**

```python
WEBM_EXTS = (".webm", ".mkv")

# Windows: keep the FFmpeg console window from flashing on every remux.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# A remuxed Opus stream shorter than this is a truncated or failed write, not a
# real track. Guards against deleting the source in favour of a broken output.
_MIN_REMUX_BYTES = 1024


def _ffmpeg_exe(ffmpeg_dir=None):
    """Absolute path to ffmpeg, or None when it cannot be found.

    Prefers *ffmpeg_dir* (the bundled folder in a packaged build) and falls back
    to PATH, mirroring how the app points yt-dlp at FFmpeg.
    """
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if ffmpeg_dir:
        cand = os.path.join(str(ffmpeg_dir), name)
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg")


def remux_webm_to_opus(audio_path, ffmpeg_dir=None):
    """Rewrap the Opus stream inside a WebM file into an Ogg container.

    WebM stores cover art as a Matroska attachment element, which mutagen cannot
    write at all. Ogg carries it as a Vorbis comment, which mutagen can. The
    audio is stream-copied (`-c:a copy`), so this is lossless and fast — no
    re-encode, the samples are bit-identical.

    The source is deleted only after the output exists and is plausibly sized,
    so a failed remux leaves the original file intact.

    Returns the new `.opus` path, or None when FFmpeg is unavailable or the
    remux failed. Never raises.
    """
    if not audio_path or not audio_path.lower().endswith(WEBM_EXTS):
        return None
    if not os.path.isfile(audio_path):
        return None

    exe = _ffmpeg_exe(ffmpeg_dir)
    if not exe:
        return None

    out_path = os.path.splitext(audio_path)[0] + ".opus"
    tmp_path = out_path + ".part"
    try:
        proc = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", audio_path,
             "-vn", "-c:a", "copy", "-f", "ogg", tmp_path],
            capture_output=True, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg exited non-zero")
        if os.path.getsize(tmp_path) < _MIN_REMUX_BYTES:
            raise RuntimeError("remux output implausibly small")

        os.replace(tmp_path, out_path)
        os.remove(audio_path)
        return out_path
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None


def embed_cover_any(audio_path, jpg_path, ffmpeg_dir=None):
    """Embed *jpg_path* into *audio_path*, whatever container it is in.

    Dispatches on extension: MP3 uses the ID3 APIC frame, MP4/M4A the `covr`
    atom, Ogg/Opus a Vorbis-comment picture block. WebM has no writable art
    mechanism, so it is first remuxed to Opus (lossless, see
    `remux_webm_to_opus`) and then embedded — which is why the audio path can
    change.

    Returns (final_audio_path, embedded). The path is the input path unless a
    remux occurred. Never raises.
    """
    if not audio_path:
        return audio_path, False
    if not jpg_path or not os.path.isfile(jpg_path):
        return audio_path, False

    lower = audio_path.lower()
    if lower.endswith(".mp3"):
        return audio_path, embed_cover(audio_path, jpg_path)
    if lower.endswith(MP4_EXTS):
        return audio_path, embed_cover_mp4(audio_path, jpg_path)
    if lower.endswith(OGG_EXTS):
        return audio_path, embed_cover_ogg(audio_path, jpg_path)
    if lower.endswith(WEBM_EXTS):
        remuxed = remux_webm_to_opus(audio_path, ffmpeg_dir)
        if not remuxed:
            return audio_path, False
        return remuxed, embed_cover_ogg(remuxed, jpg_path)
    return audio_path, False
```

- [ ] **Step 5: Run the full artwork suite**

Run: `python -m pytest tests/test_artwork.py -q`
Expected: PASS. Confirm the pre-existing `embed_cover` tests still pass — its signature and MP3-only behaviour are unchanged.

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/artwork.py tests/test_artwork.py
git commit -m "feat(artwork): add WebM->Opus remux and container dispatcher"
```

---

## Task 3: MP4 and Vorbis text tagging

**Files:**
- Modify: `cratebuilder/tagging.py`
- Test: `tests/test_tagging.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `write_track_tags_any(path, title=None, source_url=None, encoded_by=ENCODED_BY, overwrite=False) -> bool`. Same signature as the existing `write_track_tags`. **Task 5 depends on the source URL being retrievable from MP4 and Ogg files**, which is what this task makes true.

Rationale: `recover_video_id` in Task 5 reads the source URL back off the file. Without this task, non-MP3 files carry no source URL and rebuild cannot recover their `video_id`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tagging.py` (reuse the `_make_silent` / `requires_ffmpeg` helpers — if they are not importable, duplicate them at the top of this file):

```python
@requires_ffmpeg
def test_write_tags_any_mp4_round_trips(tmp_path):
    from mutagen.mp4 import MP4
    audio = _make_silent(tmp_path / "t.m4a", "aac")
    assert tagging.write_track_tags_any(
        audio, title="Track", source_url="https://youtu.be/abc123") is True
    tags = MP4(audio).tags
    assert tags["\xa9nam"] == ["Track"]
    assert "https://youtu.be/abc123" in tags["\xa9cmt"][0]


@requires_ffmpeg
def test_write_tags_any_ogg_round_trips(tmp_path):
    from mutagen.oggopus import OggOpus
    audio = _make_silent(tmp_path / "t.opus", "libopus")
    assert tagging.write_track_tags_any(
        audio, title="Track", source_url="https://youtu.be/abc123") is True
    tags = OggOpus(audio)
    assert tags["title"] == ["Track"]
    assert tags["comment"] == ["https://youtu.be/abc123"]


def test_write_tags_any_mp3_delegates_to_id3(tmp_path):
    from mutagen.id3 import ID3
    audio = _make_mp3(tmp_path / "t.mp3")
    assert tagging.write_track_tags_any(
        audio, title="Track", source_url="https://youtu.be/abc123") is True
    assert ID3(audio).getall("TIT2")


def test_write_tags_any_unknown_extension_is_false(tmp_path):
    p = tmp_path / "t.wav"
    p.write_bytes(b"RIFF0000WAVE")
    assert tagging.write_track_tags_any(str(p), title="X") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tagging.py -q -k "any"`
Expected: FAIL with `AttributeError: module 'cratebuilder.tagging' has no attribute 'write_track_tags_any'`

- [ ] **Step 3: Add imports**

At the top of `cratebuilder/tagging.py`, after the existing `mutagen.id3` try/except:

```python
try:
    from mutagen.mp4 import MP4
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    MP4 = None

try:
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
except ImportError:  # pragma: no cover - mutagen is a runtime dep
    OggOpus = None

MP4_EXTS = (".m4a", ".mp4", ".m4b")
OGG_EXTS = (".opus", ".ogg", ".oga")
```

- [ ] **Step 4: Implement**

Add at the end of `cratebuilder/tagging.py`:

```python
def write_track_tags_any(path, title=None, source_url=None,
                         encoded_by=ENCODED_BY, overwrite=False):
    """Write our standard tags onto *path*, whatever container it is in.

    MP3 delegates to `write_track_tags`. MP4 uses the iTunes atoms
    (`\\xa9nam` title, `\\xa9too` encoder, `\\xa9cmt` comment); Ogg uses the
    Vorbis comments (`title`, `encoder`, `comment`). The comment field carries
    the source URL in every container, which is what lets a database rebuild
    recover a track's video id from the file itself.

    *overwrite* False leaves an existing field alone, matching
    `write_track_tags`, so this is safe to run as a bulk backfill.

    Returns True if the file was changed, False otherwise. Never raises.
    """
    if not path or not os.path.isfile(path):
        return False

    lower = path.lower()
    if lower.endswith(".mp3"):
        return write_track_tags(path, title=title, source_url=source_url,
                                encoded_by=encoded_by, overwrite=overwrite)

    if lower.endswith(MP4_EXTS):
        if MP4 is None:
            return False
        fields = (("\xa9nam", title), ("\xa9too", encoded_by),
                  ("\xa9cmt", source_url))
    elif lower.endswith(OGG_EXTS):
        if OggOpus is None:
            return False
        fields = (("title", title), ("encoder", encoded_by),
                  ("comment", source_url))
    else:
        return False

    try:
        if lower.endswith(MP4_EXTS):
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            container = audio.tags
        else:
            opener = OggOpus if lower.endswith(".opus") else OggVorbis
            audio = opener(path)
            container = audio

        changed = False
        for key, value in fields:
            if not value:
                continue
            if not overwrite and container.get(key):
                continue
            container[key] = [value]
            changed = True

        if changed:
            audio.save()
        return changed
    except Exception:
        return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tagging.py -q`
Expected: PASS, including the pre-existing MP3 tests.

- [ ] **Step 6: Commit**

```bash
git add cratebuilder/tagging.py tests/test_tagging.py
git commit -m "feat(tagging): write title/encoder/source tags to MP4 and Ogg"
```

---

## Task 4: Wire multi-format artwork into the download path

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:4539-4584` (`_harvest_cover_art`)
- Modify: `DJ-CrateBuilder_v1.3.py:8940-8965` (the download call site)
- Modify: `DJ-CrateBuilder_v1.3.py` around `:9022-9065` (the retry call site — verify the exact lines before editing)

**Interfaces:**
- Consumes: `cb_artwork.embed_cover_any(path, jpg, ffmpeg_dir) -> (str, bool)` from Task 2; `cb_tagging.write_track_tags_any(...)` from Task 3.
- Produces: `_harvest_cover_art()` now returns a **3-tuple** `(art_path, embedded, final_audio_path)` instead of the current 2-tuple. Both call sites must be updated in the same commit or the app breaks.

This task has **no automated test** — it is monolith glue verified by launching the app. Say so plainly; do not claim test coverage for it.

- [ ] **Step 1: Change `_harvest_cover_art` to use the dispatcher**

In `DJ-CrateBuilder_v1.3.py`, replace the body from line 4574 (`embedded = cb_artwork.embed_cover(audio_path, art_path)`) through line 4581 (`return art_path, embedded`) with:

```python
            final_path, embedded = cb_artwork.embed_cover_any(
                audio_path, art_path, bundled_ffmpeg_dir())
            if embedded:
                self._dbg.debug(f"COVER ART     | {title!r}  {art_path}")
            else:
                self._dbg.debug(
                    f"COVER SIDECAR | {title!r}  saved, not embedded "
                    f"(unsupported container or tag write failed)")
            if final_path != audio_path:
                self._dbg.info(
                    f"REMUX         | {title!r}  {os.path.basename(audio_path)}"
                    f" -> {os.path.basename(final_path)}")
            return art_path, embedded, final_path
```

Update the `except` at line 4582 to return the 3-tuple:

```python
        except Exception as exc:  # pragma: no cover - defensive
            self._dbg.warning(f"COVER FAIL    | {title!r}  {exc}")
            return None, False, audio_path
```

Update the two early returns at lines 4557 and 4562 and 4567 and 4572 to `return None, False, audio_path` as well. Update the docstring's "Returns (artwork_path, embedded)" line to describe the third element.

- [ ] **Step 2: Update the primary download call site**

At `DJ-CrateBuilder_v1.3.py:8949-8950`, replace:

```python
                    _art_path, _art_embedded = self._harvest_cover_art(
                        _real_path, entry.get("id"), item_title)
```

with:

```python
                    _art_path, _art_embedded, _real_path = (
                        self._harvest_cover_art(
                            _real_path, entry.get("id"), item_title))
```

Then change the `file_path` argument in the `add_download` call at line 8960 from `expected_path` to `_real_path`, so a remuxed `.opus` is recorded under its real name rather than the stale `.webm` one.

- [ ] **Step 3: Update the retry call site**

Find the second `_harvest_cover_art` call (near `:9022-9065`). Apply the identical change: unpack three values, and pass the returned path as `file_path` to `add_download`.

Run this to locate every call site and confirm none was missed:

```bash
grep -n "_harvest_cover_art" DJ-CrateBuilder_v1.3.py
```

Expected: the `def` plus exactly two call sites, both now unpacking three values.

- [ ] **Step 4: Switch tagging to the dispatcher**

At `DJ-CrateBuilder_v1.3.py:8945`, `self._tag_track(_real_path, item_title, item_url)` runs *before* the artwork harvest, when the file may still be `.webm`. Inside `_tag_track`, change the `cb_tagging.write_track_tags(...)` call to `cb_tagging.write_track_tags_any(...)`.

Then, in `_harvest_cover_art`, after a remux has occurred, re-tag the new file — the Ogg container does not inherit the WebM tags:

```python
            if final_path != audio_path:
                cb_tagging.write_track_tags_any(
                    final_path, title=title, source_url=source_url)
```

This requires `_harvest_cover_art` to accept `source_url`. Add it as a keyword parameter defaulting to `None`, and pass `item_url` from both call sites.

- [ ] **Step 5: Verify the app still starts and the suite is unchanged**

Run: `python -m pytest -q`
Expected: `264 passed, 1 failed` — the baseline 249 plus the 15 tests added by Tasks 1-3 (5 + 6 + 4). Task 4 adds no tests of its own. Any *new* failure is a regression from this task.

Run: `python DJ-CrateBuilder_v1.3.py`
Expected: the app launches, all four tabs render. Close it.

- [ ] **Step 6: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(download): embed cover art for non-MP3 keep-original files"
```

- [ ] **Step 7: Manual end-to-end check (report honestly)**

With **Keep original format** checked, download one short YouTube track. Confirm:
1. The resulting file is `.opus` (not `.webm`).
2. It shows cover art in Windows Explorer.
3. `debug.log` contains a `REMUX` line and a `COVER ART` line.

If this cannot be run (no network, no FFmpeg), state that explicitly rather than claiming it works.

---

## Task 5: `cratebuilder/rebuild.py` — recovery and resolution

**Files:**
- Create: `cratebuilder/rebuild.py`
- Test: `tests/test_rebuild.py` (create)

**Interfaces:**
- Consumes: `cratebuilder.artwork.ARTWORK_DIR_NAME`, `artwork.has_cover`, `artwork.artwork_key`; `cratebuilder.tagging` (indirectly — reads tags Task 3 writes).
- Produces:
  - `AUDIO_EXTS: tuple[str, ...]`
  - `recover_video_id(path) -> str | None`
  - `index_artwork_dir(channel_dir) -> dict[str, str]` — maps sidecar stem to full JPEG path. Listed once per channel folder and cached by the caller.
  - `resolve_artwork(path, video_id, art_index, snapshot=None) -> tuple[str | None, int, str | None]` — returns `(artwork_path, artwork_embedded, thumbnail_url)`, matching the three DB columns and the shape `get_artwork_by_path()` already returns. **Task 6 depends on this exact 3-tuple.**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rebuild.py`:

```python
"""Tests for cratebuilder.rebuild: audio discovery + artwork reassociation."""
import os

import pytest

from cratebuilder import rebuild

pytest.importorskip("mutagen")

from mutagen.id3 import ID3, COMM, WOAS  # noqa: E402

_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _make_mp3(path, source_url=None):
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    if source_url:
        tags = ID3()
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[source_url]))
        tags.add(WOAS(url=source_url))
        tags.save(str(path), v2_version=3)
    return str(path)


def _make_art(art_dir, stem):
    os.makedirs(art_dir, exist_ok=True)
    p = os.path.join(art_dir, f"{stem}.jpg")
    with open(p, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    return p


def test_audio_exts_covers_keep_original_formats():
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".flac", ".wav"):
        assert ext in rebuild.AUDIO_EXTS


def test_recover_video_id_from_youtube_watch_url(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3",
                  "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert rebuild.recover_video_id(p) == "dQw4w9WgXcQ"


def test_recover_video_id_from_short_url(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3", "https://youtu.be/dQw4w9WgXcQ")
    assert rebuild.recover_video_id(p) == "dQw4w9WgXcQ"


def test_recover_video_id_untagged_file_is_none(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3")
    assert rebuild.recover_video_id(p) is None


def test_recover_video_id_soundcloud_url_is_none(tmp_path):
    p = _make_mp3(tmp_path / "t.mp3",
                  "https://soundcloud.com/artist/some-track")
    assert rebuild.recover_video_id(p) is None


def test_resolve_artwork_prefers_video_id_sidecar(tmp_path):
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    expected = _make_art(art_dir, "dQw4w9WgXcQ")
    _make_art(art_dir, "Some Track")

    index = rebuild.index_artwork_dir(str(tmp_path))
    path, embedded, thumb = rebuild.resolve_artwork(
        audio, "dQw4w9WgXcQ", index)

    assert path == expected


def test_resolve_artwork_falls_back_to_filename_stem(tmp_path):
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    expected = _make_art(art_dir, "Some Track")

    index = rebuild.index_artwork_dir(str(tmp_path))
    path, embedded, thumb = rebuild.resolve_artwork(audio, None, index)

    assert path == expected


def test_resolve_artwork_uses_snapshot_when_no_sidecar(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    index = rebuild.index_artwork_dir(str(tmp_path))
    snapshot = {audio: ("/old/art.jpg", 1, "https://img/x.jpg")}

    path, embedded, thumb = rebuild.resolve_artwork(
        audio, None, index, snapshot=snapshot)

    assert path == "/old/art.jpg"
    assert embedded == 1
    assert thumb == "https://img/x.jpg"


def test_resolve_artwork_no_art_anywhere_is_blank(tmp_path):
    audio = _make_mp3(tmp_path / "t.mp3")
    index = rebuild.index_artwork_dir(str(tmp_path))
    assert rebuild.resolve_artwork(audio, None, index) == (None, 0, None)


def test_resolve_artwork_never_writes_or_deletes(tmp_path):
    """The core guarantee: rebuild only reads. It must not touch the disk."""
    audio = _make_mp3(tmp_path / "Some Track.mp3")
    art_dir = os.path.join(str(tmp_path), ".artwork")
    _make_art(art_dir, "Some Track")

    def snapshot_tree():
        seen = {}
        for root, _dirs, files in os.walk(str(tmp_path)):
            for f in files:
                fp = os.path.join(root, f)
                seen[fp] = os.path.getsize(fp)
        return seen

    before = snapshot_tree()
    index = rebuild.index_artwork_dir(str(tmp_path))
    rebuild.resolve_artwork(audio, "dQw4w9WgXcQ", index)
    rebuild.resolve_artwork(audio, None, index)

    assert snapshot_tree() == before


def test_index_artwork_dir_missing_folder_is_empty(tmp_path):
    assert rebuild.index_artwork_dir(str(tmp_path)) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rebuild.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cratebuilder.rebuild'`

- [ ] **Step 3: Create the module**

Create `cratebuilder/rebuild.py`:

```python
"""Rebuild the downloads table from disk: audio discovery + artwork reuse."""
import os
import re

from cratebuilder import artwork as _artwork

AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".oga", ".flac",
              ".wav", ".mp4", ".m4b")

# The two YouTube URL shapes _tag_track writes into the comment/WOAS fields.
# SoundCloud URLs carry no id in the path, so they deliberately do not match —
# those tracks fall through to the filename-stem sidecar lookup instead.
_YT_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})"),
)


def _source_url(path):
    """Read the source URL our tagger stamped on *path*, or None.

    Checks the ID3 COMM/WOAS frames on an MP3 and the equivalent comment field
    on MP4 and Ogg. Never raises.
    """
    lower = path.lower()
    try:
        if lower.endswith(".mp3"):
            from mutagen.id3 import ID3
            tags = ID3(path)
            for frame in tags.getall("WOAS"):
                if getattr(frame, "url", None):
                    return frame.url
            for frame in tags.getall("COMM"):
                text = (getattr(frame, "text", None) or [None])[0]
                if text:
                    return text
            return None
        if lower.endswith((".m4a", ".mp4", ".m4b")):
            from mutagen.mp4 import MP4
            tags = MP4(path).tags or {}
            vals = tags.get("\xa9cmt") or []
            return vals[0] if vals else None
        if lower.endswith((".opus", ".ogg", ".oga")):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            opener = OggOpus if lower.endswith(".opus") else OggVorbis
            vals = opener(path).get("comment") or []
            return vals[0] if vals else None
    except Exception:
        return None
    return None


def recover_video_id(path):
    """Recover a track's YouTube video id from the tags on the file itself.

    A rebuild derives every row from disk, so without this the video_id column
    is None for every track — which breaks the `<video_id>.jpg` artwork key and
    makes the backfill re-fetch art it already has, writing a second identical
    JPEG under the filename stem. Reading the source URL our own tagger wrote
    keeps the key stable across a rebuild.

    Returns the 11-character id, or None when the file carries no source URL or
    the URL is not a YouTube one. Never raises.
    """
    if not path or not os.path.isfile(path):
        return None
    url = _source_url(path)
    if not url:
        return None
    for pattern in _YT_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def index_artwork_dir(channel_dir):
    """Map sidecar stem -> JPEG path for one channel folder's `.artwork/`.

    Listed once per channel folder rather than once per track: a 5,000-track
    rebuild does one listdir per channel, not five thousand.

    Returns {} when the folder does not exist. Never raises.
    """
    if not channel_dir:
        return {}
    art_dir = os.path.join(str(channel_dir), _artwork.ARTWORK_DIR_NAME)
    if not os.path.isdir(art_dir):
        return {}
    index = {}
    try:
        for name in os.listdir(art_dir):
            stem, ext = os.path.splitext(name)
            if ext.lower() in (".jpg", ".jpeg"):
                index[stem] = os.path.join(art_dir, name)
    except OSError:
        return {}
    return index


def resolve_artwork(path, video_id, art_index, snapshot=None):
    """Find the cover art already on disk for *path*. Reads only.

    Resolution order, all local — no network, and nothing is ever written or
    deleted:
      1. `.artwork/<video_id>.jpg`
      2. `.artwork/<filename-stem>.jpg`  (art left by an earlier rebuild)
      3. art embedded in the file itself
      4. the pre-wipe database snapshot, keyed by exact file path
      5. nothing — left blank for the Fetch Missing Artwork button

    Returns (artwork_path, artwork_embedded, thumbnail_url), matching the three
    downloads columns. Never raises.
    """
    snap = (snapshot or {}).get(path) or (None, 0, None)

    if video_id and video_id in art_index:
        return art_index[video_id], snap[1], snap[2]

    stem_key = _artwork.artwork_key(None, path)
    if stem_key and stem_key in art_index:
        return art_index[stem_key], snap[1], snap[2]

    try:
        if _artwork.has_cover(path):
            return snap[0], 1, snap[2]
    except Exception:
        pass

    return snap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rebuild.py -q`
Expected: PASS, all 11 tests.

- [ ] **Step 5: Commit**

```bash
git add cratebuilder/rebuild.py tests/test_rebuild.py
git commit -m "feat(rebuild): add video-id recovery and local artwork resolution"
```

---

## Task 6: Wire rebuild into the monolith, on a background thread

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:11539-11617` (`_rebuild_db_from_files`)
- Modify: `DJ-CrateBuilder_v1.3.py:6222-6226` (the help tooltip, which says ".mp3 files")

**Interfaces:**
- Consumes: `cratebuilder.rebuild.AUDIO_EXTS`, `recover_video_id`, `index_artwork_dir`, `resolve_artwork` from Task 5.
- Produces: nothing consumed by later tasks.

No automated test — this is monolith glue. Verified by launching the app and clicking the button.

- [ ] **Step 1: Add the import**

Find the existing `from cratebuilder import ...` / `import cratebuilder.x as cb_x` block near the top of `DJ-CrateBuilder_v1.3.py` (the same block that provides `cb_artwork`). Add the module alongside it, matching whatever style that block uses:

```python
from cratebuilder import rebuild as cb_rebuild
```

- [ ] **Step 2: Replace the file loop with the multi-format, artwork-aware version**

In `_rebuild_db_from_files`, replace lines 11578-11607 (from `for name in sorted(os.listdir(channel_path)):` through the closing `})` of the row dict) with:

```python
                    art_index = cb_rebuild.index_artwork_dir(channel_path)

                    for name in sorted(os.listdir(channel_path)):
                        if not name.lower().endswith(cb_rebuild.AUDIO_EXTS):
                            continue
                        full = os.path.join(channel_path, name)
                        try:
                            mtime = int(os.path.getmtime(full))
                        except OSError:
                            continue
                        # Upload date / downloaded time aren't recorded in the
                        # file itself, so fall back to the file's mtime.
                        date_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d")
                        # Recovering the id keeps the <video_id>.jpg artwork key
                        # stable, which is what stops the backfill writing a
                        # second identical JPEG under the filename stem.
                        vid = cb_rebuild.recover_video_id(full)
                        art_path, art_embedded, thumb_url = (
                            cb_rebuild.resolve_artwork(
                                full, vid, art_index, snapshot=art_snapshot))
                        rows.append({
                            "video_id":     vid,
                            "title":        os.path.splitext(name)[0],
                            "channel_name": channel_name,
                            "channel_url":  channel_url,
                            "channel_id":   channel_id,
                            "platform":     platform,
                            "genre":        genre,
                            "file_path":    full,
                            "upload_date":  date_str,
                            "ts":           mtime,
                            "bitrate":      "",
                            "artwork_path":     art_path,
                            "artwork_embedded": art_embedded,
                            "thumbnail_url":    thumb_url,
                        })
```

Update the method docstring: it currently says ".mp3 files" — change to "audio files".

- [ ] **Step 3: Move the scan onto a background thread**

`recover_video_id` opens every file to read tags, so the loop is now too slow for the main thread. Restructure `_rebuild_db_from_files` so that:

1. The `askokcancel` confirmation and `get_artwork_by_path()` snapshot stay on the main thread.
2. The platform/genre/channel walk moves into a nested `def _work():` run on a `threading.Thread(daemon=True)`.
3. `clear_all_downloads()`, `backfill_downloads(rows)`, `refresh_watchlist_totals()`, the `showinfo` and the `self._dbg.info` line are marshalled back to the main thread via `self.after(0, ...)`.
4. `self._rebuild_db_btn.config(state="disabled")` before starting and `state="normal"` in the completion callback, so the button cannot be double-clicked.

Follow the threading and progress-dialog shape already used by `_fetch_missing_artwork` at `DJ-CrateBuilder_v1.3.py:11481` — read that method first and mirror it rather than inventing a new pattern.

- [ ] **Step 4: Update the button help text**

At `DJ-CrateBuilder_v1.3.py:6222-6226`, change `"Scans the .mp3 files already in your library folders"` to `"Scans the audio files already in your library folders"`, and append a sentence: `"Cover art already on disk is reused, never re-downloaded."`

- [ ] **Step 5: Verify**

Run: `python -m pytest -q`
Expected: `275 passed, 1 failed` — 264 after Task 4, plus the 11 tests Task 5 added in `tests/test_rebuild.py`. Task 6 adds no tests of its own. Confirm the count went up and the failure list did not.

Run: `python DJ-CrateBuilder_v1.3.py`, go to Settings, click **Rebuild Database from Files**, confirm the dialog, and check that:
1. The UI stays responsive while it runs.
2. The completion dialog reports a plausible track count.
3. Opening the Database Viewer shows cover art still attached to tracks that had it.
4. No new `.jpg` files appeared in any `.artwork/` folder.

- [ ] **Step 6: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "fix(rebuild): preserve artwork and index non-MP3 audio files"
```

---

## Task 7: Watch List card — Cancel colour and button tooltips

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:207` (add a constant)
- Modify: `DJ-CrateBuilder_v1.3.py:9622-9673` (`_watchlist_fill_card` button block)

**Interfaces:** none — UI only. No automated test; tooltip hover and live enabled-state cannot be asserted headlessly. Report as manually verified.

- [ ] **Step 1: Add the module-level constant**

At `DJ-CrateBuilder_v1.3.py`, immediately after line 207 (`WL_CANCEL_IDLE = "#5e1414"`), add:

```python
WL_CANCEL_ACTIVE = YT_DARK   # live cancel on a card — matches the toolbar
```

Verify `YT_DARK` is defined above line 207 (it is, at line 198). If not, use the literal `"#cc2222"`.

- [ ] **Step 2: Replace the inert Cancel branch**

In `_watchlist_fill_card`, delete the local constant `WL_CARD_CANCEL = "#78350f"` at line 9648 and replace the cancel-button branch at lines 9650-9657 so the button is live when that card is busy:

```python
            if is_cancel:
                b = tk.Button(btns, text=label, command=cmd,
                              font=("Segoe UI", 9, "bold"),
                              relief="flat", bd=0, cursor="hand2",
                              bg=WL_CANCEL_ACTIVE, fg=TEXT,
                              activebackground=YT_RED, activeforeground=TEXT,
                              padx=10, pady=3)
                Tooltip(b, "Stop the scan or download running on this channel.")
```

The button is only appended to `card_buttons` when `is_scanning or is_downloading` (line 9626), so it is inherently only present while there is something to cancel — no `state` juggling is needed. Confirm this by reading lines 9615-9627 before editing.

- [ ] **Step 3: Attach tooltips to Scan / Force Download / Edit**

The `card_buttons` tuples at lines 9622-9644 are `(label, command, is_cancel)`. Add a fourth element carrying tooltip text, defaulting to `None` for the buttons not being changed, then attach it in the creation loop.

Change the three target entries to:

```python
            (f"🔍 Scan", lambda c=cid: self._watchlist_scan_channel(c), False,
             "Check this channel for new uploads without downloading anything."),
            ("⚡ Force Download",
             lambda c=cid: self._watchlist_force_download(c), False,
             "Re-download every track from this channel, including ones "
             "already in your library."),
            ("✏ Edit", lambda c=cid: self._watchlist_edit_channel(c), False,
             "Change this channel's genre, platform, or download settings."),
```

Give every other tuple in the list a trailing `None` so the unpacking stays uniform, update the loop's unpacking at line 9650 from `for label, cmd, is_cancel in card_buttons:` to `for label, cmd, is_cancel, tip in card_buttons:`, and at the end of the loop body — after `b.pack(...)` at line 9673 — add:

```python
                if tip:
                    Tooltip(b, tip)
```

Read lines 9622-9673 in full before editing; the exact lambda text above must match what is already there apart from the added element.

- [ ] **Step 4: Verify**

Run: `python -m pytest -q`
Expected: unchanged from Task 6.

Run: `python DJ-CrateBuilder_v1.3.py`, open the **Watch List** tab. Confirm:
1. Hovering Scan, Force Download and Edit each shows a tooltip after ~500ms.
2. No question-mark icons were added to the cards.
3. Starting a scan makes a red Cancel button appear on that card, and clicking it stops the scan.

- [ ] **Step 5: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "feat(watchlist): live red cancel button and card button tooltips"
```

---

## Task 8: About tab cleanup

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:80-84` (`ABOUT_FIELDS`)
- Modify: `DJ-CrateBuilder_v1.3.py:7364-7376` and `:7417-7423`

**Interfaces:** none — UI only.

- [ ] **Step 1: Remove the redundant Application entry**

Delete line 81 from `ABOUT_FIELDS`:

```python
    ("Application",  f"{APP_NAME}  v{APP_VERSION_FULL}"),
```

It renders the identical string already shown by the tab's own title label at line 7337. The render loop at line 7409 is data-driven, so no other change is needed.

- [ ] **Step 2: Move the GitHub button to the left column**

Cut the `self._github_btn` creation and its tooltip (lines 7364-7370) out of `btn_col`, and re-insert them into `info_col` immediately **before** the `self._issues_btn` block at line 7417. Change the parent from `btn_col` to `info_col` and the pack from `anchor="e", pady=(0,6)` to `anchor="w", pady=(4,6)`.

**Do not reorder the `btn_col.pack()` / `info_col.pack()` calls at lines 7362 and 7407.** `btn_col` must stay packed first to claim the right edge — the comment at 7358-7360 explains this, and swapping them breaks the two-column layout.

- [ ] **Step 3: Retune the now-top spacing in the right column**

`self._update_status_var`'s label at line 7376 packs with `pady=(22, 4)`. That 22px was spacing it below the GitHub button, which is now gone, so it is the top widget in `btn_col`. Change to `pady=(0, 4)`.

- [ ] **Step 4: Verify**

Run: `python DJ-CrateBuilder_v1.3.py`, open the **About** tab. Confirm:
1. "Application  DJ-CrateBuilder v1.3" no longer appears twice — only the title heading remains.
2. "View on GitHub" now sits directly above "Submit Issues / Suggestions" in the left column.
3. The right column contains only the update box, top-aligned with no stray gap.
4. The two-column layout has not collapsed.

- [ ] **Step 5: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "style(about): drop redundant app row, group link buttons left"
```

---

## Task 9: Settings and Main tab layout

**Files:**
- Modify: `DJ-CrateBuilder_v1.3.py:5715-5716`, `:5755-5790` (Settings)
- Modify: `DJ-CrateBuilder_v1.3.py:5414-5431` (Main tab genre row)
- Modify: `DJ-CrateBuilder_v1.3.py:5436-5437`, `:5755` (stale comments)

**Interfaces:** none — UI only.

- [ ] **Step 1: Rename the section title**

At line 5715, change `text="Audio Output"` to `text="File Output"`.

- [ ] **Step 2: Swap the Cover Art and Skip rows**

Move the whole `cover_row` block (lines 5771-5790) to sit **before** the `skip_row` block (lines 5755-5768). These are sequential `pack()` calls into `outer`, so source order is display order — cut and paste the blocks, changing nothing inside them.

- [ ] **Step 3: Unbold the Skip checkbutton**

At line 5758-5760, change `style="S.Bold.TCheckbutton"` to `style="S.Opt.TCheckbutton"`.

This matches every other Settings option row and follows the file's own guidance at lines 5033-5035, which states `S.Bold` is for the Main and Watch List tabs.

- [ ] **Step 4: Remove Open Folder from the Skip row**

Delete lines 5767-5768:

```python
        ttk.Button(skip_row, text="📂  Open Folder", style="MainBrowse.TButton",
                   command=self._open_download_dir).pack(side="right")
```

- [ ] **Step 5: Add the genre-folder handler**

Add a new method next to `_open_download_dir` (which is at line 5547):

```python
    def _open_genre_dir(self):
        """Open the currently selected genre's folder in the system file manager."""
        genre = self._genre_var.get()
        folder = "_No Genre" if not genre or genre == "(none)" else genre
        target = os.path.join(self._platform_dir(), folder)
        os.makedirs(target, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showerror(
                "Could Not Open Folder",
                f"Unable to open the folder:\n{exc}\n\n"
                f"Path: {target}"
            )
```

The `"_No Genre"` mapping matches the convention already used at lines 3145 and 4347.

- [ ] **Step 6: Add both buttons to the genre row**

Insert after line 5429 (the `+ New` button) and before `self._refresh_genre_list()` at line 5431:

```python
        # Root is declared first: with pack(side="right") the first-declared
        # child sits furthest right, so this reads Genre, Root left-to-right.
        ttk.Button(genre_row, text="📂  Root", style="MainBrowse.TButton",
                   command=self._open_download_dir).pack(side="right")
        ttk.Button(genre_row, text="📂  Genre", style="MainBrowse.TButton",
                   command=self._open_genre_dir).pack(side="right", padx=(0, 6))
```

- [ ] **Step 7: Update the two stale comments**

Lines 5436-5437 and 5755 both claim Skip / Open Folder were relocated *out* of the Main tab into Settings. Open Folder has now moved back. Correct both comments to describe the current arrangement.

- [ ] **Step 8: Verify**

Run: `python -m pytest -q`
Expected: unchanged from Task 6. Note `tests/test_tabs.py` and `tests/test_settings_vars.py` construct the app — if either newly fails, a widget reference was broken.

Run: `python DJ-CrateBuilder_v1.3.py`. Confirm:
1. **Settings** — the section reads "File Output"; Cover Art appears above Skip; "Skip files already downloaded" is no longer bold-white and matches its neighbours; no Open Folder button remains on the Skip row.
2. **Main** — the genre line has 📂 Genre then 📂 Root right-aligned. Clicking Genre opens the selected genre's folder; with genre "(none)" it opens `_No Genre`. Clicking Root opens the platform root.

- [ ] **Step 9: Commit**

```bash
git add DJ-CrateBuilder_v1.3.py
git commit -m "style(settings): rename File Output, reorder rows, move folder buttons"
```

---

## Final verification

- [ ] Run the full suite: `python -m pytest -q`. Expected `275 passed, 1 failed` (249 baseline + 26 new: 5 + 6 + 4 + 11), where the single failure is the pre-existing `test_new_settings_defaults` registry issue documented in Global Constraints. **Report the actual output, not the expected output.** If the count differs, say so and investigate rather than rounding to the expectation.
- [ ] Launch `python DJ-CrateBuilder_v1.3.py` and walk all four tabs once.
- [ ] Confirm `git log --oneline` shows nine task commits plus the spec commit on `feat/artwork-and-ui-pass`.
- [ ] **Do not push, tag, or open a PR.** Report completion and wait for an explicit ask.
- [ ] Report honestly which items were verified by test, which by manual launch, and which could not be verified (the Task 4 end-to-end download in particular, if no network or FFmpeg was available).

"""Shared fixtures. Loads the app's pure-logic functions.

Phase 0/1: `cb` is the single-file app module loaded via importlib.
After extraction, individual tests import from `cratebuilder.*` directly;
this loader remains as a fallback for code still living in the main file.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_ROOT, "DJ-CrateBuilder_v1.3.py")


def _load_main():
    spec = importlib.util.spec_from_file_location("cb_main", _MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cb_main"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def cb():
    return _load_main()


import shutil
import subprocess

_FFMPEG = shutil.which("ffmpeg")

# Marks a test that needs a real audio container. A hand-rolled byte literal
# cannot produce a valid MP4 or Ogg file, so these tests generate one.
requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None, reason="FFmpeg not on PATH")


def make_silent(path, codec, seconds=1):
    """Generate a real, valid silent audio file for tagging tests."""
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds),
         "-c:a", codec, str(path)],
        check=True)
    return str(path)

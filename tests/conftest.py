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


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Redirect the config file to a temp path for load/save tests."""
    cfg = tmp_path / "config.json"
    return cfg

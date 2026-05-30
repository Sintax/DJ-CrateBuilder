import importlib.util, os, sys, pytest

def _mod():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("cb_main", os.path.join(root, "DJ-CrateBuilder_v1.3.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["cb_main"] = m
    spec.loader.exec_module(m); return m

def test_auto_check_hours_to_seconds():
    m = _mod()
    f = m.auto_check_hours_to_seconds
    assert f("Off") is None
    assert f("6 hours") == 6 * 3600
    assert f("24 hours") == 24 * 3600
    assert f("nonsense") is None
    # Real runtime inputs from an unset/blank StringVar must be safe.
    assert f("") is None
    assert f(None) is None

def test_interval_label_to_seconds(cb_mod):
    f = cb_mod.interval_label_to_seconds
    assert f("Off") is None
    assert f("6 hours") == 6 * 3600
    assert f("12 hours") == 12 * 3600
    assert f("1 day") == 86400
    assert f("2 days") == 2 * 86400
    assert f("3 days") == 3 * 86400
    assert f("1 week") == 7 * 86400
    assert f("nonsense") is None
    # Real runtime inputs from an unset/blank StringVar must be safe.
    assert f("") is None
    assert f(None) is None

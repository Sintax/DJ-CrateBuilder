import importlib.util, logging, os, sys

_CACHED = None


def _module():
    global _CACHED
    if _CACHED is None:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "cb_main", os.path.join(root_dir, "DJ-CrateBuilder_v1.3.py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules["cb_main"] = m
        spec.loader.exec_module(m)
        _CACHED = m
    return _CACHED


def _handler(path, max_bytes):
    m = _module()
    h = m._HeadTrimFileHandler(str(path), max_bytes=max_bytes, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _emit(h, msg):
    h.emit(logging.LogRecord("t", logging.INFO, __file__, 0, msg, None, None))


# ── Label <-> megabyte parsing ────────────────────────────────────────────────

def test_log_limit_label_roundtrip():
    App = _module().MP3DownloaderApp
    assert App._log_limit_label(0) == "Unlimited"
    assert App._log_limit_label(4) == "4MB"
    assert App._parse_log_limit_mb("Unlimited") == 0
    assert App._parse_log_limit_mb("4MB") == 4
    assert App._parse_log_limit_mb("15MB") == 15
    # Every dropdown choice round-trips back to a clean megabyte count.
    for label in App._LOG_LIMIT_CHOICES:
        assert App._log_limit_label(App._parse_log_limit_mb(label)) == label
    # Junk falls back to unlimited rather than raising.
    assert App._parse_log_limit_mb("") == 0
    assert App._parse_log_limit_mb("garbage") == 0


# ── Head-trimming behaviour ───────────────────────────────────────────────────

def test_trims_oldest_lines_when_over_cap(tmp_path):
    # Cap small so a handful of lines blows past it; the newest line must
    # survive and the very oldest must be gone.
    h = _handler(tmp_path / "a.log", max_bytes=2000)
    try:
        for i in range(2000):
            _emit(h, f"line {i:05d} " + "x" * 40)
    finally:
        h.close()

    data = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert os.path.getsize(tmp_path / "a.log") <= 2000
    # Newest content retained, oldest dropped from the top.
    assert "line 01999" in data
    assert "line 00000" not in data
    # File starts on a clean line boundary (no partial leading line).
    assert not data.startswith("x")
    assert data.splitlines()[0].startswith("line ")


def test_unlimited_never_trims(tmp_path):
    h = _handler(tmp_path / "u.log", max_bytes=0)
    try:
        for i in range(500):
            _emit(h, f"line {i}")
    finally:
        h.close()
    data = (tmp_path / "u.log").read_text(encoding="utf-8")
    # Nothing is dropped when unlimited — the first line is still present.
    assert "line 0\n" in data
    assert "line 499" in data


def test_oversized_file_trimmed_on_open(tmp_path):
    # A log that was already huge before a cap existed gets trimmed the moment
    # the capped handler opens it (covers the 'on startup' trim).
    p = tmp_path / "old.log"
    p.write_text("".join(f"old {i:05d}\n" for i in range(5000)),
                 encoding="utf-8")
    assert os.path.getsize(p) > 4000

    h = _handler(p, max_bytes=2000)
    try:
        assert os.path.getsize(p) <= 2000
        data = p.read_text(encoding="utf-8")
        # Kept the newest tail, dropped the oldest head.
        assert "old 04999" in data
        assert "old 00000" not in data
    finally:
        h.close()


def test_lowering_cap_trims_via_maybe_trim(tmp_path):
    # Simulates the dropdown lowering the limit at runtime: bump max_bytes down
    # then call maybe_trim(), as _autosave_log_limit does.
    p = tmp_path / "live.log"
    h = _handler(p, max_bytes=0)   # start unlimited
    try:
        for i in range(3000):
            _emit(h, f"line {i:05d}")
        assert os.path.getsize(p) > 2000
        h.max_bytes = 1500
        h.maybe_trim()
        assert os.path.getsize(p) <= 1500
        data = p.read_text(encoding="utf-8")
        assert "line 02999" in data
        assert "line 00000" not in data
    finally:
        h.close()

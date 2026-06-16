import importlib.util, os, sys, tkinter as tk, pytest

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


def _root():
    try:
        r = tk.Tk()
        r.withdraw()
        return r
    except Exception as e:  # headless CI
        pytest.skip(f"no display: {e}")


def test_viewer_backfills_missing_timestamps(tmp_path, monkeypatch):
    # Config is read/written by the viewer; keep it off the real one.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    m = _module()

    track = tmp_path / "Old Track.mp3"
    track.write_bytes(b"x")   # a real file so its creation time is readable
    db = m.DownloadsDatabase(str(tmp_path / "t.db"))
    db.backfill_downloads([dict(
        video_id=None, title="Old Track", channel_name="C",
        channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
        genre="DnB", file_path=str(track), upload_date="", ts=0, bitrate="")])

    root = _root()
    try:
        v = m.DatabaseViewerWindow(root, db)
        v.update()
        # The in-memory row was filled from the file's creation time...
        d = next(x for x in v._downloads if x["title"] == "Old Track")
        assert int(d["download_timestamp"]) > 0
        # ...and the fill was persisted back to the database.
        row = next(r for r in db.get_all_downloads()
                   if r["title"] == "Old Track")
        assert int(row["download_timestamp"]) > 0
        v.destroy()
    finally:
        root.destroy()


def test_viewer_column_order_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    m = _module()
    db = m.DownloadsDatabase(str(tmp_path / "t.db"))

    root = _root()
    try:
        v = m.DatabaseViewerWindow(root, db)
        v.update()
        cols = list(v._WL_COLS)
        new_order = [cols[2]] + cols[:2] + cols[3:]   # move 3rd column to front
        v._save_col_order(v._WL_ORDER_KEY, new_order)
        v._wl_tree.configure(displaycolumns=new_order)
        v.destroy()

        # Reopening restores the saved order.
        v2 = m.DatabaseViewerWindow(root, db)
        v2.update()
        assert list(v2._wl_tree.cget("displaycolumns")) == new_order
        v2.destroy()
    finally:
        root.destroy()

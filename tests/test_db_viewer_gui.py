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


def test_reorder_columns_truth_table():
    # Pure logic (static method) — no display needed. Dropping src onto tgt must
    # land src on tgt's ORIGINAL visual slot, symmetric in both directions. This
    # locks down the rightward-drag off-by-one: reading the target index after
    # removing src used to make rightward drags land one column short.
    reorder = _module().DatabaseViewerWindow._reorder_columns
    base = ["a", "b", "c", "d"]
    expected = {
        ("a", "b"): ["b", "a", "c", "d"],
        ("a", "c"): ["b", "c", "a", "d"],
        ("a", "d"): ["b", "c", "d", "a"],
        ("b", "a"): ["b", "a", "c", "d"],
        ("b", "c"): ["a", "c", "b", "d"],
        ("b", "d"): ["a", "c", "d", "b"],
        ("c", "a"): ["c", "a", "b", "d"],
        ("c", "b"): ["a", "c", "b", "d"],
        ("c", "d"): ["a", "b", "d", "c"],
        ("d", "a"): ["d", "a", "b", "c"],
        ("d", "b"): ["a", "d", "b", "c"],
        ("d", "c"): ["a", "b", "d", "c"],
    }
    for (src, tgt), want in expected.items():
        got = reorder(base, src, tgt)
        assert got == want, f"{src}->{tgt}: got {got}, want {want}"
        # src always ends up exactly where tgt started (the visual drop slot).
        assert got.index(src) == base.index(tgt)
        # Result is always a permutation of the input — no columns lost/dupes.
        assert sorted(got) == sorted(base)


def test_reorder_columns_single_step_is_one_column():
    # The reported bug: a one-column rightward drag must advance exactly one
    # slot (previously it took two drags to move one column).
    reorder = _module().DatabaseViewerWindow._reorder_columns
    base = ["a", "b", "c", "d"]
    assert reorder(base, "a", "b").index("a") == 1   # right by one
    assert reorder(base, "b", "a").index("b") == 0   # left by one


def test_reorder_columns_edge_cases():
    reorder = _module().DatabaseViewerWindow._reorder_columns
    base = ["a", "b", "c", "d"]
    # Drop onto the non-reorderable tree column (tgt_name None) -> src to front.
    assert reorder(base, "c", None) == ["c", "a", "b", "d"]
    # Unknown src is a safe no-op (returns an unchanged copy).
    assert reorder(base, "zzz", "a") == base
    # The returned list is a copy, never the same object (caller compares them).
    assert reorder(base, "a", "b") is not base


def test_expand_all_restripes_leaf_rows(tmp_path, monkeypatch):
    # Expand All sets `open` programmatically, which does NOT fire
    # <<TreeviewOpen>>; the stripes must still be recomputed so the now-visible
    # leaf rows alternate background instead of all sharing one tag.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    m = _module()
    db = m.DownloadsDatabase(str(tmp_path / "t.db"))
    # Several leaves under one group so striping has something to alternate.
    db.backfill_downloads([
        dict(video_id=None, title=f"Track {i}", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path=f"/x/Track {i}.mp3", upload_date="",
             ts=1000 + i, bitrate="")
        for i in range(4)
    ])

    root = _root()
    try:
        v = m.DatabaseViewerWindow(root, db)
        v.update()
        v._expand_all()
        v.update()

        tree = v._dl_tree
        leaf_stripes = []

        def walk(node):
            for it in tree.get_children(node):
                tags = tree.item(it, "tags")
                if "leaf" in tags:
                    # exactly one stripe tag is applied to each visible leaf
                    assert ("oddrow" in tags) ^ ("evenrow" in tags), tags
                    leaf_stripes.append(
                        "odd" if "oddrow" in tags else "even")
                if tree.get_children(it) and \
                        v.tk.getboolean(tree.item(it, "open")):
                    walk(it)

        walk("")
        assert len(leaf_stripes) == 4
        # Adjacent visible leaves alternate — not all the same tag.
        assert all(a != b for a, b in zip(leaf_stripes, leaf_stripes[1:]))
        v.destroy()
    finally:
        root.destroy()


def test_viewer_trees_own_mousewheel_binding(tmp_path, monkeypatch):
    # Each viewer tree binds <MouseWheel> itself and returns "break", so wheel
    # scrolling stays inside the viewer instead of bubbling up to the main
    # app's application-wide bind_all handler and scrolling the primary window.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    m = _module()
    db = m.DownloadsDatabase(str(tmp_path / "t.db"))

    root = _root()
    try:
        v = m.DatabaseViewerWindow(root, db)
        v.update()
        # A non-empty bind script means the handler is installed on the widget.
        assert v._dl_tree.bind("<MouseWheel>")
        assert v._wl_tree.bind("<MouseWheel>")
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

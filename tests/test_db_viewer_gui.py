def test_viewer_backfills_missing_timestamps(cb_mod, tmp_path, app):
    track = tmp_path / "Old Track.mp3"
    track.write_bytes(b"x")   # a real file so its creation time is readable
    db = cb_mod.DownloadsDatabase(str(tmp_path / "t.db"))
    db.backfill_downloads([dict(
        video_id=None, title="Old Track", channel_name="C",
        channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
        genre="DnB", file_path=str(track), upload_date="", ts=0, bitrate="")])

    v = cb_mod.DatabaseViewerWindow(app, db)
    v.update()
    # The in-memory row was filled from the file's creation time...
    d = next(x for x in v._downloads if x["title"] == "Old Track")
    assert int(d["download_timestamp"]) > 0
    # ...and the fill was persisted back to the database.
    row = next(r for r in db.get_all_downloads()
               if r["title"] == "Old Track")
    assert int(row["download_timestamp"]) > 0


def test_reorder_columns_truth_table(cb_mod):
    # Pure logic (static method) — no display needed. Dropping src onto tgt must
    # land src on tgt's ORIGINAL visual slot, symmetric in both directions. This
    # locks down the rightward-drag off-by-one: reading the target index after
    # removing src used to make rightward drags land one column short.
    reorder = cb_mod.DatabaseViewerWindow._reorder_columns
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


def test_reorder_columns_single_step_is_one_column(cb_mod):
    # The reported bug: a one-column rightward drag must advance exactly one
    # slot (previously it took two drags to move one column).
    reorder = cb_mod.DatabaseViewerWindow._reorder_columns
    base = ["a", "b", "c", "d"]
    assert reorder(base, "a", "b").index("a") == 1   # right by one
    assert reorder(base, "b", "a").index("b") == 0   # left by one


def test_reorder_columns_edge_cases(cb_mod):
    reorder = cb_mod.DatabaseViewerWindow._reorder_columns
    base = ["a", "b", "c", "d"]
    # Drop onto the non-reorderable tree column (tgt_name None) -> src to front.
    assert reorder(base, "c", None) == ["c", "a", "b", "d"]
    # Unknown src is a safe no-op (returns an unchanged copy).
    assert reorder(base, "zzz", "a") == base
    # The returned list is a copy, never the same object (caller compares them).
    assert reorder(base, "a", "b") is not base


def test_expand_all_restripes_leaf_rows(cb_mod, tmp_path, app):
    # Expand All sets `open` programmatically, which does NOT fire
    # <<TreeviewOpen>>; the stripes must still be recomputed so the now-visible
    # leaf rows alternate background instead of all sharing one tag.
    db = cb_mod.DownloadsDatabase(str(tmp_path / "t.db"))
    # Several leaves under one group so striping has something to alternate.
    db.backfill_downloads([
        dict(video_id=None, title=f"Track {i}", channel_name="Chan",
             channel_url="https://yt/c", channel_id="UC1", platform="YouTube",
             genre="DnB", file_path=f"/x/Track {i}.mp3", upload_date="",
             ts=1000 + i, bitrate="")
        for i in range(4)
    ])

    v = cb_mod.DatabaseViewerWindow(app, db)
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


def test_viewer_trees_own_mousewheel_binding(cb_mod, tmp_path, app):
    # Each viewer tree binds <MouseWheel> itself and returns "break", so wheel
    # scrolling stays inside the viewer instead of bubbling up to the main
    # app's application-wide bind_all handler and scrolling the primary window.
    db = cb_mod.DownloadsDatabase(str(tmp_path / "t.db"))

    v = cb_mod.DatabaseViewerWindow(app, db)
    v.update()
    # A non-empty bind script means the handler is installed on the widget.
    assert v._dl_tree.bind("<MouseWheel>")
    assert v._wl_tree.bind("<MouseWheel>")


def test_viewer_column_order_persists(cb_mod, tmp_path, app):
    db = cb_mod.DownloadsDatabase(str(tmp_path / "t.db"))

    v = cb_mod.DatabaseViewerWindow(app, db)
    v.update()
    cols = list(v._WL_COLS)
    # "sel" is pinned to the front and never reorderable, so reorder a pair
    # of non-pinned columns and expect sel to stay at position 0 on reopen.
    non_sel = [c for c in cols if c != "sel"]
    # Swap the first two non-pinned columns.
    reordered = [non_sel[1], non_sel[0]] + non_sel[2:]
    new_order = ["sel"] + reordered
    v._save_col_order(v._WL_ORDER_KEY, new_order)
    v._wl_tree.configure(displaycolumns=new_order)
    v.destroy()

    # Reopening restores the saved order, with sel still pinned to the front.
    v2 = cb_mod.DatabaseViewerWindow(app, db)
    v2.update()
    assert list(v2._wl_tree.cget("displaycolumns")) == new_order


def _buttons(widget):
    """Every tk.Button in the subtree, in creation order."""
    import tkinter as tk
    out = [widget] if isinstance(widget, tk.Button) else []
    for child in widget.winfo_children():
        out += _buttons(child)
    return out


def test_destructive_toolbar_buttons_are_dark_red(cb_mod, tmp_path, app):
    db = cb_mod.DownloadsDatabase(str(tmp_path / "t.db"))
    v = cb_mod.DatabaseViewerWindow(app, db)
    v.update()
    reds = {b.cget("text") for b in _buttons(v)
            if str(b.cget("fg")).lower() == cb_mod.YT_DARK}
    assert reds == {"🧹  Folders Cleanup ‹Smart›", "🖼  Fetch Missing Artwork"}
    # Everything else keeps the ordinary dim toolbar colour.
    dims = {b.cget("text") for b in _buttons(v)
            if str(b.cget("fg")).lower() == cb_mod.TEXT_DIM}
    assert "⟳  Refresh" in dims

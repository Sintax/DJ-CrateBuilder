def test_tab_order_is_main_watchlist_settings_about(app):
    app.update()
    titles = [app._notebook.tab(i, "text") for i in app._notebook.tabs()]
    joined = " | ".join(titles)
    assert "Main" in titles[0], joined
    assert "Watch List" in titles[1], joined
    assert "Settings" in titles[2], joined
    assert "About" in titles[3], joined


def test_batch_queue_border_matches_the_destructive_red(cb_mod, app):
    """The Batch Queue panel wears the same YT_DARK as Skip and the
    Database Viewer's destructive buttons — not the brighter YT_RED."""
    outer = app._batch_canvas.master
    assert str(outer.cget("highlightbackground")).lower() == cb_mod.YT_DARK

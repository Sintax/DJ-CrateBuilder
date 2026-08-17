def test_tab_order_is_main_watchlist_settings_about(app):
    app.update()
    titles = [app._notebook.tab(i, "text") for i in app._notebook.tabs()]
    joined = " | ".join(titles)
    assert "Main" in titles[0], joined
    assert "Watch List" in titles[1], joined
    assert "Settings" in titles[2], joined
    assert "About" in titles[3], joined

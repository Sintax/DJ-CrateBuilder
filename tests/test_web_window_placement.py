"""Window size and position carried between sessions, for the WEB window.

`window_geometry` / `window_maximized` were already in the settings schema and
already written by the tkinter app; the web window neither read nor wrote
them, so every launch opened at the default.

The fitting rules themselves belong to util.fit_window_geometry and are
covered by tests/test_window_placement.py — this is the layer above them: the
service's pair of accessors, the screen-unit conversion pywebview forces on
the way in, and the state machine deciding which placements are worth
remembering at all.
"""
import time

import pytest

from cratebuilder.service import CrateBuilderService
from cratebuilder.settings import Settings

web_window = pytest.importorskip("web_window")


@pytest.fixture
def service(tmp_path):
    """A service pointed entirely at tmp_path — never the developer's config."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


# ── the service's two accessors ──────────────────────────────────────────────

def test_placement_round_trips_through_settings(service):
    assert service.window_placement() == ("", False)

    assert service.save_window_placement("1200x800+40+30", True) is True

    assert service.window_placement() == ("1200x800+40+30", True)


def test_placement_survives_a_new_service_on_the_same_config(service, tmp_path):
    """It is only worth writing if the NEXT launch reads it back."""
    service.save_window_placement("1024x768+10+20", False)

    reopened = CrateBuilderService(
        settings=Settings(path=str(tmp_path / "config.json")),
        db_path=str(tmp_path / "cratebuilder.db"))
    assert reopened.window_placement() == ("1024x768+10+20", False)


def test_saving_a_placement_never_raises(service, monkeypatch):
    """Called from a window event handler and from the close path; losing a
    window position must not take either down."""
    def boom(mapping):
        raise OSError("the config file is read-only")

    monkeypatch.setattr(service._settings, "update", boom)
    assert service.save_window_placement("800x600+0+0", False) is False


def test_placement_is_not_reachable_over_the_rpc_surface(service):
    """Where the host's window sits is not a remote browser's business, and
    keeping it off `settings.*` is what makes that true by construction."""
    assert "window_geometry" not in service.snapshot()["settings"]
    for method in ("window.placement", "window_placement"):
        with pytest.raises(Exception):
            service.call(method)


# ── screen rectangles, in the units pywebview's windows speak ────────────────

class FakeFrame:
    def __init__(self, x, y, width, height):
        self.X, self.Y, self.Width, self.Height = x, y, width, height


class FakeScreen:
    def __init__(self, x, y, width, height, scale=1.0, frame=None):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.scale = scale
        self.frame = frame


def test_screen_rects_takes_pywebviews_screens_in_the_units_they_come_in(monkeypatch):
    """The real reading off this machine: a 125% primary beside a 100%
    secondary. pywebview reports screens and windows in the SAME logical
    units — a window maximized onto that primary reads 2062 wide, the
    2048-wide screen plus its borders — so the scale it also reports is not a
    conversion to apply. Dividing by it once had the app believing the work
    area was 1638x883 and dragging every remembered window into the top-left
    corner on launch."""
    monkeypatch.setattr(web_window.webview, "screens", [
        FakeScreen(0, 0, 2048, 1152, 1.25, FakeFrame(0, 0, 2048, 1104)),
        FakeScreen(2560, 359, 1920, 1080, 1.0,
                   FakeFrame(2560, 359, 1920, 1032)),
    ])

    assert web_window.screen_rects() == [
        (0, 0, 2048, 1104),
        (2560, 359, 1920, 1032),
    ]


def test_screen_rects_puts_the_primary_first(monkeypatch):
    """fit_window_geometry centres on screens[0] when the remembered monitor
    is gone, so the wrong order lands the window on the wrong display."""
    monkeypatch.setattr(web_window.webview, "screens", [
        FakeScreen(-1920, 0, 1920, 1080),      # arranged to the LEFT
        FakeScreen(0, 0, 2560, 1440),          # the primary, at the origin
    ])

    assert web_window.screen_rects()[0] == (0, 0, 2560, 1440)


def test_screen_rects_falls_back_to_bounds_without_a_work_area(monkeypatch):
    """.frame is only a rectangle on the platforms that populate it."""
    monkeypatch.setattr(web_window.webview, "screens",
                        [FakeScreen(0, 0, 1920, 1080, 1.0, frame=None)])

    assert web_window.screen_rects() == [(0, 0, 1920, 1080)]


def test_screen_rects_is_empty_when_the_layout_cannot_be_read(monkeypatch):
    class Exploding:
        def __iter__(self):
            raise RuntimeError("no display")

    monkeypatch.setattr(web_window.webview, "screens", Exploding())
    assert web_window.screen_rects() == []


def test_a_screen_that_measures_nothing_is_skipped(monkeypatch):
    monkeypatch.setattr(web_window.webview, "screens", [
        FakeScreen(0, 0, 0, 0, 1.0, FakeFrame(0, 0, 0, 0)),
        FakeScreen(0, 0, 1920, 1080, 1.0, FakeFrame(0, 0, 1920, 1040)),
    ])
    assert web_window.screen_rects() == [(0, 0, 1920, 1040)]


# ── what create_window is handed ─────────────────────────────────────────────

class StubService:
    def __init__(self, placement=("", False), raises=False):
        self._placement = placement
        self._raises = raises
        self.saved = []

    def window_placement(self):
        if self._raises:
            raise RuntimeError("no settings")
        return self._placement

    def save_window_placement(self, geometry, maximized):
        self.saved.append((geometry, bool(maximized)))
        return True


ONE_SCREEN = [(0, 0, 1920, 1040)]


def test_nothing_remembered_leaves_the_default_placement(monkeypatch):
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    assert web_window.window_placement_kwargs(StubService()) == {}


def test_a_remembered_window_is_reopened_where_it_was(monkeypatch):
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    service = StubService(("1400x900+120+60", False))

    assert web_window.window_placement_kwargs(service) == {
        "width": 1400, "height": 900, "x": 120, "y": 60}


def test_a_window_remembered_on_a_scaled_display_reopens_where_it_was(monkeypatch):
    """The placement that was being lost: a window at 1408x882+411+154 on the
    125% primary, well inside its 2048x1104 work area, reopened at +230+1
    every launch because the screen was being read as 1638x883. Through the
    real screen list, unconverted, it comes back where it was left."""
    monkeypatch.setattr(web_window.webview, "screens", [
        FakeScreen(0, 0, 2048, 1152, 1.25, FakeFrame(0, 0, 2048, 1104)),
    ])
    service = StubService(("1408x882+411+154", False))

    assert web_window.window_placement_kwargs(service) == {
        "width": 1408, "height": 882, "x": 411, "y": 154}


def test_a_window_saved_on_a_monitor_that_is_gone_comes_home(monkeypatch):
    """The undocked-laptop case: obeyed blindly this opens at x=2600, visible
    to the window manager and unreachable with the mouse."""
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    service = StubService(("1400x900+2600+80", False))

    kwargs = web_window.window_placement_kwargs(service)

    assert 0 <= kwargs["x"] <= 1920 - kwargs["width"]
    assert 0 <= kwargs["y"] <= 1040 - kwargs["height"]


def test_a_maximized_window_reopens_maximized(monkeypatch):
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    service = StubService(("1400x900+120+60", True))

    assert web_window.window_placement_kwargs(service)["maximized"] is True


def test_an_unreadable_placement_is_not_fatal(monkeypatch):
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    assert web_window.window_placement_kwargs(StubService(raises=True)) == {}


def test_a_corrupt_geometry_falls_back_to_the_default(monkeypatch):
    """A config file carrying nonsense must not stop the window opening."""
    monkeypatch.setattr(web_window, "screen_rects", lambda: ONE_SCREEN)
    for junk in ("not a geometry", "1400x900", "+10+10", None, 17):
        assert web_window.window_placement_kwargs(StubService((junk, False))) == {}


def test_no_readable_screens_leaves_the_default(monkeypatch):
    """Nothing to check a remembered window against is not a licence to
    restore it unchecked."""
    monkeypatch.setattr(web_window, "screen_rects", lambda: [])
    service = StubService(("1400x900+120+60", False))

    assert web_window.window_placement_kwargs(service) == {}


# ── capturing the placement as the window moves ──────────────────────────────

class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeWindow:
    def __init__(self, width=1200, height=800, x=40, y=30):
        self.width, self.height, self.x, self.y = width, height, x, y
        self.events = type("Events", (), {})()
        for name in ("moved", "resized", "maximized", "restored", "minimized"):
            setattr(self.events, name, FakeEvent())


def keep(window, service, **overrides):
    """A WindowPlacement whose timers never fire within a test: the flush
    interval and the settle delay are both an hour out, so every write is
    driven by flush() and nothing here is testing the clock. The screens and
    the minimum size are the harness's own — the fakes' frames sit on
    ONE_SCREEN, and 600x400 is under every size a test picks."""
    opts = dict(interval=3600, settle_delay=3600, min_size=(600, 400),
                screens=lambda: ONE_SCREEN)
    opts.update(overrides)
    return web_window.WindowPlacement(window, service, **opts)


def make_placement(placement=("", False), **window):
    service = StubService(placement)
    window = FakeWindow(**window)
    keeper = keep(window, service)
    keeper.start()
    return keeper, window, service


def test_start_seeds_from_the_live_window_and_subscribes(tmp_path):
    keeper, window, _ = make_placement()
    try:
        for name in ("moved", "resized", "maximized", "restored", "minimized"):
            assert getattr(window.events, name).handlers, f"{name} unsubscribed"
        # Seeded, so a move alone is a complete geometry.
        keeper.on_moved(200, 150)
        assert keeper.flush() is True
    finally:
        keeper.stop()


def test_a_move_and_a_resize_are_written_as_one_geometry():
    keeper, _, service = make_placement()
    try:
        keeper.on_resized(1000, 700)
        keeper.on_moved(60, 45)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved[-1] == ("1000x700+60+45", False)


def test_an_unmoved_window_is_never_rewritten():
    """Every config write rewrites the whole store; a window that opened where
    it was remembered and was never touched must not cause one."""
    keeper, _, service = make_placement(placement=("1200x800+40+30", False))
    try:
        assert keeper.flush() is False
        keeper.on_moved(40, 30)         # exactly where it already was
        keeper.on_resized(1200, 800)
        assert keeper.flush() is False
    finally:
        keeper.stop()
    assert service.saved == []


def test_where_the_window_actually_opened_is_written_back():
    """A window does not always open where it was asked to. fit_window_geometry
    corrects a placement that no longer fits the monitors attached now, and
    pywebview converts a coordinate to physical pixels and back with int(),
    which drops a pixel on a scaled display. Measured on a 125% monitor:
    asking for 1408x882+411+154 opened 1408x881+410+153.

    Left unwritten, the stored value stays one the window never had and every
    later launch re-derives the same correction from it. Writing it back is
    what lets the pair converge on the coordinates that survive the round
    trip exactly — at most a few pixels, over at most a few launches."""
    keeper, _, service = make_placement(placement=("1408x882+411+154", False),
                                        width=1408, height=881, x=410, y=153)
    try:
        assert keeper.flush() is True
    finally:
        keeper.stop()

    assert service.saved[-1] == ("1408x881+410+153", False)


def test_a_first_run_remembers_where_it_opened():
    """Nothing remembered is still a placement worth keeping — otherwise the
    first session a user never resizes is never recorded at all."""
    keeper, _, service = make_placement()
    try:
        assert keeper.flush() is True
    finally:
        keeper.stop()

    assert service.saved[-1] == ("1200x800+40+30", False)


def test_a_maximized_windows_size_is_not_mistaken_for_the_users_choice():
    """A maximized window reports the size of the screen it fills. Writing
    that back would mean the window never unmaximizes to the size the user
    actually chose — so only the flag moves."""
    keeper, _, service = make_placement()
    try:
        keeper.on_moved(60, 45)
        keeper.on_resized(1000, 700)
        keeper.flush()

        keeper.on_maximized()
        keeper.on_resized(1920, 1040)       # the screen, not a choice
        keeper.on_moved(0, 0)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved[-1] == ("1000x700+60+45", True)


def test_unmaximizing_clears_the_flag_and_resumes_capturing():
    keeper, _, service = make_placement()
    try:
        keeper.on_maximized()
        keeper.flush()
        assert service.saved[-1][1] is True

        keeper.on_restored()
        keeper.on_resized(900, 600)
        keeper.on_moved(15, 25)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved[-1] == ("900x600+15+25", False)


def test_a_minimized_window_reports_nothing_worth_keeping():
    """Minimized — or hidden to the tray — a window's geometry describes
    where it is not."""
    keeper, _, service = make_placement()
    try:
        keeper.on_moved(60, 45)
        keeper.on_resized(1000, 700)
        keeper.flush()

        keeper.on_minimized()
        keeper.on_moved(-32000, -32000)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved == [("1000x700+60+45", False)]


def test_a_window_opened_maximized_keeps_the_size_it_had_before():
    """Opened maximized, the live frame is the screen's — the one size this
    must never store. The remembered geometry stays and so does the flag,
    with nothing written for a session that never touched the window; the
    next launch still opens maximized, over the size the user chose."""
    keeper, _, service = make_placement(placement=("1000x700+60+45", True),
                                        width=1927, height=1054, x=-7, y=-7)
    try:
        assert keeper.flush() is False
        keeper.on_moved(-7, -7)
        keeper.on_resized(1927, 1054)
        assert keeper.flush() is False

        keeper.on_restored()
        keeper.on_resized(900, 600)
        keeper.on_moved(15, 25)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved == [("900x600+15+25", False)]


def test_the_moved_of_a_minimize_arrives_before_the_minimized():
    """The order pywebview delivers a minimize in, measured on Windows at
    125%: `moved(-25600, -25600)` on one thread, then `minimized` and
    `resized(159, 27)` on two more, 3 ms later. Judged as it arrived, the
    move was recorded while the state still said normal — and the config
    carried 159x27-25600-25600 as the user's window."""
    keeper, _, service = make_placement(placement=("900x700+300+200", False),
                                        width=900, height=700, x=300, y=200)
    try:
        keeper.on_moved(-25600, -25600)
        keeper.on_minimized()
        keeper.on_resized(159, 27)
        assert keeper.flush() is False

        keeper.on_moved(300, 200)               # the restore, same order
        keeper.on_restored()
        keeper.on_resized(900, 700)
        assert keeper.flush() is False          # back exactly where it was
    finally:
        keeper.stop()

    assert service.saved == []


def test_the_resized_of_a_maximize_is_not_the_users_choice_either():
    keeper, _, service = make_placement()
    try:
        keeper.on_moved(60, 45)
        keeper.on_resized(1000, 700)
        keeper.flush()

        keeper.on_moved(-7, -7)                 # ahead of the state, as delivered
        keeper.on_maximized()
        keeper.on_resized(1927, 1054)
        keeper.flush()
    finally:
        keeper.stop()

    assert service.saved[-1] == ("1000x700+60+45", True)


def test_a_frame_no_user_could_have_placed_is_refused_whatever_the_state_says():
    """A flush that lands inside those few milliseconds sees the state the
    window is leaving. The minimized frame gives itself away regardless:
    smaller than the window's minimum, and on no screen."""
    keeper, _, service = make_placement(placement=("900x700+300+200", False),
                                        width=900, height=700, x=300, y=200)
    try:
        keeper.on_moved(-25600, -25600)
        assert keeper.flush() is False
        keeper.on_resized(159, 27)
        assert keeper.flush() is False
        keeper.on_moved(310, 210)               # a real move still lands
        assert keeper.flush() is True
    finally:
        keeper.stop()

    assert service.saved == [("900x700+310+210", False)]


def test_screens_that_cannot_be_read_refuse_nothing_by_position():
    service = StubService(("900x700+300+200", False))
    keeper = keep(FakeWindow(900, 700, 300, 200), service, screens=lambda: [])
    keeper.start()
    try:
        keeper.on_moved(-25600, -25600)
        assert keeper.flush() is True           # nothing to judge it against
        keeper.on_resized(159, 27)
        assert keeper.flush() is False          # the minimum still holds
    finally:
        keeper.stop()

    assert service.saved == [("900x700-25600-25600", False)]


def test_a_move_that_settled_before_the_minimize_is_kept():
    """The settle timer itself: a move left alone for the delay is recorded
    while the window is still normal, so the minimize that follows cannot
    take it back."""
    service = StubService(("900x700+300+200", False))
    keeper = keep(FakeWindow(900, 700, 300, 200), service, settle_delay=0.05)
    keeper.start()
    try:
        keeper.on_moved(320, 220)
        time.sleep(0.3)
        keeper.on_minimized()
        keeper.on_moved(-25600, -25600)
        assert keeper.flush() is True
    finally:
        keeper.stop()

    assert service.saved == [("900x700+320+220", False)]


def test_a_failed_write_is_retried_rather_than_dropped():
    service = StubService()
    window = FakeWindow()
    keeper = keep(window, service)
    keeper.start()
    failed = []

    def refuse(geometry, maximized):
        failed.append((geometry, maximized))
        return False

    try:
        service.save_window_placement = refuse
        keeper.on_moved(70, 80)
        assert keeper.flush() is False

        service.save_window_placement = StubService.save_window_placement.__get__(
            service, StubService)
        assert keeper.flush() is True         # still dirty, so it retries
    finally:
        keeper.stop()

    assert failed == [("1200x800+70+80", False)]
    assert service.saved[-1] == ("1200x800+70+80", False)


def test_stop_flushes_once_and_then_ignores_the_window():
    """The close path's last write, and nothing after it — pywebview keeps
    raising resize events while a window tears down."""
    keeper, _, service = make_placement()
    keeper.on_moved(90, 95)

    keeper.stop()

    assert service.saved == [("1200x800+90+95", False)]
    keeper.on_moved(500, 500)
    keeper.flush()
    assert service.saved == [("1200x800+90+95", False)]


def test_a_window_that_cannot_be_read_still_subscribes():
    """window.width blocks on pywebview's `shown` event and can time out; a
    seed that failed must not cost the subscription too."""
    class Unreadable:
        def __init__(self):
            self.events = type("Events", (), {})()
            for name in ("moved", "resized", "maximized", "restored",
                         "minimized"):
                setattr(self.events, name, FakeEvent())

        @property
        def width(self):
            raise RuntimeError("window not shown")

    service = StubService()
    window = Unreadable()
    keeper = keep(window, service)
    keeper.start()
    try:
        assert window.events.moved.handlers
        keeper.on_moved(10, 10)
        assert keeper.flush() is False        # no size half, so nothing to write

        keeper.on_resized(800, 600)
        assert keeper.flush() is True
    finally:
        keeper.stop()

    assert service.saved[-1] == ("800x600+10+10", False)

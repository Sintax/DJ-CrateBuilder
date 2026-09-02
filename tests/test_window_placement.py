"""Where the window reopens: the remembered geometry, and the monitors that
actually exist when it is restored.

A position is only meaningful against the desktop it was saved on. These pin
the cases where obeying it literally would put the window somewhere the user
cannot reach — an unplugged monitor, a smaller panel — and the case that a
naive check breaks instead: a monitor arranged left of or above the primary
one, whose coordinates are negative.
"""
import pytest

from cratebuilder import util


# The developer's own layout, read from EnumDisplayMonitors: a laptop panel at
# the origin, a monitor mounted ABOVE it (negative y), and one to the right.
THREE_SCREENS = [
    (0, 0, 2048, 1104),
    (924, -1080, 1536, 816),
    (2560, 0, 1920, 1032),
]
LAPTOP_ONLY = [(0, 0, 2048, 1104)]
MIN = (640, 620)


# ── on a screen at all ───────────────────────────────────────────────────────
@pytest.mark.parametrize("rect,expected", [
    ((100, 100, 800, 600), True),
    ((2000, 1000, 800, 600), True),           # a corner is enough
    ((1000, -900, 800, 600), True),           # the monitor above
    ((-25600, -25600, 159, 27), False),       # where Windows parks a minimized window
    ((4500, 0, 800, 600), False),             # right of everything
])
def test_window_on_screen(rect, expected):
    assert util.window_on_screen(rect, THREE_SCREENS) is expected


def test_window_on_screen_with_no_screens_is_never_true():
    assert util.window_on_screen((100, 100, 800, 600), []) is False


# ── parsing ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("850x950+120+80", (850, 950, 120, 80)),
    ("850x950+0+0", (850, 950, 0, 0)),
    # Both offsets signed: a monitor above and left of the primary is normal,
    # and reading these as corrupt is what drags the window back every launch.
    ("850x950+924-1080", (850, 950, 924, -1080)),
    ("850x950-1200-300", (850, 950, -1200, -300)),
])
def test_parse_window_geometry_accepts_signed_offsets(text, expected):
    assert util.parse_window_geometry(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "850x950", "+120+80", "banana", "850x950+120",
    "850 x 950 + 120 + 80", "0x0", "850x950+120+80+40",
])
def test_parse_window_geometry_rejects_anything_incomplete(text):
    assert util.parse_window_geometry(text) is None


def test_format_window_geometry_signs_negative_offsets():
    assert util.format_window_geometry(850, 950, 924, -1080) == "850x950+924-1080"
    assert util.format_window_geometry(850, 950, 0, 0) == "850x950+0+0"


# ── fitting ──────────────────────────────────────────────────────────────────
def test_a_window_still_on_its_monitor_is_left_where_it_was():
    for geometry in ("900x800+100+100", "900x800+2600+40", "900x800+1000-1070"):
        assert util.fit_window_geometry(
            geometry, THREE_SCREENS, MIN) == geometry


def test_nothing_remembered_yet_defers_to_the_caller():
    assert util.fit_window_geometry("", THREE_SCREENS, MIN) is None
    assert util.fit_window_geometry(None, THREE_SCREENS, MIN) is None
    assert util.fit_window_geometry("garbage", THREE_SCREENS, MIN) is None


def test_no_readable_monitors_defers_to_the_caller():
    assert util.fit_window_geometry("900x800+100+100", [], MIN) is None


def test_a_window_on_an_unplugged_monitor_comes_back_to_the_primary():
    """The undocked-laptop case. The second monitor's coordinates still parse
    fine, so nothing but checking them against the live layout catches this."""
    fitted = util.fit_window_geometry("900x800+3000+200", LAPTOP_ONLY, MIN)
    width, height, x, y = util.parse_window_geometry(fitted)
    sx, sy, sw, sh = LAPTOP_ONLY[0]
    assert (width, height) == (900, 800)
    # Centred, and wholly inside the one screen that is left.
    assert x == sx + (sw - width) // 2
    assert y == sy + (sh - height) // 2


def test_a_window_above_the_primary_monitor_is_not_mistaken_for_off_screen():
    """The regression this whole check exists for: monitor 2 here sits at
    y=-1080, and any rule that treats a negative offset as invalid throws that
    arrangement away on every launch."""
    assert util.fit_window_geometry(
        "900x800+1000-1070", THREE_SCREENS, MIN) == "900x800+1000-1070"


def test_a_window_hanging_off_an_edge_is_pulled_fully_back_on():
    # 200px of a 900px-wide window left showing on the laptop panel.
    fitted = util.fit_window_geometry("900x800+1848+100", LAPTOP_ONLY, MIN)
    width, height, x, y = util.parse_window_geometry(fitted)
    assert x + width <= 2048 and x >= 0
    assert y + height <= 1104 and y >= 0


def test_a_size_from_a_bigger_screen_is_clamped_to_the_one_it_lands_on():
    """A window sized on the 4K desktop has to still be usable on the laptop
    panel — restored verbatim its corners would be off two edges at once."""
    fitted = util.fit_window_geometry("3000x2000+10+10", LAPTOP_ONLY, MIN)
    width, height, x, y = util.parse_window_geometry(fitted)
    assert (width, height) == (2048, 1104)
    assert (x, y) == (0, 0)


def test_the_size_never_goes_under_the_minimum():
    fitted = util.fit_window_geometry("200x150+10+10", LAPTOP_ONLY, MIN)
    width, height, _x, _y = util.parse_window_geometry(fitted)
    assert (width, height) == MIN


def test_a_window_is_placed_on_whichever_monitor_it_overlaps_most():
    # Mostly on the right-hand monitor, with a sliver on the laptop panel.
    fitted = util.fit_window_geometry("900x800+2400+100", THREE_SCREENS, MIN)
    _w, _h, x, _y = util.parse_window_geometry(fitted)
    assert x >= 2560, fitted


def test_every_fitted_window_lands_on_some_monitor():
    """The guarantee the whole function exists to make."""
    candidates = [
        "900x800+100+100", "900x800+9000+9000", "900x800-4000-4000",
        "3000x2000+10+10", "200x150+10+10", "900x800+1848+100",
        "900x800+1000-1070", "900x800+2400+100", "1x1+0+0",
    ]
    for screens in (THREE_SCREENS, LAPTOP_ONLY):
        for candidate in candidates:
            fitted = util.fit_window_geometry(candidate, screens, MIN)
            width, height, x, y = util.parse_window_geometry(fitted)
            assert any(util._overlap((x, y, width, height), s) > 0
                       for s in screens), (candidate, fitted)

"""Offline tests: merging + building roll-up."""



from bassync.model import OccupancyWindow
from bassync.schedule import ScheduleBuilder
from tests.helpers import dest, dt, event, space


def test_adjacent_events_merge():
    """Two back-to-back events (within the gap) collapse into one window."""
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10)), event(1, dt(10, 3), dt(11))],
        {"1": space(1, "room", "Bldg/Rm1")})
    windows = result[dest("Bldg/Rm1")]
    assert len(windows) == 1, windows
    assert windows[0].start == dt(9) and windows[0].end == dt(11), windows[0]


def test_separated_events_stay_split():
    """Events further apart than the gap remain two windows."""
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10)), event(1, dt(11), dt(12))],
        {"1": space(1, "room", "Bldg/Rm1")})
    assert len(result[dest("Bldg/Rm1")]) == 2


def test_building_rollup_unions_rooms():
    """Two rooms feeding one building schedule produce a merged window."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "E1"), event(2, dt(10), dt(11), "E2")], space_map)
    assert len(result[dest("Bldg/Rm1")]) == 1
    assert len(result[dest("Bldg/Rm2")]) == 1
    bwin = result[dest("Bldg/Occ")]
    assert len(bwin) == 1, bwin
    assert bwin[0].start == dt(9) and bwin[0].end == dt(11), bwin[0]


def test_disjoint_rooms_give_building_two_windows():
    """Rooms occupied at different times leave the building occupied for BOTH
    windows (a union, not an intersection)."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "E1"), event(2, dt(14), dt(15), "E2")], space_map)
    assert len(result[dest("Bldg/Occ")]) == 2


def test_per_room_merge_gap_overrides_default():
    """A room with a wide merge_gap collapses windows the global default keeps
    separate."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", merge_gap=30),
        "2": space(2, "room", "Bldg/Rm2", merge_gap=5),
    }
    events = [event(1, dt(9), dt(10), "A1"), event(1, dt(10, 20), dt(11), "A2"),
              event(2, dt(9), dt(10), "B1"), event(2, dt(10, 20), dt(11), "B2")]
    result = ScheduleBuilder(5).build(events, space_map)
    assert len(result[dest("Bldg/Rm1")]) == 1, result[dest("Bldg/Rm1")]
    assert len(result[dest("Bldg/Rm2")]) == 2, result[dest("Bldg/Rm2")]


def test_cross_room_building_merge_uses_default_gap():
    """Merging windows from DIFFERENT rooms into the building uses the global
    default gap, regardless of either room's own override."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ", merge_gap=30),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ", merge_gap=30),
    }
    events = [event(1, dt(9), dt(10), "A"), event(2, dt(10, 20), dt(11), "B")]
    tight = ScheduleBuilder(5).build(events, space_map)
    assert len(tight[dest("Bldg/Occ")]) == 2, tight[dest("Bldg/Occ")]
    wide = ScheduleBuilder(30).build(events, space_map)
    assert len(wide[dest("Bldg/Occ")]) == 1, wide[dest("Bldg/Occ")]


def test_two_rooms_sharing_one_target_are_unioned():
    """A divisible room mapped as two 25Live spaces onto ONE schedule keeps
    both halves' bookings. Regression: the old builder assigned rather than
    unioned, so whichever room was processed second silently erased the first
    and half the room never got conditioned."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm100"),
        "2": space(2, "room", "Bldg/Rm100"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "A"), event(2, dt(14), dt(15), "B")], space_map)
    windows = result[dest("Bldg/Rm100")]
    assert len(windows) == 2, windows
    assert {w.start for w in windows} == {dt(9), dt(14)}, windows


def test_builder_skips_unmapped_space():
    """An event for a space that isn't in the map can't take the run down."""
    result = ScheduleBuilder(5).build([event(99, dt(9), dt(10))],
                                      {"1": space(1, "room", "Bldg/Rm1")})
    assert result == {}, result


def test_direct_building_booking_joins_rollup():
    """A bookable common area's own events land on the building schedule
    alongside the rooms that roll up into it."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "9": space(9, "building", "Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "R"), event(9, dt(13), dt(14), "A")], space_map)
    assert len(result[dest("Bldg/Occ")]) == 2, result[dest("Bldg/Occ")]


def test_overlap_helper():
    """Sanity-check the OccupancyWindow overlap primitive directly."""
    a = OccupancyWindow(dt(9), dt(10))
    b = OccupancyWindow(dt(10, 4), dt(11))
    assert a.overlaps_or_adjacent(b, gap_minutes=5)
    assert not a.overlaps_or_adjacent(b, gap_minutes=3)

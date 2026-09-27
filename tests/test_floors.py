"""Offline tests: per-floor corridor roll-up (room -> floor -> building)."""



from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, dt, event, space, with_yaml


def test_floor_rollup_room_to_floor_to_building():
    """A booked room drives its own zone, its floor's corridor, AND its
    building — while the OTHER floor's corridor stays off. That separation is
    the whole point: one evening seminar shouldn't condition the whole tower."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
  - building: b
    level: 2
    target: "B/F2_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
  - space_id: 2
    building: b
    floor: 2
    target: "B/Rm201"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.floor_count == 2, sm.floor_count
    assert sm.spaces["1"].floor_destination == dest("B/F1_Corridor")

    # Only the floor-1 room is booked.
    schedule = ScheduleBuilder(5).build([event(1, dt(18), dt(20), "E")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    assert len(schedule[dest("B/Rm101")]) == 1
    assert len(schedule[dest("B/F1_Corridor")]) == 1, "its own corridor runs"
    assert len(schedule[dest("B/Occ")]) == 1, "the building runs"
    assert schedule[dest("B/F2_Corridor")] == [], "the other floor stays off"
    assert schedule[dest("B/Rm201")] == []


def test_floor_destinations_are_cleared_when_empty():
    """Floor corridors join the managed set, so one with no bookings this week
    is actively cleared rather than left on last week's schedule."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert sm.destinations() == {dest("B/Rm101"), dest("B/F1_Corridor"),
                                 dest("B/Occ")}, sm.destinations()


def test_unknown_floor_warns_and_skips():
    """A room naming a floor with no matching entry still syncs and still rolls
    up to its building — it just drives no corridor. A typo shouldn't cost the
    room its heat."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 9
    target: "B/Rm901"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert any("floor 9" in w for w in sm.warnings), sm.warnings
    assert sm.spaces["1"].floor_destination is None
    assert sm.spaces["1"].building_destination == dest("B/Occ")


def test_floor_inherits_building_system_and_can_override():
    """A corridor is usually on the same panel as the rooms off it, so it
    inherits — but a part-finished retrofit can put it elsewhere."""
    text = """
buildings:
  - id: b
    system: supervisor
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
  - building: b
    level: 2
    system: campus_bacnet
    target: "12001:120"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
  - space_id: 2
    building: b
    floor: 2
    target: "B/Rm201"
"""
    cfg = base_config()
    cfg["systems"] = {"supervisor": {"driver": "preview"},
                      "campus_bacnet": {"driver": "preview"},
                      "sys": {"driver": "preview"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].floor_destination.system == "supervisor"
    assert sm.spaces["2"].floor_destination.system == "campus_bacnet"


def test_floor_errors_are_reported():
    """Structural mistakes in floors: are errors, not silent skips."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: nosuch
    level: 1
    target: "X/Hall"
  - building: b
    level: notanumber
    target: "B/Hall"
  - building: b
    level: 1
    target: "B/F1"
  - building: b
    level: 1
    target: "B/F1_again"
spaces: []
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("unknown building 'nosuch'" in e for e in sm.errors), sm.errors
    assert any("`level` must be a whole number" in e for e in sm.errors), sm.errors
    assert any("defined twice" in e for e in sm.errors), sm.errors


def test_room_gap_carries_into_rollup_contribution():
    """A room's own wide gap merges its windows, and that merged occupancy
    carries into the roll-ups — the corridor reflects when the room is really
    occupied, not when its individual bookings happen to start."""
    space_map = {
        "1": space(1, "room", "B/Rm1", building="B/Occ", merge_gap=30),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "A1"), event(1, dt(10, 20), dt(11), "A2")],
        space_map)
    assert len(result[dest("B/Rm1")]) == 1, result[dest("B/Rm1")]
    assert len(result[dest("B/Occ")]) == 1, result[dest("B/Occ")]

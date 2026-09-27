"""Offline tests: rooms with no schedule of their own (floor- or building-level scheduling)."""



from bassync.model import Destination
from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, dt, event, with_yaml


def test_room_without_target_still_drives_its_rollups():
    """How finely a building can be scheduled depends on how it was built out.
    A room in a building that is only schedulable at the air handler has no
    `target:` of its own — but its bookings must still turn the building on."""
    text = """
buildings:
  - id: b
    target: "B/AHU_Occ"
floors:
  - building: b
    level: 2
    target: "B/F2_Corridor"
spaces:
  - space_id: 1
    space_name: "Old wing 201"
    building: b
    floor: 2
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    room = sm.spaces["1"]
    assert room.destination is None, "no schedule of its own"
    assert room.floor_destination == dest("B/F2_Corridor")
    assert room.building_destination == dest("B/AHU_Occ")
    # It writes no room schedule, but both roll-ups are managed and driven.
    assert sm.destinations() == {dest("B/F2_Corridor"), dest("B/AHU_Occ")}

    schedule = ScheduleBuilder(5).build([event(1, dt(9), dt(11), "E")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    assert len(schedule[dest("B/F2_Corridor")]) == 1
    assert len(schedule[dest("B/AHU_Occ")]) == 1


def test_room_driving_nothing_is_an_error():
    """A room with neither a target nor a roll-up would swallow its bookings
    silently. That is always a mapping mistake, so say so."""
    text = """
buildings: []
spaces:
  - space_id: 1
    space_name: "Orphan"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("drive nothing" in e for e in sm.errors), sm.errors


def test_mixed_granularity_campus():
    """The three patterns a campus actually has, in one map: per-room where the
    controls support it, per-floor for a partial retrofit, per-building for the
    oldest air handlers."""
    text = """
buildings:
  - id: new_hall
    system: webctrl
    target: "12100:100"
  - id: mid_hall
    target: "12200:100"
  - id: old_hall
    target: "12300:100"
floors:
  - building: mid_hall
    level: 1
    target: "12200:110"
spaces:
  - space_id: 1
    building: new_hall
    target: "12100:5"
  - space_id: 2
    building: mid_hall
    floor: 1
  - space_id: 3
    building: old_hall
"""
    cfg = base_config()
    cfg["systems"] = {"webctrl": {"driver": "bacnet", "local_address": "10.0.0.1/24"},
                      "sys": {"driver": "preview"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].destination == Destination("webctrl", "12100:5")
    assert sm.spaces["2"].destination is None
    assert sm.spaces["3"].destination is None

    schedule = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "A"), event(2, dt(9), dt(10), "B"),
         event(3, dt(9), dt(10), "C")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    # Room-level where available, floor-level for the partial retrofit,
    # building-level for the oldest wing.
    assert len(schedule[Destination("webctrl", "12100:5")]) == 1
    assert len(schedule[dest("12200:110")]) == 1
    assert len(schedule[dest("12300:100")]) == 1

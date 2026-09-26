"""Offline tests: end to end."""

import os
import tempfile

from bassync.config import load_config
from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, dt, event, with_yaml


def test_mixed_campus_dry_run_routes_to_each_system():
    """The whole pipeline, three vendors at once: rooms land on their own
    system and the roll-up follows its building's."""
    from bassync.sync import _group_by_system
    text = """
buildings:
  - id: soc
    target: "12001:100"
  - id: eng
    system: supervisor
    target: "EngTech/Building_Occ"
spaces:
  - space_id: 1
    building: soc
    target: "12001:5"
  - space_id: 2
    building: soc
    target: "12001:6"
  - space_id: 3
    building: eng
    target: "EngTech/Rm110_Occ"
"""
    cfg = base_config()
    cfg["systems"] = {"campus_bacnet": {"driver": "bacnet",
                                        "local_address": "10.0.0.1/24"},
                      "supervisor": {"driver": "niagara", "host": "h"}}
    cfg["default_system"] = "campus_bacnet"
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors

    schedule = ScheduleBuilder(5).build(
        [event(1, dt(9, day=10), dt(10, day=10), "A"),
         event(2, dt(10, day=10), dt(11, day=10), "B"),
         event(3, dt(13, day=10), dt(14, day=10), "C")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])

    grouped = _group_by_system(schedule)
    assert set(grouped) == {"campus_bacnet", "supervisor"}, grouped
    assert set(grouped["campus_bacnet"]) == {"12001:5", "12001:6", "12001:100"}
    assert set(grouped["supervisor"]) == {"EngTech/Rm110_Occ",
                                          "EngTech/Building_Occ"}
    # The BACnet roll-up unions both rooms into one 09:00-11:00 window.
    rollup = grouped["campus_bacnet"]["12001:100"]
    assert len(rollup) == 1 and rollup[0].end == dt(11, day=10), rollup


def test_empty_schedules_are_still_written_so_stale_occupancy_clears():
    """A room and a building with no bookings this week both appear in the
    write set, with empty window lists. Regression: the old clear-loop skipped
    building roll-ups entirely, so a building whose rooms all went quiet kept
    running on last week's schedule."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    schedule = ScheduleBuilder(5).build([event(1, dt(9), dt(10))], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    assert schedule[dest("B/Rm2")] == [], "an unbooked room must be cleared"
    assert len(schedule[dest("B/Occ")]) == 1
    assert set(schedule) == sm.destinations()


def test_run_sync_end_to_end_writes_and_records_state():
    """The real run_sync path — fetch, build, safety check, fan out, save state
    — with 25Live stubbed and every system on the `preview` driver.

    Exercises what the unit tests can't: that a system failure is contained,
    that unbooked schedules are actually written empty, and that the run leaves
    a state file the next run can compare against."""
    import bassync.sync as sync_mod
    from bassync.model import RawEvent as RE

    map_text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
  - space_id: 3
    system: broken
    target: "X/Rm3"
"""
    with tempfile.TemporaryDirectory() as tmp:
        map_path = os.path.join(tmp, "map.yaml")
        with open(map_path, "w", encoding="utf-8") as fh:
            fh.write(map_text)
        csv_path = os.path.join(tmp, "out.csv")
        state_path = os.path.join(tmp, "last_run.json")

        cfg = load_config("/nonexistent/config.yaml")
        cfg["collegenet"]["base_url"] = "http://stub"
        cfg["space_map_file"] = map_path
        cfg["safety"]["state_file"] = state_path
        cfg["systems"] = {"sys": {"driver": "preview", "csv_file": csv_path},
                          # A system whose driver cannot be built at all.
                          "broken": {"driver": "rest", "base_url": ""}}
        cfg["default_system"] = "sys"

        original = sync_mod._fetch
        sync_mod._fetch = lambda *a, **kw: [
            RE("E1", "Class", "1", dt(9, day=10), dt(10, day=10)),
            RE("E2", "Lab", "1", dt(13, day=10), dt(14, day=10)),
        ]
        try:
            code = sync_mod.run_sync(cfg)
        finally:
            sync_mod._fetch = original

        # The broken system fails its own schedule; the rest still wrote.
        assert code == sync_mod.EXIT_WRITE_FAILURES, code
        rows = open(csv_path, encoding="utf-8").read()
        state = __import__("json").load(open(state_path, encoding="utf-8"))

    # Room 1 booked twice, room 2 unbooked but still written (cleared), and the
    # building rolled up from room 1.
    assert "B/Rm1" in rows and "B/Rm2" in rows and "B/Occ" in rows, rows
    assert state["windows"]["sys:B/Rm1"] == 2, state
    assert state["windows"]["sys:B/Rm2"] == 0, state
    assert state["windows"]["sys:B/Occ"] == 2, state


def test_run_sync_aborts_instead_of_clearing_the_campus():
    """25Live returning nothing must not become a campus-wide stand-down."""
    import bassync.sync as sync_mod
    map_text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
  - space_id: 2
    target: "B/Rm2"
"""
    with tempfile.TemporaryDirectory() as tmp:
        map_path = os.path.join(tmp, "map.yaml")
        with open(map_path, "w", encoding="utf-8") as fh:
            fh.write(map_text)
        csv_path = os.path.join(tmp, "out.csv")
        cfg = load_config("/nonexistent/config.yaml")
        cfg["collegenet"]["base_url"] = "http://stub"
        cfg["space_map_file"] = map_path
        cfg["safety"]["state_file"] = os.path.join(tmp, "last_run.json")
        cfg["systems"] = {"sys": {"driver": "preview", "csv_file": csv_path}}
        cfg["default_system"] = "sys"

        original = sync_mod._fetch
        sync_mod._fetch = lambda *a, **kw: []
        try:
            code = sync_mod.run_sync(cfg)
            wrote_anything = os.path.exists(csv_path)
            # --force is the documented escape hatch and must actually work.
            forced = sync_mod.run_sync(cfg, force=True)
        finally:
            sync_mod._fetch = original
        forced_wrote = os.path.exists(csv_path)

    assert code == sync_mod.EXIT_SAFETY_ABORT, code
    assert not wrote_anything, "the aborted run must not have written"
    assert forced == sync_mod.EXIT_OK, forced
    assert forced_wrote, "--force must let the run through"

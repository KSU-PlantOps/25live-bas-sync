"""Offline tests: safety rail."""

import os
import tempfile

from bassync.model import Destination, OccupancyWindow
from tests.helpers import base_config, dest, dt


def test_safety_blocks_a_campus_wide_clear():
    """Zero events from 25Live is an auth/query failure far more often than an
    empty campus, and writing it would stand every building down."""
    from bassync import safety
    cfg = base_config()
    verdict = safety.check(cfg, {}, {dest("A/Rm1"), dest("A/Rm2")},
                           event_count=0, previous={})
    assert not verdict
    assert "min_events" in verdict.reason


def test_safety_blocks_a_large_partial_clear():
    """Most of the campus going dark at once is stopped even when some events
    did come back."""
    from bassync import safety
    previous = {"windows": {"sys:A/Rm1": 3, "sys:A/Rm2": 2, "sys:A/Rm3": 4}}
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm2"): [], dest("A/Rm3"): []}
    verdict = safety.check(base_config(), schedule, set(schedule),
                           event_count=1, previous=previous)
    assert not verdict and "max_cleared_fraction" in verdict.reason, verdict.reason


def test_safety_allows_a_normal_run():
    """One room going quiet is ordinary and must not block the night's sync."""
    from bassync import safety
    previous = {"windows": {"sys:A/Rm1": 3, "sys:A/Rm2": 2, "sys:A/Rm3": 4}}
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm2"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm3"): []}
    assert safety.check(base_config(), schedule, set(schedule),
                        event_count=12, previous=previous)


def test_safety_allows_the_first_ever_run():
    """No history means nothing to compare against — a guard that blocks the
    first run is just an outage."""
    from bassync import safety
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))]}
    verdict = safety.check(base_config(), schedule, set(schedule),
                           event_count=5, previous={})
    assert verdict and "no previous run" in verdict.reason


def test_safety_only_counts_schedules_this_run_manages():
    """The comparison is scoped to what the run actually writes.

    Two ways that matters: a --system run only manages one BAS, and a room
    removed from the map is no longer written at all. Counting either as
    "cleared" would block a perfectly normal sync with a false alarm about
    buildings nobody is touching."""
    from bassync import safety
    previous = {"windows": {"supervisor:A/Rm1": 3, "campus_bacnet:12001:5": 2,
                            "campus_bacnet:12001:6": 4}}
    # A --system supervisor run: all_destinations holds only its schedules.
    only = Destination("supervisor", "A/Rm1")
    assert safety.check(base_config(), {only: [OccupancyWindow(dt(9), dt(10))]},
                        {only}, event_count=5, previous=previous)


def test_safety_ignores_rooms_removed_from_the_map():
    """Decommissioning rooms must not look like a mass clear."""
    from bassync import safety
    previous = {"windows": {f"sys:A/Rm{i}": 3 for i in range(1, 7)}}
    kept = {dest("A/Rm1"), dest("A/Rm2")}
    schedule = {d: [OccupancyWindow(dt(9), dt(10))] for d in kept}
    verdict = safety.check(base_config(), schedule, kept, event_count=8,
                           previous=previous)
    assert verdict, verdict.reason


def test_safety_state_merges_on_a_scoped_run():
    """A --system run must not wipe the other systems' baseline; the next full
    run would then trip its own rail."""
    from bassync import safety
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "last_run.json")
        safety.save_state(path, {Destination("a", "R1"): [OccupancyWindow(dt(9), dt(10))],
                                 Destination("b", "R2"): [OccupancyWindow(dt(9), dt(10))]},
                          event_count=4)
        safety.save_state(path, {Destination("a", "R1"): []}, event_count=1,
                          only_system="a")
        state = safety.load_state(path)
    assert state["windows"] == {"a:R1": 0, "b:R2": 1}, state


def test_safety_can_be_disabled():
    from bassync import safety
    cfg = base_config()
    cfg["safety"]["enabled"] = False
    assert safety.check(cfg, {}, {dest("A/Rm1")}, event_count=0, previous={})


def test_safety_state_roundtrip():
    from bassync import safety
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "last_run.json")
        safety.save_state(path, {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                                 dest("A/Rm2"): []}, event_count=7)
        state = safety.load_state(path)
    assert state["event_count"] == 7
    assert state["windows"] == {"sys:A/Rm1": 1, "sys:A/Rm2": 0}, state
    assert safety.load_state("/nonexistent/state.json") == {}

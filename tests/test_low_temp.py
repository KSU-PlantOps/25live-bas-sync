"""Low temp: a room's (or equipment's) low-temp schedule, driven only by the
bookings of events marked low temp — and by extra bookings marked so."""

import json
from pathlib import Path

import pytest
import yaml

import bassync.sync as sync_mod
from bassync import extras, lowtemp, mapedit, scheduled
from bassync.config import load_config
from bassync.model import Destination, RawEvent
from bassync.report import RunReport
from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map
from tests.helpers import TZ, dt

MAP = """
buildings:
  - id: stu
    name: Student Center
    target: "S/Occ"
    equipment:
      - id: ahu_2
        name: AHU-2
        target: "S/AHU2"
        low_temp_target: "S/AHU2-Cold"
spaces:
  - space_id: 1
    space_name: Ballroom
    building: stu
    target: "S/Ball"
    low_temp_target: "S/Ball-Cold"
    equipment: [ahu_2]
  - space_id: 2
    space_name: Room 204
    building: stu
    equipment: [ahu_2]
  - space_id: 3
    space_name: Lounge
    building: stu
    target: "S/Lounge"
"""


def d(target):
    return Destination("sys", target)


@pytest.fixture
def campus(tmp_path, monkeypatch):
    map_path = tmp_path / "map.yaml"
    map_path.write_text(MAP, encoding="utf-8")
    cfg = load_config("/nonexistent/config.yaml")
    cfg["collegenet"]["base_url"] = "http://stub"
    cfg["space_map_file"] = str(map_path)
    cfg["safety"]["state_file"] = str(tmp_path / "state" / "last_run.json")
    cfg["low_temp_file"] = str(tmp_path / "low_temp_events.yaml")
    cfg["extra_bookings_file"] = str(tmp_path / "extra_bookings.yaml")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    cfg["default_system"] = "sys"
    events = [RawEvent("BLOOD", "Blood drive", "1", dt(9, day=10), dt(15, day=10),
                       dt(10, day=10), dt(14, day=10)),
              RawEvent("CLASS", "Class", "1", dt(16, day=10), dt(17, day=10)),
              RawEvent("BLOOD", "Blood drive", "2", dt(9, day=10), dt(15, day=10)),
              RawEvent("TALK", "Talk", "3", dt(9, day=11), dt(10, day=11))]
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: list(events))
    return cfg, tmp_path


def _mark(cfg, *ids):
    rows: list = []
    for eid in ids:
        lowtemp.mark(rows, eid, name=f"Event {eid}", by="tester")
    lowtemp.save(cfg["low_temp_file"], rows)


# ── the room map ─────────────────────────────────────────────────────────────

def test_rooms_and_equipment_name_their_low_temp_schedules(campus):
    cfg, _tmp = campus
    sm = load_space_map(cfg["space_map_file"], cfg)
    assert not sm.errors and not sm.warnings
    assert sm.spaces["1"].low_temp_destinations == (d("S/Ball-Cold"), d("S/AHU2-Cold"))
    assert sm.spaces["2"].low_temp_destinations == (d("S/AHU2-Cold"),)
    assert sm.spaces["3"].low_temp_destinations == ()
    assert sm.low_temp == {d("S/Ball-Cold"), d("S/AHU2-Cold")}
    # Managed like any schedule: cleared when nothing marked books them.
    assert sm.low_temp <= sm.destinations()
    assert sm.low_temp <= sm.building_destinations(["stu"])
    assert sm.labels[d("S/Ball-Cold")] == "Ballroom — low temp"
    assert sm.labels[d("S/AHU2-Cold")] == "AHU-2 (Student Center) — low temp"


def test_a_low_temp_target_that_is_also_an_occupancy_schedule_is_refused(tmp_path):
    path = tmp_path / "map.yaml"
    path.write_text(MAP.replace('low_temp_target: "S/Ball-Cold"', 'low_temp_target: "S/Lounge"'))
    cfg = load_config("/nonexistent/config.yaml")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    sm = load_space_map(str(path), cfg)
    assert any("S/Lounge is also an occupancy schedule" in e for e in sm.errors)
    assert d("S/Lounge") not in sm.low_temp
    assert sm.spaces["1"].low_temp_destinations == (d("S/AHU2-Cold"),)


def test_the_editors_check_the_low_temp_target():
    raw = {"systems": {"campus": {"driver": "bacnet", "local_address": "10.0.0.5/24"}},
           "default_system": "campus"}
    room = {"space_id": 5, "target": "12001:5", "low_temp_target": "12001:5"}
    assert "same schedule as the Target" in mapedit.room_problem(room, [], [], raw)
    room["low_temp_target"] = "not a target"
    assert "Low-temp target" in mapedit.room_problem(room, [], [], raw)
    room["low_temp_target"] = "12001:9"
    assert mapedit.room_problem(room, [], [], raw) is None


# ── building the schedules ───────────────────────────────────────────────────

def test_only_marked_bookings_run_the_low_temp_schedules(campus):
    cfg, _tmp = campus
    sm = load_space_map(cfg["space_map_file"], cfg)
    events = [RawEvent("A", "Blood drive", "1", dt(9), dt(12), low_temp=True),
              RawEvent("B", "Class", "1", dt(13), dt(14)),
              RawEvent("C", "Blood drive", "2", dt(10), dt(15), low_temp=True)]
    built = ScheduleBuilder(0).build(events, sm)
    assert [(w.start, w.end) for w in built[d("S/Ball-Cold")]] == [(dt(9), dt(12))]
    # The AHU's low-temp schedule is the union of the rooms it serves.
    assert [(w.start, w.end) for w in built[d("S/AHU2-Cold")]] == [(dt(9), dt(15))]
    # Occupancy is unchanged: every booking still runs the room.
    assert [(w.start, w.end) for w in built[d("S/Ball")]] == [(dt(9), dt(12)), (dt(13), dt(14))]


def test_a_sync_writes_marked_events_to_the_low_temp_schedules(campus):
    cfg, tmp = campus
    _mark(cfg, "BLOOD")
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_OK
    written = {s.target: s for s in report.schedules}
    assert [(w.start, w.end) for w in written["S/Ball-Cold"].windows] == [(dt(9, day=10),
                                                                           dt(15, day=10))]
    assert len(written["S/AHU2-Cold"].windows) == 1
    assert report.low_temp_bookings == 2
    assert "Low temp: 2 booking(s) marked low temp" in "\n".join(report.summary_lines())
    # Unmarked, the next sync clears them.
    _mark(cfg)
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_OK
    written = {s.target: s for s in report.schedules}
    assert written["S/Ball-Cold"].windows == [] and written["S/AHU2-Cold"].windows == []


def test_low_temp_schedules_ending_are_not_a_mass_clear(campus):
    cfg, _tmp = campus
    _mark(cfg, "BLOOD", "CLASS", "TALK")
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK
    _mark(cfg)
    cfg["safety"]["max_cleared_fraction"] = 0.0          # any real clear would block
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK


def test_an_unreadable_marks_file_leaves_the_low_temp_schedules_alone(campus):
    cfg, _tmp = campus
    Path(cfg["low_temp_file"]).write_text("events: {not: a list}\n")
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_NO_MAP
    status = {s.target: s.status for s in report.schedules}
    assert status["S/Ball-Cold"] == status["S/AHU2-Cold"] == "not written"
    assert status["S/Ball"] == "preview"                     # the rest still syncs
    assert "unreadable low-temp events file" in report.outcome


def test_a_marked_event_in_a_room_without_a_low_temp_schedule_is_reported(campus, caplog):
    cfg, _tmp = campus
    _mark(cfg, "TALK")
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK
    assert "Event TALK" not in caplog.text                  # it logs the 25Live title
    assert "Talk is marked low temp in Lounge, which has no low-temp schedule" in caplog.text


# ── extra bookings ───────────────────────────────────────────────────────────

def test_a_rooms_extra_booking_can_be_low_temp(campus):
    cfg, _tmp = campus
    Path(cfg["extra_bookings_file"]).write_text(yaml.safe_dump({"bookings": [
        {"title": "Donor prep", "space_id": 1, "date": "2026-06-12", "start": "08:00",
         "end": "09:00", "exact": True, "low_temp": True}]}))
    sm = load_space_map(cfg["space_map_file"], cfg)
    got = extras.expand(cfg["extra_bookings_file"], sm, TZ, 30, now=dt(7))
    assert got.events[0].low_temp and got.events[0].booked_start == dt(8, day=12)
    built = ScheduleBuilder(0).build(got.events, sm)
    assert [(w.start, w.end) for w in built[d("S/Ball-Cold")]] == [(dt(8, day=12),
                                                                    dt(9, day=12))]
    with pytest.raises(extras.BookingError, match="for a room's booking"):
        extras.parse({"title": "x", "building": "stu", "date": "2026-06-12",
                      "start": "08:00", "end": "09:00", "low_temp": True})
    assert extras.clean_row({"title": "x", "low_temp": False}) == {"title": "x"}


# ── the marks file ───────────────────────────────────────────────────────────

def test_marks_are_kept_by_event_id(tmp_path):
    path = tmp_path / lowtemp.FILE_NAME
    rows: list = []
    assert lowtemp.mark(rows, 48213, "Blood drive", "Jane", "2026-09-30 10:00")
    assert not lowtemp.mark(rows, "48213", "again")
    lowtemp.save(path, rows)
    assert lowtemp.event_ids(path) == {"48213"}
    assert yaml.safe_load(path.read_text())["events"][0] == {
        "event_id": "48213", "name": "Blood drive", "marked_by": "Jane",
        "marked_at": "2026-09-30 10:00"}
    rows = lowtemp.read(path)
    assert lowtemp.unmark(rows, "48213") and not lowtemp.unmark(rows, "48213")
    assert lowtemp.read(tmp_path / "missing.yaml") == []


# ── what was scheduled ───────────────────────────────────────────────────────

def test_each_sync_records_what_it_wrote_per_space(campus):
    cfg, tmp = campus
    _mark(cfg, "BLOOD")
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK
    data = json.loads(scheduled.path_for(cfg).read_text())
    ballroom = data["spaces"]["1"]
    assert ballroom["name"] == "Ballroom" and ballroom["building"] == "stu"
    assert ballroom["schedules"] == ["sys:S/Ball", "sys:S/AHU2", "sys:S/Occ",
                                     "sys:S/Ball-Cold", "sys:S/AHU2-Cold"]
    blood = ballroom["bookings"][0]
    assert blood["title"] == "Blood drive" and blood["low_temp"] and not blood["extra"]
    assert blood["start"] == dt(10, day=10).isoformat()        # the booking itself
    assert blood["on"] == dt(9, day=10).isoformat()            # when it runs
    cold = data["schedules"]["sys:S/Ball-Cold"]
    assert cold["kind"] == "low_temp" and cold["status"] == "preview"
    assert cold["windows"] == [[dt(9, day=10).isoformat(), dt(15, day=10).isoformat()]]
    assert data["schedules"]["sys:S/AHU2"]["kind"] == "equipment"
    assert data["schedules"]["sys:S/Occ"]["kind"] == "building"


def test_a_limited_sync_updates_only_its_part(campus):
    cfg, tmp = campus
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK
    first = json.loads(scheduled.path_for(cfg).read_text())
    assert sync_mod.run_sync(cfg, only_system="nope") == sync_mod.EXIT_NO_MAP
    assert json.loads(scheduled.path_for(cfg).read_text()) == first    # nothing written


def test_a_schedule_held_for_the_marks_file_says_so(campus):
    """It used to blame a broken room-map row or extra booking."""
    cfg, _tmp = campus
    Path(cfg["low_temp_file"]).write_text("events: {not: a list}\n")
    report = RunReport("SYNC", "t", TZ)
    sync_mod.run_sync(cfg, report=report)
    why = {s.target: s.error for s in report.schedules if s.status == "not written"}
    assert "the low-temp events file can't be read" in why["S/Ball-Cold"]
    assert "room-map row" not in why["S/Ball-Cold"]


def test_validate_reads_the_marks_file(campus, caplog):
    cfg, _tmp = campus
    cfg["collegenet"]["base_url"] = ""
    Path(cfg["low_temp_file"]).write_text("events: {not: a list}\n")
    with caplog.at_level("INFO"):
        assert sync_mod.run_validate(cfg) == sync_mod.EXIT_VALIDATION_FAILED
    assert "[FAIL] Low-temp events load" in caplog.text
    _mark(cfg, "BLOOD", "TALK")
    caplog.clear()
    with caplog.at_level("INFO"):
        sync_mod.run_validate(cfg)
    assert "[PASS] Low-temp events load — 2 event(s) marked" in caplog.text


def test_validate_can_check_one_system(campus, caplog):
    cfg, _tmp = campus
    cfg["collegenet"]["base_url"] = ""
    cfg["systems"]["other"] = {"driver": "preview"}
    with caplog.at_level("INFO"):
        sync_mod.run_validate(cfg, only_system="other")
    assert "System 'other' reachable" in caplog.text
    assert "System 'sys'" not in caplog.text

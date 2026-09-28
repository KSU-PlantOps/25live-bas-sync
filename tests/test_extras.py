"""Extra bookings: occupancy the sync schedules that isn't in 25Live."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
import yaml

import bassync.sync as sync_mod
from bassync import extras
from bassync.config import load_config
from bassync.model import RawEvent
from bassync.report import RunReport
from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map
from tests.helpers import TZ, base_config, dest, with_yaml

MAP = """
buildings:
  - id: sci
    name: Science Hall
    target: "SCI/Occ"
    pre_condition_minutes: 45
    post_buffer_minutes: 10
  - id: annex
    name: Annex
    target: "ANX/Occ"
floors:
  - building: sci
    level: 2
    target: "SCI/F2"
spaces:
  - space_id: 101
    space_name: Science Hall 101
    building: sci
    target: "SCI/Rm101"
    pre_condition_minutes: 30
    post_buffer_minutes: 15
  - space_id: 201
    building: sci
    floor: 2
"""
NOW = datetime(2026, 10, 5, 7, 0, tzinfo=TZ)          # a Monday morning


def _map():
    cfg = base_config()
    sm = with_yaml(MAP, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    return sm


def _expand(rows, sm=None, now=NOW, days=7, tmp=None):
    sm = sm or _map()
    path = Path(tmp) / "extra_bookings.yaml"
    path.write_text(yaml.safe_dump({"bookings": rows}), encoding="utf-8")
    return extras.expand(path, sm, TZ, days, now=now)


# ── reading a row ────────────────────────────────────────────────────────────

def test_one_day_booking_parses():
    b = extras.parse({"title": "Open house", "building": "sci", "date": "2026-10-06",
                      "start": "08:00", "end": "14:00"})
    assert (b.building, b.day, b.start, b.end, b.overnight) == (
        "sci", date(2026, 10, 6), time(8), time(14), False)
    assert b.where == "building sci"
    assert b.when() == "Tue Oct 06, 2026 08:00–14:00"


def test_weekly_booking_parses_days_in_any_spelling():
    b = extras.parse({"building": "sci", "floor": "2", "days": ["Wednesday", "mon"],
                      "from": date(2026, 9, 1), "until": "2026-12-12",
                      "start": 1080, "end": "21:00"})        # 18:00 as YAML 1.1 reads it
    assert b.days == (0, 2) and b.floor == 2 and b.start == time(18)
    assert b.when() == "Every Mon, Wed 18:00–21:00, Sep 01 – Dec 12, 2026"
    assert b.title == "Extra booking"


def test_room_ids_are_normalised_like_the_room_map():
    assert extras.parse({"space_id": 101.0, "date": "2026-10-06",
                         "start": "8:00", "end": "9:00"}).space_id == "101"


def test_overnight_and_midnight_ends():
    late = extras.parse({"building": "sci", "date": "2026-10-06",
                         "start": "22:00", "end": "02:00"})
    assert late.overnight and late.span() == "22:00–02:00 (next day)"
    midnight = extras.parse({"building": "sci", "date": "2026-10-06",
                             "start": "18:00", "end": "24:00"})
    assert midnight.overnight and midnight.span() == "18:00–24:00"


@pytest.mark.parametrize("row, message", [
    ({"space_id": 1, "building": "sci", "date": "2026-10-06", "start": "8:00", "end": "9:00"},
     "not both"),
    ({"date": "2026-10-06", "start": "8:00", "end": "9:00"}, "either a room"),
    ({"space_id": 1, "floor": 2, "date": "2026-10-06", "start": "8:00", "end": "9:00"},
     "`floor` goes with `building`"),
    ({"building": "sci", "start": "8:00", "end": "9:00"}, "either a `date`"),
    ({"building": "sci", "date": "2026-10-06", "days": ["mon"], "start": "8:00",
      "end": "9:00"}, "either a `date`"),
    ({"building": "sci", "date": "2026-10-06", "until": "2026-10-09", "start": "8:00",
      "end": "9:00"}, "for weekly bookings"),
    ({"building": "sci", "days": ["funday"], "start": "8:00", "end": "9:00"},
     "not a day of the week"),
    ({"building": "sci", "days": ["mon"], "from": "2026-10-09", "until": "2026-10-01",
      "start": "8:00", "end": "9:00"}, "before `from`"),
    ({"building": "sci", "date": "10/06/2026", "start": "8:00", "end": "9:00"},
     "must be a date"),
    ({"building": "sci", "date": "2026-10-06", "start": "8:00"}, "`start` and an `end`"),
    ({"building": "sci", "date": "2026-10-06", "start": "8:00", "end": "8:00"},
     "the same time"),
    ({"building": "sci", "date": "2026-10-06", "start": "25:00", "end": "8:00"},
     "24-hour time"),
    ({"building": "sci", "date": "2026-10-06", "start": "24:00", "end": "8:00"},
     "before midnight"),
    ("nope", "not a mapping"),
])
def test_bad_rows_say_why(row, message):
    with pytest.raises(extras.BookingError, match=message):
        extras.parse(row)


def test_weekly_start_dates_respect_bounds():
    b = extras.parse({"building": "sci", "days": ["mon", "fri"], "from": "2026-10-06",
                      "until": "2026-10-16", "start": "8:00", "end": "9:00"})
    got = b.start_dates(date(2026, 10, 1), date(2026, 10, 31))
    assert got == [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 16)]
    assert b.ended(date(2026, 10, 17)) and not b.ended(date(2026, 10, 16))
    open_ended = extras.parse({"building": "sci", "days": ["mon"], "start": "8:00",
                               "end": "9:00"})
    assert not open_ended.ended(date(2099, 1, 1))


# ── turning rows into occupancy ──────────────────────────────────────────────

def test_room_booking_gets_the_room_buffers_and_goes_through_the_room(tmp_path):
    out = _expand([{"title": "Make-up lab", "space_id": 101, "date": "2026-10-06",
                    "start": "13:00", "end": "16:00"}], tmp=tmp_path)
    assert not out.errors and not out.warnings and out.occurrences == 1
    [ev] = out.events
    assert (ev.space_id, ev.title) == ("101", "Make-up lab")
    assert ev.start == datetime(2026, 10, 6, 12, 30, tzinfo=TZ)     # 30 min run-up
    assert ev.end == datetime(2026, 10, 6, 16, 15, tzinfo=TZ)       # 15 min run-down
    assert ev.event_id.startswith("extra:1:")


def test_exact_booking_has_no_buffers(tmp_path):
    out = _expand([{"space_id": 101, "date": "2026-10-06", "start": "13:00",
                    "end": "16:00", "exact": True}], tmp=tmp_path)
    [ev] = out.events
    assert (ev.start.hour, ev.end.hour, ev.end.minute) == (13, 16, 0)


def test_building_and_floor_bookings_use_the_building_buffers(tmp_path):
    out = _expand([
        {"building": "sci", "date": "2026-10-06", "start": "08:00", "end": "12:00"},
        {"building": "sci", "floor": 2, "date": "2026-10-07", "start": "18:00",
         "end": "21:00"},
    ], tmp=tmp_path)
    assert not out.events and out.occurrences == 2
    [bw] = out.windows[dest("SCI/Occ")]
    assert (bw.start, bw.end) == (datetime(2026, 10, 6, 7, 15, tzinfo=TZ),
                                  datetime(2026, 10, 6, 12, 10, tzinfo=TZ))
    [fw] = out.windows[dest("SCI/F2")]
    assert fw.start == datetime(2026, 10, 7, 17, 15, tzinfo=TZ)


def test_only_occurrences_inside_the_run_window(tmp_path):
    out = _expand([{"building": "annex", "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "06:00", "end": "07:30", "exact": True}],
                  tmp=tmp_path, days=3)
    starts = [w.start for w in out.windows[dest("ANX/Occ")]]
    # From today (its occurrence is still today's) up to three days from now
    # at 07:00 — the same horizon as the 25Live fetch; nothing before today.
    assert [s.date() for s in starts] == [date(2026, 10, 5), date(2026, 10, 6),
                                         date(2026, 10, 7), date(2026, 10, 8)]


def test_last_nights_overnight_booking_is_clipped_to_today(tmp_path):
    out = _expand([{"building": "annex", "date": "2026-10-04", "start": "22:00",
                    "end": "09:00", "exact": True}], tmp=tmp_path)
    [w] = out.windows[dest("ANX/Occ")]
    assert w.start == datetime(2026, 10, 5, 0, 0, tzinfo=TZ)
    assert w.end == datetime(2026, 10, 5, 9, 0, tzinfo=TZ)


def test_past_bookings_add_nothing(tmp_path):
    out = _expand([{"building": "annex", "date": "2026-10-01", "start": "08:00",
                    "end": "09:00"}], tmp=tmp_path)
    assert out.occurrences == 0 and not out.windows


def test_rows_naming_unknown_places_are_warnings_not_errors(tmp_path):
    out = _expand([
        {"space_id": 999, "date": "2026-10-06", "start": "8:00", "end": "9:00"},
        {"building": "nope", "date": "2026-10-06", "start": "8:00", "end": "9:00"},
        {"building": "sci", "floor": 7, "date": "2026-10-06", "start": "8:00", "end": "9:00"},
    ], tmp=tmp_path)
    assert not out.errors and len(out.warnings) == 3
    assert "room 999 isn't in the room map" in out.warnings[0]
    assert not out.events and not out.windows


def test_broken_row_holds_what_it_would_have_driven(tmp_path):
    out = _expand([
        {"title": "Typo", "space_id": 101, "date": "2026-10-06", "start": "1300",
         "end": "16:00"},
        {"building": "sci", "floor": 2, "days": ["someday"], "start": "8:00", "end": "9:00"},
    ], tmp=tmp_path)
    assert len(out.errors) == 2 and "Extra booking 1 (Typo)" in out.errors[0]
    assert out.held == {dest("SCI/Rm101"), dest("SCI/Occ"), dest("SCI/F2")}


def test_missing_file_is_no_bookings():
    out = extras.expand("/nonexistent/extra_bookings.yaml", _map(), TZ, 7, now=NOW)
    assert (out.events, out.windows, out.errors) == ([], {}, [])


def test_builder_merges_extra_windows_into_the_roll_up(tmp_path):
    sm = _map()
    out = _expand([{"building": "sci", "date": "2026-10-06", "start": "09:30",
                    "end": "11:00", "exact": True}], sm=sm, tmp=tmp_path)
    room = RawEvent("E1", "Class", "101", datetime(2026, 10, 6, 8, tzinfo=TZ),
                    datetime(2026, 10, 6, 10, tzinfo=TZ))
    schedule = ScheduleBuilder(5).build([room] + out.events, sm, out.windows)
    [w] = schedule[dest("SCI/Occ")]
    assert (w.start.hour, w.end.hour) == (8, 11)           # one window, both sources
    assert [x.end.hour for x in schedule[dest("SCI/Rm101")]] == [10]


def test_a_building_with_no_rooms_is_still_managed():
    """Its entry names a schedule the sync owns: cleared when nothing books
    it, and drivable by extra bookings."""
    sm = _map()
    assert dest("ANX/Occ") in sm.destinations()
    assert set(sm.buildings) == {"sci", "annex"}
    assert sm.buildings["sci"].pre_condition_minutes == 45
    assert sm.buildings["annex"].post_buffer_minutes == 15      # the global default
    assert sm.floors == {("sci", 2): dest("SCI/F2")}


def test_invalid_building_target_without_rooms_is_reported():
    cfg = base_config()
    cfg["systems"] = {"sys": {"driver": "bacnet", "local_address": "10.0.0.1/24"}}
    sm = with_yaml("buildings:\n  - id: x\n    target: 'not-a-target'\n",
                   lambda p: load_space_map(p, cfg))
    assert any("not-a-target" in e or "Building x" in e for e in sm.errors), sm.errors
    assert "x" not in sm.buildings


def test_dump_and_read_round_trip(tmp_path):
    rows = [{"title": "Tour", "building": "sci", "date": date(2026, 10, 6),
             "start": "08:00", "end": "09:00", "exact": False, "note": ""}]
    path = tmp_path / "extra_bookings.yaml"
    assert extras.save(path, rows)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Bookings the sync schedules")
    assert extras.read(path) == [{"title": "Tour", "building": "sci",
                                  "date": "2026-10-06", "start": "08:00",
                                  "end": "09:00"}]
    assert not extras.save(path, rows)                  # unchanged: not rewritten


def test_config_puts_the_file_beside_config_yaml(tmp_path):
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["extra_bookings_file"] == str(tmp_path / "extra_bookings.yaml")


# ── a whole run ──────────────────────────────────────────────────────────────

@pytest.fixture
def campus(tmp_path, monkeypatch):
    map_path = tmp_path / "map.yaml"
    map_path.write_text(MAP, encoding="utf-8")
    cfg = load_config(str(tmp_path / "config.yaml"))
    cfg["collegenet"]["base_url"] = "http://stub"
    cfg["space_map_file"] = str(map_path)
    cfg["safety"]["state_file"] = str(tmp_path / "state" / "last_run.json")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    cfg["default_system"] = "sys"
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: [])
    return cfg, tmp_path


def _tomorrow() -> str:
    return (datetime.now(TZ).date() + timedelta(days=1)).isoformat()


def _state(cfg) -> dict:
    return json.loads(Path(cfg["safety"]["state_file"]).read_text(encoding="utf-8"))


def test_run_writes_extra_bookings_and_reports_them(campus):
    cfg, tmp = campus
    (tmp / "extra_bookings.yaml").write_text(yaml.safe_dump({"bookings": [
        {"title": "Open house", "building": "annex", "date": _tomorrow(),
         "start": "09:00", "end": "12:00"}]}), encoding="utf-8")
    report = RunReport("SYNC", "t", TZ)
    cfg["safety"]["min_events"] = 0
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_OK
    assert _state(cfg)["windows"]["sys:ANX/Occ"] == 1
    assert _state(cfg)["event_count"] == 0             # 25Live's count only
    assert report.extra_bookings == 1
    assert "Extra bookings: 1 occurrence(s) not from 25Live" in report.summary_lines()


def test_extra_bookings_dont_count_as_25live_bookings(campus):
    """A 25Live outage still trips min_events, whatever the extra bookings."""
    cfg, tmp = campus
    (tmp / "extra_bookings.yaml").write_text(yaml.safe_dump({"bookings": [
        {"building": "annex", "date": _tomorrow(), "start": "09:00", "end": "12:00"}]}),
        encoding="utf-8")
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_SAFETY_ABORT


def test_broken_extra_booking_alerts_and_holds_its_schedules(campus):
    cfg, tmp = campus
    cfg["safety"]["min_events"] = 0
    (tmp / "extra_bookings.yaml").write_text(yaml.safe_dump({"bookings": [
        {"building": "sci", "date": _tomorrow(), "start": "9", "end": "12:00"}]}),
        encoding="utf-8")
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_NO_MAP
    assert "broken extra booking" in report.outcome
    windows = _state(cfg)["windows"]
    assert "sys:SCI/Occ" not in windows and "sys:ANX/Occ" in windows
    held = [s for s in report.schedules if s.status == "not written"]
    assert [s.target for s in held] == ["SCI/Occ"]


def test_unreadable_extra_bookings_file_writes_nothing(campus):
    cfg, tmp = campus
    cfg["safety"]["min_events"] = 0
    (tmp / "extra_bookings.yaml").write_text("bookings: [unclosed", encoding="utf-8")
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_NO_MAP
    assert "unreadable" in report.outcome
    assert not Path(cfg["safety"]["state_file"]).exists()


def test_validate_checks_the_extra_bookings(campus, monkeypatch, caplog):
    cfg, tmp = campus
    (tmp / "extra_bookings.yaml").write_text(yaml.safe_dump({"bookings": [
        {"building": "sci", "date": _tomorrow(), "start": "9", "end": "12:00"}]}),
        encoding="utf-8")
    cfg["collegenet"]["base_url"] = ""
    with caplog.at_level("INFO"):
        assert sync_mod.run_validate(cfg) == sync_mod.EXIT_VALIDATION_FAILED
    assert "Extra bookings load" in caplog.text and "24-hour time" in caplog.text

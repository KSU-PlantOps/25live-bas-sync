"""A sync limited to some buildings (--building): what it writes, what it
leaves alone, and how it keeps the safety baseline."""

import json
import logging
from pathlib import Path

import pytest

import bassync.sync as sync_mod
from bassync import cli
from bassync.config import load_config
from bassync.model import Destination, RawEvent
from bassync.report import RunReport
from bassync.spacemap import load_space_map
from tests.helpers import TZ, dt

MAP = """
buildings:
  - id: sci
    name: Science
    target: "S/Occ"
    equipment:
      - id: ahu
        target: "S/AHU"
  - id: art
    name: Art
    target: "A/Occ"
floors:
  - building: sci
    level: 1
    target: "S/F1"
spaces:
  - space_id: 1
    building: sci
    floor: 1
    target: "S/Rm1"
    equipment: [ahu]
  - space_id: 2
    building: sci
    system: other
    target: "O/Rm2"
  - space_id: 3
    building: art
    target: "A/Rm3"
"""


def d(target, system="sys"):
    return Destination(system, target)


@pytest.fixture
def campus(tmp_path, monkeypatch):
    map_path = tmp_path / "map.yaml"
    map_path.write_text(MAP, encoding="utf-8")
    cfg = load_config("/nonexistent/config.yaml")
    cfg["collegenet"]["base_url"] = "http://stub"
    cfg["space_map_file"] = str(map_path)
    cfg["safety"]["state_file"] = str(tmp_path / "state" / "last_run.json")
    cfg["systems"] = {"sys": {"driver": "preview"}, "other": {"driver": "preview"}}
    cfg["default_system"] = "sys"
    events = [RawEvent("E1", "Class", "1", dt(9, day=10), dt(10, day=10)),
              RawEvent("E3", "Art", "3", dt(13, day=10), dt(14, day=10))]
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: list(events))
    return cfg, tmp_path


def _state(cfg) -> dict:
    return json.loads(Path(cfg["safety"]["state_file"]).read_text())["windows"]


def test_a_buildings_schedules_are_its_own_its_floors_equipment_and_rooms(campus):
    cfg, _tmp = campus
    sm = load_space_map(cfg["space_map_file"], cfg)
    assert sm.building_destinations(["sci"]) == {
        d("S/Occ"), d("S/F1"), d("S/AHU"), d("S/Rm1"), d("O/Rm2", "other")}
    assert sm.building_destinations(["art"]) == {d("A/Occ"), d("A/Rm3")}
    assert sm.building_ids == {"sci", "art"}


def test_a_building_sync_writes_only_that_building(campus):
    cfg, _tmp = campus
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report, only_buildings=["art"]) == sync_mod.EXIT_OK
    assert set(_state(cfg)) == {"sys:A/Occ", "sys:A/Rm3"}
    assert {s.target for s in report.schedules} == {"A/Occ", "A/Rm3"}
    assert report.only_buildings == ["art"]
    assert "Limited to building: art" in "\n".join(report.summary_lines())


def test_it_keeps_the_rest_of_the_baseline(campus):
    """A full run, then one building: the other building's baseline stays, so
    the next full run isn't read as a mass clear."""
    cfg, _tmp = campus
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK
    full = _state(cfg)
    assert sync_mod.run_sync(cfg, only_buildings=["art"]) == sync_mod.EXIT_OK
    assert _state(cfg) == full


def test_an_unknown_building_writes_nothing(campus):
    cfg, _tmp = campus
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report,
                             only_buildings=["nope"]) == sync_mod.EXIT_NO_MAP
    assert "no building nope" in report.outcome
    assert not Path(cfg["safety"]["state_file"]).exists()


def test_a_dry_run_can_be_limited_to_a_building(campus, caplog):
    cfg, _tmp = campus
    caplog.set_level(logging.INFO)
    assert sync_mod.run_sync(cfg, dry_run=True, only_buildings=["sci"]) == sync_mod.EXIT_OK
    assert "Limited to building 'sci': 5 schedule(s)" in caplog.text
    assert "A/Rm3" not in caplog.text.split("DRY RUN")[-1]


def test_the_command_line_takes_buildings_but_not_with_a_system(capsys):
    for argv in (["--building", "a", "--system", "s"], ["--building", "a", "--validate"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert "--building" in capsys.readouterr().err


def test_a_limited_run_that_succeeds_leaves_the_success_ping_to_full_syncs(campus, monkeypatch):
    cfg, tmp = campus
    pings = []
    monkeypatch.setattr(cli, "ping_monitor", lambda mon, ok: pings.append(ok))
    monkeypatch.setattr(cli, "send_run_report", lambda *a: None)
    monkeypatch.setattr(cli, "runs_dir", lambda c: tmp / "runs")
    assert cli.live_sync(cfg, False, None, ["art"]) == sync_mod.EXIT_OK
    assert pings == []
    assert cli.live_sync(cfg, False, None, None) == sync_mod.EXIT_OK
    assert pings == [True]
    assert cli.live_sync(cfg, False, None, ["nope"]) == sync_mod.EXIT_NO_MAP
    assert pings == [True, False]                        # a failure still reports

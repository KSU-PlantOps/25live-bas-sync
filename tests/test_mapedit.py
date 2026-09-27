"""Row checks and building cascades shared by the desktop editor and the web UI."""

from bassync import mapedit

CONFIG = {
    "systems": {
        "campus": {"driver": "bacnet", "local_address": "10.0.0.5/24"},
        "west": {"driver": "rest", "base_url": "https://ebo.example.edu",
                 "write": {"method": "PUT", "path": "/s/{target}"}},
    },
    "default_system": "campus",
}
BUILDINGS = [{"id": "SCI", "name": "Science", "target": "12001:5"},
             {"id": "ART", "system": "west", "target": "art/main"}]
FLOORS = [{"building": "SCI", "level": 1, "target": "12001:6"}]
ROOMS = [{"space_id": 101, "building": "SCI", "floor": 1, "target": "12001:7"},
         {"space_id": 102, "building": "SCI"}]


def test_effective_system_inherits_then_defaults():
    assert mapedit.effective_system({"system": "west"}, BUILDINGS, CONFIG) == "west"
    assert mapedit.effective_system({}, BUILDINGS, CONFIG, "ART") == "west"
    assert mapedit.effective_system({}, BUILDINGS, CONFIG, "SCI") == "campus"
    assert mapedit.effective_system({}, [], {"systems": {"only": {}}}) == "only"
    assert mapedit.effective_system({}, [], {"systems": {"a": {}, "b": {}}}) == ""


def test_room_problem():
    check = mapedit.room_problem
    assert check({}, ROOMS, BUILDINGS, CONFIG) == "Space ID is required."
    assert "already used" in check({"space_id": 101, "target": "12001:9"},
                                   ROOMS, BUILDINGS, CONFIG)
    # Editing a row doesn't clash with itself.
    assert check(dict(ROOMS[0]), ROOMS, BUILDINGS, CONFIG, index=0) is None
    assert "drive nothing" in check({"space_id": 200}, ROOMS, BUILDINGS, CONFIG)
    assert check({"space_id": 200, "building": "SCI"}, ROOMS, BUILDINGS, CONFIG) is None
    # The target is checked against the system the row resolves to.
    problem = check({"space_id": 200, "target": "not-a-bacnet-target"},
                    ROOMS, BUILDINGS, CONFIG)
    assert problem and "system 'campus'" in problem


def test_building_and_floor_problems():
    assert mapedit.building_problem({}, BUILDINGS, CONFIG) == "Building ID is required."
    assert "already in use" in mapedit.building_problem(
        {"id": "SCI", "target": "12001:1"}, BUILDINGS, CONFIG)
    assert "Target is required" in mapedit.building_problem({"id": "NEW"}, BUILDINGS, CONFIG)
    assert mapedit.building_problem({"id": "NEW", "target": "12001:1"},
                                    BUILDINGS, CONFIG) is None

    fp = mapedit.floor_problem
    assert fp({}, FLOORS, BUILDINGS, CONFIG) == "Building is required."
    assert fp({"building": "SCI"}, FLOORS, BUILDINGS, CONFIG) == "Floor # is required."
    assert "already defined" in fp({"building": "SCI", "level": 1, "target": "12001:8"},
                                   FLOORS, BUILDINGS, CONFIG)
    assert fp({"building": "SCI", "level": 1, "target": "12001:8"},
              FLOORS, BUILDINGS, CONFIG, index=0) is None
    assert fp({"building": "SCI", "level": 2, "target": "12001:8"},
              FLOORS, BUILDINGS, CONFIG) is None


def test_rename_building_repoints_floors_and_rooms():
    floors = [dict(f) for f in FLOORS]
    rooms = [dict(r) for r in ROOMS]
    mapedit.rename_building("SCI", "SCIENCE", floors, rooms)
    assert {f["building"] for f in floors} == {"SCIENCE"}
    assert {r["building"] for r in rooms} == {"SCIENCE"}


def test_building_delete_is_refused_while_rooms_depend_on_it():
    blocked, _ = mapedit.building_delete_check("SCI", FLOORS, ROOMS)
    assert blocked and "102" in blocked and "101" not in blocked


def test_building_delete_takes_floors_and_detaches_rooms():
    rooms = [ROOMS[0], {"space_id": 300, "target": "12001:30"}]
    blocked, consequences = mapedit.building_delete_check("SCI", FLOORS, rooms)
    assert blocked is None
    assert "1 floor schedule" in consequences and "101" in consequences
    buildings, floors, rooms_after = mapedit.delete_building(
        "SCI", BUILDINGS, FLOORS, rooms)
    assert [b["id"] for b in buildings] == ["ART"]
    assert floors == []
    assert rooms_after[0] == {"space_id": 101, "target": "12001:7"}
    assert ROOMS[0]["building"] == "SCI"          # the input rows aren't mutated


def test_saving_keeps_the_file_readable_by_others(tmp_path):
    """An atomic save used to leave the file owner-only (mkstemp's 0600), so a
    sync running as another user could no longer read it."""
    import os
    import stat
    path = tmp_path / "space_mapping.yaml"
    assert mapedit.write_if_changed(path, "spaces: []\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    os.chmod(path, 0o640)
    assert mapedit.write_if_changed(path, "spaces: [] # edited\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert (tmp_path / "space_mapping.yaml.bak").read_text() == "spaces: []\n"

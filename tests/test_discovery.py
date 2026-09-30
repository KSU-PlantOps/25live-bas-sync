"""What discovery keeps for the setup guide, and its guesses at buildings."""

import json

import pytest

from bassync import discovery


@pytest.mark.parametrize("name, building", [
    ("Science Hall 1021", "Science Hall"),
    ("Science Hall 204 (Chem lab)", "Science Hall"),
    ("Science Hall, Room 101", "Science Hall"),
    ("Science Hall - Rm 3B", "Science Hall"),
    ("SCI 101", "SCI"),
    ("Liberal Arts Bldg #210", "Liberal Arts"),
    ("101 Main Street", ""),               # a number first: nothing to go on
    ("Student Center Ballroom", ""),        # no number: left to the second pass
    ("", ""),
])
def test_the_building_is_the_words_before_the_room_number(name, building):
    assert discovery.guess_from_name(name) == building


def test_buildings_come_from_25live_then_numbers_then_shared_starts():
    spaces = [
        {"space_id": "1", "space_name": "SC 204", "formal_name": "Student Center 204"},
        {"space_id": "2", "space_name": "Student Center Ballroom"},
        {"space_id": "3", "space_name": "Heritage Hall Atrium"},
        {"space_id": "4", "space_name": "Heritage Hall Lounge"},
        {"space_id": "5", "space_name": "The Quad"},
        {"space_id": "6", "space_name": "The Commons"},     # "The" alone isn't a building
        {"space_id": "7", "space_name": "ART 5", "building": "Art Center"},
    ]
    assert discovery.guess_buildings(spaces) == {
        "1": "Student Center", "2": "Student Center", "3": "Heritage Hall",
        "4": "Heritage Hall", "5": "", "6": "", "7": "Art Center"}


def test_building_ids_look_like_the_example_map_and_never_clash():
    assert discovery.building_id("Science Hall") == "science_hall"
    assert discovery.building_id("St. John's (Annex)") == "st_john_s_annex"
    assert discovery.building_id("Science Hall", taken={"science_hall"}) == "science_hall_2"
    assert discovery.building_id("!!!") == "building"


def test_what_a_discovery_found_is_kept_and_read_back_cleanly(tmp_path):
    spaces = [{"space_id": 12, "space_name": "SCI 101", "formal_name": "Science Hall 101",
               "capacity": 40, "building": "", "bookings": 3}]
    path = discovery.save(tmp_path, 60, spaces)
    assert path == tmp_path / "discovery.json"
    kept = discovery.load(tmp_path)
    assert kept["days"] == 60 and kept["when"]
    assert kept["spaces"] == [{"space_id": "12", "space_name": "SCI 101",
                               "formal_name": "Science Hall 101", "building": "",
                               "capacity": 40, "bookings": 3}]


def test_a_missing_or_mangled_file_is_no_discovery(tmp_path):
    assert discovery.load(tmp_path) is None
    (tmp_path / "discovery.json").write_text("not json")
    assert discovery.load(tmp_path) is None
    (tmp_path / "discovery.json").write_text(json.dumps(
        {"spaces": [{"space_id": ""}, "junk", {"space_id": "5", "capacity": "lots"}]}))
    assert discovery.load(tmp_path)["spaces"] == [
        {"space_id": "5", "space_name": "5", "formal_name": "", "building": "",
         "capacity": None, "bookings": 0}]


def test_guessing_buildings_for_thousands_of_unnumbered_spaces_is_quick():
    """Every unnumbered name used to be compared with every other: 3,000 of
    them took about ten seconds, on every load of the import page."""
    import time

    from bassync.discovery import guess_buildings
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    spaces = [{"space_id": str(i),
               "space_name": f"Tower {letters[i % 26]} Wing {letters[i // 26 % 26]} "
                             f"Area {letters[i // 676 % 26]}"} for i in range(3000)]
    started = time.perf_counter()
    out = guess_buildings(spaces)
    assert time.perf_counter() - started < 2
    assert len(out) == 3000


def test_unnumbered_names_share_the_longest_start_they_have_in_common():
    from bassync.discovery import guess_buildings
    out = guess_buildings([
        {"space_id": "1", "space_name": "Student Center Ballroom"},
        {"space_id": "2", "space_name": "Student Center Lounge"},
        {"space_id": "3", "space_name": "Student Center West Lounge"},
        {"space_id": "4", "space_name": "The Quad"},
        {"space_id": "5", "space_name": "The Green"},
        {"space_id": "6", "space_name": "Science Hall 101"},
        {"space_id": "7", "space_name": "Science Hall Atrium"},
        {"space_id": "8", "space_name": "Chapel"},
    ])
    assert out == {"1": "Student Center", "2": "Student Center", "3": "Student Center",
                   "4": "", "5": "", "6": "Science Hall", "7": "Science Hall", "8": ""}

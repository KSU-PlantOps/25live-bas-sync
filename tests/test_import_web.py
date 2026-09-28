"""Room map → From 25Live: adding the rooms 25Live has, a building at a time."""

import html
import re

import pytest
import yaml

pytest.importorskip("flask")

from bassync import discovery  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import app_for, post, signed_in_client, the_map, version  # noqa: E402

FOUND = [
    {"space_id": "301", "space_name": "LA 110", "formal_name": "Liberal Arts 110",
     "capacity": 35, "building": "", "bookings": 7},
    {"space_id": "302", "space_name": "LA 215", "formal_name": "Liberal Arts 215",
     "capacity": 30, "building": "", "bookings": 0},
    {"space_id": "303", "space_name": "LA Lounge", "formal_name": "Liberal Arts Lounge",
     "capacity": None, "building": "", "bookings": 2},
    {"space_id": "150", "space_name": "SCI 150", "formal_name": "", "capacity": 20,
     "building": "Science", "bookings": 4},
    {"space_id": "101", "space_name": "Lab", "formal_name": "", "capacity": 12,
     "building": "Science", "bookings": 9},
    {"space_id": "900", "space_name": "The Quad", "formal_name": "", "capacity": None,
     "building": "", "bookings": 3},
]


@pytest.fixture
def found(site):
    discovery.save(site.paths.state_dir, 60, FOUND)
    return site


def _form(client, **fields):
    return {"version": version(client, "/map/import"), **fields}


def test_the_rooms_are_grouped_by_building(found):
    page = html.unescape(signed_in_client(found).get("/map/import").text)
    assert "Found 6 spaces in 2 buildings" in page
    order = [page.index(name) for name in ("<strong>Liberal Arts</strong>",
                                           "<strong>Science</strong>",
                                           "<strong>No building found</strong>")]
    assert order == sorted(order)
    assert "a new building" in page and "into building <code>SCI</code>" in page
    assert "Add Liberal Arts" in page and "Add Science" in page
    # 101 is in the room map already; 302 has no bookings and The Quad no
    # building, so neither is ticked; the rest are.
    def box(space_id):
        found_box = re.search(rf'<input type="checkbox" name="pick" value="{space_id}"[^>]*>', page)
        return found_box.group(0) if found_box else None
    assert box("101") is None
    assert "checked" not in box("302") and "checked" not in box("900")
    assert "checked" in box("301") and "checked" in box("150")


def test_one_building_at_a_time(found):
    c = signed_in_client(found)
    r = post(c, "/map/import", _form(c, only="liberal arts", pick=["301", "303", "150"],
                                     **{"b.301": "Liberal Arts", "b.303": "Liberal Arts",
                                        "b.150": "Science"}))
    # Only that building's ticked rooms; then straight on to the new building.
    buildings = the_map(found)["buildings"]
    assert r.location == f"/map/buildings/{len(buildings) - 1}/edit"
    assert buildings[-1] == {"id": "liberal_arts", "name": "Liberal Arts",
                             "system": "staging", "target": "liberal_arts"}
    rooms = {r["space_id"]: r for r in the_map(found)["spaces"]}
    assert rooms[301]["building"] == rooms[303]["building"] == "liberal_arts"
    assert 150 not in rooms
    cfg = yaml.safe_load(found.paths.config.read_text())
    assert cfg["systems"]["staging"] == {"driver": "preview"}
    assert cfg["default_system"] == "campus"
    page = html.unescape(c.get("/map/buildings/2/edit").text)
    assert "wait on the 'staging' system" in page
    # The next building goes into the one the map already has.
    r = post(c, "/map/import", _form(c, only="Science", pick=["150"], **{"b.150": "Science"}))
    assert r.location == "/map/import"
    assert {r["space_id"]: r for r in the_map(found)["spaces"]}[150]["building"] == "SCI"
    assert "All added" in c.get("/map/import").text


def test_adding_a_building_with_nothing_ticked_says_so(found):
    c = signed_in_client(found)
    r = post(c, "/map/import", _form(c, only="Liberal Arts", pick=["150"],
                                     **{"b.150": "Science"}))
    assert r.status_code == 422 and "Tick the rooms to add" in r.text


def test_advanced_can_add_rooms_basic_can_not_see_them(found, monkeypatch):
    configure_sso(found)
    advanced = signed_in_as(app_for(found), monkeypatch, ["BAS_Users"])
    assert advanced.get("/map/import").status_code == 200
    assert post(advanced, "/map/import/find", {"days": "30", "every": "1"}).location == "/map/import"
    assert found.jobs.started[-1][:2] == ("discover", ["--discover-days", "30"])
    r = post(advanced, "/map/import", _form(advanced, pick=["301"],
                                            **{"b.301": "Liberal Arts"}))
    assert r.status_code == 302
    assert any(r["space_id"] == 301 for r in the_map(found)["spaces"])
    basic = signed_in_as(app_for(found), monkeypatch, ["VPN_Role_PlantOps_BAS"])
    assert basic.get("/map/import").status_code == 403
    assert post(basic, "/map/import", {}).status_code == 403


def test_the_room_and_job_pages_lead_here(found):
    c = signed_in_client(found)
    assert 'href="/map/import"' in c.get("/map/rooms").text
    assert 'href="/map/import"' in c.get("/map/buildings").text


def test_every_space_is_asked_for_unless_unticked(found):
    c = signed_in_client(found)
    post(c, "/map/import/find", {"days": "90", "every": "1"})
    assert found.jobs.started[-1][1] == ["--discover-days", "90"]
    post(c, "/map/import/find", {"days": "90"})
    assert found.jobs.started[-1][1] == ["--discover-days", "90", "--discover-booked-only"]


@pytest.mark.parametrize("listing, words, warned", [
    ("every", "everything 25Live lists, with bookings", False),
    ("booked-only", "booked\n      in the 60 days", False),
    ("booked", "booked\n      in the 60 days", False),         # kept by an older version
    ("booked-fallback", "booked\n      in the 60 days", True),
])
def test_the_page_says_what_kind_of_list_it_is(site, listing, words, warned):
    discovery.save(site.paths.state_dir, 60, FOUND, listing=listing)
    page = html.unescape(signed_in_client(site).get("/map/import").text)
    assert words in page
    assert ("didn't list every space" in page) is warned
    ticked = 'name="every" value="1" checked' in page
    assert ticked is (listing != "booked-only")

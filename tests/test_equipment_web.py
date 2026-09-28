"""Equipment on the Room map pages: one AHU for many rooms, several for one."""

import html

import pytest

pytest.importorskip("flask")

from .webhelpers import post, signed_in_client, the_map, version  # noqa: E402


@pytest.fixture
def client(site):
    return signed_in_client(site)


def _add(client, **fields):
    return post(client, "/map/equipment/save",
                {"version": version(client, "/map/equipment/new"), "index": "", **fields})


def _sci(site) -> dict:
    return next(b for b in the_map(site)["buildings"] if b["id"] == "SCI")


def _room(site, space_id) -> dict:
    return next(r for r in the_map(site)["spaces"] if r["space_id"] == space_id)


def test_equipment_is_kept_under_its_building_and_rooms_list_it(client, site):
    assert _add(client, building="SCI", id="ahu_3", name="AHU-3",
                target="12001:30").status_code == 302
    assert _add(client, building="SCI", id="vav_2", target="12001:31").status_code == 302
    assert _sci(site)["equipment"] == [{"id": "ahu_3", "name": "AHU-3", "target": "12001:30"},
                                       {"id": "vav_2", "target": "12001:31"}]
    # The room form offers the building's equipment; a room can take several.
    page = client.get("/map/rooms/1/edit").text
    assert 'name="equipment" value="ahu_3"' in page and "AHU-3" in page
    v = version(client, "/map/rooms/1/edit")
    r = post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                         "building": "SCI", "equipment": ["ahu_3", "vav_2"]})
    assert r.status_code == 302, r.text
    assert _room(site, 102)["equipment"] == ["ahu_3", "vav_2"]
    v = version(client, "/map/rooms/0/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "0", "space_id": "101",
                                     "building": "SCI", "floor": "1", "target": "12001:7",
                                     "equipment": "ahu_3"})
    # The list counts who uses each; the room list shows what each room has.
    listing = client.get("/map/equipment").text
    assert "<code>12001:30</code>" in listing and ">2</td>" in listing.replace("\n", "").replace(" ", "")
    assert "ahu_3, vav_2" in client.get("/map/rooms").text
    # Unticking everything takes the key off the room.
    v = version(client, "/map/rooms/1/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                     "building": "SCI"})
    assert "equipment" not in _room(site, 102)


def test_equipment_is_checked_like_any_schedule(client, site):
    r = _add(client, building="SCI", id="ahu", target="not-a-target")
    assert r.status_code == 422 and "Target for system" in r.text
    assert _add(client, building="NOPE", id="ahu", target="12001:30").status_code == 422
    _add(client, building="SCI", id="ahu", target="12001:30")
    r = _add(client, building="SCI", id="ahu", target="12001:31")
    assert r.status_code == 422 and "already has equipment called 'ahu'" in html.unescape(r.text)
    # The same id in another building is a different piece of equipment.
    assert _add(client, building="ART", id="ahu", target="art/ahu").status_code == 302


def test_a_room_lists_only_its_own_buildings_equipment(client, site):
    _add(client, building="ART", id="ahu", target="art/ahu")
    v = version(client, "/map/rooms/1/edit")
    r = post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                         "building": "SCI", "equipment": "ahu"})
    assert r.status_code == 422 and "isn't equipment of building 'SCI'" in html.unescape(r.text)
    assert 'value="ahu" checked' in r.text                   # what was ticked stays ticked


def test_renaming_equipment_repoints_its_rooms_and_it_stays_in_its_building(client, site):
    _add(client, building="SCI", id="ahu", target="12001:30")
    v = version(client, "/map/rooms/1/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                     "building": "SCI", "equipment": "ahu"})
    v = version(client, "/map/equipment/0/edit")
    r = post(client, "/map/equipment/save", {"version": v, "index": "0", "building": "SCI",
                                             "id": "ahu_east", "target": "12001:30"})
    assert r.status_code == 302
    assert _room(site, 102)["equipment"] == ["ahu_east"]
    v = version(client, "/map/equipment/0/edit")
    r = post(client, "/map/equipment/save", {"version": v, "index": "0", "building": "ART",
                                             "id": "ahu_east", "target": "art/x"})
    assert r.status_code == 422 and "can't move to another building" in html.unescape(r.text)


def test_equipment_in_use_can_not_be_deleted(client, site):
    _add(client, building="SCI", id="ahu", target="12001:30")
    v = version(client, "/map/rooms/1/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                     "building": "SCI", "equipment": "ahu"})
    page = client.get("/map/equipment/0/delete").text
    assert "1 room(s) list this equipment: 102" in page
    v = version(client, "/map/rooms/1/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "1", "space_id": "102",
                                     "building": "SCI"})
    v = version(client, "/map/equipment/0/delete")
    assert post(client, "/map/equipment/0/delete", {"version": v}).status_code == 302
    assert "equipment" not in _sci(site)


def test_editing_a_building_keeps_its_equipment(client, site):
    _add(client, building="SCI", id="ahu", target="12001:30")
    v = version(client, "/map/buildings/0/edit")
    post(client, "/map/buildings/save", {"version": v, "index": "0", "id": "SCIENCE",
                                         "name": "Science", "target": "12001:5"})
    building = the_map(site)["buildings"][0]
    assert building["id"] == "SCIENCE" and building["equipment"] == [
        {"id": "ahu", "target": "12001:30"}]
    assert client.get("/map/equipment").text.count("SCIENCE") >= 1


def test_the_status_page_counts_equipment(client, site):
    _add(client, building="SCI", id="ahu", target="12001:30")
    assert "1 equipment" in client.get("/").text


def test_deleting_a_building_takes_its_equipment_off_the_rooms_it_leaves():
    from bassync import mapedit
    buildings = [{"id": "SCI", "target": "1:1", "equipment": [{"id": "ahu", "target": "1:2"}]}]
    rooms = [{"space_id": 1, "building": "SCI", "target": "1:3", "equipment": ["ahu"]}]
    assert mapedit.building_delete_check("SCI", [], rooms)[0] is None
    left, _floors, kept = mapedit.delete_building("SCI", buildings, [], rooms)
    assert left == [] and kept == [{"space_id": 1, "target": "1:3"}]

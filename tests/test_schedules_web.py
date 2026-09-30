"""The Schedules pages: what was written for each space, for people who see
only that (an events team), and marking events low temp there."""

import html
import json
from datetime import datetime, timedelta

import pytest
import yaml

pytest.importorskip("flask")

from bassync import lowtemp  # noqa: E402
from bassync.web import access  # noqa: E402

from .helpers import TZ  # noqa: E402
from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import app_for, post, signed_in_client  # noqa: E402

EVENTS = {"id": "events", "name": "Events team",
          "capabilities": ["view_schedules", "low_temp"]}
VIEWERS = {"id": "viewers", "name": "Viewers", "capabilities": ["view_schedules"]}


def _at(days, hour, minute=0):
    day = datetime.now(TZ).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).isoformat()


@pytest.fixture
def synced(site):
    """A record of the last sync, as bassync/scheduled.py writes it."""
    booking = {"id": "48213", "title": "Red Cross blood drive", "start": _at(1, 10),
               "end": _at(1, 14), "on": _at(1, 9), "off": _at(1, 15), "low_temp": False,
               "extra": False}
    lab = {"system": "campus", "target": "12001:7", "label": "Lab", "kind": "room",
           "status": "written", "error": "", "at": _at(0, 2),
           "windows": [[_at(1, 9), _at(1, 15)]]}
    data = {"updated": _at(0, 2), "full": True,
            "schedules": {"campus:12001:7": lab,
                          "campus:12001:6": dict(lab, target="12001:6", kind="floor",
                                                 label="Floor 1 of Science"),
                          "campus:12001:5": dict(lab, target="12001:5", kind="building",
                                                 label="Building Science", status="failed",
                                                 error="device offline")},
            "spaces": {"101": {"name": "Lab", "building": "SCI", "floor": 1, "at": _at(0, 2),
                               "schedules": ["campus:12001:7", "campus:12001:6",
                                             "campus:12001:5"],
                               "bookings": [booking]}}}
    site.paths.scheduled_file.parent.mkdir(parents=True, exist_ok=True)
    site.paths.scheduled_file.write_text(json.dumps(data))
    return site


def _as(site, monkeypatch, *roles, group="Events"):
    web = configure_sso(site)
    web["roles"] = access.default_roles() + [dict(r) for r in roles]
    web["groups"].append({"group": group, "role": roles[0]["id"], "note": ""})
    access.save(site.paths.web_file, web)
    return signed_in_as(app_for(site), monkeypatch, [group])


def test_the_spaces_list_and_a_spaces_page(synced):
    c = signed_in_client(synced)
    page = html.unescape(c.get("/schedules").text)
    assert "Lab" in page and "Red Cross blood drive" in page and "not synced yet" in page
    page = html.unescape(c.get("/schedules/space/101").text)
    assert "Red Cross blood drive" in page and "25Live event 48213" in page
    assert "10:00–14:00" in page and "09:00–15:00" in page             # booked, and runs
    assert "Floor 1 of Science" in page and "Floor corridor" in page
    assert "The last write failed</span>: device offline" in page
    assert c.get("/schedules/space/999").status_code == 404


def test_an_events_role_sees_only_the_schedules(synced, monkeypatch):
    c = _as(synced, monkeypatch, VIEWERS)
    assert c.get("/").location == "/schedules"                 # its home page
    assert c.get("/schedules").status_code == 200
    assert c.get("/schedules/space/101").status_code == 200
    for page in ("/runs", "/map/rooms", "/bookings", "/settings/connection"):
        assert c.get(page).status_code == 403, page
    # Seeing isn't marking.
    page = c.get("/schedules/space/101").text
    assert "Mark low temp" not in page
    assert post(c, "/schedules/low-temp", {"event_id": "48213", "action": "mark"}).status_code == 403
    nav = c.get("/schedules").text
    assert 'href="/schedules"' in nav and 'href="/runs"' not in nav


def test_marking_an_event_low_temp(synced, monkeypatch):
    c = _as(synced, monkeypatch, EVENTS)
    page = c.get("/schedules/space/101").text
    assert "Mark low temp" in page and "has no low-temp schedule" in page
    r = post(c, "/schedules/low-temp", {"event_id": "48213", "name": "Red Cross blood drive",
                                        "action": "mark", "back": "101"})
    assert r.location == "/schedules/space/101"
    rows = lowtemp.read(synced.paths.low_temp_file)
    assert rows[0]["event_id"] == "48213" and rows[0]["name"] == "Red Cross blood drive"
    page = html.unescape(c.get("/schedules/space/101").text)
    assert "low temp from the next sync" in page and "Unmark" in page
    listing = html.unescape(c.get("/schedules/low-temp").text)
    assert "Red Cross blood drive" in listing and 'href="/schedules/space/101"' in listing
    # Marking by ID, for an event booked since the last sync.
    post(c, "/schedules/low-temp", {"event_id": "50001", "name": "Exam", "action": "mark",
                                    "back": "list"})
    assert lowtemp.event_ids(synced.paths.low_temp_file) == {"48213", "50001"}
    assert "not in the last sync" in c.get("/schedules/low-temp").text
    post(c, "/schedules/low-temp", {"event_id": "48213", "action": "unmark", "back": "101"})
    assert lowtemp.event_ids(synced.paths.low_temp_file) == {"50001"}
    assert post(c, "/schedules/low-temp", {"event_id": "", "action": "mark"}).status_code == 400


def test_only_a_low_temp_role_sets_it_on_an_extra_booking(synced, monkeypatch):
    advanced = _as(synced, monkeypatch, dict(EVENTS, id="helper", name="Helper",
                                             capabilities=["edit_bookings", "low_temp"]),
                   group="Helpers")
    form = {"title": "Donor prep", "where": "r:101", "repeat": "once",
            "date": (datetime.now(TZ).date() + timedelta(days=2)).isoformat(),
            "start": "08:00", "end": "09:00", "low_temp": "1"}
    assert "Run the room colder" in advanced.get("/bookings/new").text
    from .webhelpers import version
    assert post(advanced, "/bookings/save", dict(form, version=version(advanced, "/bookings/new"))
                ).status_code == 302
    saved = yaml.safe_load(synced.paths.extras_file.read_text())["bookings"][0]
    assert saved["low_temp"] is True
    # Someone who may edit bookings but not low temp keeps it as it was.
    plain = signed_in_as(app_for(synced), monkeypatch, ["BAS_Users"])      # Advanced
    assert "Run the room colder" not in plain.get("/bookings/new").text
    post(plain, "/bookings/save", dict(form, index="0", low_temp="",
                                       version=version(plain, "/bookings/new")))
    assert yaml.safe_load(synced.paths.extras_file.read_text())["bookings"][0]["low_temp"] is True


def test_the_low_temp_field_is_on_the_room_and_equipment_forms(site):
    c = signed_in_client(site)
    assert 'name="low_temp_target"' in c.get("/map/rooms/new").text
    assert 'name="low_temp_target"' in c.get("/map/equipment/new").text

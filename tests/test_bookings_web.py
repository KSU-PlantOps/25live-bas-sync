"""The Extra bookings page."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

pytest.importorskip("flask")

from bassync import extras  # noqa: E402
from bassync.web import access  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import PASSWORD, app_for, post  # noqa: E402

TZ = ZoneInfo("America/New_York")


def _day(offset: int) -> str:
    return (datetime.now(TZ).date() + timedelta(days=offset)).isoformat()


def _client(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


def _version(c) -> str:
    import re
    return re.search(r'name="version" value="([^"]*)"', c.get("/bookings/new").text).group(1)


def _rows(site) -> list:
    return extras.read(site.paths.extras_file)


def _save(c, **form):
    data = {"version": _version(c), "index": "", "title": "Open house", "where": "b:SCI",
            "repeat": "once", "date": _day(3), "start": "08:00", "end": "12:00"}
    data.update(form)
    return post(c, "/bookings/save", data)


def test_add_a_one_day_building_booking(site, caplog):
    c = _client(site)
    with caplog.at_level("INFO"):
        r = _save(c, note="Admissions")
    assert r.status_code == 302, r.text
    assert _rows(site) == [{"title": "Open house", "building": "SCI", "date": _day(3),
                            "start": "08:00", "end": "12:00", "note": "Admissions",
                            "added_by": "local admin"}]
    assert "added the extra booking 'Open house'" in caplog.text
    page = c.get("/bookings").text
    assert "Open house" in page and "Science" in page and "Admissions" in page


def test_weekly_floor_booking_and_exact_room_booking(site):
    c = _client(site)
    assert _save(c, title="Custodial", where="f:SCI:1", repeat="weekly",
                 days=["mon", "wed"], until=_day(60), start="18:00",
                 end="21:00").status_code == 302
    assert _save(c, title="Lab make-up", where="r:101", start="13:00", end="16:00",
                 exact="1").status_code == 302
    floor, room = _rows(site)
    assert (floor["building"], floor["floor"], floor["days"]) == ("SCI", 1, ["mon", "wed"])
    assert "date" not in floor and floor["until"] == _day(60)
    assert (room["space_id"], room["exact"]) == (101, True)
    page = c.get("/bookings").text
    assert "Every Mon, Wed 18:00–21:00" in page and "exact times" in page


@pytest.mark.parametrize("form, message", [
    ({"title": ""}, "Give it a title"),
    ({"where": ""}, "Pick where it is"),
    ({"end": "08:00"}, "the same time"),
    ({"repeat": "weekly"}, "either a `date`, or weekly `days`"),
    ({"date": ""}, "either a `date`"),
])
def test_a_bad_booking_is_refused_with_why(site, form, message):
    c = _client(site)
    r = _save(c, **form)
    assert r.status_code == 422 and message in r.text
    assert _rows(site) == []


def test_edit_keeps_who_added_it_and_copy_makes_a_new_one(site):
    c = _client(site)
    _save(c)
    rows = _rows(site)
    rows[0]["added_by"] = "Pat Doe"
    extras.save(site.paths.extras_file, rows)
    form = c.get("/bookings/0/edit").text
    assert 'value="Open house"' in form and 'value="b:SCI" selected' in form
    assert _save(c, index="0", title="Open house (moved)", where="b:ART").status_code == 302
    assert _rows(site)[0]["added_by"] == "Pat Doe" and _rows(site)[0]["building"] == "ART"
    copy = c.get("/bookings/0/copy").text
    assert "Open house (moved) (copy)" in copy and 'name="index" value=""' in copy


def test_delete_and_delete_ended(site):
    c = _client(site)
    _save(c, title="Past", date=_day(-5))
    _save(c, title="Future")
    page = c.get("/bookings").text
    assert "1 ended" in page
    post(c, "/bookings/delete-ended", {"version": _version(c)})
    assert [r["title"] for r in _rows(site)] == ["Future"]
    post(c, "/bookings/0/delete", {"version": _version(c)})
    assert _rows(site) == []


def test_a_change_made_meanwhile_is_not_overwritten(site):
    c = _client(site)
    stale = _version(c)
    _save(c, title="First")
    r = post(c, "/bookings/save", {"version": stale, "index": "", "title": "Second",
                                   "where": "b:SCI", "repeat": "once", "date": _day(2),
                                   "start": "08:00", "end": "09:00"})
    assert r.status_code == 422 and "changed since this page was opened" in r.text
    assert [r["title"] for r in _rows(site)] == ["First"]


def test_a_booking_naming_a_place_the_map_lacks_is_flagged(site):
    extras.save(site.paths.extras_file, [
        {"title": "Gone", "building": "OLD", "date": _day(1), "start": "8:00", "end": "9:00"},
        {"title": "Broken", "building": "SCI", "date": _day(1), "start": "8:00"}])
    page = _client(site).get("/bookings").text
    assert "building OLD isn&#39;t in the room map" in page
    assert "give a `start` and an `end` time" in page


def test_unreadable_file_is_reported_and_not_edited(site):
    site.paths.extras_file.write_text("bookings: [oops", encoding="utf-8")
    c = _client(site)
    page = c.get("/bookings").text
    assert "writes nothing until it" in page and "Add booking" not in page
    assert c.get("/bookings/0/edit").status_code == 409


def test_who_may_see_and_change_them(site, monkeypatch):
    configure_sso(site)
    web = access.load(site.paths.web_file)[0]
    web["roles"].append({"id": "viewer", "name": "Viewer",
                         "capabilities": ["view_basic", "view_all"]})
    web["groups"].append({"group": "Viewers", "role": "viewer", "note": ""})
    access.save(site.paths.web_file, web)
    extras.save(site.paths.extras_file, [{"title": "Tour", "building": "SCI",
                                          "date": _day(1), "start": "8:00", "end": "9:00"}])
    app = app_for(site)
    basic = signed_in_as(app, monkeypatch, ["VPN_Role_PlantOps_BAS"])
    assert basic.get("/bookings").status_code == 403
    viewer = signed_in_as(app, monkeypatch, ["Viewers"])
    page = viewer.get("/bookings").text
    assert "Tour" in page and "Add booking" not in page and "Delete" not in page
    assert viewer.get("/bookings/new").status_code == 403
    assert post(viewer, "/bookings/0/delete", {"version": "x"}).status_code == 403
    advanced = signed_in_as(app, monkeypatch, ["BAS_Users"])
    assert "Add booking" in advanced.get("/bookings").text
    assert ">Bookings<" in advanced.get("/").text


def test_the_files_page_edits_them_with_the_bookings_capability(site, monkeypatch):
    configure_sso(site)
    c = signed_in_as(app_for(site), monkeypatch, ["BAS_Users"])        # Advanced
    assert "extra_bookings.yaml" in c.get("/files").text
    import re
    page = c.get("/files/extras").text
    version = re.search(r'name="version" value="([^"]*)"', page).group(1)
    text = yaml.safe_dump({"bookings": [{"title": "X", "building": "SCI", "date": _day(1),
                                         "start": "8:00"}]})
    r = post(c, "/files/extras", {"text": text, "version": version})
    assert r.status_code == 422 and "Booking 1: give a `start` and an `end` time" in r.text
    r = post(c, "/files/extras", {"text": text, "version": version, "confirm": "1"})
    assert r.status_code == 302
    assert post(c, "/files/config", {"text": "x: 1", "version": "x"}).status_code == 403

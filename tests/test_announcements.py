"""Announcements: scheduled messages on the home page for everyone signed in."""

import html
from datetime import datetime, timedelta

import pytest
import yaml

pytest.importorskip("flask")

from bassync.web import access, announce  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import app_for, post, signed_in_client, version  # noqa: E402

TZ_NAME = "America/New_York"


def _local(**delta) -> str:
    from zoneinfo import ZoneInfo
    return (datetime.now(ZoneInfo(TZ_NAME)) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M")


def _add(client, **fields):
    form = {"version": version(client, "/announcements/new"), "level": "info",
            "starts": _local(hours=-1), **fields}
    return post(client, "/announcements/save", form)


def test_an_announcement_shows_on_the_home_page_while_it_is_current(site):
    c = signed_in_client(site)
    assert "Post an announcement" in c.get("/").text
    r = _add(c, message="BAS maintenance Saturday 6–10 AM;\nsyncs may be late.",
             level="warning", ends=_local(hours=2))
    assert r.status_code == 302 and r.location == "/announcements"
    home = html.unescape(c.get("/").text)
    assert "BAS maintenance Saturday 6–10 AM;\nsyncs may be late." in home
    assert 'class="announcement warning"' in home and "Manage announcements" in home
    saved = yaml.safe_load(site.paths.announcements_file.read_text())["announcements"][0]
    assert saved["level"] == "warning" and saved["added_by"]
    assert saved["starts"] == _local(hours=-1).replace("T", " ")
    # Not before it starts, nor after it ends.
    _add(c, message="Next week's shutdown", starts=_local(days=2))
    _add(c, message="Last week's shutdown", starts=_local(days=-9), ends=_local(days=-8))
    home = c.get("/").text
    assert "Next week" not in home and "Last week" not in home
    listing = c.get("/announcements").text
    assert "Next week" in listing and "1 ended" in listing


def test_it_is_checked_and_can_be_edited_and_deleted(site):
    c = signed_in_client(site)
    r = _add(c, message="Backwards", ends=_local(hours=-3))
    assert r.status_code == 422 and "It ends before it starts" in r.text
    assert _add(c, message="").status_code == 422
    assert _add(c, message="x", level="shouty").status_code == 422
    _add(c, message="Typo in thsi")
    r = post(c, "/announcements/save", {"version": version(c, "/announcements/0/edit"),
                                        "index": "0", "message": "Fixed", "level": "info",
                                        "starts": _local(hours=-1)})
    assert r.status_code == 302 and "Fixed" in c.get("/").text
    _add(c, message="Old", starts=_local(days=-3), ends=_local(days=-2))
    post(c, "/announcements/delete-ended", {"version": version(c, "/announcements")})
    rows = announce.read(site.paths.announcements_file)
    assert [r["message"] for r in rows] == ["Fixed"]
    post(c, "/announcements/0/delete", {"version": version(c, "/announcements")})
    assert announce.read(site.paths.announcements_file) == []
    # A stale form is refused rather than overwriting someone else's change.
    assert _add(c, message="x", version="stale").status_code == 422


def test_posting_needs_its_own_capability_but_everyone_sees_them(site, monkeypatch):
    admin = signed_in_client(site)
    _add(admin, message="Hello, everyone")
    configure_sso(site)
    basic = signed_in_as(app_for(site), monkeypatch, ["VPN_Role_PlantOps_BAS"])
    home = basic.get("/").text
    assert "Hello, everyone" in home and "Manage announcements" not in home
    assert basic.get("/announcements").status_code == 403
    assert post(basic, "/announcements/save", {"message": "x"}).status_code == 403
    # A role that sees only the schedules gets them there, its home page.
    web = access.load(site.paths.web_file)[0]
    web["roles"] = access.default_roles() + [
        {"id": "events", "name": "Events", "capabilities": ["view_schedules"]}]
    web["groups"].append({"group": "Events", "role": "events", "note": ""})
    access.save(site.paths.web_file, web)
    events = signed_in_as(app_for(site), monkeypatch, ["Events"])
    assert "Hello, everyone" in events.get("/schedules").text


def test_a_broken_file_shows_nothing_on_the_home_page(site):
    site.paths.announcements_file.write_text("announcements: {not: a list}\n")
    c = signed_in_client(site)
    assert c.get("/").status_code == 200
    assert "must be a list" in c.get("/announcements").text

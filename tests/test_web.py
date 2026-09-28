"""The web UI: sign-in, CSRF, and every page that reads or changes a file."""

import re

import pytest
import yaml

pytest.importorskip("flask")

from bassync import history, mapedit  # noqa: E402
from bassync.report import RunReport  # noqa: E402
from bassync.service import Paths, Service  # noqa: E402

from .helpers import TZ, dt  # noqa: E402
from .test_service import FakeJobs  # noqa: E402
from .webhelpers import (  # noqa: E402
    CONFIG,
    MAP,
    PASSWORD,
    app_for,
    csrf,
    post,
    signed_in_client,
    the_map,
    version,
)


@pytest.fixture
def client(site):
    return signed_in_client(site)


# ── signing in ───────────────────────────────────────────────────────────────

def test_everything_but_the_login_needs_a_password(site):
    c = app_for(site).test_client()
    r = c.get("/map/rooms")
    assert r.status_code == 302 and "/login?next=/map/rooms" in r.location
    assert c.get("/api/status").status_code == 401
    assert c.post("/jobs", data={"kind": "sync"}).status_code == 401
    assert c.get("/healthz").json == {"ok": True, "boot": site.boot}
    assert site.jobs.started == []


def test_wrong_passwords_lock_the_address_out(site):
    c = app_for(site).test_client()
    for _ in range(5):
        assert c.post("/login", data={"password": "guess"}).status_code == 401
    r = c.post("/login", data={"password": PASSWORD})
    assert r.status_code == 401 and "Too many wrong passwords" in r.text


def test_sign_in_only_redirects_within_the_site(site):
    c = app_for(site).test_client()
    for evil in ("//evil.example/x", "https://evil.example/", "javascript:alert(1)"):
        r = c.post("/login", data={"password": PASSWORD, "next": evil})
        assert r.location == "/"
        c.post("/logout", data={"csrf": csrf(c)})


def test_changing_the_password_signs_everyone_out(site):
    before = app_for(site).test_client()
    before.post("/login", data={"password": PASSWORD})
    with before.session_transaction() as s:
        signed_in = dict(s)
    # The same session key on disk, a new password: the old session no longer counts.
    after = app_for(site, BAS_WEB_PASSWORD="a brand new password!!").test_client()
    with after.session_transaction() as s:
        s.update(signed_in)
    assert after.get("/").status_code == 302


def test_no_login_when_auth_is_off(site):
    c = app_for(site, BAS_WEB_AUTH="none", BAS_WEB_PASSWORD="").test_client()
    assert c.get("/").status_code == 200
    assert c.get("/login").status_code == 302


def test_every_change_needs_the_csrf_token(client, site):
    r = client.post("/jobs", data={"kind": "sync"})
    assert r.status_code == 400
    assert site.jobs.started == []
    r = client.post("/jobs", data={"kind": "sync"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 400


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


# ── pages ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "/", "/runs", "/jobs", "/map/rooms", "/map/buildings", "/map/floors",
    "/map/rooms/new", "/map/rooms/0/edit", "/map/buildings/1/edit", "/map/floors/0/edit",
    "/map/rooms/new?copy=0", "/map/buildings/0/delete", "/settings/connection",
    "/settings/connection?system=west", "/settings/connection?system=campus&driver=rest",
    "/settings/defaults", "/settings/schedule", "/files", "/files/config", "/files/map",
    "/files/defaults", "/logs", "/logs?which=service", "/api/status"])
def test_pages_render(client, url):
    assert client.get(url).status_code == 200


def test_dashboard_lists_missing_passwords_and_problems(client, site):
    text = client.get("/").text
    assert "BAS_25LIVE_PASSWORD" in text and "BAS_SYS_WEST_PASSWORD" in text
    assert "BAS_SYS_CAMPUS_PASSWORD" not in text         # bacnet needs none
    assert "daily at 02:00" in text


# ── the room map ─────────────────────────────────────────────────────────────

def test_add_a_room(client, site):
    v = version(client, "/map/rooms/new")
    r = post(client, "/map/rooms/save", {"version": v, "index": "", "space_id": "103",
                                         "space_name": "Studio", "building": "SCI",
                                         "target": "", "system": ""})
    assert r.status_code == 302
    rooms = the_map(site)["spaces"]
    assert rooms[-1] == {"space_id": 103, "space_name": "Studio", "building": "SCI"}


def test_edit_keeps_fields_the_form_does_not_show(client, site):
    data = dict(MAP)
    data["spaces"] = [dict(MAP["spaces"][0], custom_note="keep me"), MAP["spaces"][1]]
    site.paths.space_map.write_text(yaml.safe_dump(data), encoding="utf-8")
    v = version(client, "/map/rooms/0/edit")
    post(client, "/map/rooms/save", {"version": v, "index": "0", "space_id": "101",
                                     "space_name": "Wet lab", "building": "SCI",
                                     "floor": "1", "target": "12001:7"})
    room = the_map(site)["spaces"][0]
    assert room["space_name"] == "Wet lab" and room["custom_note"] == "keep me"


def test_row_problems_are_refused(client, site):
    before = site.paths.space_map.read_text()
    v = version(client, "/map/rooms/new")
    r = post(client, "/map/rooms/save", {"version": v, "space_id": "101", "target": "12001:9"})
    assert r.status_code == 422 and "already used" in r.text
    r = post(client, "/map/rooms/save", {"version": v, "space_id": "200", "target": "garbage"})
    assert r.status_code == 422 and "Target for system" in r.text
    r = post(client, "/map/rooms/save", {"version": v, "space_id": "abc", "building": "SCI"})
    assert r.status_code == 422 and "whole number" in r.text
    assert site.paths.space_map.read_text() == before


def test_a_stale_form_does_not_overwrite_someone_elses_save(client, site):
    v = version(client, "/map/rooms/0/edit")
    site.paths.space_map.write_text(yaml.safe_dump({**MAP, "spaces": MAP["spaces"][:1]}),
                                    encoding="utf-8")
    r = post(client, "/map/rooms/save", {"version": v, "index": "0", "space_id": "101",
                                         "building": "SCI", "target": "12001:7"})
    assert r.status_code == 409 and "changed since this form was opened" in r.text
    assert len(the_map(site)["spaces"]) == 1


def test_new_map_problems_need_confirming(client, site):
    v = version(client, "/map/rooms/new")
    data = {"version": v, "space_id": "300", "building": "SCI",
            "pre_condition_minutes": "5000"}
    r = post(client, "/map/rooms/save", data)
    assert r.status_code == 422 and "Save anyway" in r.text
    assert len(the_map(site)["spaces"]) == 2
    r = post(client, "/map/rooms/save", {**data, "confirm": "1"})
    assert r.status_code == 302 and len(the_map(site)["spaces"]) == 3


def test_renaming_a_building_repoints_its_rooms_and_floors(client, site):
    v = version(client, "/map/buildings/0/edit")
    post(client, "/map/buildings/save", {"version": v, "index": "0", "id": "SCIENCE",
                                         "target": "12001:5"})
    data = the_map(site)
    assert data["buildings"][0]["id"] == "SCIENCE"
    assert {r.get("building") for r in data["spaces"]} == {"SCIENCE"}
    assert data["floors"][0]["building"] == "SCIENCE"


def test_deleting_a_building_its_rooms_depend_on_is_refused(client, site):
    r = client.get("/map/buildings/0/delete")
    assert "102" in r.text and "Delete</button>" not in r.text
    r = post(client, "/map/buildings/0/delete", {"version": version(client, "/map/rooms/new")})
    assert len(the_map(site)["buildings"]) == 2


def test_deleting_a_building_takes_its_floors(client, site):
    data = dict(MAP)
    data["spaces"] = [MAP["spaces"][0]]                 # 101 has its own target
    site.paths.space_map.write_text(yaml.safe_dump(data), encoding="utf-8")
    page = client.get("/map/buildings/0/delete").text
    assert "1 floor schedule" in page
    v = re.search(r'name="version" value="([^"]*)"', page).group(1)
    assert post(client, "/map/buildings/0/delete", {"version": v}).status_code == 302
    after = the_map(site)
    assert [b["id"] for b in after["buildings"]] == ["ART"]
    assert "floors" not in after
    assert "building" not in after["spaces"][0] and "floor" not in after["spaces"][0]


def test_delete_a_room(client, site):
    v = re.search(r'name="version" value="([^"]*)"',
                  client.get("/map/rooms/1/delete").text).group(1)
    post(client, "/map/rooms/1/delete", {"version": v})
    assert [r["space_id"] for r in the_map(site)["spaces"]] == [101]


def test_prefilled_from_discover(client):
    r = client.get("/map/rooms/new?space_id=555&space_name=Board+Room")
    assert 'value="555"' in r.text and 'value="Board Room"' in r.text


# ── settings ─────────────────────────────────────────────────────────────────

def _connection_form(client, system="campus"):
    page = client.get(f"/settings/connection?system={system}").text
    fields = dict(re.findall(r'<input id="c-([^"]+)" name="[^"]+" value="([^"]*)"', page))
    fields["version"] = re.search(r'name="version" value="([^"]*)"', page).group(1)
    fields["system"] = system
    fields["driver"] = "bacnet" if system == "campus" else "rest"
    fields["state"] = "2"
    fields["default_system"] = "campus"
    return fields


def test_connection_save_keeps_everything_it_does_not_show(client, site):
    form = _connection_form(client)
    form["timezone"] = "America/Chicago"
    r = post(client, "/settings/connection", form)
    assert r.status_code == 302, r.text
    saved = yaml.safe_load(site.paths.config.read_text())
    assert saved["timezone"] == "America/Chicago"
    assert saved["systems"]["west"]["write"] == {"method": "PUT", "path": "/s/{target}"}
    assert (site.paths.config.parent / "config.yaml.bak").exists()


def test_connection_problems_need_confirming(client, site):
    form = _connection_form(client)
    form["timezone"] = "Mars/Olympus"
    r = post(client, "/settings/connection", form)
    assert r.status_code == 422 and "refuse to start" in r.text
    assert "Mars" not in site.paths.config.read_text()


def test_add_and_remove_a_system(client, site):
    r = post(client, "/settings/systems", {"name": "annex", "driver": "preview"})
    assert r.status_code == 302 and "system=annex" in r.location
    assert yaml.safe_load(site.paths.config.read_text())["systems"]["annex"] == {"driver": "preview"}
    assert post(client, "/settings/systems/annex/delete").status_code == 302
    assert "annex" not in yaml.safe_load(site.paths.config.read_text())["systems"]


def test_bad_system_names_are_refused(client, site):
    for name in ("", "has space", "../x", "campus"):
        post(client, "/settings/systems", {"name": name, "driver": "bacnet"})
    assert set(yaml.safe_load(site.paths.config.read_text())["systems"]) == {"campus", "west"}


def test_a_system_in_use_is_not_removed(client, site):
    post(client, "/settings/systems/west/delete")
    post(client, "/settings/systems/campus/delete")     # the default
    assert set(yaml.safe_load(site.paths.config.read_text())["systems"]) == {"campus", "west"}


def test_defaults(client, site):
    r = post(client, "/settings/defaults", {"pre_condition_minutes": "45",
                                            "post_buffer_minutes": "10",
                                            "merge_gap_minutes": "5", "lookahead_days": "0"})
    assert r.status_code == 200 and "between 1 and 366" in r.text
    post(client, "/settings/defaults", {"pre_condition_minutes": "45",
                                        "post_buffer_minutes": "10",
                                        "merge_gap_minutes": "5", "lookahead_days": "14"})
    assert mapedit.load_defaults(site.paths.defaults)["pre_condition_minutes"] == 45


def test_schedule(client, site):
    r = post(client, "/settings/schedule", {"enabled": "1", "times": "25:00"})
    assert "24-hour" in r.text
    post(client, "/settings/schedule", {"enabled": "1", "times": "14:30, 2:00"})
    assert yaml.safe_load(site.paths.config.read_text())["schedule"] == {
        "enabled": True, "times": ["02:00", "14:30"], "run_on_start": False}
    assert site.schedule.times == ["02:00", "14:30"]    # applied at once


def test_raw_edit_keeps_comments_and_checks_the_file(client, site):
    v = version(client, "/files/config")
    r = post(client, "/files/config", {"version": v, "text": "timezone: [unclosed"})
    assert r.status_code == 422 and "valid YAML" in r.text
    r = post(client, "/files/config", {"version": v, "text": "timezone: Nowhere/Here\n"})
    assert r.status_code == 422 and "Save anyway" in r.text
    text = "# my notes\n" + yaml.safe_dump(CONFIG)
    r = post(client, "/files/config", {"version": v, "text": text})
    assert r.status_code == 302 and site.paths.config.read_text() == text


def test_raw_room_map_edit_is_checked_against_the_config(client, site):
    v = version(client, "/files/map")
    bad = yaml.safe_dump({"spaces": [{"space_id": 9, "target": "nonsense"}]})
    r = post(client, "/files/map", {"version": v, "text": bad})
    assert r.status_code == 422 and "Room map:" in r.text


def test_backup_zip(client):
    import io
    import zipfile
    r = client.get("/files/backup.zip")
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert set(names) == {"config.yaml", "space_mapping.yaml"}


# ── jobs and history ─────────────────────────────────────────────────────────

def test_sync_now_with_options(client, site):
    r = post(client, "/jobs", {"kind": "sync", "system": "campus", "force": "1"})
    assert r.status_code == 302
    kind, args, trigger = site.jobs.started[0]
    assert (kind, args) == ("sync", ["--system", "campus", "--force"])
    assert trigger.startswith("web (")


def test_jobs_refuse_unknown_kinds_and_systems(client, site):
    assert post(client, "/jobs", {"kind": "rm"}).status_code == 400
    assert post(client, "/jobs", {"kind": "sync", "system": "nope"}).status_code == 400
    assert site.jobs.started == []


def test_discover_days_are_bounded(client, site):
    post(client, "/jobs", {"kind": "discover", "days": "99999"})
    assert site.jobs.started[0][1] == ["--discover-days", "366"]


def test_a_busy_service_says_so(client, site):
    site.jobs.busy_with = "Validate"
    r = post(client, "/jobs", {"kind": "sync"}, follow_redirects=True)
    assert "Validate is already running" in r.text


def test_run_history_and_its_sandboxed_report(client, site):
    report = RunReport("SYNC", "9.9", TZ)
    report.started = dt(2, day=10)
    report.add_schedule("campus", "12001:5", "<b>Science</b>", [], "written")
    report.finish(0)
    run_id = history.save_report(report, site.paths.runs_dir)
    assert run_id in client.get("/runs").text
    assert client.get(f"/runs/{run_id}").status_code == 200
    r = client.get(f"/runs/{run_id}/report")
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert "&lt;b&gt;Science" in r.text                   # escaped by the report
    assert client.get(f"/runs/{run_id}/schedules.csv").mimetype == "text/csv"
    assert client.get("/runs/..%2f..%2fx").status_code == 404


def test_discovered_spaces_are_offered_as_rooms(site):
    from bassync.web import views
    app = app_for(site)
    lines = ["INFO  Discovered 2 spaces", "",
             "# --- discovered spaces: fill in building (and floor) and/or a target for each ---",
             "spaces:", "- space_id: 101", "  space_name: Lab", "  building: ''",
             "- space_id: 555", "  space_name: Board Room"]
    with app.app_context():
        found = views._discovered_spaces(lines)
    assert found == [{"space_id": 101, "space_name": "Lab", "mapped": True},
                     {"space_id": 555, "space_name": "Board Room", "mapped": False}]


def test_a_fresh_install_renders_and_is_set_up_from_the_browser(tmp_path):
    files = Paths(tmp_path / "config.yaml", tmp_path / "defaults.yaml",
                  tmp_path / "space_mapping.yaml", tmp_path / "state",
                  tmp_path / "logs" / "25live_sync.log")
    service = Service(files, jobs=FakeJobs(), environ={})
    service.tick()
    c = app_for(service).test_client()
    c.post("/login", data={"password": PASSWORD})
    # The first visit goes to the setup guide; the rest of the site works too.
    assert c.get("/").location == "/setup"
    for url in ("/setup", "/map/rooms", "/map/buildings/new", "/settings/connection",
                "/settings/schedule", "/settings/defaults", "/files", "/files/config",
                "/runs", "/jobs", "/logs"):
        assert c.get(url).status_code == 200, url
    # A system, then the connection settings, then a building: all from nothing.
    post(c, "/settings/systems", {"name": "campus", "driver": "bacnet"})
    form = _connection_form(c)
    form.update({"collegenet.instance": "demo", "systems.campus.local_address": "10.0.0.5/24",
                 "timezone": "America/Chicago"})
    assert post(c, "/settings/connection", form).status_code == 302
    saved = yaml.safe_load(files.config.read_text())
    assert saved["default_system"] == "campus"            # the only system
    assert saved["systems"]["campus"]["local_address"] == "10.0.0.5/24"
    # config.yaml exists now: the status page shows, with the guide's steps.
    assert "Getting started" in c.get("/").text
    v = version(c, "/map/buildings/new")
    assert post(c, "/map/buildings/save", {"version": v, "id": "SCI",
                                           "target": "12001:5"}).status_code == 302
    assert yaml.safe_load(files.space_map.read_text())["buildings"][0]["id"] == "SCI"
    service.tick()
    assert service.schedule.times == ["02:00"] and service.next_due is not None


def test_campus_is_kept_shown_and_filterable(client, site):
    v = version(client, "/map/buildings/0/edit")
    post(client, "/map/buildings/save", {"version": v, "index": "0", "id": "SCI",
                                         "name": "Science", "campus": "Kennesaw",
                                         "target": "12001:5"})
    assert the_map(site)["buildings"][0]["campus"] == "Kennesaw"
    rooms = client.get("/map/rooms").text
    assert 'data-campus="Kennesaw"' in rooms and "All campuses" in rooms
    assert '<option value="Kennesaw">' in client.get("/map/buildings/new").text
    assert "Kennesaw: 2 rooms" in client.get("/").text

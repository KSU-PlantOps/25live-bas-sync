"""The setup guide: a new install, from nothing to syncing, in the browser."""

import html
import json

import pytest
import yaml

pytest.importorskip("flask")

from bassync import discovery, secretstore  # noqa: E402
from bassync.collegenet import CollegeNetClient  # noqa: E402
from bassync.service import Paths, Service  # noqa: E402
from bassync.updates import UpdateChecker  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .test_service import FakeJobs  # noqa: E402
from .webhelpers import PASSWORD, app_for, post, the_map, version  # noqa: E402

FOUND = [
    {"space_id": "11", "space_name": "SCI 101", "formal_name": "Science Hall 101",
     "capacity": 40, "building": "", "bookings": 12},
    {"space_id": "12", "space_name": "SCI 204", "formal_name": "Science Hall 204 (Chem lab)",
     "capacity": 24, "building": "", "bookings": 3},
    {"space_id": "13", "space_name": "Gallery", "formal_name": "",
     "capacity": None, "building": "Art Center", "bookings": 1},
    {"space_id": "14", "space_name": "The Quad", "formal_name": "",
     "capacity": None, "building": "", "bookings": 2},
]


@pytest.fixture
def fresh(tmp_path):
    files = Paths(tmp_path / "config.yaml", tmp_path / "defaults.yaml",
                  tmp_path / "space_mapping.yaml", tmp_path / "state",
                  tmp_path / "logs" / "25live_sync.log")
    service = Service(files, jobs=FakeJobs(), environ={})
    service.updates = UpdateChecker(fetch=lambda: [])      # never the network
    service.tick()
    return service


@pytest.fixture
def admin(fresh):
    c = app_for(fresh).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


@pytest.fixture
def connected(monkeypatch):
    """25Live answers the connection check."""
    calls = []

    def check(self):
        calls.append((self.base_url, self.session.auth))
        return True, f"HTTP 200 from {self.base_url}/events.xml"
    monkeypatch.setattr(CollegeNetClient, "check_connection", check)
    return calls


def _config(service) -> dict:
    return yaml.safe_load(service.paths.config.read_text())


def _connect(c):
    return post(c, "/setup/25live", {"instance": "demo", "username": "svc",
                                      "password": "hunter22", "state": "2"})


def test_a_new_install_opens_on_the_guide_and_every_step_shows(admin):
    assert admin.get("/").location == "/setup"
    page = admin.get("/setup").text
    assert "Welcome" in page and "Nothing is written to your BAS" in page
    for step, words in [("25live", "Instance name"), ("campus", "Timezone"),
                        ("bas", "BACnet device ID"), ("rooms", "Find rooms in 25Live"),
                        ("schedules", "No buildings yet"), ("finish", "Run validate")]:
        r = admin.get(f"/setup/{step}")
        assert r.status_code == 200 and words in r.text, step
    assert admin.get("/setup/nonsense").status_code == 404


def test_the_whole_guide_from_nothing_to_a_schedule(fresh, admin, connected):
    # 25Live: saved, the password stored (not in config.yaml), and tested.
    r = _connect(admin)
    assert r.status_code == 302 and r.location == "/setup/campus"
    cfg = _config(fresh)
    assert cfg["collegenet"] == {"instance": "demo", "username": "svc", "include_states": [2]}
    assert cfg["schedule"]["enabled"] is False              # nothing runs by itself yet
    assert "hunter22" not in fresh.paths.config.read_text()
    assert secretstore.get("BAS_25LIVE_PASSWORD", fresh.paths.secrets_file, {}) == "hunter22"
    assert connected == [("https://webservices.collegenet.com/r25ws/wrd/demo/run",
                          ("svc", "hunter22"))]
    assert admin.get("/").status_code == 200                # config.yaml exists now

    # The campus.
    r = post(admin, "/setup/campus", {"timezone": "America/Chicago", "lookahead_days": "14",
                                      "pre_condition_minutes": "45"})
    assert r.location == "/setup/bas"
    assert _config(fresh)["timezone"] == "America/Chicago"
    defaults = yaml.safe_load(fresh.paths.defaults.read_text())
    assert defaults["lookahead_days"] == 14 and defaults["pre_condition_minutes"] == 45

    # A BACnet system, made the default.
    r = post(admin, "/setup/bas", {"name": "campus", "local_address": "10.0.0.5/24",
                                   "device_id": "599001", "bbmd_address": ""})
    assert r.location == "/setup/rooms"
    cfg = _config(fresh)
    assert cfg["systems"]["campus"] == {"driver": "bacnet", "local_address": "10.0.0.5/24",
                                        "device_id": 599001}
    assert cfg["default_system"] == "campus"

    # Rooms: look in 25Live, then add what it found.
    assert post(admin, "/map/import/find", {"days": "90", "back": "setup"}).location == "/setup/rooms"
    assert fresh.jobs.started[-1][:2] == ("discover", ["--discover-days", "90"])
    discovery.save(fresh.paths.state_dir, 90, FOUND)
    page = admin.get("/setup/rooms").text
    assert 'value="Science Hall"' in page and 'value="Art Center"' in page
    assert "Found 4 spaces in 2 buildings" in page
    r = post(admin, "/map/import", {
        "back": "setup", "version": version(admin, "/setup/rooms"),
        "pick": ["11", "12", "13", "14"],
        "b.11": "Science Hall", "b.12": "science hall", "b.13": "Art Center", "b.14": ""})
    assert r.location == "/setup/schedules"
    m = the_map(fresh)
    assert m["buildings"] == [
        {"id": "science_hall", "name": "Science Hall", "system": "staging", "target": "science_hall"},
        {"id": "art_center", "name": "Art Center", "system": "staging", "target": "art_center"}]
    assert m["spaces"] == [
        {"space_id": 11, "space_name": "SCI 101", "building": "science_hall"},
        {"space_id": 12, "space_name": "SCI 204", "building": "science_hall"},
        {"space_id": 13, "space_name": "Gallery", "building": "art_center"}]
    cfg = _config(fresh)
    assert cfg["systems"]["staging"] == {"driver": "preview"}
    assert cfg["default_system"] == "campus"
    assert "Left out 1 with no building" in admin.get("/setup/schedules").text

    # Schedules: one building gets its own; the other stays staged for now.
    page = admin.get("/setup/schedules").text
    assert page.count("staged — nothing written yet") == 2
    r = post(admin, "/setup/schedules", {"version": version(admin, "/setup/schedules"),
                                         "t.science_hall": "12001:5", "s.science_hall": "campus",
                                         "t.art_center": "", "s.art_center": "campus"})
    assert r.location == "/setup/schedules"                  # one still staged
    science, art = the_map(fresh)["buildings"]
    assert science == {"id": "science_hall", "name": "Science Hall", "target": "12001:5"}
    assert art["system"] == "staging"
    assert "1 of 2 to do" in admin.get("/setup/finish").text

    # Finish: the schedule goes on, and the status page stops sending people here.
    r = post(admin, "/setup/finish", {"action": "on", "times": "12:30, 2:00"})
    assert r.location == "/"
    assert _config(fresh)["schedule"] == {"enabled": True, "times": ["02:00", "12:30"],
                                          "run_on_start": False}
    assert fresh.schedule.enabled and fresh.schedule.times == ["02:00", "12:30"]
    state = json.loads((fresh.paths.state_dir / "setup.json").read_text())
    assert state["finished"]
    page = admin.get("/").text
    assert "Setup is finished" in page and "Getting started" not in page


def test_a_failed_connection_is_saved_and_explained(fresh, admin, monkeypatch):
    monkeypatch.setattr(CollegeNetClient, "check_connection",
                        lambda self: (False, "auth failed (HTTP 401)"))
    r = _connect(admin)
    assert r.status_code == 200
    assert ("Saved, but 25Live didn't accept the connection: auth failed (HTTP 401)"
            in html.unescape(r.text))
    assert _config(fresh)["collegenet"]["instance"] == "demo"
    # ...and can be tried again without typing it all in.
    monkeypatch.setattr(CollegeNetClient, "check_connection", lambda self: (True, "HTTP 200"))
    assert post(admin, "/setup/25live/test").location == "/setup/25live"


def test_the_25live_step_says_what_is_missing(fresh, admin, connected):
    r = post(admin, "/setup/25live", {"instance": "has spaces", "username": ""})
    assert r.status_code == 422
    for words in ("letters, digits", "username", "at least one kind", "password"):
        assert words in r.text
    assert not fresh.paths.config.exists() and not connected
    # A password already stored doesn't have to be typed again.
    secretstore.write(fresh.paths.secrets_file, "BAS_25LIVE_PASSWORD", "stored")
    r = post(admin, "/setup/25live", {"base_url": "https://r25.example.edu/run/",
                                      "username": "svc", "state": ["2", "4"]})
    assert r.location == "/setup/campus"
    assert _config(fresh)["collegenet"] == {"base_url": "https://r25.example.edu/run",
                                            "username": "svc", "include_states": [2, 4]}
    assert connected[-1][1] == ("svc", "stored")


def test_the_campus_and_bas_steps_check_what_they_are_given(fresh, admin, connected):
    _connect(admin)
    r = post(admin, "/setup/campus", {"timezone": "Mars/Olympus", "lookahead_days": "0"})
    assert r.status_code == 422 and "timezone" in r.text and "between 1 and 366" in r.text
    r = post(admin, "/setup/bas", {"name": "staging", "local_address": "10.0.0.5",
                                   "device_id": "9999999", "bbmd_address": "bad address!"})
    assert r.status_code == 422
    for words in ("kept for buildings", "prefix", "0 to 4194302", "BBMD"):
        assert words in r.text
    assert "systems" not in _config(fresh)


def test_carrying_on_without_a_bas_stages_everything(fresh, admin, connected):
    _connect(admin)
    assert post(admin, "/setup/bas", {"action": "later"}).location == "/setup/rooms"
    cfg = _config(fresh)
    assert cfg["systems"] == {"staging": {"driver": "preview"}}
    assert cfg["default_system"] == "staging"
    # A real system added later becomes the default in its place.
    post(admin, "/setup/bas", {"name": "campus", "local_address": "10.0.0.5/24",
                               "device_id": "599001"})
    assert _config(fresh)["default_system"] == "campus"


def test_import_skips_rooms_already_mapped_and_reuses_buildings(site, connected):
    """On a site that's already set up: the room map's own buildings are
    matched by name or id, and rooms it has are left alone."""
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    discovery.save(site.paths.state_dir, 30, [
        {"space_id": "101", "space_name": "Lab (renamed in 25Live)", "building": "Science"},
        {"space_id": "150", "space_name": "SCI 150", "building": ""},
    ])
    page = c.get("/setup/rooms").text
    assert "added</span>" in page                           # 101 is mapped already
    r = post(c, "/map/import", {"back": "setup", "version": version(c, "/setup/rooms"),
                                        "pick": ["101", "150"], "b.101": "Science",
                                        "b.150": "sci"})
    assert r.location == "/setup/rooms"                     # no new building
    rooms = {r["space_id"]: r for r in the_map(site)["spaces"]}
    assert rooms[101]["space_name"] == "Lab" and rooms[150]["building"] == "SCI"
    assert len(the_map(site)["buildings"]) == 2


def test_import_refuses_a_stale_page_and_an_empty_choice(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    discovery.save(site.paths.state_dir, 30, FOUND)
    r = post(c, "/map/import", {"back": "setup", "version": "stale", "pick": "11", "b.11": "X"})
    assert r.status_code == 409 and "changed since" in r.text
    r = post(c, "/map/import", {"back": "setup", "version": version(c, "/setup/rooms")})
    assert r.status_code == 422 and "Tick the rooms" in r.text


def test_a_schedule_must_suit_its_system(fresh, admin, connected):
    _connect(admin)
    post(admin, "/setup/bas", {"name": "campus", "local_address": "10.0.0.5/24",
                               "device_id": "599001"})
    discovery.save(fresh.paths.state_dir, 30, FOUND[:1])
    post(admin, "/map/import", {"back": "setup", "version": version(admin, "/setup/rooms"),
                                        "pick": "11", "b.11": "Science Hall"})
    r = post(admin, "/setup/schedules", {"version": version(admin, "/setup/schedules"),
                                         "t.science_hall": "not-a-target",
                                         "s.science_hall": "campus"})
    assert r.status_code == 422 and "Science Hall:" in r.text
    assert 'value="not-a-target"' in r.text                 # what was typed stays
    assert the_map(fresh)["buildings"][0]["system"] == "staging"


def test_finishing_without_the_schedule_leaves_it_off(fresh, admin, connected):
    _connect(admin)
    assert post(admin, "/setup/finish", {"action": "off"}).location == "/"
    assert _config(fresh)["schedule"]["enabled"] is False
    r = post(admin, "/setup/finish", {"action": "on", "times": "25:00"})
    assert r.status_code == 422 and "HH:MM" in r.text


def test_skipping_stops_the_redirect(fresh, admin):
    assert post(admin, "/setup/skip").location == "/"
    assert admin.get("/").status_code == 200
    assert admin.get("/setup").status_code == 200           # still there to use


def test_an_existing_site_is_not_sent_to_the_guide(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    page = c.get("/").text                                  # no redirect: config.yaml exists
    assert "Getting started" in page and "25Live</a> <span class=\"hint\">— demo" in page
    # With its password set every step is done, and the schedule is on.
    secretstore.write(site.paths.secrets_file, "BAS_25LIVE_PASSWORD", "x")
    assert "Getting started" not in c.get("/").text


def test_a_site_that_has_synced_is_not_shown_the_guide(site):
    from bassync import history
    from bassync.report import RunReport

    from .helpers import TZ
    report = RunReport("SYNC", "9.9", TZ)
    report.finish(0, "")
    history.save_report(report, site.paths.runs_dir)
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    assert "Getting started" not in c.get("/").text


def test_only_people_who_can_change_settings_get_the_guide(fresh, monkeypatch):
    configure_sso(fresh)
    c = signed_in_as(app_for(fresh), monkeypatch, ["BAS_Users"])      # advanced
    assert c.get("/").status_code == 200                   # no redirect for them
    assert c.get("/setup").status_code == 403
    assert post(c, "/map/import", {"back": "setup"}).status_code == 403


def test_the_steps_read_the_job_runner_s_summaries(fresh, admin, monkeypatch):
    """The job runner lists jobs as summaries (dicts): the last search for
    rooms, and the last checks, are shown from those."""
    jobs = [{"id": "j3", "kind": "validate", "label": "Validate", "started": "2026-10-01T02:00:00+00:00",
             "finished": "2026-10-01T02:01:00+00:00", "exit_code": 0, "running": False},
            {"id": "j2", "kind": "discover", "label": "Discover spaces",
             "started": "2026-10-01T01:00:00+00:00", "finished": "2026-10-01T01:01:00+00:00",
             "exit_code": 3, "running": False},
            {"id": "j1", "kind": "dry-run", "label": "Dry run", "started": "2026-10-01T00:00:00+00:00",
             "finished": None, "exit_code": None, "running": True}]
    monkeypatch.setattr(fresh.jobs, "recent", lambda limit=30: jobs, raising=False)
    page = html.unescape(admin.get("/setup/rooms").text)
    assert "The last search didn't finish (exit 3)" in page and "/jobs/j2" in page
    page = admin.get("/setup/finish").text
    assert "passed" in page and "/jobs/j3" in page and "running…" in page

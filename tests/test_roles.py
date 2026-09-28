"""Roles you can change: named sets of capabilities, edited on the Access page."""

import pytest
import yaml

pytest.importorskip("flask")

from bassync.web import access  # noqa: E402

from .test_access import GROUPS, configure_sso, signed_in_as  # noqa: E402
from .webhelpers import PASSWORD, app_for, post  # noqa: E402


@pytest.fixture
def sso_site(site):
    configure_sso(site)
    return site


SCHEDULER = {"id": "scheduler", "name": "Scheduler",
             "capabilities": ["view_basic", "view_all", "edit_bookings"]}


def _admin_client(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


def _roles(site) -> dict:
    return {r["id"]: r for r in access.load(site.paths.web_file)[0]["roles"]}


# ── the model ────────────────────────────────────────────────────────────────

def test_the_built_in_roles_are_the_defaults():
    roles = {r["id"]: r for r in access.clean_roles(None)}
    assert set(roles) == {"basic", "advanced", "admin"}
    assert access.role_capabilities(roles["basic"]) == {"view_basic", "sync"}
    assert "edit_map" in access.role_capabilities(roles["advanced"])
    assert "edit_settings" not in access.role_capabilities(roles["advanced"])
    assert access.role_capabilities(roles["admin"]) == access.EVERYTHING


def test_roles_are_cleaned_and_get_what_their_capabilities_need():
    roles = access.clean_roles([
        {"id": "ops", "name": "Ops", "capabilities": ["edit_bookings", "nonsense"]},
        {"id": "ops", "name": "Duplicate id"},
        {"id": "Bad Id!", "name": "x"},
        {"id": "force_only", "capabilities": ["force"]},
        "not a mapping",
    ])
    assert [r["id"] for r in roles] == ["ops", "force_only"]
    assert roles[0]["capabilities"] == ["view_basic", "view_all", "edit_bookings"]
    assert roles[1]["capabilities"] == ["view_basic", "sync", "force"]
    assert roles[1]["name"] == "force_only"
    assert access.clean_roles([]) == access.default_roles()


def test_several_roles_add_up():
    settings = access.empty()
    settings["roles"].append(SCHEDULER)
    settings["groups"] = [{"group": "Ops", "role": "scheduler", "note": ""},
                          {"group": "Viewers", "role": "basic", "note": ""},
                          {"group": "Ghost", "role": "gone", "note": ""}]
    roles = access.roles_for(["ops", "VIEWERS", "ghost"], settings)
    assert roles == ["basic", "scheduler"]
    assert access.capabilities_of(roles, settings) == {"view_basic", "sync", "view_all",
                                                       "edit_bookings"}
    assert access.roles_for(["ghost"], settings) == []


def test_unchanged_built_in_roles_are_not_written(tmp_path):
    path = tmp_path / "web.yaml"
    access.save(path, access.empty())
    assert "roles" not in yaml.safe_load(path.read_text())
    settings = access.empty()
    settings["roles"].append(SCHEDULER)
    access.save(path, settings)
    assert access.load(path)[0]["roles"][-1] == SCHEDULER


def test_new_role_ids_are_unique():
    settings = access.empty()
    assert access.new_role_id("Night Shift!", settings) == "night_shift"
    assert access.new_role_id("Admin", settings) == "admin_2"


# ── the Access page ──────────────────────────────────────────────────────────

def test_access_page_shows_a_column_per_role(site):
    page = _admin_client(site).get("/settings/access").text
    for name in ("Basic", "Advanced", "Admin"):
        assert f'value="{name}"' in page
    assert 'name="cap:advanced" value="edit_map"' in page
    assert "Restart the service" in page and "Add and edit extra bookings" in page


def test_saving_the_table_changes_what_a_role_may_do(site, caplog):
    c = _admin_client(site)
    with caplog.at_level("INFO"):
        r = post(c, "/settings/access/roles", {
            "name:basic": "Viewer", "cap:basic": ["view_basic"],
            "name:advanced": "Advanced", "cap:advanced": ["edit_map", "edit_settings"],
            "name:admin": "Admin", "all:admin": "1"})
    assert r.status_code == 302
    roles = _roles(site)
    assert roles["basic"] == {"id": "basic", "name": "Viewer", "capabilities": ["view_basic"]}
    assert roles["advanced"]["capabilities"] == ["view_basic", "view_all", "edit_map",
                                                 "edit_settings"]
    assert roles["admin"]["capabilities"] == "all"
    assert "changed roles — Viewer: view_basic; Advanced:" in caplog.text


def test_a_role_that_can_do_nothing_is_refused(site):
    c = _admin_client(site)
    post(c, "/settings/access/roles", {"name:basic": "Basic", "name:advanced": "Advanced",
                                       "cap:advanced": ["edit_map"], "name:admin": "Admin",
                                       "all:admin": "1"})
    assert _roles(site)["basic"]["capabilities"] == ["view_basic", "sync"]   # unchanged


def test_add_copy_and_delete_a_role(site):
    c = _admin_client(site)
    post(c, "/settings/access/roles/add", {"name": "Scheduler", "copy": "advanced"})
    assert _roles(site)["scheduler"]["capabilities"] == _roles(site)["advanced"]["capabilities"]
    post(c, "/settings/access/roles/add", {"name": "scheduler"})         # same name
    assert len(_roles(site)) == 4
    post(c, "/settings/access/groups", {"group": "Sched", "role": "scheduler"})
    post(c, "/settings/access/roles/delete", {"role": "scheduler"})      # in use
    assert "scheduler" in _roles(site)
    post(c, "/settings/access/groups/0/delete")
    post(c, "/settings/access/roles/delete", {"role": "scheduler"})
    assert "scheduler" not in _roles(site)


def test_restoring_the_built_in_roles_keeps_the_others(site):
    c = _admin_client(site)
    post(c, "/settings/access/roles/add", {"name": "Scheduler"})
    post(c, "/settings/access/roles", {"name:basic": "Viewer", "cap:basic": ["view_basic"],
                                       "name:advanced": "Advanced", "cap:advanced": ["edit_map"],
                                       "name:admin": "Admin", "all:admin": "1",
                                       "name:scheduler": "Scheduler",
                                       "cap:scheduler": ["edit_bookings"]})
    post(c, "/settings/access/roles/reset")
    roles = _roles(site)
    assert roles["basic"]["name"] == "Basic" and roles["basic"]["capabilities"] == ["view_basic",
                                                                                   "sync"]
    assert roles["scheduler"]["capabilities"] == ["view_basic", "view_all", "edit_bookings"]


def test_roles_can_not_lock_everyone_out(sso_site):
    """With the local password off, some group's role must still be able to
    change sign-in and roles."""
    web = access.load(sso_site.paths.web_file)[0]
    web["local_password"] = False
    assert access.lockout_problem(web, True) is None          # BAS_Admins -> Admin
    web["roles"][2]["capabilities"] = ["view_basic", "edit_settings"]
    assert "can change sign-in and roles" in access.lockout_problem(web, True)


def test_the_route_refuses_a_change_that_would_lock_everyone_out(sso_site, monkeypatch):
    web = access.load(sso_site.paths.web_file)[0]
    web["local_password"] = False
    access.save(sso_site.paths.web_file, web)
    c = signed_in_as(app_for(sso_site), monkeypatch, ["BAS_Admins"])
    r = post(c, "/settings/access/roles", {
        "name:basic": "Basic", "cap:basic": ["sync"], "name:advanced": "Advanced",
        "cap:advanced": ["edit_map"], "name:admin": "Admin", "cap:admin": ["edit_settings"]})
    assert r.status_code == 302
    assert _roles(sso_site)["admin"]["capabilities"] == "all"              # not saved
    assert "can change sign-in and roles" in c.get("/settings/access").text


# ── what people can do ───────────────────────────────────────────────────────

def test_a_custom_role_gets_exactly_its_capabilities(sso_site, monkeypatch):
    web = access.load(sso_site.paths.web_file)[0]
    web["roles"].append({"id": "settings", "name": "Settings editor",
                         "capabilities": ["view_basic", "view_all", "edit_settings"]})
    web["groups"].append({"group": "BAS_Settings", "role": "settings", "note": ""})
    access.save(sso_site.paths.web_file, web)
    c = signed_in_as(app_for(sso_site), monkeypatch, ["BAS_Settings"])
    assert "Settings editor" in c.get("/").text
    page = c.get("/settings/schedule").text
    assert "Read-only" not in page
    assert post(c, "/settings/schedule", {"enabled": "1", "times": "03:00"}).status_code == 302
    assert c.get("/settings/access").status_code == 403
    denied = post(c, "/settings/passwords", {"name": "BAS_25LIVE_PASSWORD", "value": "x"})
    assert denied.status_code == 403 and "Set and clear stored passwords" in denied.text
    assert post(c, "/jobs", {"kind": "sync"}).status_code == 403            # no sync
    assert c.get("/logs?which=activity").status_code == 403


def test_basic_plus_a_custom_role_add_up(sso_site, monkeypatch):
    web = access.load(sso_site.paths.web_file)[0]
    web["roles"].append(SCHEDULER)
    web["groups"].append({"group": "Schedulers", "role": "scheduler", "note": ""})
    access.save(sso_site.paths.web_file, web)
    c = signed_in_as(app_for(sso_site), monkeypatch, ["VPN_Role_PlantOps_BAS", "Schedulers"])
    assert "Basic + Scheduler" in c.get("/").text
    assert c.get("/map/rooms").status_code == 200                          # view_all
    assert post(c, "/jobs", {"kind": "sync"}).status_code == 302            # from Basic
    assert post(c, "/jobs", {"kind": "dry-run"}).status_code == 403


def test_a_group_whose_role_was_removed_grants_nothing(sso_site, monkeypatch):
    configure_sso(sso_site, groups=GROUPS + [{"group": "Old", "role": "retired", "note": ""}])
    from .test_access import sso_sign_in
    r = sso_sign_in(app_for(sso_site).test_client(), monkeypatch, ["Old"])
    assert r.status_code == 302 and "error=" in r.location
    page = _admin_client(sso_site).get("/settings/access").text
    assert "no role “retired”" in page


def test_the_local_password_can_always_do_everything(site):
    web = access.load(site.paths.web_file)[0]
    web["roles"] = [{"id": "admin", "name": "Admin", "capabilities": ["view_basic"]}]
    access.save(site.paths.web_file, web)
    c = _admin_client(site)
    assert c.get("/settings/access").status_code == 200
    assert c.get("/logs?which=activity").status_code == 200

"""Roles limited to syncing some systems or buildings."""

import html

import pytest

pytest.importorskip("flask")

from bassync.web import access  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import PASSWORD, app_for, post, the_map, version  # noqa: E402

SCI_OPS = {"id": "sci_ops", "name": "Science ops",
           "capabilities": ["view_basic", "view_all", "sync"],
           "sync_only": {"buildings": ["SCI"]}}
WEST_OPS = {"id": "west_ops", "name": "West ops",
            "capabilities": ["view_basic", "sync"],
            "sync_only": {"systems": ["west"]}}


def _with_roles(site, *roles, groups=()):
    web = configure_sso(site)
    web["roles"].extend(dict(r) for r in roles)
    web["groups"].extend({"group": g, "role": r, "note": ""} for g, r in groups)
    access.save(site.paths.web_file, web)
    return web


def _admin(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


# ── the model ────────────────────────────────────────────────────────────────

def test_limits_are_kept_cleaned_and_combined():
    roles = access.clean_roles([
        {"id": "a", "capabilities": ["sync"], "sync_only": {"systems": ["west", "west", " "],
                                                            "buildings": "not a list"}},
        {"id": "b", "capabilities": ["sync"], "sync_only": {"buildings": ["SCI"]}},
        {"id": "c", "capabilities": ["sync"], "sync_only": {}},
        {"id": "d", "capabilities": ["view_basic"], "sync_only": {"buildings": ["ART"]}},
    ])
    assert roles[0]["sync_only"] == {"systems": ["west"]}
    assert "sync_only" not in roles[2]
    settings = {"roles": roles}
    assert access.sync_scope(["a", "b"], settings) == {"systems": {"west"}, "buildings": {"SCI"}}
    assert access.sync_scope(["a", "c"], settings) is None          # c isn't limited
    assert access.sync_scope(["a", "d"], settings) == {"systems": {"west"}, "buildings": set()}
    scope = access.sync_scope(["a", "b"], settings)
    assert access.may_sync(scope, system="west") and access.may_sync(scope, building="SCI")
    assert not access.may_sync(scope) and not access.may_sync(scope, building="ART")
    assert access.may_sync(None) and access.may_sync(None, building="anything")
    assert access.describe_scope(scope) == "the system west and the building SCI"


def test_renaming_a_building_is_followed():
    settings = {"roles": access.clean_roles([dict(SCI_OPS)])}
    assert access.rename_building(settings, "SCI", "SCIENCE")
    assert settings["roles"][0]["sync_only"] == {"buildings": ["SCIENCE"]}
    assert not access.rename_building(settings, "ART", "ARTS")


# ── what a limited role may start ────────────────────────────────────────────

def test_a_role_limited_to_a_building_syncs_only_that(site, monkeypatch):
    _with_roles(site, SCI_OPS, groups=[("Sci_Ops", "sci_ops")])
    c = signed_in_as(app_for(site), monkeypatch, ["Sci_Ops"])
    page = c.get("/").text
    assert 'value="building:SCI"' in page
    assert 'value="building:ART"' not in page and ">Everything<" not in page
    assert 'value="system:' not in page
    denied = post(c, "/jobs", {"kind": "sync"})
    assert denied.status_code == 403 and "may sync only the building SCI" in html.unescape(denied.text)
    assert post(c, "/jobs", {"kind": "sync", "only": "building:ART"}).status_code == 403
    assert post(c, "/jobs", {"kind": "sync", "system": "campus"}).status_code == 403
    assert post(c, "/jobs", {"kind": "sync", "only": "building:SCI"}).status_code == 302
    assert site.jobs.started[-1][:2] == ("sync", ["--building", "SCI"])
    # The buildings list offers Sync on its own building only.
    listing = c.get("/map/buildings").text
    assert listing.count('name="building" value="SCI"') == 1
    assert 'name="building" value="ART"' not in listing


def test_a_role_limited_to_a_system_and_roles_add_up(site, monkeypatch):
    _with_roles(site, SCI_OPS, WEST_OPS, groups=[("Sci_Ops", "sci_ops"), ("West", "west_ops")])
    c = signed_in_as(app_for(site), monkeypatch, ["Sci_Ops", "West"])
    page = c.get("/").text
    assert 'value="system:west"' in page and 'value="building:SCI"' in page
    assert 'value="system:campus"' not in page
    assert post(c, "/jobs", {"kind": "sync", "only": "system:west"}).status_code == 302
    assert site.jobs.started[-1][:2] == ("sync", ["--system", "west"])
    assert post(c, "/jobs", {"kind": "sync", "only": "building:SCI"}).status_code == 302
    assert post(c, "/jobs", {"kind": "sync"}).status_code == 403


def test_an_unlimited_role_still_syncs_everything(site, monkeypatch):
    _with_roles(site, SCI_OPS, groups=[("Sci_Ops", "sci_ops")])
    c = signed_in_as(app_for(site), monkeypatch, ["Sci_Ops", "VPN_Role_PlantOps_BAS"])
    assert ">Everything<" in c.get("/").text                      # Basic has no limits
    assert post(c, "/jobs", {"kind": "sync"}).status_code == 302
    assert site.jobs.started[-1][:2] == ("sync", [])


def test_a_limit_to_a_building_that_is_gone_says_so(site, monkeypatch):
    _with_roles(site, dict(SCI_OPS, sync_only={"buildings": ["GONE"]}),
                groups=[("Sci_Ops", "sci_ops")])
    c = signed_in_as(app_for(site), monkeypatch, ["Sci_Ops"])
    page = c.get("/").text
    assert "none of those is in the room map any more" in page
    assert ">Sync now</button>" not in page
    assert post(c, "/jobs", {"kind": "sync", "only": "building:GONE"}).status_code == 400


# ── setting the limits ───────────────────────────────────────────────────────

def test_limits_are_set_on_the_access_page(site):
    c = _admin(site)
    page = c.get("/settings/access").text
    assert 'name="sync_building:basic"' in page and 'name="sync_system:basic" value="west"' in page
    r = post(c, "/settings/access/roles", {
        "name:basic": "Basic", "cap:basic": ["view_basic", "sync"],
        "limits:basic": "1", "sync_building:basic": ["SCI", "ART"], "sync_system:basic": "west",
        "name:advanced": "Advanced", "cap:advanced": ["edit_map", "sync"], "limits:advanced": "1",
        "name:admin": "Admin", "all:admin": "1", "limits:admin": "1"})
    assert r.status_code == 302
    roles = {r["id"]: r for r in access.load(site.paths.web_file)[0]["roles"]}
    assert roles["basic"]["sync_only"] == {"systems": ["west"], "buildings": ["SCI", "ART"]}
    assert "sync_only" not in roles["advanced"] and "sync_only" not in roles["admin"]
    page = c.get("/settings/access").text
    assert '<option value="SCI" selected>' in page
    # Saving without a role's limits on the form leaves them as they were.
    post(c, "/settings/access/roles", {
        "name:basic": "Basic", "cap:basic": ["view_basic", "sync"],
        "name:advanced": "Advanced", "cap:advanced": ["edit_map", "sync"],
        "name:admin": "Admin", "all:admin": "1"})
    roles = {r["id"]: r for r in access.load(site.paths.web_file)[0]["roles"]}
    assert roles["basic"]["sync_only"]["buildings"] == ["SCI", "ART"]


def test_renaming_a_building_keeps_who_may_sync_it(site):
    _with_roles(site, SCI_OPS)
    c = _admin(site)
    v = version(c, "/map/buildings/0/edit")
    post(c, "/map/buildings/save", {"version": v, "index": "0", "id": "SCIENCE",
                                    "name": "Science", "target": "12001:5"})
    assert the_map(site)["buildings"][0]["id"] == "SCIENCE"
    roles = {r["id"]: r for r in access.load(site.paths.web_file)[0]["roles"]}
    assert roles["sci_ops"]["sync_only"] == {"buildings": ["SCIENCE"]}

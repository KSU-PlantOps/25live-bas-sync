"""The JSON API under /api/v1/ and the API tokens it signs in with."""

import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
import yaml

pytest.importorskip("flask")

from bassync.web import access, tokens  # noqa: E402

from .webhelpers import app_for, csrf, post, signed_in_client  # noqa: E402


def _token(site, role="ai", days=90, name="assistant") -> str:
    _row, secret = tokens.create(tokens.path_for(site.paths.state_dir), name, role, days,
                                 "test")
    return secret


def _api(site):
    return app_for(site).test_client()


def _auth(secret):
    return {"Authorization": f"Bearer {secret}"}


def _set_roles(site, *extra):
    web = access.load(site.paths.web_file)[0]
    web["roles"] = access.default_roles() + [dict(r) for r in extra]
    access.save(site.paths.web_file, web)


# ── tokens ───────────────────────────────────────────────────────────────────

def test_a_token_is_shown_once_and_only_its_hash_is_kept(site):
    c = signed_in_client(site)
    r = post(c, "/settings/access/tokens", {"name": "Claude", "role": "ai", "days": "30"})
    assert r.status_code == 200
    secret = r.text.split('id="new-token">')[1].split("<")[0]
    assert secret.startswith(tokens.PREFIX)
    path = tokens.path_for(site.paths.state_dir)
    assert secret not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    listing = c.get("/settings/access/tokens").text
    assert "Claude" in listing and secret not in listing and "AI assistant" in listing
    assert _api(site).get("/api/v1/", headers=_auth(secret)).status_code == 200


def test_a_token_signs_in_as_its_role_without_a_session(site):
    secret = _token(site)
    r = _api(site).get("/api/v1/", headers=_auth(secret))
    assert r.status_code == 200
    caller = r.json["caller"]
    assert caller["role"] == "AI assistant" and caller["via"] == "token"
    assert "edit_bookings" in caller["capabilities"] and "sync" not in caller["capabilities"]
    assert caller["may_sync"] == "nothing"
    assert "Set-Cookie" not in r.headers


def test_a_token_never_holds_an_administrators_capabilities(site):
    """Even with the Admin role, which has everything."""
    secret = _token(site, role="admin")
    caps = set(_api(site).get("/api/v1/", headers=_auth(secret)).json["caller"]["capabilities"])
    assert "sync" in caps
    assert not caps & tokens.TOKEN_NEVER


def test_a_token_can_not_open_the_web_ui(site):
    secret = _token(site, role="admin")
    c = _api(site)
    assert c.get("/settings/connection", headers=_auth(secret)).status_code == 302
    assert c.get("/api/status", headers=_auth(secret)).status_code == 401


def test_bad_revoked_and_expired_tokens_are_refused(site, caplog):
    c = _api(site)
    r = c.get("/api/v1/status", headers=_auth("bsk_nope"))
    assert r.status_code == 401 and "Bearer" in r.json["error"]
    secret = _token(site, name="old")
    path = tokens.path_for(site.paths.state_dir)
    rows = tokens.read(path)
    rows[0]["expires"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    tokens._write(path, rows)
    assert c.get("/api/v1/", headers=_auth(secret)).status_code == 401
    secret = _token(site, name="revoked")
    admin = signed_in_client(site)
    token_id = next(r["id"] for r in tokens.read(path) if r["name"] == "revoked")
    assert post(admin, f"/settings/access/tokens/{token_id}/revoke").status_code == 302
    assert c.get("/api/v1/", headers=_auth(secret)).status_code == 401
    assert "unknown, revoked or expired token" in caplog.text


def test_wrong_tokens_are_throttled(site):
    c = _api(site)
    for _ in range(10):
        c.get("/api/v1/", headers=_auth("bsk_wrong"))
    r = c.get("/api/v1/", headers=_auth(_token(site)))
    assert r.status_code == 429 and "Too many" in r.json["error"]


def test_a_token_whose_role_is_gone_is_refused(site):
    secret = _token(site, role="nobody")
    r = _api(site).get("/api/v1/", headers=_auth(secret))
    assert r.status_code == 403 and "no longer exists" in r.json["error"]


def test_making_a_token_is_checked(site):
    c = signed_in_client(site)
    assert post(c, "/settings/access/tokens", {"name": "", "role": "ai",
                                               "days": "30"}).status_code == 422
    assert post(c, "/settings/access/tokens", {"name": "x", "role": "nope",
                                               "days": "30"}).status_code == 422
    assert post(c, "/settings/access/tokens", {"name": "x", "role": "ai",
                                               "days": "7"}).status_code == 422
    assert tokens.read(tokens.path_for(site.paths.state_dir)) == []


# ── reading ──────────────────────────────────────────────────────────────────

def test_the_read_endpoints_answer(site):
    c, h = _api(site), _auth(_token(site))
    assert c.get("/api/v1/status", headers=h).json["job"] is None
    assert c.get("/api/v1/runs", headers=h).json == {"runs": []}
    assert c.get("/api/v1/runs/20260101T000000Z", headers=h).status_code == 404
    spaces = c.get("/api/v1/spaces", headers=h).json["spaces"]
    assert sorted(s["id"] for s in spaces) == ["101", "102"]
    assert c.get("/api/v1/spaces/101", headers=h).json["bookings"] == []
    assert c.get("/api/v1/spaces/999", headers=h).status_code == 404
    room_map = c.get("/api/v1/map", headers=h).json
    assert room_map["buildings"][0]["id"] == "SCI"


def test_a_role_without_the_capability_is_refused(site):
    _set_roles(site, {"id": "events", "name": "Events", "capabilities": ["view_schedules"]})
    c, h = _api(site), _auth(_token(site, role="events"))
    assert c.get("/api/v1/spaces", headers=h).status_code == 200
    r = c.get("/api/v1/map", headers=h)
    assert r.status_code == 403 and "See everything else" in r.json["error"]
    assert c.post("/api/v1/low-temp", json={"event_id": "1"}, headers=h).status_code == 403


# ── jobs ─────────────────────────────────────────────────────────────────────

def test_the_ai_role_can_dry_run_and_validate_but_not_sync(site):
    c, h = _api(site), _auth(_token(site))
    r = c.post("/api/v1/jobs", json={"kind": "dry-run", "building": "SCI"}, headers=h)
    assert r.status_code == 202 and r.json["url"] == "/api/v1/jobs/job1"
    assert c.post("/api/v1/jobs", json={"kind": "validate", "system": "campus"},
                  headers=h).status_code == 202
    assert [(k, a) for k, a, _t in site.jobs.started] == [
        ("dry-run", ["--building", "SCI"]), ("validate", ["--system", "campus"])]
    assert "api (token “assistant”" in site.jobs.started[0][2]
    assert c.post("/api/v1/jobs", json={"kind": "sync"}, headers=h).status_code == 403
    for kind in ("force", "discover", "test-alert", "test-email"):
        assert c.post("/api/v1/jobs", json={"kind": kind}, headers=h).status_code == 400
    assert c.post("/api/v1/jobs", json={"kind": "dry-run", "system": "nope"},
                  headers=h).status_code == 400
    assert len(site.jobs.started) == 2


def test_sync_through_the_api_keeps_the_roles_limits(site):
    _set_roles(site, {"id": "bot", "name": "Bot", "capabilities": ["view_basic", "sync"],
                      "sync_only": {"buildings": ["SCI"]}})
    c, h = _api(site), _auth(_token(site, role="bot"))
    assert c.post("/api/v1/jobs", json={"kind": "sync"}, headers=h).status_code == 403
    assert c.post("/api/v1/jobs", json={"kind": "sync", "building": "SCI"},
                  headers=h).status_code == 202
    assert site.jobs.started == [("sync", ["--building", "SCI"], site.jobs.started[0][2])]
    assert c.post("/api/v1/jobs", json={"kind": "sync", "force": True, "building": "SCI"},
                  headers=h).json["job"]["args"] == []      # force is never passed on


def test_a_busy_service_is_a_conflict(site):
    site.jobs.busy_with = "Sync"
    r = _api(site).post("/api/v1/jobs", json={"kind": "dry-run"}, headers=_auth(_token(site)))
    assert r.status_code == 409 and "Sync is already running" in r.json["error"]


def test_a_browser_session_needs_the_csrf_token(site):
    c = signed_in_client(site)
    assert c.post("/api/v1/jobs", json={"kind": "dry-run"}).status_code == 400
    r = c.post("/api/v1/jobs", json={"kind": "dry-run"}, headers={"X-CSRF-Token": csrf(c)})
    assert r.status_code == 202


# ── extra bookings ───────────────────────────────────────────────────────────

OPEN_HOUSE = {"title": "Open house", "building": "SCI", "date": "2026-11-14",
              "start": "08:00", "end": "14:00"}


def _extras(site) -> list:
    return yaml.safe_load(site.paths.extras_file.read_text())["bookings"]


def test_add_change_and_delete_an_extra_booking(site, caplog):
    c, h = _api(site), _auth(_token(site))
    with caplog.at_level("INFO"):
        r = c.post("/api/v1/bookings", json=OPEN_HOUSE, headers=h)
    assert r.status_code == 201, r.json
    assert r.json["booking"]["where"] == "Science" and r.json["booking"]["problem"] == ""
    assert _extras(site)[0]["added_by"] == "token “assistant”"
    assert "added the extra booking 'Open house'" in caplog.text and "through the API" in caplog.text
    version = c.get("/api/v1/bookings", headers=h).json["version"]
    changed = dict(OPEN_HOUSE, end="15:00", version=version)
    assert c.put("/api/v1/bookings/0", json=changed, headers=h).status_code == 200
    assert _extras(site)[0]["end"] == "15:00" and _extras(site)[0]["added_by"] == "token “assistant”"
    assert c.put("/api/v1/bookings/0", json=changed, headers=h).status_code == 409   # stale
    version = c.get("/api/v1/bookings", headers=h).json["version"]
    assert c.delete(f"/api/v1/bookings/0?version={version}", headers=h).status_code == 200
    assert c.get("/api/v1/bookings", headers=h).json["bookings"] == []


def test_a_bad_booking_says_what_is_wrong(site):
    c, h = _api(site), _auth(_token(site))
    r = c.post("/api/v1/bookings", json={"title": "x", "building": "SCI", "date": "someday",
                                         "start": "08:00", "end": "09:00", "colour": "red"},
               headers=h)
    assert r.status_code == 422
    assert "Unknown field 'colour'" in r.json["error"] and "date" in r.json["error"]
    r = c.post("/api/v1/bookings", json={"date": "2026-11-14", "start": "08:00", "end": "09:00"},
               headers=h)
    assert "title" in r.json["error"] and "Say where it is" in r.json["error"]
    assert c.post("/api/v1/bookings", data="nope", headers=h).status_code == 400
    assert not site.paths.extras_file.exists()


def test_low_temp_on_a_booking_needs_the_low_temp_capability(site):
    _set_roles(site, {"id": "booker", "name": "Booker",
                      "capabilities": ["view_basic", "view_all", "edit_bookings"]})
    c, h = _api(site), _auth(_token(site, role="booker"))
    assert c.post("/api/v1/bookings", json=dict(OPEN_HOUSE, low_temp=True),
                  headers=h).status_code == 201
    assert "low_temp" not in _extras(site)[0]


# ── low temp ─────────────────────────────────────────────────────────────────

def test_mark_and_unmark_an_event_low_temp(site):
    c, h = _api(site), _auth(_token(site))
    r = c.post("/api/v1/low-temp", json={"event_id": "12345", "name": "Blood drive"}, headers=h)
    assert r.status_code == 200 and r.json["changed"] is True
    assert c.post("/api/v1/low-temp", json={"event_id": "12345"},
                  headers=h).json["changed"] is False
    events = c.get("/api/v1/low-temp", headers=h).json["events"]
    assert [e["event_id"] for e in events] == ["12345"]
    assert c.delete("/api/v1/low-temp/12345", headers=h).json["changed"] is True
    assert c.get("/api/v1/low-temp", headers=h).json["events"] == []
    assert c.post("/api/v1/low-temp", json={}, headers=h).status_code == 400


# ── the roles form ───────────────────────────────────────────────────────────

def test_a_role_missing_from_the_saved_form_is_left_alone(site):
    """A role added after the Access page was drawn isn't on it; saving that
    page mustn't take everything away from the new role."""
    c = signed_in_client(site)
    r = post(c, "/settings/access/roles", {
        "name:basic": "Basic", "cap:basic": ["view_basic"], "name:advanced": "Advanced",
        "cap:advanced": ["edit_map"], "name:admin": "Admin", "all:admin": "1"})
    assert r.status_code == 302
    roles = {r["id"]: r for r in access.load(site.paths.web_file)[0]["roles"]}
    assert roles["ai"]["capabilities"] == access.default_roles()[3]["capabilities"]
    assert roles["basic"]["capabilities"] == ["view_basic"]

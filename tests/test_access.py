"""Roles, Microsoft Entra ID sign-in, and the passwords the web UI can store."""

import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

pytest.importorskip("flask")

from bassync import secretstore  # noqa: E402
from bassync.web import access, entra  # noqa: E402

from .webhelpers import CONFIG, PASSWORD, app_for, post, version  # noqa: E402

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT = "11111111-2222-3333-4444-555555555555"
GROUPS = [{"group": "VPN_Role_PlantOps_BAS", "role": "basic", "note": ""},
          {"group": "BAS_Users", "role": "advanced", "note": ""},
          {"group": "BAS_Admins", "role": "admin", "note": ""}]


def configure_sso(service, groups=GROUPS, **sso):
    settings = access.empty()
    settings["sso"].update({"enabled": True, "tenant_id": TENANT, "client_id": CLIENT, **sso})
    settings["groups"] = [dict(g) for g in groups]
    access.save(service.paths.web_file, settings)
    secretstore.write(service.paths.secrets_file, "BAS_WEB_SSO_CLIENT_SECRET", "s3cret")
    return settings


def jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def sso_sign_in(client, monkeypatch, groups, *, claims=None, state=None, status=200,
                body=None, host="login.microsoftonline.com"):
    """Walk the browser side of the flow against a fake Entra token endpoint.
    Returns the callback's response."""
    start = client.get("/auth/login?next=/runs")
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.location).query)
    assert start.location.startswith(f"https://{host}/{TENANT}/oauth2/v2.0/authorize?")
    assert query["code_challenge_method"] == ["S256"]
    seen = {}

    def fake_post(url, data, timeout):
        seen.update(url=url, data=data)
        # PKCE: the verifier sent now must hash to the challenge sent before.
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert digest == query["code_challenge"][0]
        token = {"aud": CLIENT, "tid": TENANT, "iss": f"https://{host}/{TENANT}/v2.0",
                 "exp": time.time() + 3600, "nbf": time.time() - 10,
                 "nonce": query["nonce"][0], "name": "Pat Doe",
                 "preferred_username": "pdoe@example.edu", "oid": "abc",
                 "groups": groups, **(claims or {})}
        return FakeResponse(status, body if body is not None else {"id_token": jwt(token)})

    monkeypatch.setattr(entra.requests, "post", fake_post)
    r = client.get(f"/auth/callback?code=the-code&state={state or query['state'][0]}")
    if seen:
        assert seen["url"] == f"https://{host}/{TENANT}/oauth2/v2.0/token"
        assert seen["data"]["client_secret"] == "s3cret" and seen["data"]["code"] == "the-code"
    return r


@pytest.fixture
def sso_site(site):
    configure_sso(site)
    return site


def signed_in_as(app, monkeypatch, groups):
    c = app.test_client()
    r = sso_sign_in(c, monkeypatch, groups)
    assert r.status_code == 302 and r.location == "/runs", r.location
    return c


# ── sign-in ──────────────────────────────────────────────────────────────────

def test_login_page_offers_microsoft_when_sso_is_complete(site):
    c = app_for(site).test_client()
    assert "Sign in with Microsoft" not in c.get("/login").text
    configure_sso(site)
    page = c.get("/login").text
    assert "Sign in with Microsoft" in page and "Local password" in page


def test_incomplete_sso_is_not_offered(site):
    configure_sso(site, groups=[])
    assert "Sign in with Microsoft" not in app_for(site).test_client().get("/login").text


def test_the_highest_role_wins_and_matching_ignores_case(sso_site, monkeypatch):
    app = app_for(sso_site)
    c = signed_in_as(app, monkeypatch, ["vpn_role_plantops_bas", "bas_users", "BAS_ADMINS"])
    assert "Admin" in c.get("/").text
    assert c.get("/settings/access").status_code == 200


def test_basic_sees_the_basics_and_can_sync_everything(sso_site, monkeypatch):
    c = signed_in_as(app_for(sso_site), monkeypatch, ["VPN_Role_PlantOps_BAS", "Some_Other"])
    page = c.get("/").text
    assert "Sync now" in page and "Dry run" not in page and "Force" not in page
    assert ">Rooms<" not in page and ">Access<" not in page
    assert c.get("/runs").status_code == 200
    for url in ("/map/rooms", "/jobs", "/logs", "/settings/connection", "/files",
                "/settings/access", "/settings/alerts"):
        assert c.get(url).status_code == 403, url
    assert post(c, "/jobs", {"kind": "sync"}).status_code == 302
    assert sso_site.jobs.started[-1][:2] == ("sync", [])
    assert post(c, "/jobs", {"kind": "dry-run"}).status_code == 403
    assert post(c, "/jobs", {"kind": "sync", "system": "campus"}).status_code == 403
    assert post(c, "/jobs", {"kind": "sync", "force": "1"}).status_code == 403


def test_advanced_edits_the_map_but_not_the_settings(sso_site, monkeypatch):
    c = signed_in_as(app_for(sso_site), monkeypatch, ["BAS_Users"])
    assert c.get("/settings/connection").status_code == 200        # can look
    assert "Read-only" in c.get("/settings/connection").text
    v = version(c, "/map/rooms/new")
    assert post(c, "/map/rooms/save", {"version": v, "space_id": "900",
                                       "building": "SCI"}).status_code == 302
    assert post(c, "/jobs", {"kind": "validate"}).status_code == 302
    assert post(c, "/jobs", {"kind": "sync", "force": "1"}).status_code == 403
    for url, data in (("/settings/defaults", {"lookahead_days": "3"}),
                      ("/settings/schedule", {"times": "03:00"}),
                      ("/settings/systems", {"name": "x", "driver": "preview"}),
                      ("/files/config", {"text": "x: 1"}),
                      ("/settings/passwords", {"name": "BAS_25LIVE_PASSWORD", "value": "p"}),
                      ("/settings/alerts", {}), ("/settings/access", {})):
        assert post(c, url, data).status_code == 403, url
    assert c.get("/settings/access").status_code == 403


def test_no_matching_group_is_refused_and_shown_to_admins(sso_site, monkeypatch):
    app = app_for(sso_site)
    r = sso_sign_in(app.test_client(), monkeypatch, ["Students", "a1b2c3d4-0000-0000-0000-000000000000"])
    assert "/login?error=" in r.location and "group" in r.location
    admin = app.test_client()
    admin.post("/login", data={"password": PASSWORD})
    page = admin.get("/settings/access").text
    assert "Last refused sign-in: Pat Doe" in page and "Students" in page


@pytest.mark.parametrize("claims, why", [
    ({"tid": "00000000-0000-0000-0000-000000000000"}, "different tenant"),
    ({"aud": "someone-else"}, "different application"),
    ({"exp": time.time() - 3600}, "expired"),
    ({"nonce": "replayed"}, "belong to this sign-in"),
    ({"iss": "https://evil.example/v2.0"}, "issuer"),
    ({"_claim_names": {"groups": "src1"}}, "more groups"),
    ({"groups": None}, "groups claim"),
])
def test_bad_tokens_are_refused(sso_site, monkeypatch, claims, why):
    r = sso_sign_in(app_for(sso_site).test_client(), monkeypatch, ["BAS_Admins"], claims=claims)
    assert "/login?error=" in r.location
    assert why.split()[0] in parse_qs(urlparse(r.location).query)["error"][0]


def test_a_forged_state_or_a_refusal_from_microsoft(sso_site, monkeypatch):
    app = app_for(sso_site)
    r = sso_sign_in(app.test_client(), monkeypatch, ["BAS_Admins"], state="forged")
    assert "didn" in parse_qs(urlparse(r.location).query)["error"][0]
    r = sso_sign_in(app.test_client(), monkeypatch, ["BAS_Admins"], status=400,
                    body={"error": "invalid_client", "error_description": "AADSTS7000215: bad secret"})
    assert "AADSTS7000215" in parse_qs(urlparse(r.location).query)["error"][0]
    # A callback with no sign-in in progress is refused too.
    assert "/login?error=" in app.test_client().get("/auth/callback?code=x&state=y").location


def test_removing_a_group_signs_its_people_out(sso_site, monkeypatch):
    app = app_for(sso_site)
    c = signed_in_as(app, monkeypatch, ["BAS_Users"])
    assert c.get("/map/rooms").status_code == 200
    configure_sso(sso_site, groups=[GROUPS[2]])
    assert c.get("/map/rooms").status_code == 302                  # to the sign-in page


def test_lowering_a_role_applies_at_once(sso_site, monkeypatch):
    app = app_for(sso_site)
    c = signed_in_as(app, monkeypatch, ["BAS_Users"])
    configure_sso(sso_site, groups=[{"group": "BAS_Users", "role": "basic", "note": ""}])
    assert c.get("/map/rooms").status_code == 403


def test_a_new_sso_app_signs_everyone_out(sso_site, monkeypatch):
    app = app_for(sso_site)
    c = signed_in_as(app, monkeypatch, ["BAS_Admins"])
    configure_sso(sso_site, client_id="99999999-2222-3333-4444-555555555555")
    assert c.get("/").status_code == 302


def test_us_government_cloud(site, monkeypatch):
    configure_sso(site, authority_host="login.microsoftonline.us")
    r = sso_sign_in(app_for(site).test_client(), monkeypatch, ["BAS_Admins"],
                    host="login.microsoftonline.us")
    assert r.location == "/runs"


def test_the_authority_override_is_https_or_localhost():
    settings = access.empty()
    settings["sso"]["tenant_id"] = TENANT
    assert entra.authority(settings, "http://127.0.0.1:9999").startswith("http://127.0.0.1")
    with pytest.raises(entra.SsoError):
        entra.authority(settings, "http://idp.example.edu")


# ── the local password ───────────────────────────────────────────────────────

def test_local_password_can_be_turned_off_once_sso_works(sso_site):
    app = app_for(sso_site)
    c = app.test_client()
    c.post("/login", data={"password": PASSWORD})
    page = c.get("/settings/access")
    assert page.status_code == 200
    form = {"enabled": "1", "tenant_id": TENANT, "client_id": CLIENT,
            "authority_host": "login.microsoftonline.com", "public_url": ""}
    assert post(c, "/settings/access", form).status_code == 302   # local password off
    assert access.load(sso_site.paths.web_file)[0]["local_password"] is False
    # The session that used it ends, and the password no longer works.
    assert c.get("/").status_code == 302
    fresh = app.test_client()
    r = fresh.post("/login", data={"password": PASSWORD})
    assert r.status_code == 401 and "turned off" in r.text


def test_settings_that_would_lock_everyone_out_are_refused(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    # Local password off with no SSO at all:
    r = post(c, "/settings/access", {"authority_host": "login.microsoftonline.com"})
    assert r.status_code == 422 and "once single sign-on" in r.text
    # SSO on, but no Admin group:
    configure_sso(site, groups=[GROUPS[0]])
    r = post(c, "/settings/access", {"enabled": "1", "tenant_id": TENANT, "client_id": CLIENT,
                                     "authority_host": "login.microsoftonline.com"})
    assert r.status_code == 422 and "Admin role" in r.text
    assert access.load(site.paths.web_file)[0]["local_password"] is True


def test_access_page_saves_and_keeps_the_secret_out_of_web_yaml(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    post(c, "/settings/access/groups", {"group": "BAS_Admins", "role": "admin"})
    r = post(c, "/settings/access", {"enabled": "1", "tenant_id": TENANT, "client_id": CLIENT,
                                     "client_secret": "top-secret", "local_password": "1",
                                     "authority_host": "login.microsoftonline.com",
                                     "public_url": "https://bas.example.edu/"})
    assert r.status_code == 302, r.text
    text = site.paths.web_file.read_text()
    assert "top-secret" not in text and "BAS_Admins" in text
    assert secretstore.read(site.paths.secrets_file)["BAS_WEB_SSO_CLIENT_SECRET"] == "top-secret"
    assert stat.S_IMODE(os.stat(site.paths.secrets_file).st_mode) == 0o600
    assert "https://bas.example.edu/auth/callback" in c.get("/settings/access").text
    assert "Sign in with Microsoft" in app_for(site).test_client().get("/login").text


def test_group_mappings_add_and_remove(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    post(c, "/settings/access/groups", {"group": " BAS_Users ", "role": "advanced", "note": "techs"})
    post(c, "/settings/access/groups", {"group": "bas_users", "role": "admin"})      # duplicate
    post(c, "/settings/access/groups", {"group": "X", "role": "superuser"})           # bad role
    groups = access.load(site.paths.web_file)[0]["groups"]
    assert groups == [{"group": "BAS_Users", "role": "advanced", "note": "techs"}]
    post(c, "/settings/access/groups/0/delete")
    assert access.load(site.paths.web_file)[0]["groups"] == []


# ── passwords and alerts ─────────────────────────────────────────────────────

def test_passwords_are_stored_owner_only_and_the_environment_wins(site, monkeypatch):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    assert post(c, "/settings/passwords", {"name": "BAS_25LIVE_PASSWORD",
                                           "value": "hunter2"}).status_code == 302
    store = site.paths.secrets_file
    assert secretstore.read(store) == {"BAS_25LIVE_PASSWORD": "hunter2"}
    assert "set here" in c.get("/settings/connection").text
    assert "hunter2" not in c.get("/settings/connection").text
    assert post(c, "/settings/passwords", {"name": "BAS_WEB_PASSWORD", "value": "x"}).status_code == 400
    monkeypatch.setenv("BAS_25LIVE_PASSWORD", "from-env")
    assert secretstore.get("BAS_25LIVE_PASSWORD", store) == "from-env"
    assert "set in the environment" in c.get("/settings/connection").text
    post(c, "/settings/passwords", {"name": "BAS_25LIVE_PASSWORD", "clear": "1"})
    assert secretstore.read(store) == {}


def test_load_credentials_uses_stored_passwords(tmp_path, monkeypatch):
    from bassync.config import load_config, load_credentials
    environ = {k: v for k, v in os.environ.items()
               if k not in ("BAS_25LIVE_PASSWORD", "BAS_SMTP_PASSWORD")}
    monkeypatch.setattr(os, "environ", environ)          # nothing leaks out
    cfg = load_config("/nonexistent/config.yaml")
    cfg["safety"]["state_file"] = str(tmp_path / "last_run.json")
    secretstore.write(tmp_path / "secrets.json", "BAS_25LIVE_PASSWORD", "stored-pw")
    secretstore.write(tmp_path / "secrets.json", "BAS_SMTP_PASSWORD", "smtp-pw")
    load_credentials(cfg)
    assert cfg["collegenet"]["password"] == "stored-pw"
    assert os.environ["BAS_SMTP_PASSWORD"] == "smtp-pw"          # read at send time


def test_alerts_page_saves_smtp_settings(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    v = version(c, "/settings/alerts")
    r = post(c, "/settings/alerts", {
        "version": v, "alerts.enabled": "1", "alerts.email.enabled": "1",
        "alerts.email.smtp_host": "smtp.example.edu", "alerts.email.smtp_port": "587",
        "alerts.email.security": "starttls", "alerts.email.username": "svc-bas",
        "alerts.email.from_addr": "bas@example.edu",
        "alerts.email.to_addrs": "a@example.edu\nb@example.edu, c@example.edu",
        "alerts.email.notify_on_success": "yes", "alerts.email.report": "summary",
        "alerts.email.attach_csv": "1", "alerts.webhook_format": "teams",
        "alerts.webhook_notify_on_success": "", "monitoring.ping_url": "https://hc.example/x"})
    assert r.status_code == 302, r.text
    saved = yaml.safe_load(site.paths.config.read_text())
    email = saved["alerts"]["email"]
    assert saved["alerts"]["enabled"] is True and email["smtp_port"] == 587
    assert email["to_addrs"] == ["a@example.edu", "b@example.edu", "c@example.edu"]
    assert email["notify_on_success"] is True and email["report"] == "summary"
    assert "webhook_notify_on_success" not in saved["alerts"]
    assert saved["monitoring"]["ping_url"] == "https://hc.example/x"
    assert saved["systems"] == CONFIG["systems"]                 # the rest untouched
    r = post(c, "/settings/alerts", {"version": version(c, "/settings/alerts"),
                                     "alerts.email.smtp_port": "99999"})
    assert r.status_code == 422 and "1 to 65535" in r.text


def test_secret_store_names_and_permissions(tmp_path):
    path = tmp_path / "s" / "secrets.json"
    for name in ("BAS_WEB_PASSWORD", "PATH", "BAS_SYS_x_PASSWORD"):
        with pytest.raises(ValueError):
            secretstore.write(path, name, "x")
    secretstore.write(path, "BAS_SYS_EBO_WEST_PASSWORD", "pw")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    env = {"BAS_SMTP_PASSWORD": "env"}
    secretstore.write(path, "BAS_SMTP_PASSWORD", "stored")
    assert secretstore.fill_environ(path, env) == ["BAS_SYS_EBO_WEST_PASSWORD"]
    assert env["BAS_SMTP_PASSWORD"] == "env"
    path.write_text("[1, 2]")
    assert secretstore.read(path) == {}


# ── nothing unguarded ────────────────────────────────────────────────────────

def test_every_route_checks_the_role(site):
    app = app_for(site)
    open_endpoints = {"static", "login", "logout", "healthz", "auth_login", "auth_callback",
                      "branding_css", "branding_logo"}
    for endpoint, fn in app.view_functions.items():
        if endpoint not in open_endpoints:
            assert hasattr(fn, "required"), f"{endpoint} has no @requires"


def test_the_service_serves_the_web_ui_with_sso_and_no_password(site):
    from bassync.service import sso_ready, web_settings
    assert not sso_ready(site.paths, {})
    configure_sso(site)
    assert sso_ready(site.paths, {})
    assert web_settings({}, sso=True)["enabled"]
    assert not web_settings({}, sso=False)["enabled"]

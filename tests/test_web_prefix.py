"""The web UI under a path prefix: BAS_WEB_BASE_PATH for a proxy that forwards
the full path (IIS URL Rewrite + ARR), X-Forwarded-Prefix for one that strips
it — and, with neither, exactly the site-root behaviour of before."""

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("flask")

from bassync.service import base_path, web_settings  # noqa: E402
from bassync.web import redirect_uri  # noqa: E402

from .test_access import configure_sso, sso_sign_in  # noqa: E402
from .webhelpers import PASSWORD, app_for, csrf  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "bassync" / "web" / "static"
# Every URL a page hands the browser: links, forms, scripts, and the URLs
# app.js fetches or navigates to, which come from data- attributes.
_URLS = re.compile(r'(?:href|src|action|data-[a-z-]+)="(/[^"]*)"')


def cookie_paths(response) -> list:
    return [m.group(1) for c in response.headers.getlist("Set-Cookie")
            if c.startswith("bas_sync_session=")
            for m in [re.search(r"Path=([^;]*)", c)] if m]


def token(c, prefix) -> str:
    """The CSRF token, from the session cookie the browser sends under `prefix`."""
    with c.session_transaction(f"{prefix}/") as s:
        return s["csrf"]


def sign_in(c, prefix="", next_="/", **kw):
    return c.post(f"{prefix}/login", data={"password": PASSWORD, "next": next_}, **kw)


# ── the setting ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, path", [
    (None, ""), ("", ""), ("/", ""), ("  ", ""), ("bas", "/bas"), ("/bas/", "/bas"),
    ("//bas//", "/bas"), (" /bas ", "/bas"), ("/plant-ops/bas_sync", "/plant-ops/bas_sync"),
    ("/v1.2", "/v1.2")])
def test_base_path_is_normalised(value, path):
    assert base_path(value) == path


@pytest.mark.parametrize("value", ["/b as", "/../x", "/./x", "/bas?x=1", "/bas#x", "/a\\b",
                                   "/%2e%2e", "/ü"])
def test_a_base_path_that_isnt_a_plain_path_keeps_the_web_ui_off(value):
    assert base_path(value) is None
    settings = web_settings({"BAS_WEB_PASSWORD": PASSWORD, "BAS_WEB_BASE_PATH": value})
    assert not settings["enabled"] and "BAS_WEB_BASE_PATH" in settings["reason"]


def test_unset_means_the_site_root():
    assert web_settings({"BAS_WEB_PASSWORD": PASSWORD})["base_path"] == ""
    assert web_settings({"BAS_WEB_PASSWORD": PASSWORD,
                         "BAS_WEB_BASE_PATH": "bas/"})["base_path"] == "/bas"


# ── at the site root (unset): as before ──────────────────────────────────────

def test_root_mode_is_unchanged(site):
    app = app_for(site)
    assert app.session_interface.__class__.__name__ == "SecureCookieSessionInterface"
    c = app.test_client()
    r = c.get("/map/rooms")
    assert r.status_code == 302 and r.location == "/login?next=/map/rooms"
    assert c.get("/static/app.css").status_code == 200
    r = sign_in(c, next_="/runs")
    assert r.location == "/runs" and cookie_paths(r) == ["/"]
    page = c.get("/").text
    assert 'href="/static/app.css' in page
    assert c.post("/logout", data={"csrf": csrf(c)}).location == "/login"


def test_root_mode_ignores_x_forwarded_prefix_without_behind_proxy(site):
    c = app_for(site).test_client()
    r = c.get("/map/rooms", headers={"X-Forwarded-Prefix": "/bas"})
    assert r.location == "/login?next=/map/rooms"


# ── BAS_WEB_BASE_PATH: the proxy forwards the full path ──────────────────────

@pytest.fixture
def prefixed(site):
    return app_for(site, BAS_WEB_BASE_PATH="/bas/")


def test_pages_and_static_files_are_under_the_prefix(prefixed):
    c = prefixed.test_client()
    r = c.get("/bas/map/rooms")
    assert r.status_code == 302 and r.location == "/bas/login?next=/map/rooms"
    login = c.get("/bas/login")
    assert login.status_code == 200 and 'href="/bas/static/app.css' in login.text
    assert c.get("/bas/static/app.css").status_code == 200
    assert c.get("/bas/static/app.js").status_code == 200
    assert c.get("/bas/healthz").json["ok"] is True
    assert sign_in(c, "/bas").location == "/bas/"
    for url in ("/bas/", "/bas/map/rooms", "/bas/settings/access", "/bas/runs", "/bas/jobs",
                "/bas/bookings", "/bas/schedules", "/bas/settings/connection"):
        page = c.get(url)
        assert page.status_code == 200, url
        escaped = [u for u in _URLS.findall(page.text) if not u.startswith("/bas/")]
        assert not escaped, f"{url} links outside the prefix: {escaped}"


def test_the_bare_prefix_redirects_to_the_slash(prefixed):
    c = prefixed.test_client()
    r = c.get("/bas")
    assert r.status_code == 308 and r.headers["Location"] == "/bas/"
    assert c.get("/bas?x=1").headers["Location"] == "/bas/?x=1"


@pytest.mark.parametrize("path", ["/", "/login", "/static/app.css", "/healthz", "/basement",
                                  "/SensorView/", "/api/v1/status"])
def test_paths_outside_the_prefix_are_not_found(prefixed, path):
    r = prefixed.test_client().get(path)
    assert r.status_code == 404 and "/bas/" in r.text


def test_sign_in_and_out_stay_under_the_prefix(prefixed):
    c = prefixed.test_client()
    r = sign_in(c, "/bas", next_="/runs")
    assert r.status_code == 302 and r.location == "/bas/runs"
    assert cookie_paths(r) == ["/bas"]
    assert c.get("/bas/runs").status_code == 200        # the cookie comes back
    r = c.post("/bas/logout", data={"csrf": token(c, "/bas")})
    assert r.location == "/bas/login" and cookie_paths(r) == ["/bas"]
    assert c.get("/bas/runs").status_code == 302


def test_a_next_that_already_has_the_prefix_is_taken_as_is(prefixed):
    c = prefixed.test_client()
    assert sign_in(c, "/bas", next_="/bas/runs?x=1").location == "/bas/runs?x=1"
    c.post("/bas/logout", data={"csrf": token(c, "/bas")})
    assert sign_in(c, "/bas", next_="/bas").location == "/bas/"


@pytest.mark.parametrize("evil", ["//evil.example/x", "https://evil.example/", "javascript:alert(1)",
                                  "/\\evil.example", "\\\\evil.example", "/\t/evil.example",
                                  "evil.example"])
def test_sign_in_only_redirects_within_the_prefix(prefixed, site, evil):
    c = prefixed.test_client()
    assert sign_in(c, "/bas", next_=evil).location == "/bas/"
    # ...and the same at the site root.
    assert sign_in(app_for(site).test_client(), next_=evil).location == "/"


def test_the_login_form_keeps_next_within_the_app(prefixed):
    c = prefixed.test_client()
    page = c.get("/bas/login?next=/runs").text
    assert 'name="next" value="/runs"' in page


def test_the_service_page_shows_the_prefix(prefixed, site):
    site.web = {"host": "0.0.0.0", "port": 8080, "cert": "", "behind_proxy": False,
                "auth": "password", "base_path": "/bas"}
    c = prefixed.test_client()
    sign_in(c, "/bas")
    assert "http://0.0.0.0:8080/bas/" in c.get("/bas/settings/service").text


def test_the_api_works_under_the_prefix(prefixed):
    c = prefixed.test_client()
    assert c.get("/bas/api/v1/status").status_code == 401
    sign_in(c, "/bas")
    page = c.get("/bas/settings/access/tokens").text
    assert "http://localhost/bas/api/v1/" in page


# ── SSO: the callback is under the prefix ────────────────────────────────────

def test_sso_redirect_uri_includes_the_prefix(prefixed):
    with prefixed.test_request_context("/settings/access", base_url="https://h.example/bas/"):
        sso = {"public_url": ""}
        assert redirect_uri({"sso": sso}) == "https://h.example/bas/auth/callback"
        sso["public_url"] = "https://bas.example.edu"
        assert redirect_uri({"sso": sso}) == "https://bas.example.edu/bas/auth/callback"
        sso["public_url"] = "https://bas.example.edu/bas"
        assert redirect_uri({"sso": sso}) == "https://bas.example.edu/bas/auth/callback"
    with prefixed.test_request_context("/settings/access", base_url="https://h.example/"):
        assert redirect_uri({"sso": {"public_url": "https://bas.example.edu"}}) == \
            "https://bas.example.edu/auth/callback"


def test_sso_sign_in_under_the_prefix(site, monkeypatch):
    configure_sso(site, public_url="https://ops.example.edu")
    c = app_for(site, BAS_WEB_BASE_PATH="/bas").test_client()
    start = c.get("/bas/auth/login?next=/runs")
    sent = parse_qs(urlparse(start.location).query)["redirect_uri"]
    assert sent == ["https://ops.example.edu/bas/auth/callback"]
    r = sso_sign_in(c, monkeypatch, ["BAS_Admins"], prefix="/bas")
    assert r.status_code == 302 and r.location == "/bas/runs"
    assert cookie_paths(r) == ["/bas"]
    assert c.get("/bas/runs").status_code == 200


# ── X-Forwarded-Prefix: the proxy strips the prefix ──────────────────────────

FWD = {"X-Forwarded-Prefix": "/bas"}


def test_x_forwarded_prefix_behind_a_proxy(site):
    c = app_for(site, BAS_WEB_BEHIND_PROXY="1").test_client()
    r = c.get("/map/rooms", headers=FWD)
    assert r.location == "/bas/login?next=/map/rooms"
    assert 'href="/bas/static/app.css' in c.get("/login", headers=FWD).text
    r = sign_in(c, next_="/runs", headers=FWD)
    assert r.location == "/bas/runs" and cookie_paths(r) == ["/bas"]
    # The same proxy without the header: the site root, as before.
    c = app_for(site, BAS_WEB_BEHIND_PROXY="1").test_client()
    assert c.get("/map/rooms").location == "/login?next=/map/rooms"
    assert cookie_paths(sign_in(c)) == ["/"]


@pytest.mark.parametrize("bad", ["//evil.example", "/a b", "/../x", "https://evil.example"])
def test_a_forwarded_prefix_must_be_a_plain_path(site, bad):
    c = app_for(site, BAS_WEB_BEHIND_PROXY="1").test_client()
    assert c.get("/login", headers={"X-Forwarded-Prefix": bad}).status_code == 400


def test_base_path_wins_over_a_forwarded_prefix(site):
    c = app_for(site, BAS_WEB_BASE_PATH="/bas", BAS_WEB_BEHIND_PROXY="1").test_client()
    # The full path forwarded, whatever the header says: still /bas.
    assert c.get("/bas/map/rooms").location == "/bas/login?next=/map/rooms"
    r = c.get("/bas/map/rooms", headers={"X-Forwarded-Prefix": "/other"})
    assert r.location == "/bas/login?next=/map/rooms"
    # Stripped by a proxy that says it stripped /bas: accepted.
    assert c.get("/map/rooms", headers=FWD).location == "/bas/login?next=/map/rooms"
    # Stripped, and claiming some other prefix: outside the web UI.
    assert c.get("/map/rooms", headers={"X-Forwarded-Prefix": "/other"}).status_code == 404
    assert c.get("/map/rooms").status_code == 404


# ── nothing the browser loads hard-codes a site-root path ────────────────────

def test_static_files_use_no_root_paths():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert not re.findall(r"""["'`]/[A-Za-z_]""", js), "app.js: use a data- URL from url_for"
    assert not re.findall(r"""url\(\s*["']?/""", css), "app.css: use a relative url()"

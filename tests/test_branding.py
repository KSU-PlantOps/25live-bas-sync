"""The Appearance page (name, logo, colour, sign-in contact) and the activity log."""

import io
import os
import stat
import struct
import zlib

import pytest

pytest.importorskip("flask")

from bassync.web import access  # noqa: E402

from .test_access import configure_sso, sso_sign_in  # noqa: E402
from .webhelpers import PASSWORD, app_for, post  # noqa: E402


def png(width=4, height=2) -> bytes:
    """A tiny valid PNG."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\xfd\xbb\x30" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def admin_client(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


def save(c, logo=None, **fields):
    data = {"site_name": "", "notice": "", "contact_name": "", "contact_email": "",
            "contact_phone": "", "accent": "#1f5fd6", "accent_default": "1", **fields}
    if logo is not None:
        data["logo"] = (io.BytesIO(logo[1]), logo[0])
    return post(c, "/settings/appearance", data, content_type="multipart/form-data")


def test_name_notice_and_contact_reach_the_sign_in_page(site):
    c = admin_client(site)
    r = save(c, site_name="KSU Plant Ops — BAS scheduling",
             notice="Authorized Plant Operations staff only.\nActivity is logged.",
             contact_name="BAS Controls Shop", contact_email="plantopsbas@kennesaw.edu",
             contact_phone="470 555 0100")
    assert r.status_code == 302, r.text
    login = app_for(site).test_client().get("/login").text
    assert "KSU Plant Ops — BAS scheduling" in login
    assert "Authorized Plant Operations staff only." in login
    assert 'href="mailto:plantopsbas@kennesaw.edu"' in login and 'href="tel:4705550100"' in login
    page = c.get("/").text
    assert "<title>Status · KSU Plant Ops — BAS scheduling</title>" in page
    assert "plantopsbas@kennesaw.edu" in page                     # the footer


def test_the_notice_is_text_not_html(site):
    c = admin_client(site)
    save(c, notice="<script>alert(1)</script>")
    login = app_for(site).test_client().get("/login").text
    assert "<script>alert(1)" not in login and "&lt;script&gt;" in login


def test_logo_upload_serve_replace_and_remove(site):
    c = admin_client(site)
    assert save(c, logo=("ksu.png", png())).status_code == 302
    folder = site.paths.web_file.parent
    assert (folder / "web-logo.png").read_bytes() == png()
    assert stat.S_IMODE(os.stat(folder / "web-logo.png").st_mode) == 0o644
    anon = app_for(site).test_client()
    r = anon.get("/branding/logo")                                 # the sign-in page needs it
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.headers["Content-Security-Policy"] == "default-src 'none'"
    assert 'src="/branding/logo' in anon.get("/login").text
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    save(c, logo=("ksu.jpg", jpeg))
    assert not (folder / "web-logo.png").exists() and (folder / "web-logo.jpg").exists()
    assert anon.get("/branding/logo").mimetype == "image/jpeg"
    save(c, remove_logo="1")
    assert not (folder / "web-logo.jpg").exists()
    assert anon.get("/branding/logo").status_code == 404


@pytest.mark.parametrize("name, data, why", [
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "SVG"),
    ("logo.png", b"not really a png", "PNG, JPEG or WebP"),
    ("big.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (access.LOGO_MAX_BYTES + 10), "512 KB"),
])
def test_bad_logos_are_refused(site, name, data, why):
    r = save(admin_client(site), logo=(name, data))
    assert r.status_code == 422 and why in r.text
    assert not list(site.paths.web_file.parent.glob("web-logo.*"))


def test_accent_colour(site):
    c = admin_client(site)
    save(c, accent="#FDBB30", accent_default="")
    css = app_for(site).test_client().get("/branding.css")
    assert css.mimetype == "text/css" and "--accent: #fdbb30" in css.text
    assert "--accent-text: #111111" in css.text                   # dark text on gold
    assert 'href="/branding.css' in c.get("/").text
    save(c, accent="#000000", accent_default="")
    assert "--accent-text: #ffffff" in c.get("/branding.css").text
    r = save(c, accent="gold", accent_default="")
    assert r.status_code == 422 and "#fdbb30" in r.text
    save(c)                                                       # back to the default
    assert c.get("/branding.css").text == ""
    assert 'href="/branding.css' not in c.get("/").text


def test_branding_survives_access_changes(site):
    c = admin_client(site)
    save(c, site_name="Plant Ops")
    post(c, "/settings/access/groups", {"group": "BAS_Admins", "role": "admin"})
    assert access.load(site.paths.web_file)[0]["branding"]["site_name"] == "Plant Ops"


def test_appearance_and_activity_are_admin_only(site, monkeypatch):
    configure_sso(site)
    c = app_for(site).test_client()
    assert sso_sign_in(c, monkeypatch, ["BAS_Users"]).location == "/runs"
    assert c.get("/settings/appearance").status_code == 403
    assert c.get("/logs?which=activity").status_code == 403
    page = c.get("/settings/connection").text
    assert ">Connection<" in page and ">Appearance<" not in page and ">Access<" not in page


def test_activity_lists_who_did_what_newest_first(site):
    log = site.paths.log_file.parent / "service.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "2026-09-27 02:00:00  INFO    [scheduler] schedule: daily at 02:00\n"
        "2026-09-27 02:01:00  INFO    [web] SSO sign-in: Pat (pat@x.edu) as admin, from 10.0.0.9\n"
        "2026-09-27 02:02:00  INFO    [web] saved room 101 (by pat@x.edu, from 10.0.0.9)\n")
    page = admin_client(site).get("/logs?which=activity").text
    assert page.index("saved room 101") < page.index("SSO sign-in: Pat")
    assert "[scheduler]" not in page and "schedule: daily" not in page


def test_sections_and_their_pages(site):
    c = admin_client(site)
    page = c.get("/map/buildings").text
    assert 'aria-current="page">Room map<' in page
    assert 'aria-current="page">Buildings<' in page
    page = c.get("/settings/schedule").text
    assert 'aria-current="page">Settings<' in page and 'aria-current="page">Schedule<' in page
    assert ">Appearance<" in page and ">Access<" in page

"""Shared pieces for the web UI tests (test_web.py, test_access.py)."""

import re

import yaml

from bassync.service import Paths, Service, web_settings

from .test_service import FakeJobs

PASSWORD = "correct horse battery staple"
CONFIG = {
    "collegenet": {"instance": "demo", "username": "svc"},
    "systems": {
        "campus": {"driver": "bacnet", "local_address": "10.0.0.5/24"},
        "west": {"driver": "rest", "base_url": "https://ebo.example.edu",
                 "write": {"method": "PUT", "path": "/s/{target}"}},
    },
    "default_system": "campus",
    "timezone": "America/New_York",
}
MAP = {
    "buildings": [{"id": "SCI", "name": "Science", "target": "12001:5"},
                  {"id": "ART", "system": "west", "target": "art/main"}],
    "floors": [{"building": "SCI", "level": 1, "target": "12001:6"}],
    "spaces": [{"space_id": 101, "space_name": "Lab", "building": "SCI", "floor": 1,
                "target": "12001:7"},
               {"space_id": 102, "building": "SCI"}],
}


def make_site(tmp_path):
    (tmp_path / "config.yaml").write_text("# hand-written comment\n" + yaml.safe_dump(CONFIG),
                                          encoding="utf-8")
    (tmp_path / "space_mapping.yaml").write_text(yaml.safe_dump(MAP), encoding="utf-8")
    files = Paths(tmp_path / "config.yaml", tmp_path / "defaults.yaml",
                  tmp_path / "space_mapping.yaml", tmp_path / "state",
                  tmp_path / "logs" / "25live_sync.log")
    service = Service(files, jobs=FakeJobs(), environ={})
    service.tick()
    return service


def app_for(service, **env):
    from bassync.web import create_app
    env.setdefault("BAS_WEB_PASSWORD", PASSWORD)
    app = create_app(service, web_settings(env))
    app.config["TESTING"] = True
    return app


def signed_in_client(site):
    c = app_for(site).test_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302
    return c


def csrf(client) -> str:
    with client.session_transaction() as s:
        return s["csrf"]


def post(client, url, data=None, **kw):
    return client.post(url, data={"csrf": csrf(client), **(data or {})}, **kw)


def version(client, url) -> str:
    return re.search(r'name="version" value="([^"]*)"', client.get(url).text).group(1)


def the_map(site):
    return yaml.safe_load(site.paths.space_map.read_text())



"""The Safety settings page."""

import json

import pytest
import yaml

pytest.importorskip("flask")

from .webhelpers import PASSWORD, app_for, post  # noqa: E402


def _client(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


def _form(c, **changes):
    import re
    page = c.get("/settings/safety").text
    data = {"version": re.search(r'name="version" value="([^"]*)"', page).group(1),
            "enabled": "1", "max_cleared": "34", "min_events": "1",
            "on_map_errors": "skip", "attempts": "3", "backoff": "2"}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def _config(site) -> dict:
    return yaml.safe_load(site.paths.config.read_text())


def test_shows_the_built_in_values(site):
    page = _client(site).get("/settings/safety").text
    assert 'name="max_cleared" value="34"' in page and 'value="skip" checked' in page
    assert "None yet — the first live run" in page


def test_saves_safety_and_retry_settings(site):
    c = _client(site)
    r = post(c, "/settings/safety", _form(c, max_cleared="50", min_events="10",
                                           on_map_errors="abort", attempts="5", backoff="1.5"))
    assert r.status_code == 302, r.text
    cfg = _config(site)
    assert cfg["safety"] == {"enabled": True, "min_events": 10, "max_cleared_fraction": 0.5,
                             "on_map_errors": "abort"}
    assert cfg["retry"] == {"attempts": 5, "backoff_seconds": 1.5}


@pytest.mark.parametrize("field, value, message", [
    ("max_cleared", "150", "between 0 and 100"),
    ("min_events", "lots", "must be a number"),
    ("attempts", "99", "between 0 and 20"),
    ("on_map_errors", "ignore", "Pick what a broken row does"),
])
def test_bad_values_are_refused(site, field, value, message):
    c = _client(site)
    r = post(c, "/settings/safety", _form(c, **{field: value}))
    assert r.status_code == 422 and message in r.text
    assert "safety" not in _config(site)


def test_turning_the_check_off_needs_confirming(site):
    c = _client(site)
    form = _form(c, enabled=None)
    r = post(c, "/settings/safety", form)
    assert r.status_code == 422 and "nothing stops a run from clearing every" in r.text
    assert "safety" not in _config(site)
    assert post(c, "/settings/safety", dict(form, confirm="1")).status_code == 302
    assert _config(site)["safety"]["enabled"] is False


def test_shows_the_baseline(site):
    site.paths.state_dir.mkdir(parents=True, exist_ok=True)
    (site.paths.state_dir / "last_run.json").write_text(json.dumps({
        "written_at": "2026-09-27T02:00:00-04:00", "event_count": 120,
        "windows": {"campus:1:1": 4, "campus:1:2": 0, "campus:1:3": 2}}))
    page = _client(site).get("/settings/safety").text
    assert "2 of 3 schedule(s) had bookings, from 120 25Live booking(s)" in page


def test_read_only_without_edit_settings(site, monkeypatch):
    from .test_access import configure_sso, signed_in_as
    configure_sso(site)
    c = signed_in_as(app_for(site), monkeypatch, ["BAS_Users"])            # Advanced
    assert "Read-only" in c.get("/settings/safety").text
    assert post(c, "/settings/safety", {"enabled": "1"}).status_code == 403

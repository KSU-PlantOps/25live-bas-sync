"""Restarting the service from the web UI, and the Service page."""

import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from bassync import service as service_mod
from bassync.jobs import Job, JobManager

from .test_service import FakeJobs, make_service


def _wait(event, seconds=3.0) -> bool:
    return event.wait(seconds)


def test_restart_stops_the_service_and_is_flagged(tmp_path):
    svc = make_service(tmp_path)
    assert svc.restart_blocker() is None
    assert svc.request_restart() is None
    assert svc.restart_requested and _wait(svc.stop_event)
    assert "already stopping" in svc.restart_blocker()


def test_no_restart_while_a_job_runs(tmp_path):
    svc = make_service(tmp_path)
    svc.jobs.current = Job(id="j", kind="sync", label="Sync", args=[], trigger="", started="")
    assert "Sync is running" in svc.request_restart()
    assert not svc.restart_requested and not svc.stop_event.is_set()


def test_no_restart_just_before_a_scheduled_sync(tmp_path):
    svc = make_service(tmp_path)
    now = datetime.now(timezone.utc)
    svc.next_due = now + timedelta(seconds=60)
    assert "next two minutes" in svc.restart_blocker(now)
    svc.next_due = now + timedelta(minutes=10)
    assert svc.restart_blocker(now) is None


def test_the_job_manager_closes_only_when_idle(tmp_path):
    jobs = JobManager(tmp_path / "jobs", command=[sys.executable, "-c",
                                                   "import time; time.sleep(5)"])
    job = jobs.start("sync")
    assert jobs.close_if_idle() == "Sync"
    job.process.kill()
    for _ in range(50):
        if jobs.current is None:
            break
        time.sleep(0.05)
    assert jobs.close_if_idle() is None
    with pytest.raises(Exception, match="shutting down"):
        jobs.start("sync")


def test_restart_in_place_execs_the_same_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(sys, "orig_argv", ["python3", "-m", "bassync.service"], raising=False)
    monkeypatch.setattr(service_mod.os, "execv",
                        lambda path, argv: seen.update(path=path, argv=argv))
    service_mod.restart_in_place()
    assert seen == {"path": sys.executable, "argv": ["python3", "-m", "bassync.service"]}


def test_a_failed_exec_exits_for_the_restart_policy(monkeypatch):
    def boom(path, argv):
        raise OSError("no such file")
    monkeypatch.setattr(service_mod.os, "execv", boom)
    assert service_mod.restart_in_place() == 3


# ── the web UI ───────────────────────────────────────────────────────────────

flask = pytest.importorskip("flask")

from bassync.web import access  # noqa: E402

from .test_access import configure_sso, signed_in_as  # noqa: E402
from .webhelpers import PASSWORD, app_for, post  # noqa: E402


def _admin(site):
    c = app_for(site).test_client()
    c.post("/login", data={"password": PASSWORD})
    return c


def test_service_page_shows_what_is_running(site):
    site.web = {"host": "0.0.0.0", "port": 8080, "cert": "", "behind_proxy": True, "auth": "password"}
    page = _admin(site).get("/settings/service").text
    assert "Restart the service" in page and "http://0.0.0.0:8080, behind a reverse proxy" in page
    assert str(site.paths.extras_file) in page and "The newest release." in page


def test_restart_from_the_web_ui(site, caplog):
    c = _admin(site)
    with caplog.at_level("INFO"):
        r = post(c, "/settings/service/restart")
    assert r.status_code == 200 and 'id="restarting"' in r.text
    assert f'data-boot="{site.boot}"' in r.text
    assert site.restart_requested and _wait(site.stop_event)
    assert "restarted the service" in caplog.text


def test_restart_is_refused_while_a_job_runs(site):
    site.jobs = FakeJobs()
    site.jobs.current = Job(id="j", kind="sync", label="Sync", args=[], trigger="", started="")
    c = _admin(site)
    assert "disabled" in c.get("/settings/service").text
    r = post(c, "/settings/service/restart")
    assert r.status_code == 302 and not site.restart_requested
    assert "Sync is running" in c.get("/settings/service").text


def test_restart_needs_the_capability(site, monkeypatch):
    configure_sso(site)
    c = signed_in_as(app_for(site), monkeypatch, ["BAS_Users"])          # Advanced
    page = c.get("/settings/service").text
    assert "Restart the service</button>" not in page
    assert post(c, "/settings/service/restart").status_code == 403
    assert not site.restart_requested


def test_update_available_shows_on_the_dashboard_and_service_page(site):
    from bassync.updates import UpdateChecker
    site.updates = UpdateChecker("1.3.0", fetch=lambda: [{
        "tag_name": "v1.3.1", "name": "v1.3.1 — fixes", "prerelease": False,
        "html_url": "https://github.com/x/releases/v1.3.1", "published_at": "2026-10-01T00:00:00Z"}])
    site.updates.refresh()
    c = _admin(site)
    assert "v1.3.1 — fixes</strong> is available" in c.get("/").text
    page = c.get("/settings/service").text
    assert "is available" in page and "docker compose pull" in page


def test_update_checks_can_be_turned_off_and_run_now(site):
    c = _admin(site)
    post(c, "/settings/service/updates", {"check": "0"})
    assert access.load(site.paths.web_file)[0]["updates"]["check"] is False
    assert "Checking for new releases is off" in c.get("/settings/service").text
    post(c, "/settings/service/updates", {"action": "check"})
    assert "This is the newest release" in c.get("/settings/service").text

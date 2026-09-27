"""The long-running service: the schedule, the settings it reads from the
environment, and its health check."""

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from bassync import service as service_mod
from bassync.jobs import Job, JobBusy
from bassync.service import Paths, Service, health, web_settings

NY = ZoneInfo("America/New_York")


class FakeJobs:
    """Records what would have started; `busy` makes it refuse."""

    def __init__(self):
        self.started: list = []
        self.busy_with = ""

    @property
    def busy(self):
        return bool(self.busy_with)

    current = None

    def start(self, kind, extra_args=None, trigger=""):
        if self.busy_with:
            raise JobBusy(self.busy_with)
        self.started.append((kind, list(extra_args or []), trigger))
        return Job(id=f"job{len(self.started)}", kind=kind, label=kind, args=[],
                   trigger=trigger, started="")

    def recent(self, limit=30):
        return []

    def get(self, job_id):
        return None

    def shutdown(self, grace=0):
        pass


def make_service(tmp_path, schedule=None, environ=None, extra=None):
    cfg = {"timezone": "America/New_York", **(extra or {})}
    if schedule is not None:
        cfg["schedule"] = schedule
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    files = Paths(tmp_path / "config.yaml", tmp_path / "defaults.yaml",
                  tmp_path / "map.yaml", tmp_path / "state", tmp_path / "logs" / "sync.log")
    return Service(files, jobs=FakeJobs(), environ=environ or {})


def at(hour, minute=0, second=0, day=10):
    return datetime(2026, 3, day, hour, minute, second, tzinfo=NY).astimezone(timezone.utc)


def test_runs_the_sync_at_the_scheduled_time(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    assert svc.tick(at(1, 59)) is None
    assert svc.next_due == at(2, 0)
    assert svc.tick(at(2, 0, 5)) == "job1"
    assert svc.jobs.started == [("sync", [], "schedule")]
    assert svc.next_due == at(2, 0, day=11)
    assert svc.tick(at(2, 1)) is None                     # once, not every tick


def test_several_times_a_day(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00", "13:30"]})
    svc.tick(at(3))
    assert svc.next_due == at(13, 30)


def test_a_due_sync_waits_for_a_running_job(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    svc.tick(at(1, 59))
    svc.jobs.busy_with = "Validate"
    assert svc.tick(at(2, 0, 1)) is None
    assert svc.pending_since == at(2, 0, 1)
    svc.jobs.busy_with = ""
    assert svc.tick(at(2, 7)) == "job1"
    assert svc.jobs.started[0][2] == "schedule (waited 6 min for another job)"
    assert svc.pending_since is None


def test_schedule_off_means_no_runs(tmp_path):
    svc = make_service(tmp_path, {"enabled": False, "times": ["02:00"]})
    svc.tick(at(1))
    assert svc.next_due is None and "off" in svc.describe()
    assert svc.tick(at(2, 0, 5)) is None


def test_a_schedule_change_applies_at_once(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    svc.tick(at(1))
    make_service(tmp_path, {"times": ["01:30"]})      # rewrites config.yaml
    svc.tick(at(1, 0, 30))
    assert svc.next_due == at(1, 30)


def test_default_schedule_is_nightly_at_two(tmp_path):
    svc = make_service(tmp_path)
    svc.tick(at(1))
    assert svc.schedule.times == ["02:00"] and svc.next_due == at(2, 0)


def test_sync_at_overrides_config(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]},
                       environ={"SYNC_AT": "4:15", "SYNC_ON_START": "1"})
    svc.tick(at(1))
    assert svc.schedule.times == ["04:15"] and svc.schedule.source == "SYNC_AT"
    assert svc.schedule.run_on_start and "from SYNC_AT" in svc.describe()


def test_a_broken_config_keeps_the_last_good_schedule(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    svc.tick(at(1))
    (tmp_path / "config.yaml").write_text("timezone: Mars/Olympus\n", encoding="utf-8")
    svc.tick(at(1, 30))
    assert svc.schedule.times == ["02:00"] and "Mars/Olympus" in svc.schedule.error
    assert svc.next_due == at(2, 0)


def test_spring_forward_runs_when_the_clock_jumps(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:30"]})
    svc.tick(at(1, 0, day=8))                          # 2026-03-08: 02:00 -> 03:00
    assert svc.next_due == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)


def test_run_on_start(tmp_path):
    import threading
    svc = make_service(tmp_path, {"times": ["02:00"], "run_on_start": True})
    stop = threading.Event()
    stop.set()                                         # one pass, then out
    svc.run_scheduler(stop)
    assert svc.jobs.started == [("sync", [], "start")]


def test_heartbeat_and_health(tmp_path, capsys):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    assert health(svc.paths) == 1
    svc._heartbeat()
    assert health(svc.paths) == 0
    old = time.time() - 600
    os.utime(svc.paths.state_dir / service_mod.HEARTBEAT_FILE, (old, old))
    assert health(svc.paths) == 1
    assert "unhealthy: last heartbeat" in capsys.readouterr().out.splitlines()[-1]


@pytest.mark.parametrize("env, enabled, reason", [
    ({}, False, "BAS_WEB_PASSWORD is not set"),
    ({"BAS_WEB_PASSWORD": "x" * 16}, True, ""),
    ({"BAS_WEB_AUTH": "none"}, True, ""),
    ({"BAS_WEB_AUTH": "ldap", "BAS_WEB_PASSWORD": "x"}, False, "not `password` or `none`"),
    ({"BAS_WEB_PASSWORD": "x", "BAS_WEB_PORT": "http"}, False, "is not a port"),
    ({"BAS_WEB_PASSWORD": "x", "BAS_WEB_TLS_CERT": "/c.pem"}, False, "both"),
])
def test_web_settings(env, enabled, reason):
    settings = web_settings(env)
    assert settings["enabled"] is enabled
    assert reason in settings["reason"]


def test_web_settings_defaults():
    s = web_settings({"BAS_WEB_PASSWORD": "pw", "BAS_WEB_BEHIND_PROXY": "1"})
    assert (s["host"], s["port"], s["behind_proxy"]) == ("0.0.0.0", 8080, True)


def test_a_bad_sync_at_stops_the_service_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_AT", "29:00")
    monkeypatch.setattr(service_mod, "resolve_paths", lambda: make_service(tmp_path).paths)
    monkeypatch.setattr(service_mod, "_setup_logging", lambda path: None)
    assert service_mod.main([]) == 2


def test_resolve_paths_follows_the_environment_and_the_config(tmp_path, monkeypatch):
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({
        "safety": {"state_file": str(tmp_path / "st" / "last_run.json")},
        "log_file": str(tmp_path / "lg" / "sync.log")}), encoding="utf-8")
    monkeypatch.setenv("BAS_CONFIG", str(tmp_path / "c.yaml"))
    monkeypatch.setenv("BAS_SPACE_MAP", str(tmp_path / "m.yaml"))
    files = service_mod.resolve_paths()
    assert files.config == tmp_path / "c.yaml" and files.space_map == tmp_path / "m.yaml"
    assert files.state_dir == tmp_path / "st" and files.runs_dir == tmp_path / "st" / "runs"
    assert files.log_file == tmp_path / "lg" / "sync.log"


def test_no_scheduled_syncs_before_there_is_a_config(tmp_path):
    svc = make_service(tmp_path, {"times": ["02:00"]})
    (tmp_path / "config.yaml").unlink()
    svc.tick(at(1))
    assert svc.next_due is None and "No config.yaml yet" in svc.schedule.error
    assert svc.tick(at(2, 0, 5)) is None and svc.jobs.started == []
